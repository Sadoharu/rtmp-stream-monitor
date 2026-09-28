from __future__ import annotations

import gzip
import json
import logging
import shutil
import glob
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in ("agent", "stream", "event", "details"):
            if hasattr(record, name):
                payload[name] = getattr(record, name)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _gzip_rotator(source: str, destination: str) -> None:
    with open(source, "rb") as source_file, gzip.open(destination, "wb") as destination_file:
        shutil.copyfileobj(source_file, destination_file)
    Path(source).unlink()


class GzipDailyHandler(TimedRotatingFileHandler):
    def rotation_filename(self, default_name: str) -> str:
        return default_name + ".gz"

    def getFilesToDelete(self) -> list[str]:
        candidates = sorted(glob.glob(str(self.baseFilename) + ".*.gz"))
        if self.backupCount <= 0:
            return candidates
        return candidates[:-self.backupCount]


def configure_logging(directory: str | Path, retention_days: int = 30, agent_name: str | None = None) -> None:
    log_dir = Path(directory)
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    app_handler = GzipDailyHandler(log_dir / "rtmp-monitor.jsonl", when="midnight", backupCount=retention_days, encoding="utf-8", utc=True)
    app_handler.setFormatter(formatter)
    app_handler.rotator = _gzip_rotator
    root.addHandler(app_handler)
    if agent_name:
        ffmpeg_logger = logging.getLogger("rtmp_monitor.ffmpeg")
        ffmpeg_logger.setLevel(logging.INFO)
        ffmpeg_logger.propagate = False
        ffmpeg_handler = RotatingFileHandler(log_dir / "ffmpeg-stderr.jsonl", maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8")
        ffmpeg_handler.setFormatter(formatter)
        ffmpeg_logger.addHandler(ffmpeg_handler)
