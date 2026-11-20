"""Persist scheduler settings in the application volume."""

import json
import logging
from threading import Lock

from .mcp import save_json
from .sync import TZ


DEFAULT_MINUTES = 30
LOG = logging.getLogger(__name__)


class Config:
    def __init__(self, directory):
        self.path = directory / "config.json"
        self.lock = Lock()

    def read(self):
        try:
            data = json.loads(self.path.read_bytes())
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except FileNotFoundError:
            return {}
        except (ValueError, OSError):
            LOG.warning("config.json is invalid; using the default sync interval")
            return {}

    def load(self):
        minutes = self.read().get("sync_interval_minutes")
        return minutes if type(minutes) is int and 1 <= minutes <= 1440 else DEFAULT_MINUTES

    def save(self, minutes):
        self.update({"sync_interval_minutes": minutes})

    def retention_days(self):
        days = self.read().get("log_retention_days")
        return days if type(days) is int and 7 <= days <= 365 else 90

    def update(self, values):
        with self.lock:
            save_json(self.path, {**self.read(), **values, "timezone": TZ.key})
