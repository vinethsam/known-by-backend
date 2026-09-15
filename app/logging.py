"""Allowlisted JSON logging: avoid secret-bearing payloads and exception text."""

import json
import logging
from datetime import datetime, timezone

CONTEXT_FIELDS = (
    "job_id",
    "person_id",
    "source_id",
    "domain",
    "retrieval_method",
    "pipeline_stage",
    "duration_ms",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "cost",
    "error_code",
    "prompt_version",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        data.update({key: getattr(record, key) for key in CONTEXT_FIELDS if hasattr(record, key)})
        # Exception strings can contain provider URLs/credentials. Log only error codes/type.
        if record.exc_info:
            data["exception_type"] = record.exc_info[0].__name__
        return json.dumps(data, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
