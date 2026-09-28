"""Shops, sessions, login caches, runtime leases, QR attempts and shop notes data access."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from openkefu.web.db import Database, json_dumps

SHOP_WITH_CACHE_SELECT = """
    SELECT s.*, EXISTS(SELECT 1 FROM shop_login_caches c WHERE c.shop_id=s.id) AS has_login_cache
    FROM shops s
"""


class ShopsRepository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # shops
    # ------------------------------------------------------------------
    def all_shop_ids(self) -> set[int]:
        return {int(row["id"]) for row in self.db.query("SELECT id FROM shops")}

    def exists(self, shop_id: int) -> bool:
        return bool(self.db.query_one("SELECT id FROM shops WHERE id=%s", (shop_id,)))

    def by_id(self, shop_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM shops WHERE id=%s", (shop_id,))

    def row_with_cache(self, shop_id: int) -> dict[str, Any] | None:
        return self.db.query_one(f"{SHOP_WITH_CACHE_SELECT} WHERE s.id=%s", (shop_id,))

    def list_with_cache_admin(self) -> list[dict[str, Any]]:
        return self.db.query(f"{SHOP_WITH_CACHE_SELECT} ORDER BY s.id DESC")

    def list_with_cache_for_user(self, user_id: int) -> list[dict[str, Any]]:
        return self.db.query(
            f"""
            {SHOP_WITH_CACHE_SELECT}
            JOIN shop_assignments sa ON sa.shop_id=s.id
            WHERE sa.user_id=%s
            ORDER BY s.id DESC
            """,
            (user_id,),
        )

    def create(self, *, name: str, remark: str, auto_reply_enabled: bool, transfer_csids: list[str] | None,
               created_by: int, greeting_message: str | None, greeting_use_llm: bool, force_ai_reply: bool) -> int:
        with self.db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO shops (name, remark, auto_reply_enabled, transfer_csids, created_by, created_by_user_id,
                                       greeting_message, greeting_use_llm, force_ai_reply)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (name, remark, int(auto_reply_enabled),
                     json_dumps(transfer_csids) if transfer_csids else None,
                     created_by, created_by,
                     greeting_message, int(greeting_use_llm), int(force_ai_reply)),
                )
                shop_id = int(cursor.lastrowid)
                cursor.execute(
                    "INSERT IGNORE INTO shop_assignments (shop_id, user_id) VALUES (%s,%s)",
                    (shop_id, created_by),
                )
        return shop_id

    def update_fields(self, shop_id: int, assignments: dict[str, Any]) -> None:
        if not assignments:
            return
        fields = ", ".join(f"{name}=%s" for name in assignments)
        self.db.execute(f"UPDATE shops SET {fields} WHERE id=%s", [*assignments.values(), shop_id])

    def set_status(self, shop_id: int, status: str, *, last_error: str | None = None, mall_id: str | None = None) -> None:
        assignments: dict[str, Any] = {"status": status, "last_error": last_error}
        if mall_id is not None:
            assignments["mall_id"] = mall_id
        self.update_fields(shop_id, assignments)

    def mark_online(self, shop_id: int, *, mall_id: str, nickname: str | None) -> None:
        self.db.execute(
            "UPDATE shops SET status='online', mall_id=%s, nickname=%s, last_error=NULL WHERE id=%s",
            (mall_id, nickname, shop_id),
        )

    def saved_credentials(self, shop_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT login_username, login_password FROM shops WHERE id=%s", (shop_id,))

    def mark_logged_in(self, shop_id: int, mall_id: str, cursor=None) -> None:
        sql = "UPDATE shops SET status='logged_in', mall_id=%s, last_error=NULL WHERE id=%s"
        if cursor is not None:
            cursor.execute(sql, (mall_id, shop_id))
        else:
            self.db.execute(sql, (mall_id, shop_id))

    def get_auto_reply_enabled(self, shop_id: int) -> bool:
        row = self.db.query_one("SELECT auto_reply_enabled FROM shops WHERE id=%s", (shop_id,))
        return bool((row or {}).get("auto_reply_enabled"))

    def get_transfer_csids(self, shop_id: int) -> list[str]:
        row = self.db.query_one("SELECT transfer_csids FROM shops WHERE id=%s", (shop_id,))
        raw = (row or {}).get("transfer_csids")
        if isinstance(raw, str):
            try:
                import json as _json
                value = _json.loads(raw)
                return value if isinstance(value, list) else []
            except (TypeError, ValueError):
                return []
        return raw if isinstance(raw, list) else []

    def auto_reply_state(self, shop_id: int) -> dict[str, Any]:
        return self.db.query_one(
            "SELECT auto_reply_enabled, status FROM shops WHERE id=%s", (shop_id,),
        ) or {}

    def get_auto_reply_flags(self, shop_id: int) -> dict[str, Any]:
        row = self.db.query_one("SELECT auto_reply_enabled, force_ai_reply FROM shops WHERE id=%s", (shop_id,))
        return {
            "auto_reply_enabled": bool((row or {}).get("auto_reply_enabled")),
            "force_ai_reply": bool((row or {}).get("force_ai_reply")),
        }

    def get_mall_and_name(self, shop_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT mall_id, name FROM shops WHERE id=%s", (shop_id,))

    def get_creator(self, shop_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT created_by_user_id, created_by FROM shops WHERE id=%s", (shop_id,))

    def assignment_candidates(self, shop_id: int) -> list[dict[str, Any]]:
        return self.db.query("SELECT user_id FROM shop_assignments WHERE shop_id=%s ORDER BY id LIMIT 2", (shop_id,))

    def login_result_cache(self, shop_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT login_result_json FROM shop_login_caches WHERE shop_id=%s", (shop_id,))

    def get_greeting_message(self, shop_id: int) -> str | None:
        row = self.db.query_one("SELECT greeting_message FROM shops WHERE id=%s", (shop_id,))
        return (row or {}).get("greeting_message")

    def set_greeting_message(self, shop_id: int, message: str) -> None:
        self.db.execute("UPDATE shops SET greeting_message=%s WHERE id=%s", (message, shop_id))

    def delete(self, shop_id: int) -> None:
        self.db.execute("DELETE FROM shops WHERE id=%s", (shop_id,))

    # saved login credentials (encrypted values are passed through as-is)
    def save_login_credentials(self, shop_id: int, encrypted_username: str, encrypted_password: str) -> None:
        self.db.execute(
            "UPDATE shops SET login_username=%s, login_password=%s WHERE id=%s",
            (encrypted_username, encrypted_password, shop_id),
        )

    def clear_login_credentials(self, shop_id: int) -> None:
        self.db.execute("UPDATE shops SET login_username=NULL, login_password=NULL WHERE id=%s", (shop_id,))

    def has_login_credentials(self, shop_id: int) -> bool:
        row = self.db.query_one("SELECT login_username FROM shops WHERE id=%s", (shop_id,))
        return bool((row or {}).get("login_username"))

    # ------------------------------------------------------------------
    # shop sessions
    # ------------------------------------------------------------------
    def create_session(self, shop_id: int, status: str, cursor=None, **extra: Any) -> int:
        columns = {"shop_id": shop_id, "status": status, **extra}
        names = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))
        sql = f"INSERT INTO shop_sessions ({names}) VALUES ({placeholders})"
        params = list(columns.values())
        if cursor is not None:
            cursor.execute(sql, params)
            return int(cursor.lastrowid)
        return self.db.execute(sql, params)

    def end_session(self, session_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE shop_sessions SET status=%s, ended_at=NOW() WHERE id=%s",
            (status, session_id),
        )

    def mark_session_logged_in(self, session_id: int, mall_id: str, cursor=None) -> None:
        sql = "UPDATE shop_sessions SET status='logged_in', mall_id=%s WHERE id=%s"
        if cursor is not None:
            cursor.execute(sql, (mall_id, session_id))
        else:
            self.db.execute(sql, (mall_id, session_id))

    def update_session(self, session_id: int, assignments: dict[str, Any]) -> None:
        if not assignments:
            return
        fields = ", ".join(f"{name}=%s" for name in assignments)
        self.db.execute(f"UPDATE shop_sessions SET {fields} WHERE id=%s", [*assignments.values(), session_id])

    # ------------------------------------------------------------------
    # shop login caches
    # ------------------------------------------------------------------
    def upsert_login_cache(self, *, shop_id: int, cookies_json: str, cookie_string: str,
                           requests_headers_json: str, base_headers_json: str, login_result_json: str,
                           cursor=None) -> None:
        sql = """
            INSERT INTO shop_login_caches
            (shop_id, cookies_json, cookie_string, requests_headers_json, base_headers_json, login_result_json)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                cookies_json=VALUES(cookies_json),
                cookie_string=VALUES(cookie_string),
                requests_headers_json=VALUES(requests_headers_json),
                base_headers_json=VALUES(base_headers_json),
                login_result_json=VALUES(login_result_json)
        """
        params = (shop_id, cookies_json, cookie_string, requests_headers_json, base_headers_json, login_result_json)
        if cursor is not None:
            cursor.execute(sql, params)
        else:
            self.db.execute(sql, params)

    def login_cache(self, shop_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM shop_login_caches WHERE shop_id=%s", (shop_id,))

    def update_login_cache_cookies(self, shop_id: int, *, cookies_json: str, cookie_string: str,
                                   base_headers_json: str, login_result_json: str) -> None:
        self.db.execute(
            """
            UPDATE shop_login_caches
            SET cookies_json=%s,
                cookie_string=%s,
                base_headers_json=%s,
                login_result_json=%s
            WHERE shop_id=%s
            """,
            (cookies_json, cookie_string, base_headers_json, login_result_json, shop_id),
        )

    def delete_login_cache(self, shop_id: int) -> None:
        self.db.execute("DELETE FROM shop_login_caches WHERE shop_id=%s", (shop_id,))

    # ------------------------------------------------------------------
    # runtime leases
    # ------------------------------------------------------------------
    def acquire_lease(self, shop_id: int, *, worker_id: str, pid: int, ttl_seconds: int, required: bool = False) -> bool:
        """Atomically take over the shop lease under a row lock.

        Returns False when another live worker holds it (and required is False);
        raises RuntimeError when required and the lease is owned elsewhere.
        """
        with self.db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM shop_runtime_leases WHERE shop_id=%s FOR UPDATE", (shop_id,))
                row = cursor.fetchone()
                now = datetime.now()
                can_take = (
                    not row
                    or str(row.get("worker_id") or "") == worker_id
                    or (row.get("expires_at") and row["expires_at"] < now)
                )
                if not can_take:
                    if required:
                        raise RuntimeError(f"shop runtime is owned by worker {row.get('worker_id')}")
                    return False
                expires_at = now + timedelta(seconds=ttl_seconds)
                if row:
                    cursor.execute(
                        """
                        UPDATE shop_runtime_leases
                        SET worker_id=%s, pid=%s, heartbeat_at=NOW(), expires_at=%s,
                            status='online', version=version+1
                        WHERE shop_id=%s
                        """,
                        (worker_id, pid, expires_at, shop_id),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO shop_runtime_leases
                        (shop_id, worker_id, pid, heartbeat_at, expires_at, status, version)
                        VALUES (%s,%s,%s,NOW(),%s,'online',1)
                        """,
                        (shop_id, worker_id, pid, expires_at),
                    )
        return True

    def release_lease(self, shop_id: int, *, worker_id: str, status: str) -> None:
        self.db.execute(
            """
            UPDATE shop_runtime_leases
            SET status=%s, expires_at=NOW(), heartbeat_at=NOW()
            WHERE shop_id=%s AND worker_id=%s
            """,
            (status, shop_id, worker_id),
        )

    def owns_shop(self, shop_id: int, *, worker_id: str) -> bool:
        row = self.db.query_one(
            "SELECT worker_id, expires_at FROM shop_runtime_leases WHERE shop_id=%s",
            (shop_id,),
        )
        if not row:
            return False
        expires_at = row.get("expires_at")
        return str(row.get("worker_id") or "") == worker_id and (not expires_at or expires_at >= datetime.now())

    def active_leased_shop_ids(self, shop_ids: list[int]) -> set[int]:
        if not shop_ids:
            return set()
        placeholders = ",".join(["%s"] * len(shop_ids))
        rows = self.db.query(
            f"""
            SELECT shop_id
            FROM shop_runtime_leases
            WHERE shop_id IN ({placeholders})
              AND expires_at>=NOW()
              AND status='online'
            """,
            shop_ids,
        )
        return {int(row["shop_id"]) for row in rows}

    def update_lease_fields(self, shop_id: int, assignments: dict[str, Any], *, worker_id: str | None = None) -> None:
        where = "WHERE shop_id=%s"
        params: list[Any] = [*assignments.values(), shop_id]
        if worker_id is not None:
            where += " AND worker_id=%s"
            params.append(worker_id)
        fields = ", ".join(f"{name}=%s" for name in assignments)
        self.db.execute(f"UPDATE shop_runtime_leases SET {fields} {where}", params)

    def lease_row(self, shop_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM shop_runtime_leases WHERE shop_id=%s", (shop_id,))

    def runtime_workers(self) -> list[dict[str, Any]]:
        return self.db.query(
            """
            SELECT worker_id, pid, status, COUNT(*) AS shop_count,
                   MAX(heartbeat_at) AS heartbeat_at, MAX(expires_at) AS expires_at
            FROM shop_runtime_leases
            GROUP BY worker_id, pid, status
            ORDER BY heartbeat_at DESC
            """
        )

    def shop_owner(self, shop_id: int) -> dict[str, Any] | None:
        return self.lease_row(shop_id)

    # ------------------------------------------------------------------
    # QR login attempts
    # ------------------------------------------------------------------
    def latest_qr_attempt_id(self, shop_id: int) -> int | None:
        row = self.db.query_one(
            "SELECT id FROM qr_login_attempts WHERE shop_id=%s ORDER BY id DESC LIMIT 1",
            (shop_id,),
        )
        return int(row["id"]) if row else None

    def create_qr_attempt(self, *, shop_id: int, session_id: int, token: str, qrcode_path: str) -> int:
        return self.db.execute(
            """
            INSERT INTO qr_login_attempts (shop_id, session_id, token, qrcode_path, status)
            VALUES (%s,%s,%s,%s,'pending')
            """,
            (shop_id, session_id, token, qrcode_path),
        )

    def qr_attempt(self, *, shop_id: int, attempt_id: int | None = None) -> dict[str, Any] | None:
        if attempt_id:
            return self.db.query_one(
                "SELECT qrcode_path FROM qr_login_attempts WHERE id=%s AND shop_id=%s",
                (attempt_id, shop_id),
            )
        return self.db.query_one(
            "SELECT qrcode_path FROM qr_login_attempts WHERE shop_id=%s ORDER BY id DESC LIMIT 1",
            (shop_id,),
        )

    def fail_qr_attempt(self, attempt_id: int, error: str) -> None:
        self.db.execute(
            "UPDATE qr_login_attempts SET status='failed', error=%s, completed_at=NOW() WHERE id=%s",
            (error, attempt_id),
        )

    def succeed_qr_attempt(self, attempt_id: int, query_json: str, cursor=None) -> None:
        sql = "UPDATE qr_login_attempts SET status='success', query_result=%s, completed_at=NOW() WHERE id=%s"
        params = (query_json, attempt_id)
        if cursor is not None:
            cursor.execute(sql, params)
        else:
            self.db.execute(sql, params)

    def mall_owner(self, mall_id: str, *, exclude_shop_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT id, name FROM shops WHERE mall_id=%s AND id<>%s LIMIT 1",
            (mall_id, exclude_shop_id),
        )

    def update_qr_attempt(self, attempt_id: int, assignments: dict[str, Any]) -> None:
        if not assignments:
            return
        fields = ", ".join(f"{name}=%s" for name in assignments)
        self.db.execute(f"UPDATE qr_login_attempts SET {fields} WHERE id=%s", [*assignments.values(), attempt_id])

    def stale_qr_attempts(self, *, days: int) -> list[dict[str, Any]]:
        return self.db.query(
            f"SELECT id, qrcode_path FROM qr_login_attempts WHERE created_at < DATE_SUB(NOW(), INTERVAL {int(days)} DAY)"
        )

    def delete_stale_qr_attempts(self, *, days: int) -> int:
        return self.db.execute(
            f"DELETE FROM qr_login_attempts WHERE created_at < DATE_SUB(NOW(), INTERVAL {int(days)} DAY)"
        )

    # ------------------------------------------------------------------
    # shop notes
    # ------------------------------------------------------------------
    def notes(self, shop_id: int) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT id, shop_id, content, created_by, created_at, updated_at FROM shop_notes WHERE shop_id=%s ORDER BY id ASC",
            (shop_id,),
        )

    def note_columns(self) -> str:
        return "id, shop_id, content, created_by, created_at, updated_at"

    def create_note(self, shop_id: int, content: str, created_by: int) -> int:
        return self.db.execute(
            "INSERT INTO shop_notes (shop_id, content, created_by) VALUES (%s,%s,%s)",
            (shop_id, content, created_by),
        )

    def note_by_id(self, note_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            f"SELECT {self.note_columns()} FROM shop_notes WHERE id=%s",
            (note_id,),
        )

    def note_exists(self, note_id: int, shop_id: int) -> bool:
        return bool(
            self.db.query_one("SELECT id FROM shop_notes WHERE id=%s AND shop_id=%s", (note_id, shop_id))
        )

    def update_note(self, note_id: int, content: str) -> None:
        self.db.execute("UPDATE shop_notes SET content=%s WHERE id=%s", (content, note_id))

    def delete_note(self, note_id: int) -> None:
        self.db.execute("DELETE FROM shop_notes WHERE id=%s", (note_id,))

    # ------------------------------------------------------------------
    # cascading cleanup on shop delete (cross-table, single transaction)
    # ------------------------------------------------------------------
    def delete_shop_chat_records(self, shop_id: int) -> None:
        with self.db.connect() as conn:
            with conn.cursor() as cursor:
                for table in (
                    "return_record_drafts",
                    "return_records",
                    "action_requests",
                    "intent_events",
                    "transfer_attempts",
                    "reply_attempts",
                    "messages",
                    "conversations",
                ):
                    cursor.execute(f"DELETE FROM {table} WHERE shop_id=%s", (shop_id,))

    # ------------------------------------------------------------------
    # startup reconnect / stale-online maintenance (manager-specific)
    # ------------------------------------------------------------------
    def startup_reconnect_candidates(self, *, worker_id: str, grace_seconds: int) -> list[dict[str, Any]]:
        return self.db.query(
            """
            SELECT s.id
            FROM shops s
            JOIN shop_login_caches c ON c.shop_id=s.id
            JOIN shop_runtime_leases l ON l.shop_id=s.id
            WHERE s.status IN ('online','connecting')
              AND l.status='online'
              AND (
                    l.worker_id=%s
                    OR l.expires_at<DATE_SUB(NOW(), INTERVAL %s SECOND)
                  )
            ORDER BY s.id
            """,
            (worker_id, grace_seconds),
        )

    def non_reconnectable_online_shop_ids(self, *, worker_id: str, grace_seconds: int) -> list[int]:
        rows = self.db.query(
            f"""
            SELECT s.id
            FROM shops s
            LEFT JOIN shop_login_caches c ON c.shop_id=s.id
            LEFT JOIN shop_runtime_leases l ON l.shop_id=s.id
            WHERE s.status IN ('online','connecting')
              AND NOT (
                    c.shop_id IS NOT NULL
                    AND l.status='online'
                    AND (
                          l.worker_id=%s
                          OR l.expires_at<DATE_SUB(NOW(), INTERVAL %s SECOND)
                        )
                  )
              AND NOT (
                    l.status='online'
                    AND l.worker_id<>%s
                    AND l.expires_at>=DATE_SUB(NOW(), INTERVAL %s SECOND)
                  )
            ORDER BY s.id
            """,
            (worker_id, grace_seconds, worker_id, grace_seconds),
        )
        return [int(row["id"]) for row in rows]

    def bulk_mark_sessions_offline(self, shop_ids: list[int], *, error: str) -> None:
        if not shop_ids:
            return
        placeholders = ",".join(["%s"] * len(shop_ids))
        self.db.execute(
            f"""
            UPDATE shop_sessions
            SET status='offline',
                ended_at=COALESCE(ended_at, NOW()),
                error=COALESCE(error, %s)
            WHERE status IN ('online','connecting')
              AND shop_id IN ({placeholders})
            """,
            [error, *shop_ids],
        )

    def bulk_mark_shops_offline(self, shop_ids: list[int]) -> None:
        if not shop_ids:
            return
        placeholders = ",".join(["%s"] * len(shop_ids))
        self.db.execute(
            f"""
            UPDATE shops
            SET status='offline',
                last_error=NULL
            WHERE status IN ('online','connecting')
              AND id IN ({placeholders})
            """,
            shop_ids,
        )

    def bulk_release_stale_leases(self, shop_ids: list[int], *, worker_id: str) -> None:
        if not shop_ids:
            return
        placeholders = ",".join(["%s"] * len(shop_ids))
        self.db.execute(
            f"""
            UPDATE shop_runtime_leases
            SET status='offline',
                expires_at=NOW(),
                heartbeat_at=NOW()
            WHERE shop_id IN ({placeholders})
              AND (worker_id=%s OR expires_at<NOW())
            """,
            [*shop_ids, worker_id],
        )

    def claim_startup_reconnect_lease(self, shop_id: int, *, worker_id: str, pid: int,
                                      expires_at: datetime, grace_seconds: int) -> bool:
        updated = self.db.execute(
            """
            UPDATE shop_runtime_leases
            SET worker_id=%s,
                pid=%s,
                heartbeat_at=NOW(),
                expires_at=%s,
                status='online',
                version=version+1
            WHERE shop_id=%s
              AND status='online'
              AND (
                    worker_id=%s
                    OR expires_at<DATE_SUB(NOW(), INTERVAL %s SECOND)
                  )
            """,
            (worker_id, pid, expires_at, shop_id, worker_id, grace_seconds),
        )
        return bool(updated)

    def end_session_with_error(self, session_id: int, message: str) -> None:
        self.db.execute(
            "UPDATE shop_sessions SET status='offline', ended_at=COALESCE(ended_at, NOW()), error=%s WHERE id=%s",
            (message, session_id),
        )

    def end_shop_sessions_with_error(self, shop_id: int, message: str) -> None:
        self.db.execute(
            """
            UPDATE shop_sessions
            SET status='offline',
                ended_at=COALESCE(ended_at, NOW()),
                error=COALESCE(error, %s)
            WHERE shop_id=%s
              AND status IN ('online','connecting')
            """,
            (message, shop_id),
        )

    def set_status_with_error(self, shop_id: int, status: str, message: str) -> None:
        self.db.execute(
            "UPDATE shops SET status=%s, last_error=%s WHERE id=%s",
            (status, message, shop_id),
        )

    def force_offline_lease(self, shop_id: int, *, worker_id: str) -> None:
        self.db.execute(
            """
            UPDATE shop_runtime_leases
            SET status='offline',
                expires_at=NOW(),
                heartbeat_at=NOW()
            WHERE shop_id=%s
              AND (worker_id=%s OR expires_at<NOW())
            """,
            (shop_id, worker_id),
        )

    def has_active_lease(self, shop_id: int) -> bool:
        return bool(
            self.db.query_one(
                """
                SELECT shop_id
                FROM shop_runtime_leases
                WHERE shop_id=%s
                  AND status='online'
                  AND expires_at>=NOW()
                """,
                (shop_id,),
            )
        )

    def heartbeat_lease(self, shop_id: int, *, worker_id: str, pid: int, expires_at: datetime) -> None:
        self.db.execute(
            """
            UPDATE shop_runtime_leases
            SET heartbeat_at=NOW(), expires_at=%s, pid=%s
            WHERE shop_id=%s AND worker_id=%s
            """,
            (expires_at, pid, shop_id, worker_id),
        )

    def stale_online_shop_ids(self, *, grace_seconds: int) -> list[int]:
        rows = self.db.query(
            f"""
            SELECT s.id
            FROM shops s
            LEFT JOIN shop_runtime_leases l ON l.shop_id=s.id
            WHERE s.status IN ('online','connecting')
              AND (
                    l.shop_id IS NULL
                    OR l.status<>'online'
                    OR l.expires_at<DATE_SUB(NOW(), INTERVAL %s SECOND)
                  )
            ORDER BY s.id
            """,
            (grace_seconds,),
        )
        return [int(row["id"]) for row in rows]

    def expire_offline_lease(self, shop_id: int) -> None:
        self.db.execute(
            """
            UPDATE shop_runtime_leases
            SET status='offline', expires_at=NOW(), heartbeat_at=NOW()
            WHERE shop_id=%s AND expires_at<NOW()
            """,
            (shop_id,),
        )

    def mark_shop_offline_with_error(self, shop_id: int, message: str) -> None:
        self.db.execute(
            "UPDATE shops SET status='offline', last_error=%s WHERE id=%s",
            (message, shop_id),
        )
