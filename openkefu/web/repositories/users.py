"""Users, roles, app settings and shop assignments data access."""

from __future__ import annotations

import json
from typing import Any

from openkefu.web.db import Database, json_dumps

USER_PUBLIC_COLUMNS = "id, username, display_name, role, is_active, created_at, updated_at, auth_version"


class UsersRepository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    def has_any_user(self) -> bool:
        return bool(self.db.query_one("SELECT id FROM users LIMIT 1"))

    def by_username(self, username: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM users WHERE username=%s", (username,))

    def username_exists(self, username: str) -> bool:
        return bool(self.db.query_one("SELECT id FROM users WHERE username=%s", (username,)))

    def by_id(self, user_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM users WHERE id=%s", (user_id,))

    def by_id_public(self, user_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            f"SELECT {USER_PUBLIC_COLUMNS} FROM users WHERE id=%s",
            (user_id,),
        )

    def active_auth_user(self, user_id: int) -> dict[str, Any] | None:
        """Columns needed for JWT-authenticated user resolution."""
        return self.db.query_one(
            """
            SELECT id, username, display_name, role, is_active, auth_version
            FROM users
            WHERE id=%s
            """,
            (user_id,),
        )

    def create(self, *, username: str, password_hash: str, display_name: str, role: str, is_active: bool = True) -> int:
        return self.db.execute(
            """
            INSERT INTO users
            (username, password_hash, display_name, role, is_active)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (username, password_hash, display_name, role, int(is_active)),
        )

    def setup_admin_atomically(self, *, username: str, password_hash: str, display_name: str) -> int:
        """Create the first admin under an app_settings row lock, or raise ValueError when initialized."""
        with self.db.connect() as conn:
            with conn.cursor() as cursor:
                # 原子化初始化：对 app_settings 加行锁串行化并发请求，
                # 避免"未初始化窗口"被并发双请求抢建两个 admin。
                cursor.execute("SELECT value_json FROM app_settings WHERE `key`='registration_enabled' FOR UPDATE")
                cursor.execute("SELECT id FROM users LIMIT 1")
                if cursor.fetchone():
                    raise ValueError("system already initialized")
                cursor.execute(
                    """
                    INSERT INTO users (username, password_hash, display_name, role, is_active)
                    VALUES (%s, %s, %s, 'admin', 1)
                    """,
                    (username, password_hash, display_name),
                )
                return int(cursor.lastrowid)

    def update_fields(self, user_id: int, assignments: dict[str, Any]) -> None:
        """Update plain columns; auth_version is never touched here."""
        if not assignments:
            return
        fields = ", ".join(f"{name}=%s" for name in assignments)
        self.db.execute(
            f"UPDATE users SET {fields} WHERE id=%s",
            [*assignments.values(), user_id],
        )

    def update_user(self, user_id: int, assignments: dict[str, Any]) -> None:
        """Update columns; when password_hash is set, also bump auth_version to revoke tokens."""
        if str(assignments.get("password_hash") or ""):
            assignments = {**assignments, "auth_version": "auth_version+1"}
            sql_fields = ", ".join(
                f"{name}=%s" if name != "auth_version" else "auth_version=auth_version+1"
                for name in assignments
            )
            values = [v for k, v in assignments.items() if k != "auth_version"]
            self.db.execute(f"UPDATE users SET {sql_fields} WHERE id=%s", [*values, user_id])
            return
        self.update_fields(user_id, assignments)

    def list_with_assignments(self) -> list[dict[str, Any]]:
        users = self.db.query(
            f"""
            SELECT {USER_PUBLIC_COLUMNS}
            FROM users
            ORDER BY id DESC
            """
        )
        by_user = self.assignment_index()
        for item in users:
            item["shop_ids"] = by_user.get(int(item["id"]), [])
        return users

    # ------------------------------------------------------------------
    # shop assignments
    # ------------------------------------------------------------------
    def assignment_index(self) -> dict[int, list[int]]:
        by_user: dict[int, list[int]] = {}
        for item in self.db.query("SELECT shop_id, user_id FROM shop_assignments"):
            by_user.setdefault(int(item["user_id"]), []).append(int(item["shop_id"]))
        return by_user

    def assigned_shop_ids(self, user_id: int) -> set[int]:
        return {
            int(row["shop_id"])
            for row in self.db.query("SELECT shop_id FROM shop_assignments WHERE user_id=%s", (user_id,))
        }

    def has_assignment(self, shop_id: int, user_id: int) -> bool:
        return bool(
            self.db.query_one(
                "SELECT id FROM shop_assignments WHERE shop_id=%s AND user_id=%s",
                (shop_id, user_id),
            )
        )

    def assign_shops(self, user_id: int, shop_ids: list[int]) -> None:
        for shop_id in shop_ids:
            self.db.execute(
                "INSERT IGNORE INTO shop_assignments (shop_id, user_id) VALUES (%s,%s)",
                (shop_id, user_id),
            )

    # ------------------------------------------------------------------
    # app settings
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.db.query_one("SELECT value_json FROM app_settings WHERE `key`=%s", (key,))
        if not row:
            return default
        try:
            return json.loads(str(row.get("value_json") or "null"))
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute(
            """
            INSERT INTO app_settings (`key`, value_json)
            VALUES (%s,%s)
            ON DUPLICATE KEY UPDATE value_json=VALUES(value_json)
            """,
            (key, json_dumps(value)),
        )
