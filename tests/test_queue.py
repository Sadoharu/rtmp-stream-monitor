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
