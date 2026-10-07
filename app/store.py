"""SQLite-backed idempotency store.

The binding key is (idempotency_key, request_fingerprint). The first request
for an idempotency key pins its fingerprint; a later request reusing the same
key with a different normalized plan/snapshot is rejected with
IDEMPOTENCY_KEY_CONFLICT. Identical repeats return the stored bytes verbatim,
guaranteeing byte-identical JSON.
"""
from __future__ import annotations

import sqlite3
import threading


class IdempotencyStore:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS responses (
                idempotency_key TEXT PRIMARY KEY,
                fingerprint     TEXT NOT NULL,
                status_code     INTEGER NOT NULL,
                body            BLOB NOT NULL,
                created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        self._conn.commit()

    def get(self, key: str):
        with self._lock:
            cur = self._conn.execute(
                "SELECT fingerprint, status_code, body FROM responses WHERE idempotency_key = ?",
                (key,),
            )
            return cur.fetchone()

    def put(self, key: str, fp: str, status_code: int, body: bytes) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO responses (idempotency_key, fingerprint, status_code, body) "
                    "VALUES (?, ?, ?, ?)",
                    (key, fp, status_code, sqlite3.Binary(body)),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                # Concurrent first insert; the stored response now wins.
                self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
