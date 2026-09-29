import gzip
import logging
import time

from rtmp_monitor.logging_setup import GzipDailyHandler, _gzip_rotator


def test_daily_logs_are_gzipped_and_old_archives_respect_retention(tmp_path):
    path = tmp_path / "rtmp-monitor.jsonl"
    handler = GzipDailyHandler(path, when="midnight", backupCount=2, encoding="utf-8", utc=True)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.rotator = _gzip_rotator
    epoch = int(time.time())

    try:
        for index in range(3):
            record = logging.LogRecord("test", logging.INFO, "", 0, f"sample-{index}", (), None)
            handler.emit(record)
            handler.rolloverAt = epoch + 86_400 * (index + 1)
            handler.doRollover()
    finally:
        handler.close()

    archives = sorted(tmp_path.glob("rtmp-monitor.jsonl.*.gz"))
    assert len(archives) == 2
    contents = []
    for archive in archives:
        with gzip.open(archive, "rt", encoding="utf-8") as log_file:
            contents.append(log_file.read().strip())
    assert contents == ["sample-1", "sample-2"]
