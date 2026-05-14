"""SQLite: пользователи и обращения (персистентность между перезапусками)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_profile_line(full_name: str, department: str, module: str) -> str:
    return f"{full_name.strip()} | {department.strip()} | {module.strip()}"


class Storage:
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        chat_id INTEGER,
                        full_name TEXT NOT NULL DEFAULT '',
                        department TEXT NOT NULL DEFAULT '',
                        module TEXT NOT NULL DEFAULT '',
                        default_anonymous INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT,
                        updated_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS submissions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        chat_id INTEGER,
                        kind TEXT NOT NULL,
                        text TEXT NOT NULL,
                        anonymous INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        admin_note TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'open'
                    );
                    CREATE INDEX IF NOT EXISTS idx_submissions_user
                        ON submissions(user_id, created_at DESC);
                    CREATE TABLE IF NOT EXISTS submission_replies (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        submission_id INTEGER NOT NULL,
                        body TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        from_user INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS idx_replies_submission
                        ON submission_replies(submission_id, id);
                    """
                )
                conn.commit()
                self._migrate_submissions_columns(conn)
                self._migrate_submission_replies_columns(conn)
                conn.commit()
            finally:
                conn.close()

    def _migrate_submissions_columns(self, conn: sqlite3.Connection) -> None:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='submissions'")
        if cur.fetchone() is None:
            return
        cur = conn.execute("PRAGMA table_info(submissions)")
        cols = {str(r[1]) for r in cur.fetchall()}
        if "admin_note" not in cols:
            conn.execute(
                "ALTER TABLE submissions ADD COLUMN admin_note TEXT NOT NULL DEFAULT ''"
            )
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE submissions ADD COLUMN status TEXT NOT NULL DEFAULT 'open'"
            )

    def _migrate_submission_replies_columns(self, conn: sqlite3.Connection) -> None:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='submission_replies'"
        )
        if cur.fetchone() is None:
            return
        cur = conn.execute("PRAGMA table_info(submission_replies)")
        cols = {str(r[1]) for r in cur.fetchall()}
        if "from_user" not in cols:
            conn.execute(
                "ALTER TABLE submission_replies ADD COLUMN from_user INTEGER NOT NULL DEFAULT 0"
            )

    def get_user(self, user_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT user_id, chat_id, full_name, department, module, "
                    "default_anonymous, created_at, updated_at FROM users WHERE user_id = ?",
                    (user_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return dict(row)
            finally:
                conn.close()

    def upsert_user(
        self,
        user_id: int,
        *,
        chat_id: Optional[int],
        full_name: str,
        department: str,
        module: str,
        default_anonymous: bool,
    ) -> None:
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, chat_id, full_name, department, module,
                        default_anonymous, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        chat_id = excluded.chat_id,
                        full_name = excluded.full_name,
                        department = excluded.department,
                        module = excluded.module,
                        default_anonymous = excluded.default_anonymous,
                        updated_at = excluded.updated_at
                    """,
                    (
                        user_id,
                        chat_id,
                        full_name.strip(),
                        department.strip(),
                        module.strip(),
                        1 if default_anonymous else 0,
                        now,
                        now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def update_user_chat(self, user_id: int, chat_id: Optional[int]) -> None:
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE users SET chat_id = ?, updated_at = ? WHERE user_id = ?",
                    (chat_id, now, user_id),
                )
                conn.commit()
            finally:
                conn.close()

    def update_full_name(self, user_id: int, full_name: str) -> None:
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE users SET full_name = ?, updated_at = ? WHERE user_id = ?",
                    (full_name.strip(), now, user_id),
                )
                conn.commit()
            finally:
                conn.close()

    def update_department(self, user_id: int, department: str) -> None:
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE users SET department = ?, updated_at = ? WHERE user_id = ?",
                    (department.strip(), now, user_id),
                )
                conn.commit()
            finally:
                conn.close()

    def update_module(self, user_id: int, module: str) -> None:
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE users SET module = ?, updated_at = ? WHERE user_id = ?",
                    (module.strip(), now, user_id),
                )
                conn.commit()
            finally:
                conn.close()

    def update_user_profile(
        self,
        user_id: int,
        *,
        full_name: Optional[str] = None,
        department: Optional[str] = None,
        module: Optional[str] = None,
        default_anonymous: Optional[bool] = None,
    ) -> bool:
        """Частичное обновление профиля (None — поле не менять). False, если строки нет."""
        cols: list[str] = []
        args: list[Any] = []
        if full_name is not None:
            cols.append("full_name = ?")
            args.append(str(full_name).strip())
        if department is not None:
            cols.append("department = ?")
            args.append(str(department).strip())
        if module is not None:
            cols.append("module = ?")
            args.append(str(module).strip())
        if default_anonymous is not None:
            cols.append("default_anonymous = ?")
            args.append(1 if default_anonymous else 0)
        if not cols:
            return True
        now = _utc_now()
        cols.append("updated_at = ?")
        args.append(now)
        args.append(user_id)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"UPDATE users SET {', '.join(cols)} WHERE user_id = ?",
                    args,
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def add_submission(
        self,
        user_id: int,
        chat_id: Optional[int],
        kind: str,
        text: str,
        anonymous: bool,
    ) -> int:
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    INSERT INTO submissions (user_id, chat_id, kind, text, anonymous, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        chat_id,
                        kind,
                        text.strip(),
                        1 if anonymous else 0,
                        now,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def list_user_submissions(self, user_id: int, limit: int = 30) -> List[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    SELECT id, kind, text, anonymous, created_at, status
                    FROM submissions
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                )
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def list_submissions_dashboard(self, limit: int = 200) -> List[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    SELECT s.id, s.user_id, s.chat_id, s.kind, s.text, s.anonymous,
                           s.created_at, s.admin_note, s.status,
                           u.full_name, u.department, u.module,
                           COALESCE(
                               (SELECT MAX(sr.created_at)
                                FROM submission_replies sr
                                WHERE sr.submission_id = s.id),
                               s.created_at
                           ) AS thread_last_activity
                    FROM submissions s
                    LEFT JOIN users u ON u.user_id = s.user_id
                    ORDER BY s.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def list_users_registry(self, limit: int = 2000) -> List[dict[str, Any]]:
        """Все профили пользователей для монитора (новые первыми)."""
        safe_limit = max(1, min(int(limit), 5000))
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    SELECT user_id, chat_id, full_name, department, module,
                           default_anonymous, created_at, updated_at
                    FROM users
                    ORDER BY datetime(created_at) DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                )
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def get_submission(self, submission_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    SELECT s.id, s.user_id, s.chat_id, s.kind, s.text, s.anonymous,
                           s.created_at, s.admin_note, s.status,
                           u.full_name, u.department, u.module,
                           COALESCE(
                               (SELECT MAX(sr.created_at)
                                FROM submission_replies sr
                                WHERE sr.submission_id = s.id),
                               s.created_at
                           ) AS thread_last_activity
                    FROM submissions s
                    LEFT JOIN users u ON u.user_id = s.user_id
                    WHERE s.id = ?
                    """,
                    (submission_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def add_submission_reply(
        self, submission_id: int, body: str, *, from_user: bool = False
    ) -> int:
        now = _utc_now()
        text = body.strip()
        fu = 1 if from_user else 0
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    INSERT INTO submission_replies (submission_id, body, created_at, from_user)
                    VALUES (?, ?, ?, ?)
                    """,
                    (submission_id, text, now, fu),
                )
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def get_submission_thread(self, submission_id: int) -> List[dict[str, Any]]:
        """Переписка: текст заявки, затем сообщения из submission_replies (поддержка и пользователь)."""
        row = self.get_submission(submission_id)
        if not row:
            return []
        out: list[dict[str, Any]] = [
            {
                "role": "user",
                "text": row["text"],
                "created_at": row["created_at"],
            }
        ]
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    SELECT body, created_at, from_user
                    FROM submission_replies
                    WHERE submission_id = ?
                    ORDER BY id ASC
                    """,
                    (submission_id,),
                )
                for r in cur.fetchall():
                    is_user = bool(r["from_user"])
                    out.append(
                        {
                            "role": "user" if is_user else "admin",
                            "text": r["body"],
                            "created_at": r["created_at"],
                        }
                    )
                return out
            finally:
                conn.close()

    def update_submission_ticket(
        self,
        submission_id: int,
        *,
        admin_note: Optional[str] = None,
        status: Optional[str] = None,
    ) -> bool:
        parts: list[str] = []
        args: list[Any] = []
        if admin_note is not None:
            parts.append("admin_note = ?")
            args.append(admin_note.strip())
        if status is not None:
            if status not in ("open", "closed"):
                raise ValueError("invalid status")
            parts.append("status = ?")
            args.append(status)
        if not parts:
            return False
        args.append(submission_id)
        sql = f"UPDATE submissions SET {', '.join(parts)} WHERE id = ?"
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(sql, args)
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()
