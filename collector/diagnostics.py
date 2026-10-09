"""Local diagnostics.

Two kinds, both under the collector's private state directory (mode 0700, files 0600):

- ``runs.jsonl`` — one line per run with aggregate counts and fixed codes only. Safe to
  read and share; contains no page content, names, URLs or identifiers.
- Page snapshots — OFF by default. When ``[diagnostics] enabled = true``, the HTML of a
  page the adapter could not understand (selector drift, unknown state) is saved
  *encrypted* (Fernet, key in the keychain) and deleted after ``ttl_hours`` (max 72). Used
  only to repair an adapter; ``clear-diagnostics`` removes everything immediately.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import keyring
from cryptography.fernet import Fernet

_KEY_SERVICE = "job-tracker-collector-diagnostics"
_KEY_ACCOUNT = "snapshot-key"


def _write_private(path: Path, data: bytes, append: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    path.chmod(0o600)


class DiagnosticStore:
    def __init__(self, root: Path, *, snapshots_enabled: bool, ttl_hours: int) -> None:
        self._root = root
        self._snapshots = root / "snapshots"
        self._enabled = snapshots_enabled
        self._ttl = ttl_hours * 3600
        root.mkdir(mode=0o700, parents=True, exist_ok=True)

    # -- aggregate run log ---------------------------------------------------------

    def record_run(self, summary: dict[str, Any]) -> None:
        line = json.dumps(summary, sort_keys=True, default=str) + "\n"
        _write_private(self._root / "runs.jsonl", line.encode(), append=True)

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        path = self._root / "runs.jsonl"
        if not path.is_file():
            return []
        lines = path.read_text().splitlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]

    # -- encrypted snapshots -------------------------------------------------------

    def _fernet(self) -> Fernet:
        key = keyring.get_password(_KEY_SERVICE, _KEY_ACCOUNT)
        if not key:
            key = Fernet.generate_key().decode()
            keyring.set_password(_KEY_SERVICE, _KEY_ACCOUNT, key)
        return Fernet(key.encode())

    def save_snapshot(self, source_key: str, reason: str, html: str) -> Path | None:
        if not self._enabled:
            return None
        self.purge_expired()
        self._snapshots.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._snapshots / f"{int(time.time())}-{source_key}-{reason}.bin"
        _write_private(path, self._fernet().encrypt(html.encode("utf-8")))
        return path

    def read_snapshot(self, path: Path) -> str:
        return self._fernet().decrypt(path.read_bytes()).decode("utf-8")

    def purge_expired(self, now: float | None = None) -> int:
        if not self._snapshots.is_dir():
            return 0
        cutoff = (now or time.time()) - self._ttl
        removed = 0
        for path in self._snapshots.glob("*.bin"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        return removed

    def clear_all(self) -> int:
        removed = 0
        if self._snapshots.is_dir():
            for path in self._snapshots.glob("*.bin"):
                path.unlink()
                removed += 1
        runs = self._root / "runs.jsonl"
        if runs.exists():
            runs.unlink()
            removed += 1
        pending = self._root / "pending"
        if pending.is_dir():
            for path in pending.glob("*.json"):
                path.unlink()
                removed += 1
        try:
            keyring.delete_password(_KEY_SERVICE, _KEY_ACCOUNT)
        except keyring.errors.PasswordDeleteError:
            pass
        return removed
