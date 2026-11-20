"""Optional local password gate. A trusted HTTPS proxy supplies the TLS scheme."""

import base64
from hashlib import scrypt
from hmac import compare_digest
import json
from pathlib import Path
from secrets import token_bytes

from .mcp import save_json


USERNAME = "suunto"


def derive(password, salt):
    return scrypt(password.encode("utf-8"), salt=salt, n=32768, r=8, p=1, dklen=32, maxmem=64*1024**2)


class Access:
    def __init__(self, directory):
        self.path = Path(directory) / "access.json"

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
            return len(salt) == 16 and len(digest) == 32 and compare_digest(digest, derive(password, salt))
        except (KeyError, TypeError, ValueError, OSError):
            return False
