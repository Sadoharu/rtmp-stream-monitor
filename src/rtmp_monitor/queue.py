from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Iterator


class LocalQueue:
    """Durable, bounded outbox for telemetry produced while central is offline."""

    def __init__(self, path: str | Path, max_bytes: int, max_rows: int):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes, self.max_rows = max_bytes, max_rows
        self._lock = Lock()
        self.dropped_rows = 0
        self._connect_and_initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        try:
            yield db
            db.commit()
        except BaseException:
            try:
                db.rollback()
            except sqlite3.DatabaseError:
                pass
            raise
        finally:
            db.close()

    def _connect_and_initialize(self) -> None:
        try:
            with self._connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
                db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, payload TEXT NOT NULL)")
        except sqlite3.DatabaseError:
            corrupt = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            try:
                self.path.replace(corrupt)
            except OSError:
                pass
            for suffix in ("-wal", "-shm"):
                try:
                    self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)
                except OSError:
                    pass
            with self._connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, payload TEXT NOT NULL)")

    def put(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO outbox(created,payload) VALUES(?,?)", (time.time(), encoded))
            self._trim(db)

    def _trim(self, db: sqlite3.Connection) -> None:
        while True:
            rows, total = db.execute("SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM outbox").fetchone()
            if rows <= self.max_rows and total <= self.max_bytes:
                break
            oldest = db.execute("SELECT id FROM outbox ORDER BY id LIMIT 1").fetchone()
            if not oldest:
                break
            db.execute("DELETE FROM outbox WHERE id=?", (oldest[0],))
            self.dropped_rows += 1

    def peek(self, limit: int = 100) -> list[tuple[int, dict[str, Any]]]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT id,payload FROM outbox ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [(row_id, json.loads(payload)) for row_id, payload in rows]

    def ack(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock, self._connect() as db:
            db.executemany("DELETE FROM outbox WHERE id=?", [(row_id,) for row_id in ids])

    @property
    def size(self) -> int:
        with self._lock, self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
