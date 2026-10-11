from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .schemas import Visit


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def session_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.lock = threading.RLock()
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, created_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS visits (id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id), payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS visits_owner ON visits(owner)")
            conn.execute("CREATE TABLE IF NOT EXISTS bookmarks (owner TEXT NOT NULL REFERENCES sessions(id), source_id TEXT NOT NULL, PRIMARY KEY(owner,source_id))")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def new_session(self) -> tuple[str, str, str]:
        cookie = secrets.token_urlsafe(32)
        owner = session_digest(cookie)
        csrf = secrets.token_urlsafe(32)
        with self.lock, self.connect() as conn:
            conn.execute("INSERT INTO sessions VALUES (?, ?, ?)", (owner, csrf, now()))
        return cookie, owner, csrf

    def session(self, cookie: str | None) -> tuple[str, str] | None:
        if not cookie or len(cookie) > 200:
            return None
        owner = session_digest(cookie)
        with self.connect() as conn:
            row = conn.execute("SELECT csrf FROM sessions WHERE id=?", (owner,)).fetchone()
        return (owner, row[0]) if row else None

    def list_visits(self, owner: str) -> list[Visit]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM visits WHERE owner=? ORDER BY updated_at DESC, id DESC", (owner,)).fetchall()
        return [Visit.model_validate_json(row[0]) for row in rows]

    def get(self, owner: str, visit_id: str) -> Visit | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM visits WHERE id=? AND owner=?", (visit_id, owner)).fetchone()
        return Visit.model_validate_json(row[0]) if row else None

    def put(self, owner: str, visit: Visit) -> None:
        with self.lock, self.connect() as conn:
            conn.execute("INSERT INTO visits(id,owner,payload,updated_at) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at WHERE visits.owner=excluded.owner", (visit.id, owner, visit.model_dump_json(), visit.updated_at))

    def delete(self, owner: str, visit_id: str) -> bool:
        with self.lock, self.connect() as conn:
            return bool(conn.execute("DELETE FROM visits WHERE id=? AND owner=?", (visit_id, owner)).rowcount)

    def clear(self, owner: str) -> None:
        with self.lock, self.connect() as conn:
            conn.execute("DELETE FROM visits WHERE owner=?", (owner,))
            conn.execute("DELETE FROM bookmarks WHERE owner=?", (owner,))

    def bookmarks(self, owner: str) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT source_id FROM bookmarks WHERE owner=? ORDER BY source_id", (owner,)).fetchall()
        return [row[0] for row in rows]

    def bookmark(self, owner: str, source_id: str, saved: bool) -> list[str]:
        with self.lock, self.connect() as conn:
            if saved:
                conn.execute("INSERT OR IGNORE INTO bookmarks(owner,source_id) VALUES(?,?)", (owner,source_id))
            else:
                conn.execute("DELETE FROM bookmarks WHERE owner=? AND source_id=?", (owner,source_id))
        return self.bookmarks(owner)

    def expire(self, cutoff: str) -> None:
        """Remove inactive records and expired visitor sessions from the live database."""
        with self.lock, self.connect() as conn:
            conn.execute("DELETE FROM visits WHERE updated_at < ? OR owner IN (SELECT id FROM sessions WHERE created_at < ?)", (cutoff, cutoff))
            conn.execute("DELETE FROM bookmarks WHERE owner IN (SELECT id FROM sessions WHERE created_at < ?)", (cutoff,))
            conn.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))

    def close(self) -> None:
        # Connections are short-lived, including read connections.
        pass
