"""Optional local password gate. A trusted HTTPS proxy supplies the TLS scheme."""

import base64
from hashlib import scrypt
from hmac import compare_digest
import json
from pathlib import Path
from secrets import token_bytes
from threading import BoundedSemaphore

from .mcp import save_json


USERNAME = "suunto"


class AccessBusy(RuntimeError):
    pass


def derive(password, salt):
    return scrypt(password.encode("utf-8"), salt=salt, n=32768, r=8, p=1, dklen=32, maxmem=64*1024**2)


class Access:
    def __init__(self, directory, *, allow_http=False):
        self.path = Path(directory) / "access.json"
        self.checks = BoundedSemaphore(2)
        self.allow_http = allow_http

    def enabled(self):
        return self.path.exists()

    def set_password(self, password):
        if not 12 <= len(password) <= 128:
            raise ValueError("看板密码须为 12–128 个字符。")
        salt = token_bytes(16)
        save_json(self.path, {"salt": salt.hex(), "digest": derive(password, salt).hex()})

    def clear(self):
        self.path.unlink(missing_ok=True)

    def accepts(self, authorization):
        kind, _, encoded = authorization.partition(" ")
        if kind.lower() != "basic" or len(encoded) > 1024:
            return False
        try:
            user, delimiter, password = base64.b64decode(encoded, validate=True).decode("utf-8").partition(":")
            if user != USERNAME or delimiter != ":" or len(password) > 128:
                return False
            data = json.loads(self.path.read_bytes())
            salt, digest = bytes.fromhex(data["salt"]), bytes.fromhex(data["digest"])
            if len(salt) != 16 or len(digest) != 32:
                return False
            if not self.checks.acquire(blocking=False):
                raise AccessBusy
            try:
                return compare_digest(digest, derive(password, salt))
            finally:
                self.checks.release()
        except (KeyError, TypeError, ValueError, OSError):
            return False
