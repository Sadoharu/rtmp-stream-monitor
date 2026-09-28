import sqlite3
import time

import pytest

from rtmp_monitor.queue import LocalQueue


def test_queue_is_durable_and_bounded(tmp_path):
    queue = LocalQueue(tmp_path / "queue.db", max_bytes=1024 * 1024, max_rows=2)
    queue.put({"n": 1})
    queue.put({"n": 2})
    queue.put({"n": 3})
    assert queue.size == 2
    assert queue.dropped_rows == 1
    batch = queue.peek(10)
    assert [row[1]["n"] for row in batch] == [2, 3]
    queue.ack([batch[0][0]])
    assert queue.size == 1


def test_queue_byte_limit_counts_utf8_bytes(tmp_path):
    queue = LocalQueue(tmp_path / "queue.db", max_bytes=20, max_rows=10)
    queue.put({"x": "é" * 10})
    assert queue.size == 0
    assert queue.dropped_rows == 1


def test_queue_closes_database_file_after_each_operation(tmp_path):
    queue = LocalQueue(tmp_path / "queue.db", max_bytes=1024 * 1024, max_rows=100)
    queue.put({"message": "hello"})
    batch = queue.peek()
    assert batch[0][1] == {"message": "hello"}
    queue.ack([batch[0][0]])
    assert queue.size == 0

    queue.path.rename(tmp_path / "queue-renamed.db")


def test_queue_quarantines_corrupt_database_on_startup(tmp_path, caplog):
    path = tmp_path / "queue.db"
    path.write_bytes(b"not a sqlite database")

    queue = LocalQueue(path, max_bytes=1024 * 1024, max_rows=100)

    assert queue.size == 0
    quarantined = [item for item in tmp_path.glob("queue.db.corrupt-*") if item.read_bytes() == b"not a sqlite database"]
    assert len(quarantined) == 1
    assert "during startup" in caplog.text


def test_queue_recovers_from_corruption_during_runtime_and_keeps_new_item(tmp_path):
    path = tmp_path / "queue.db"
    queue = LocalQueue(path, max_bytes=1024 * 1024, max_rows=100)
    queue.put({"n": 1})
    path.write_bytes(b"database disk image is malformed")

    queue.put({"n": 2})

    assert [payload for _, payload in queue.peek()] == [{"n": 2}]
    assert any(item.read_bytes() == b"database disk image is malformed" for item in tmp_path.glob("queue.db.corrupt-*"))


def test_queue_discards_only_malformed_json_rows(tmp_path):
    path = tmp_path / "queue.db"
    queue = LocalQueue(path, max_bytes=1024 * 1024, max_rows=100)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO outbox(created,payload) VALUES(?,?)", (time.time(), "not-json"))
        db.execute("INSERT INTO outbox(created,payload) VALUES(?,?)", (time.time(), '["not", "an object"]'))
        db.execute("INSERT INTO outbox(created,payload) VALUES(?,?)", (time.time(), '{"valid":true}'))

    assert [payload for _, payload in queue.peek()] == [{"valid": True}]
    assert queue.size == 1
    assert queue.dropped_rows == 2


def test_queue_does_not_quarantine_transient_database_errors(tmp_path, monkeypatch):
    def fail_initialization(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(LocalQueue, "_initialize", fail_initialization)
    path = tmp_path / "queue.db"

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        LocalQueue(path, max_bytes=1024 * 1024, max_rows=100)

    assert list(tmp_path.glob("queue.db.corrupt-*")) == []
