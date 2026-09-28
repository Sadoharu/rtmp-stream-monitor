from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

LOG = logging.getLogger("rtmp_monitor.queue")


class LocalQueue:
    """Durable, bounded outbox for telemetry produced while central is offline."""

    def __init__(self, path: str | Path, max_bytes: int, max_rows: int):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes, self.max_rows = max_bytes, max_rows
        self._lock = Lock()
        self.dropped_rows = 0
        try:
            self._initialize()
        except sqlite3.DatabaseError as exc:
            if not _is_corruption(exc):
                raise
            self._recover_corruption(exc, "startup")

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

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, payload TEXT NOT NULL)")
            check = db.execute("PRAGMA quick_check").fetchall()
            if check != [("ok",)]:
                details = "; ".join(str(row[0]) for row in check)
                raise sqlite3.DatabaseError(f"database disk image is malformed: quick_check: {details}")

    def _quarantine_database(self) -> Path | None:
        archived = self.path.with_name(f"{self.path.name}.corrupt-{uuid.uuid4().hex}")
        found = False
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{self.path}{suffix}")
            if not source.exists():
                continue
            destination = Path(f"{archived}{suffix}")
            try:
                source.replace(destination)
            except OSError as exc:
                raise OSError(f"Cannot quarantine corrupt telemetry queue file {source}: {exc}") from exc
            found = True
        return archived if found else None

    def _recover_corruption(self, error: sqlite3.DatabaseError, operation: str) -> None:
        archived = self._quarantine_database()
        LOG.error(
            "Corrupt local telemetry outbox detected during %s (%s). Quarantined files at %s; "
            "the new outbox will start empty and telemetry stored only in the damaged database may be lost.",
            operation,
            error,
            archived or "no existing file",
        )
        self._initialize()

    def put(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            try:
                with self._connect() as db:
                    db.execute("INSERT INTO outbox(created,payload) VALUES(?,?)", (time.time(), encoded))
                    self._trim(db)
            except sqlite3.DatabaseError as exc:
                if not _is_corruption(exc):
                    raise
                self._recover_corruption(exc, "put")
                with self._connect() as db:
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
        with self._lock:
            try:
                with self._connect() as db:
                    rows = db.execute("SELECT id,payload FROM outbox ORDER BY id LIMIT ?", (limit,)).fetchall()
                    result = []
                    malformed_ids = []
                    for row_id, payload in rows:
                        try:
                            value = json.loads(payload)
                        except (json.JSONDecodeError, TypeError):
                            malformed_ids.append((row_id,))
                            continue
                        if not isinstance(value, dict):
                            malformed_ids.append((row_id,))
                            continue
                        result.append((row_id, value))
                    if malformed_ids:
                        db.executemany("DELETE FROM outbox WHERE id=?", malformed_ids)
                        self.dropped_rows += len(malformed_ids)
                        LOG.error("Removed %d malformed telemetry payload(s) from the local outbox", len(malformed_ids))
                    return result
            except sqlite3.DatabaseError as exc:
                if not _is_corruption(exc):
                    raise
                self._recover_corruption(exc, "peek")
                return []

    def ack(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            try:
                with self._connect() as db:
                    db.executemany("DELETE FROM outbox WHERE id=?", [(row_id,) for row_id in ids])
            except sqlite3.DatabaseError as exc:
                if not _is_corruption(exc):
                    raise
                self._recover_corruption(exc, "ack")

    @property
    def size(self) -> int:
        with self._lock:
            try:
                with self._connect() as db:
                    return int(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
            except sqlite3.DatabaseError as exc:
                if not _is_corruption(exc):
                    raise
                self._recover_corruption(exc, "size")
                return 0


def _is_corruption(error: sqlite3.DatabaseError) -> bool:
    error_code = getattr(error, "sqlite_errorcode", None)
    corrupt_codes = {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
    if error_code is not None:
        return (error_code & 0xFF) in corrupt_codes
    message = str(error).lower()
    return "database disk image is malformed" in message or "file is not a database" in message
