from __future__ import annotations

import json
import logging
import queue
import threading
from contextlib import contextmanager
from typing import Any, Iterable

import pymysql
from pymysql.cursors import DictCursor

from openkefu.web.config import AppConfig
from openkefu.web.crypto import ENCRYPTED_PREFIX, TextCipher


logger = logging.getLogger(__name__)


class Database:
    def __init__(self, config: AppConfig):
        self.config = config
        self._pool: queue.LifoQueue | None = None
        self._pool_lock = threading.Lock()
        self._pool_created = 0

    def _connect(self, *, database: bool = True):
        mysql = self.config.mysql
        connection_kwargs = dict(
            host=mysql.host,
            port=mysql.port,
            user=mysql.user,
            password=mysql.password,
            database=mysql.database if database else None,
            charset=mysql.charset,
            cursorclass=DictCursor,
            autocommit=False,
        )
        ssl_ca = str(getattr(mysql, "ssl_ca", "") or "")
        if ssl_ca:
            connection_kwargs.update(
                ssl_ca=ssl_ca,
                ssl_cert=getattr(mysql, "ssl_cert", "") or None,
                ssl_key=getattr(mysql, "ssl_key", "") or None,
                ssl_verify_cert=True,
                ssl_verify_identity=bool(getattr(mysql, "ssl_verify_identity", True)),
            )
        return pymysql.connect(**connection_kwargs)

    def _pool_enabled(self) -> bool:
        return int(getattr(self.config.mysql, "pool_size", 1) or 1) > 1

    def _pooled_connect(self):
        if not self._pool_enabled():
            return self._connect(database=True), False
        with self._pool_lock:
            if self._pool is None:
                self._pool = queue.LifoQueue(maxsize=self.config.mysql.pool_size)
        conn = None
        # 每次等待后重新检查容量：其他线程建连失败可能已归还预留槽位。
        for attempt in range(7):
            try:
                conn = self._pool.get_nowait()
                break
            except queue.Empty:
                reserved = False
                with self._pool_lock:
                    if self._pool_created < self.config.mysql.pool_size:
                        self._pool_created += 1
                        reserved = True
                if reserved:
                    # 建连可能阻塞，不持有池锁；失败时必须归还槽位。
                    return self._connect_reserved(), True
                if attempt == 6:
                    raise RuntimeError("数据库连接池已满，请稍后重试")
                try:
                    conn = self._pool.get(timeout=2)
                    break
                except queue.Empty:
                    continue
        try:
            conn.ping(reconnect=False)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conn = self._connect_reserved()
        return conn, True

    def _connect_reserved(self):
        try:
            return self._connect(database=True)
        except Exception:
            with self._pool_lock:
                self._pool_created -= 1
            raise

    def _release_pooled(self, conn, pooled: bool) -> None:
        if not pooled:
            conn.close()
            return
        try:
            if self._pool is not None:
                self._pool.put_nowait(conn)
                return
        except queue.Full:
            pass
        try:
            conn.close()
        except Exception:
            pass
        finally:
            with self._pool_lock:
                self._pool_created -= 1

    @contextmanager
    def connect(self):
        conn, pooled = self._pooled_connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._release_pooled(conn, pooled)

    def initialize(self) -> None:
        mysql = self.config.mysql
        with self._connect(database=False) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{mysql.database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            conn.commit()

        # api + worker 分离部署时两个进程会同时执行 DDL/迁移；
        # 用 MySQL 命名锁互斥，避免并发 ALTER TABLE 报 Duplicate column name 导致启动失败。
        # 加锁与释放必须发生在同一个连接上（GET_LOCK 是会话级）。
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT GET_LOCK('openkefu_schema_migration', 120) AS lock_acquired")
                lock_acquired = cursor.fetchone().get("lock_acquired") == 1
            if not lock_acquired:
                raise RuntimeError("another process is initializing the database schema; try again later")
            try:
                with conn.cursor() as cursor:
                    for statement in SCHEMA:
                        cursor.execute(statement)
                    self._bootstrap_roles(cursor)
                    self._bootstrap_settings(cursor)
                    self._migrate(cursor, mysql)
                    self._encrypt_existing_sensitive_values(cursor)
                    self._verify_schema(cursor, mysql)
            finally:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT RELEASE_LOCK('openkefu_schema_migration')")
        logger.info("database initialized")

    def _encrypt_existing_sensitive_values(self, cursor) -> None:
        cipher = TextCipher(self.config.security.data_encryption_key)
        self._encrypt_table_columns(
            cursor,
            table="shop_login_caches",
            pk="shop_id",
            columns=(
                "cookies_json",
                "cookie_string",
                "requests_headers_json",
                "base_headers_json",
                "login_result_json",
            ),
            cipher=cipher,
        )
        self._encrypt_table_columns(
            cursor,
            table="shop_sessions",
            pk="id",
            columns=("access_token", "token_result"),
            cipher=cipher,
        )

    def _encrypt_table_columns(self, cursor, *, table: str, pk: str, columns: tuple[str, ...], cipher: TextCipher) -> None:
        column_list = ", ".join([pk, *columns])
        cursor.execute(f"SELECT {column_list} FROM {table}")
        rows = cursor.fetchall()
        for row in rows:
            updates = []
            values = []
            for column in columns:
                value = row.get(column)
                if value in (None, ""):
                    continue
                value_text = str(value)
                if value_text.startswith(ENCRYPTED_PREFIX):
                    continue
                updates.append(f"{column}=%s")
                values.append(cipher.encrypt(value_text))
            if not updates:
                continue
            values.append(row[pk])
            cursor.execute(f"UPDATE {table} SET {', '.join(updates)} WHERE {pk}=%s", values)

    def query(self, sql: str, params: Iterable[Any] | dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())

    def query_one(self, sql: str, params: Iterable[Any] | dict[str, Any] | None = None) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Iterable[Any] | dict[str, Any] | None = None) -> int:
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return int(cursor.lastrowid or cursor.rowcount)

    def execute_many(self, sql: str, values: list[Iterable[Any]]) -> None:
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.executemany(sql, values)

    def _bootstrap_roles(self, cursor) -> None:
        cursor.execute("INSERT IGNORE INTO roles (name, display_name) VALUES ('admin', '管理员')")
        cursor.execute("INSERT IGNORE INTO roles (name, display_name) VALUES ('service', '客服')")

    def _bootstrap_settings(self, cursor) -> None:
        # 安全默认：新装默认关闭开放注册，由管理员在设置页显式开启；
        # INSERT IGNORE 保证不覆盖既有部署的现有值。
        cursor.execute(
            """
            INSERT IGNORE INTO app_settings (`key`, value_json)
            VALUES ('registration_enabled', 'false')
            """
        )

    @staticmethod
    def _verify_schema(cursor, mysql) -> None:
        cursor.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
            (mysql.database,),
        )
        existing_tables = {str(row["TABLE_NAME"]) for row in cursor.fetchall()}
        missing_tables = [name for name in REQUIRED_SCHEMA if name not in existing_tables]

        missing_columns: list[str] = []
        for table_name, required_columns in REQUIRED_SCHEMA.items():
            if table_name not in existing_tables:
                continue
            cursor.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
                """,
                (mysql.database, table_name),
            )
            existing_columns = {str(row["COLUMN_NAME"]) for row in cursor.fetchall()}
            missing_columns.extend(
                f"{table_name}.{column_name}"
                for column_name in required_columns
                if column_name not in existing_columns
            )

        if missing_tables or missing_columns:
            details = []
            if missing_tables:
                details.append("missing tables: " + ", ".join(missing_tables))
            if missing_columns:
                details.append("missing columns: " + ", ".join(missing_columns))
            raise RuntimeError("database schema is incomplete; " + "; ".join(details))

        logger.info(
            "database schema verified: %s tables, %s required columns",
            len(REQUIRED_SCHEMA),
            sum(len(columns) for columns in REQUIRED_SCHEMA.values()),
        )

    @staticmethod
    def _migrate(cursor, mysql) -> None:
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='shops' AND COLUMN_NAME='transfer_csids'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                "ALTER TABLE shops ADD COLUMN transfer_csids TEXT NULL DEFAULT NULL"
            )
            logger.info("migration: added transfer_csids column to shops")

            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='shops' AND COLUMN_NAME='transfer_csid'",
                (mysql.database,),
            )
            if cursor.fetchone()["cnt"] > 0:
                cursor.execute(
                    "UPDATE shops SET transfer_csids = "
                    "CONCAT('[\"', IFNULL(transfer_csid, ''), '\"]') "
                    "WHERE transfer_csid IS NOT NULL AND transfer_csid != ''"
                )
                cursor.execute(
                    "ALTER TABLE shops DROP COLUMN transfer_csid"
                )
                logger.info("migration: migrated transfer_csid to transfer_csids")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='shops' AND COLUMN_NAME='nickname'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                "ALTER TABLE shops ADD COLUMN nickname VARCHAR(128) NULL DEFAULT NULL"
            )
            logger.info("migration: added nickname column to shops")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='conversations' AND COLUMN_NAME='mall_id'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                "ALTER TABLE conversations ADD COLUMN mall_id VARCHAR(64) NULL AFTER shop_id"
            )
            logger.info("migration: added mall_id column to conversations")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='conversations' AND COLUMN_NAME='transferred_at'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                "ALTER TABLE conversations ADD COLUMN transferred_at TIMESTAMP NULL DEFAULT NULL"
            )
            logger.info("migration: added transferred_at column to conversations")

        for column_name, column_sql in (
            ("bot_reply_enabled", "ALTER TABLE conversations ADD COLUMN bot_reply_enabled TINYINT(1) NOT NULL DEFAULT 1"),
            ("human_attention_required", "ALTER TABLE conversations ADD COLUMN human_attention_required TINYINT(1) NOT NULL DEFAULT 0"),
            ("human_attention_reason", "ALTER TABLE conversations ADD COLUMN human_attention_reason VARCHAR(128) NULL DEFAULT NULL"),
            ("human_attention_at", "ALTER TABLE conversations ADD COLUMN human_attention_at TIMESTAMP NULL DEFAULT NULL"),
        ):
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='conversations' AND COLUMN_NAME=%s",
                (mysql.database, column_name),
            )
            if cursor.fetchone()["cnt"] == 0:
                cursor.execute(column_sql)
                logger.info("migration: added %s column to conversations", column_name)

        cursor.execute(
            """
            UPDATE conversations c
            JOIN shops s ON s.id=c.shop_id
            LEFT JOIN conversations existing
              ON existing.shop_id=c.shop_id
             AND existing.user_uid=c.user_uid
             AND existing.mall_id=s.mall_id
             AND existing.id<>c.id
            SET c.mall_id=s.mall_id
            WHERE (c.mall_id IS NULL OR c.mall_id='')
              AND s.mall_id IS NOT NULL
              AND s.mall_id<>''
              AND existing.id IS NULL
            """
        )

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='conversations' AND INDEX_NAME='uk_conversation_user'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] > 0:
            cursor.execute("ALTER TABLE conversations DROP INDEX uk_conversation_user")
            logger.info("migration: dropped old conversation unique key")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='conversations' AND INDEX_NAME='uk_conversation_shop_mall_user'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                "ALTER TABLE conversations ADD UNIQUE KEY uk_conversation_shop_mall_user (shop_id, mall_id, user_uid)"
            )
            logger.info("migration: added conversation unique key with mall_id")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='shops' AND COLUMN_NAME='expire_time'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] > 0:
            cursor.execute("ALTER TABLE shops DROP COLUMN expire_time")
            logger.info("migration: dropped expire_time column from shops")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='shops' AND COLUMN_NAME='created_by_user_id'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute("ALTER TABLE shops ADD COLUMN created_by_user_id BIGINT NULL")
            logger.info("migration: added created_by_user_id column to shops")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='shops' AND COLUMN_NAME='greeting_message'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute("ALTER TABLE shops ADD COLUMN greeting_message TEXT NULL")
            logger.info("migration: added greeting_message column to shops")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='shops' AND COLUMN_NAME='closing_message'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute("ALTER TABLE shops ADD COLUMN closing_message TEXT NULL")
            logger.info("migration: added closing_message column to shops")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='shops' AND COLUMN_NAME='greeting_use_llm'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute("ALTER TABLE shops ADD COLUMN greeting_use_llm TINYINT(1) NOT NULL DEFAULT 0")
            logger.info("migration: added greeting_use_llm column to shops")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='shops' AND COLUMN_NAME='closing_use_llm'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute("ALTER TABLE shops ADD COLUMN closing_use_llm TINYINT(1) NOT NULL DEFAULT 0")
            logger.info("migration: added closing_use_llm column to shops")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='shops' AND COLUMN_NAME='force_ai_reply'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute("ALTER TABLE shops ADD COLUMN force_ai_reply TINYINT(1) NOT NULL DEFAULT 0")
            logger.info("migration: added force_ai_reply column to shops")

        # 自动续登账密（TextCipher 加密存储，仅用户勾选保存；用于登录态过期时自动重登）
        for column_name, column_sql in (
            ("login_username", "ALTER TABLE shops ADD COLUMN login_username VARCHAR(255) NULL"),
            ("login_password", "ALTER TABLE shops ADD COLUMN login_password TEXT NULL"),
        ):
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='shops' AND COLUMN_NAME=%s",
                (mysql.database, column_name),
            )
            if cursor.fetchone()["cnt"] == 0:
                cursor.execute(column_sql)
                logger.info("migration: added %s column to shops", column_name)

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='shop_notes'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                """
                CREATE TABLE shop_notes (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    shop_id BIGINT NOT NULL,
                    content TEXT NOT NULL,
                    created_by BIGINT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_shop_notes_shop (shop_id),
                    CONSTRAINT fk_shop_notes_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            logger.info("migration: created shop_notes table")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='note_sets'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                """
                CREATE TABLE note_sets (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    name VARCHAR(160) NOT NULL,
                    description VARCHAR(512) NOT NULL DEFAULT '',
                    status VARCHAR(32) NOT NULL DEFAULT 'active',
                    created_by BIGINT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_note_sets_status (status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE note_set_shops (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    note_set_id BIGINT NOT NULL,
                    shop_id BIGINT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_ns_shop (note_set_id, shop_id),
                    INDEX idx_ns_shop_shop (shop_id),
                    CONSTRAINT fk_ns_shops_ns FOREIGN KEY (note_set_id) REFERENCES note_sets(id) ON DELETE CASCADE,
                    CONSTRAINT fk_ns_shops_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE note_set_items (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    note_set_id BIGINT NOT NULL,
                    content TEXT NOT NULL,
                    created_by BIGINT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_note_set_items_ns (note_set_id),
                    CONSTRAINT fk_ns_items_ns FOREIGN KEY (note_set_id) REFERENCES note_sets(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            logger.info("migration: created note_sets tables")

            cursor.execute("SELECT COUNT(*) AS cnt FROM shop_notes")
            if cursor.fetchone()["cnt"] > 0:
                cursor.execute("SELECT DISTINCT shop_id FROM shop_notes")
                shop_ids = [str(row["shop_id"]) for row in cursor.fetchall()]
                for sid in shop_ids:
                    cursor.execute("SELECT name FROM shops WHERE id=%s", (sid,))
                    shop_row = cursor.fetchone()
                    shop_name = shop_row["name"] if shop_row else f"店铺{sid}"
                    cursor.execute(
                        "INSERT INTO note_sets (name, description, status) VALUES (%s,%s,'active')",
                        (f"{shop_name}注意事项", f"从旧版注意事项迁移，绑定店铺{sid}"),
                    )
                    note_set_id = cursor.lastrowid
                    cursor.execute(
                        "INSERT INTO note_set_shops (note_set_id, shop_id) VALUES (%s,%s)",
                        (note_set_id, sid),
                    )
                    cursor.execute(
                        """
                        INSERT INTO note_set_items (note_set_id, content, created_by, created_at, updated_at)
                        SELECT %s, content, created_by, created_at, updated_at
                        FROM shop_notes WHERE shop_id=%s ORDER BY id ASC
                        """,
                        (note_set_id, sid),
                    )
                logger.info("migration: migrated shop_notes to note_sets")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='knowledge_qa_items'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                """
                CREATE TABLE knowledge_qa_items (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    knowledge_base_id BIGINT NOT NULL,
                    question TEXT NOT NULL,
                    reply TEXT NULL,
                    image_base64 LONGTEXT NULL,
                    image_name VARCHAR(255) NULL,
                    image_content_type VARCHAR(128) NULL,
                    sort_order INT NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_knowledge_qa_kb (knowledge_base_id, sort_order),
                    CONSTRAINT fk_knowledge_qa_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            logger.info("migration: created knowledge_qa_items table")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='knowledge_chunks' AND COLUMN_NAME='embedding_json'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute("ALTER TABLE knowledge_chunks ADD COLUMN embedding_json LONGTEXT NULL AFTER vector_id")
            logger.info("migration: added embedding_json column to knowledge_chunks")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='users' AND COLUMN_NAME='auth_version'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute("ALTER TABLE users ADD COLUMN auth_version INT NOT NULL DEFAULT 1 AFTER password_hash")
            logger.info("migration: added auth_version column to users")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='runtime_logs' AND COLUMN_NAME='run_id'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute("ALTER TABLE runtime_logs ADD COLUMN run_id VARCHAR(64) NULL")
            logger.info("migration: added run_id column to runtime_logs")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='runtime_logs' AND COLUMN_NAME='pid'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute("ALTER TABLE runtime_logs ADD COLUMN pid INT NULL")
            logger.info("migration: added pid column to runtime_logs")

        # 配额/数量限制体系已移除：清理 users 表遗留的限制列。
        for legacy_user_column in ("max_shops", "max_knowledge_bases", "max_llm_replies", "llm_reply_count"):
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='users' AND COLUMN_NAME=%s",
                (mysql.database, legacy_user_column),
            )
            if cursor.fetchone()["cnt"]:
                cursor.execute(f"ALTER TABLE users DROP COLUMN {legacy_user_column}")
                logger.info("migration: dropped legacy users.%s column", legacy_user_column)

        # 服务器状态监控功能已移除：清理历史部署遗留的三张表（无业务数据价值）。
        for legacy_status_table in ("server_status_collectors", "server_status_snapshots", "server_status_rollups_hourly"):
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (mysql.database, legacy_status_table),
            )
            if cursor.fetchone()["cnt"]:
                cursor.execute(f"DROP TABLE {legacy_status_table}")
                logger.info("migration: dropped legacy %s table", legacy_status_table)

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='messages' AND INDEX_NAME='uk_messages_shop_msg_id'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            # 先清理历史重复（保留每个 msg_id 最大 id 的行），再建唯一索引，
            # 使消息入库查重从应用层 TOCTOU 变为数据库层幂等。
            cursor.execute(
                """
                DELETE m_old FROM messages m_old
                JOIN messages m_new
                  ON m_old.shop_id=m_new.shop_id
                 AND m_old.msg_id=m_new.msg_id
                 AND m_old.id < m_new.id
                WHERE m_old.msg_id IS NOT NULL AND m_old.msg_id <> ''
                """
            )
            cursor.execute(
                """
                ALTER TABLE messages
                ADD COLUMN msg_id_key VARCHAR(128)
                    GENERATED ALWAYS AS (NULLIF(msg_id, '')) STORED,
                ADD UNIQUE KEY uk_messages_shop_msg_id (shop_id, msg_id_key)
                """
            )
            logger.info("migration: added messages unique key (shop_id, msg_id)")

        runtime_log_indexes = {
            "idx_runtime_logs_created": "(created_at)",
            "idx_runtime_logs_module": "(module)",
            "idx_runtime_logs_action": "(action)",
            "idx_runtime_logs_level": "(level)",
        }
        for index_name, index_sql in runtime_log_indexes.items():
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='runtime_logs' AND INDEX_NAME=%s",
                (mysql.database, index_name),
            )
            if cursor.fetchone()["cnt"] == 0:
                cursor.execute(f"ALTER TABLE runtime_logs ADD INDEX {index_name} {index_sql}")
                logger.info("migration: added runtime_logs index %s", index_name)

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='return_records'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                """
                CREATE TABLE return_records (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    shop_id BIGINT NOT NULL,
                    conversation_id BIGINT NULL,
                    message_id BIGINT NULL,
                    user_uid VARCHAR(64) NULL,
                    username VARCHAR(128) NOT NULL DEFAULT '',
                    order_no VARCHAR(128) NOT NULL DEFAULT '',
                    order_status VARCHAR(128) NOT NULL DEFAULT '',
                    record_type VARCHAR(32) NOT NULL,
                    new_address TEXT NULL,
                    remark TEXT NULL,
                    source_message TEXT NULL,
                    slots_json LONGTEXT NULL,
                    order_snapshot_json LONGTEXT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_return_records_shop (shop_id, created_at),
                    INDEX idx_return_records_type (record_type, created_at),
                    INDEX idx_return_records_order (order_no),
                    CONSTRAINT fk_return_records_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            logger.info("migration: created return_records table")

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='return_record_drafts'",
            (mysql.database,),
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                """
                CREATE TABLE return_record_drafts (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    shop_id BIGINT NOT NULL,
                    conversation_id BIGINT NOT NULL,
                    user_uid VARCHAR(64) NULL,
                    username VARCHAR(128) NOT NULL DEFAULT '',
                    record_type VARCHAR(32) NOT NULL,
                    order_no VARCHAR(128) NOT NULL DEFAULT '',
                    order_status VARCHAR(128) NOT NULL DEFAULT '',
                    new_address TEXT NULL,
                    remark TEXT NULL,
                    source_message TEXT NULL,
                    slots_json LONGTEXT NULL,
                    order_snapshot_json LONGTEXT NULL,
                    missing_fields_json LONGTEXT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_return_draft_conversation (shop_id, conversation_id),
                    INDEX idx_return_drafts_shop (shop_id, updated_at),
                    CONSTRAINT fk_return_drafts_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            logger.info("migration: created return_record_drafts table")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


REQUIRED_SCHEMA = {
    "app_settings": ["key", "value_json", "updated_at"],
    "roles": ["name", "display_name"],
    "users": [
        "id", "username", "password_hash", "auth_version", "display_name", "role", "is_active",
        "created_at", "updated_at",
    ],
    "shops": [
        "id", "name", "remark", "mall_id", "status", "auto_reply_enabled",
        "last_error", "created_by", "created_by_user_id",
        "greeting_message", "closing_message", "greeting_use_llm",
        "closing_use_llm", "force_ai_reply", "transfer_csids", "nickname",
        "login_username", "login_password",
        "created_at", "updated_at",
    ],
    "shop_assignments": ["id", "shop_id", "user_id", "created_at"],
    "shop_sessions": [
        "id", "shop_id", "status", "mall_id", "access_token", "ws_base_url",
        "token_result", "started_at", "ended_at", "error",
    ],
    "shop_login_caches": [
        "shop_id", "cookies_json", "cookie_string", "requests_headers_json",
        "base_headers_json", "login_result_json", "updated_at",
    ],
    "shop_runtime_leases": [
        "shop_id", "worker_id", "pid", "heartbeat_at", "expires_at", "status", "version",
    ],
    "qr_login_attempts": [
        "id", "shop_id", "session_id", "token", "qrcode_path", "status",
        "query_result", "created_at", "completed_at", "error",
    ],
    "conversations": [
        "id", "shop_id", "mall_id", "conv_id", "chat_type_id", "chat_type",
        "user_uid", "nickname", "last_message_preview", "last_message_at",
        "unread_count", "transferred_at", "bot_reply_enabled",
        "human_attention_required", "human_attention_reason", "human_attention_at",
        "created_at", "updated_at",
    ],
    "messages": [
        "id", "shop_id", "conversation_id", "direction", "msg_id",
        "client_msg_id", "user_uid", "sender_role", "message_type", "kind",
        "content", "goods_json", "size_json", "raw_json", "status", "error",
        "message_at", "created_at",
    ],
    "reply_attempts": [
        "id", "shop_id", "conversation_id", "message_id", "user_uid", "content",
        "status", "result_json", "error", "created_at",
    ],
    "transfer_attempts": [
        "id", "shop_id", "conversation_id", "user_uid", "csid", "remark",
        "status", "result_json", "error", "created_at",
    ],
    "knowledge_bases": [
        "id", "name", "description", "status", "created_by", "created_at", "updated_at",
    ],
    "knowledge_base_shops": ["id", "knowledge_base_id", "shop_id", "created_at"],
    "knowledge_files": [
        "id", "knowledge_base_id", "filename", "content_type", "file_path",
        "file_size", "file_hash", "status", "error", "created_at", "updated_at",
    ],
    "knowledge_chunks": [
        "id", "knowledge_base_id", "file_id", "chunk_index", "content",
        "source_label", "vector_id", "embedding_json", "token_count", "created_at",
    ],
    "knowledge_qa_items": [
        "id", "knowledge_base_id", "question", "reply", "image_base64",
        "image_name", "image_content_type", "sort_order", "created_at", "updated_at",
    ],
    "note_sets": [
        "id", "name", "description", "status", "created_by", "created_at", "updated_at",
    ],
    "note_set_shops": ["id", "note_set_id", "shop_id", "created_at"],
    "note_set_items": [
        "id", "note_set_id", "content", "created_by", "created_at", "updated_at",
    ],
    "intent_events": [
        "id", "shop_id", "conversation_id", "message_id", "user_uid", "intent_code",
        "raw_intent_code", "confidence", "resolution_status", "reply", "slots_json",
        "actions_json", "knowledge_json", "raw_json", "status", "error", "created_at",
    ],
    "action_requests": [
        "id", "shop_id", "conversation_id", "message_id", "intent_event_id",
        "user_uid", "action_type", "payload_json", "status", "result_json",
        "error", "created_at", "updated_at",
    ],
    "runtime_logs": [
        "id", "level", "module", "action", "message", "shop_id", "mall_id",
        "conversation_id", "user_uid", "request_id", "run_id", "pid",
        "error_trace", "context_json", "created_at",
    ],
    "shop_notes": [
        "id", "shop_id", "content", "created_by", "created_at", "updated_at",
    ],
    "return_records": [
        "id", "shop_id", "conversation_id", "message_id", "user_uid", "username",
        "order_no", "order_status", "record_type", "new_address", "remark",
        "source_message", "slots_json", "order_snapshot_json", "created_at",
    ],
    "return_record_drafts": [
        "id", "shop_id", "conversation_id", "user_uid", "username", "record_type",
        "order_no", "order_status", "new_address", "remark", "source_message",
        "slots_json", "order_snapshot_json", "missing_fields_json", "created_at",
        "updated_at",
    ],
}


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        `key` VARCHAR(96) PRIMARY KEY,
        value_json LONGTEXT NOT NULL,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS roles (
        name VARCHAR(32) PRIMARY KEY,
        display_name VARCHAR(64) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        username VARCHAR(64) NOT NULL UNIQUE,
        password_hash VARCHAR(255) NOT NULL,
        auth_version INT NOT NULL DEFAULT 1,
        display_name VARCHAR(128) NOT NULL,
        role VARCHAR(32) NOT NULL,
        is_active TINYINT(1) NOT NULL DEFAULT 1,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT fk_users_role FOREIGN KEY (role) REFERENCES roles(name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS shops (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        name VARCHAR(128) NOT NULL,
        remark VARCHAR(512) NOT NULL DEFAULT '',
        mall_id VARCHAR(64) NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'idle',
        auto_reply_enabled TINYINT(1) NOT NULL DEFAULT 1,
        last_error TEXT NULL,
        created_by BIGINT NULL,
        created_by_user_id BIGINT NULL,
        greeting_message TEXT NULL,
        closing_message TEXT NULL,
        greeting_use_llm TINYINT(1) NOT NULL DEFAULT 0,
        closing_use_llm TINYINT(1) NOT NULL DEFAULT 0,
        force_ai_reply TINYINT(1) NOT NULL DEFAULT 0,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_shops_status (status),
        INDEX idx_shops_mall_id (mall_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS shop_assignments (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        shop_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_shop_user (shop_id, user_id),
        CONSTRAINT fk_shop_assignments_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE,
        CONSTRAINT fk_shop_assignments_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS shop_sessions (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        shop_id BIGINT NOT NULL,
        status VARCHAR(32) NOT NULL,
        mall_id VARCHAR(64) NULL,
        access_token VARCHAR(255) NULL,
        ws_base_url VARCHAR(255) NULL,
        token_result LONGTEXT NULL,
        started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP NULL,
        error TEXT NULL,
        INDEX idx_shop_sessions_shop (shop_id, started_at),
        CONSTRAINT fk_shop_sessions_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS shop_login_caches (
        shop_id BIGINT PRIMARY KEY,
        cookies_json LONGTEXT NOT NULL,
        cookie_string LONGTEXT NULL,
        requests_headers_json LONGTEXT NULL,
        base_headers_json LONGTEXT NULL,
        login_result_json LONGTEXT NULL,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT fk_shop_login_caches_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS shop_runtime_leases (
        shop_id BIGINT PRIMARY KEY,
        worker_id VARCHAR(128) NOT NULL,
        pid INT NOT NULL,
        heartbeat_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        status VARCHAR(32) NOT NULL DEFAULT 'online',
        version BIGINT NOT NULL DEFAULT 1,
        INDEX idx_runtime_leases_worker (worker_id, expires_at),
        INDEX idx_runtime_leases_expires (expires_at),
        CONSTRAINT fk_runtime_leases_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS qr_login_attempts (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        shop_id BIGINT NOT NULL,
        session_id BIGINT NULL,
        token VARCHAR(255) NOT NULL,
        qrcode_path VARCHAR(1024) NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        query_result LONGTEXT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP NULL,
        error TEXT NULL,
        INDEX idx_qr_shop (shop_id, created_at),
        CONSTRAINT fk_qr_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        shop_id BIGINT NOT NULL,
        mall_id VARCHAR(64) NULL,
        conv_id VARCHAR(128) NULL,
        chat_type_id VARCHAR(32) NULL,
        chat_type VARCHAR(64) NULL,
        user_uid VARCHAR(64) NOT NULL,
        nickname VARCHAR(128) NOT NULL DEFAULT '',
        last_message_preview VARCHAR(512) NOT NULL DEFAULT '',
        last_message_at BIGINT NULL,
        unread_count INT NOT NULL DEFAULT 0,
        transferred_at TIMESTAMP NULL DEFAULT NULL,
        bot_reply_enabled TINYINT(1) NOT NULL DEFAULT 1,
        human_attention_required TINYINT(1) NOT NULL DEFAULT 0,
        human_attention_reason VARCHAR(128) NULL DEFAULT NULL,
        human_attention_at TIMESTAMP NULL DEFAULT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_conversation_shop_mall_user (shop_id, mall_id, user_uid),
        INDEX idx_conversation_shop_updated (shop_id, updated_at),
        CONSTRAINT fk_conversations_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        shop_id BIGINT NOT NULL,
        conversation_id BIGINT NULL,
        direction VARCHAR(16) NOT NULL,
        msg_id VARCHAR(128) NULL,
        client_msg_id VARCHAR(128) NULL,
        user_uid VARCHAR(64) NULL,
        sender_role VARCHAR(32) NULL,
        message_type INT NULL,
        kind VARCHAR(32) NOT NULL DEFAULT 'unknown',
        content TEXT NULL,
        goods_json LONGTEXT NULL,
        size_json LONGTEXT NULL,
        raw_json LONGTEXT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'received',
        error TEXT NULL,
        message_at BIGINT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_messages_conversation (conversation_id, created_at),
        INDEX idx_messages_shop (shop_id, created_at),
        CONSTRAINT fk_messages_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS reply_attempts (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        shop_id BIGINT NOT NULL,
        conversation_id BIGINT NULL,
        message_id BIGINT NULL,
        user_uid VARCHAR(64) NOT NULL,
        content TEXT NOT NULL,
        status VARCHAR(32) NOT NULL,
        result_json LONGTEXT NULL,
        error TEXT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_reply_shop (shop_id, created_at),
        CONSTRAINT fk_reply_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS transfer_attempts (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        shop_id BIGINT NOT NULL,
        conversation_id BIGINT NULL,
        user_uid VARCHAR(64) NOT NULL,
        csid VARCHAR(64) NOT NULL,
        remark VARCHAR(512) NOT NULL DEFAULT '',
        status VARCHAR(32) NOT NULL,
        result_json LONGTEXT NULL,
        error TEXT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_transfer_shop (shop_id, created_at),
        CONSTRAINT fk_transfer_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_bases (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        name VARCHAR(160) NOT NULL,
        description VARCHAR(512) NOT NULL DEFAULT '',
        status VARCHAR(32) NOT NULL DEFAULT 'active',
        created_by BIGINT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_knowledge_bases_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_base_shops (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        knowledge_base_id BIGINT NOT NULL,
        shop_id BIGINT NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_kb_shop (knowledge_base_id, shop_id),
        INDEX idx_kb_shop_shop (shop_id),
        CONSTRAINT fk_kb_shops_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE,
        CONSTRAINT fk_kb_shops_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_files (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        knowledge_base_id BIGINT NOT NULL,
        filename VARCHAR(255) NOT NULL,
        content_type VARCHAR(128) NULL,
        file_path VARCHAR(1024) NOT NULL,
        file_size BIGINT NOT NULL DEFAULT 0,
        file_hash VARCHAR(128) NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'uploaded',
        error TEXT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_knowledge_files_kb (knowledge_base_id, created_at),
        CONSTRAINT fk_knowledge_files_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_chunks (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        knowledge_base_id BIGINT NOT NULL,
        file_id BIGINT NOT NULL,
        chunk_index INT NOT NULL,
        content TEXT NOT NULL,
        source_label VARCHAR(255) NULL,
        vector_id VARCHAR(128) NULL,
        embedding_json LONGTEXT NULL,
        token_count INT NOT NULL DEFAULT 0,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_file_chunk (file_id, chunk_index),
        INDEX idx_knowledge_chunks_kb (knowledge_base_id, file_id),
        INDEX idx_knowledge_chunks_vector (vector_id),
        CONSTRAINT fk_knowledge_chunks_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE,
        CONSTRAINT fk_knowledge_chunks_file FOREIGN KEY (file_id) REFERENCES knowledge_files(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_qa_items (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        knowledge_base_id BIGINT NOT NULL,
        question TEXT NOT NULL,
        reply TEXT NULL,
        image_base64 LONGTEXT NULL,
        image_name VARCHAR(255) NULL,
        image_content_type VARCHAR(128) NULL,
        sort_order INT NOT NULL DEFAULT 0,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_knowledge_qa_kb (knowledge_base_id, sort_order),
        CONSTRAINT fk_knowledge_qa_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS note_sets (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        name VARCHAR(160) NOT NULL,
        description VARCHAR(512) NOT NULL DEFAULT '',
        status VARCHAR(32) NOT NULL DEFAULT 'active',
        created_by BIGINT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_note_sets_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS note_set_shops (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        note_set_id BIGINT NOT NULL,
        shop_id BIGINT NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_ns_shop (note_set_id, shop_id),
        INDEX idx_ns_shop_shop (shop_id),
        CONSTRAINT fk_ns_shops_ns FOREIGN KEY (note_set_id) REFERENCES note_sets(id) ON DELETE CASCADE,
        CONSTRAINT fk_ns_shops_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS note_set_items (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        note_set_id BIGINT NOT NULL,
        content TEXT NOT NULL,
        created_by BIGINT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_note_set_items_ns (note_set_id),
        CONSTRAINT fk_ns_items_ns FOREIGN KEY (note_set_id) REFERENCES note_sets(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS intent_events (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        shop_id BIGINT NOT NULL,
        conversation_id BIGINT NULL,
        message_id BIGINT NULL,
        user_uid VARCHAR(64) NULL,
        intent_code VARCHAR(96) NOT NULL,
        raw_intent_code VARCHAR(128) NULL,
        confidence DECIMAL(5,4) NOT NULL DEFAULT 0,
        resolution_status VARCHAR(32) NOT NULL,
        reply TEXT NOT NULL,
        slots_json LONGTEXT NULL,
        actions_json LONGTEXT NULL,
        knowledge_json LONGTEXT NULL,
        raw_json LONGTEXT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'created',
        error TEXT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_intent_events_shop (shop_id, created_at),
        INDEX idx_intent_events_conversation (conversation_id, created_at),
        CONSTRAINT fk_intent_events_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS action_requests (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        shop_id BIGINT NOT NULL,
        conversation_id BIGINT NULL,
        message_id BIGINT NULL,
        intent_event_id BIGINT NULL,
        user_uid VARCHAR(64) NULL,
        action_type VARCHAR(96) NOT NULL,
        payload_json LONGTEXT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        result_json LONGTEXT NULL,
        error TEXT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_action_requests_shop (shop_id, created_at),
        INDEX idx_action_requests_status (status, created_at),
        CONSTRAINT fk_action_requests_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE,
        CONSTRAINT fk_action_requests_intent FOREIGN KEY (intent_event_id) REFERENCES intent_events(id) ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_logs (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        level VARCHAR(16) NOT NULL,
        module VARCHAR(128) NOT NULL,
        action VARCHAR(128) NOT NULL,
        message TEXT NOT NULL,
        shop_id BIGINT NULL,
        mall_id VARCHAR(64) NULL,
        conversation_id BIGINT NULL,
        user_uid VARCHAR(64) NULL,
        request_id VARCHAR(128) NULL,
        run_id VARCHAR(64) NULL,
        pid INT NULL,
        error_trace LONGTEXT NULL,
        context_json LONGTEXT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_runtime_logs_created (created_at),
        INDEX idx_runtime_logs_shop (shop_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS shop_notes (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        shop_id BIGINT NOT NULL,
        content TEXT NOT NULL,
        created_by BIGINT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_shop_notes_shop (shop_id),
        CONSTRAINT fk_shop_notes_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS return_records (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        shop_id BIGINT NOT NULL,
        conversation_id BIGINT NULL,
        message_id BIGINT NULL,
        user_uid VARCHAR(64) NULL,
        username VARCHAR(128) NOT NULL DEFAULT '',
        order_no VARCHAR(128) NOT NULL DEFAULT '',
        order_status VARCHAR(128) NOT NULL DEFAULT '',
        record_type VARCHAR(32) NOT NULL,
        new_address TEXT NULL,
        remark TEXT NULL,
        source_message TEXT NULL,
        slots_json LONGTEXT NULL,
        order_snapshot_json LONGTEXT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_return_records_shop (shop_id, created_at),
        INDEX idx_return_records_type (record_type, created_at),
        INDEX idx_return_records_order (order_no),
        CONSTRAINT fk_return_records_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS return_record_drafts (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        shop_id BIGINT NOT NULL,
        conversation_id BIGINT NOT NULL,
        user_uid VARCHAR(64) NULL,
        username VARCHAR(128) NOT NULL DEFAULT '',
        record_type VARCHAR(32) NOT NULL,
        order_no VARCHAR(128) NOT NULL DEFAULT '',
        order_status VARCHAR(128) NOT NULL DEFAULT '',
        new_address TEXT NULL,
        remark TEXT NULL,
        source_message TEXT NULL,
        slots_json LONGTEXT NULL,
        order_snapshot_json LONGTEXT NULL,
        missing_fields_json LONGTEXT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_return_draft_conversation (shop_id, conversation_id),
        INDEX idx_return_drafts_shop (shop_id, updated_at),
        CONSTRAINT fk_return_drafts_shop FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]
