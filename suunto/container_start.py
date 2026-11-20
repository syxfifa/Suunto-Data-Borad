"""Initialize the mounted data directory for the non-root dashboard."""

import os
import stat
import sys


APP_UID = APP_GID = 10001
DATA_FILES = (
    ".maintenance.lock", "suunto.sqlite", "suunto.sqlite-wal", "suunto.sqlite-shm",
    "suunto.sqlite-journal", "config.json", "token.json", "access.json",
)


def prepare_data():
    directory = os.open("/data", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in DATA_FILES:
            try:
                handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                continue
            try:
                entry = os.fstat(handle)
                if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                    raise OSError(f"/data/{name} must be a regular file with one link")
                os.fchown(handle, APP_UID, APP_GID)
                os.fchmod(handle, 0o600)
            finally:
                os.close(handle)
        os.fchown(directory, APP_UID, APP_GID)
        os.fchmod(directory, 0o700)
    finally:
        os.close(directory)


def main():
    try:
        prepare_data()
    except OSError as exc:
        sys.exit(f"Cannot initialize /data for UID {APP_UID}: {exc}. "
                 "Use a writable local NAS folder that supports Linux ownership.")


if __name__ == "__main__":
    main()
