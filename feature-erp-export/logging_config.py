"""JSON logs to stdout, with a safety net that masks anything shaped like an API key."""
import json
import logging
import os
import re
import sys

_KEY_RE = re.compile(r"pme_[A-Za-z0-9_\-]{16,}")
_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _KEY_RE.sub("pme_[REDACTED]", record.getMessage())
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update({k: v for k, v in record.__dict__.items() if k not in _STD})
        if record.exc_info:
            out["exc"] = _KEY_RE.sub("pme_[REDACTED]", self.formatException(record.exc_info))
        return json.dumps(out, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
