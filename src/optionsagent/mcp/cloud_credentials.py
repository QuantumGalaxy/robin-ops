"""Encrypted rotating OAuth storage for a systemd-managed Linux service."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path


def private_read(path, limit=65536, *, systemd_key=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        # systemd 255 exposes LoadCredential files as root:root 0440 with
        # per-service access; ordinary token files must remain owner-only.
        allowed_group_read = systemd_key and info.st_uid == 0 and info.st_gid == 0
        forbidden = 0o037 if allowed_group_read else 0o077
        if not stat.S_ISREG(info.st_mode) or info.st_mode & forbidden:
            raise ValueError('Credential files must be private regular files')
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('Credential file exceeds size limit')
        return data


class EncryptedStore:
    def __init__(self, path):
        from cryptography.fernet import Fernet

        directory = os.environ.get('CREDENTIALS_DIRECTORY')
        if not directory:
            raise ValueError('Cloud credentials require systemd LoadCredential')
        key = private_read(Path(directory) / 'oauth-key', 128, systemd_key=True).strip()
        self.cipher = Fernet(key)
        self.path = Path(path)
        folder = self.path.parent
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Cloud credential directory must be private and owned by service user')

    def read(self):
        from .oauth import RESOURCE

        try:
            encrypted = private_read(self.path)
        except FileNotFoundError:
            return {}
        value = json.loads(self.cipher.decrypt(encrypted))
        if not isinstance(value, dict) or value.get('resource') != RESOURCE:
            raise ValueError('Invalid encrypted OAuth record')
        return value

    def save(self, value):
        from .oauth import RESOURCE

        if not isinstance(value, dict) or value.get('resource') != RESOURCE:
            raise ValueError('Invalid OAuth resource')
        encrypted = self.cipher.encrypt(json.dumps(value, allow_nan=False).encode())
        fd, tmp = tempfile.mkstemp(prefix='.oauth-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def clear(self):
        self.path.unlink(missing_ok=True)
