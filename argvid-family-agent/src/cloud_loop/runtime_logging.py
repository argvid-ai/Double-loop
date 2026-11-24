"""Bounded application logs owned by each node."""

from copy import deepcopy
from pathlib import Path

from uvicorn.config import LOGGING_CONFIG


def runtime_log_config(root, side, settings):
    folder = Path(root) / "processes"
    folder.mkdir(exist_ok=True)
    config = deepcopy(LOGGING_CONFIG)
    config["handlers"]["default"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "formatter": "default",
        "filename": str(folder / (side + "-runtime.log")),
        "maxBytes": settings.runtime_log_bytes,
        "backupCount": settings.runtime_log_backups,
        "encoding": "utf-8",
    }
    config["formatters"]["default"]["use_colors"] = False
    return config
