"""Persist scheduler settings in the application volume."""

import json
import logging

from .mcp import save_json
from .sync import TZ


DEFAULT_MINUTES = 30
LOG = logging.getLogger(__name__)


class Config:
    def __init__(self, directory):
        self.path = directory / "config.json"

    def load(self):
        try:
            minutes = json.loads(self.path.read_bytes())["sync_interval_minutes"]
            if type(minutes) is not int or not 1 <= minutes <= 1440:
                raise ValueError()
            return minutes
        except FileNotFoundError:
            return DEFAULT_MINUTES
        except (ValueError, KeyError, TypeError, OSError):
            LOG.warning("config.json is invalid; using the default sync interval")
            return DEFAULT_MINUTES

    def save(self, minutes):
        save_json(self.path, {"sync_interval_minutes": minutes, "timezone": TZ.key})
