"""Disposable CI trial for initializing a bind mount before starting Uvicorn."""
import os
import sys

from suunto.container_start import APP_GID, APP_UID, prepare_data

if os.geteuid() != 0:
    sys.exit("The isolated test entrypoint must start as root.")

prepare_data()
os.umask(0o077)
os.setgroups([])
os.setgid(APP_GID)
os.setuid(APP_UID)
os.execvp(sys.argv[1], sys.argv[1:])
