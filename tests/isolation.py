"""Test-process isolation, installed by tests/conftest.py before any backend module loads.

backend/config.py reads the environment at import time, so this module must run first. It:

1. Points JOB_TRACKER_DIR at a fresh temporary directory, so no test can open or create the
   real database (`<repo>/.job-tracker/applications.db` or a JOB_TRACKER_DIR from the shell).
2. Disables every external integration: POLLER_ENABLED=false (no Gmail credentials, keychain,
   Google client, poller thread or sleep watcher), CACHE_ENABLED=false (no Redis),
   LLM_ENABLED=false (no Ollama/Groq), and removes Gmail/Groq/auth secrets from the
   environment. The repository .env file is not loaded.
3. Replaces the keyring backend with one that records the access and raises, so an
   un-mocked keychain read can never reach the macOS keychain.
4. Guards sockets: connections and DNS lookups for anything but loopback/UNIX sockets are
   recorded and refused. conftest fails any test that triggered one unless the test is
   marked ``@pytest.mark.allow_network`` (no test currently is).

Tests that cover Gmail, keyring or HTTP behaviour mock those boundaries explicitly.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import tempfile
from pathlib import Path

TEST_JOB_TRACKER_DIR = Path(tempfile.mkdtemp(prefix="job-tracker-tests-"))

_FORCED_ENV = {
    "JOB_TRACKER_DIR": str(TEST_JOB_TRACKER_DIR),
    "APP_ENV": "test",
    "POLLER_ENABLED": "false",
    "CACHE_ENABLED": "false",
    "LLM_ENABLED": "false",
    "DB_AUTO_MIGRATE": "true",
    "REDIS_URL": "redis://127.0.0.1:1/0",  # unreachable loopback even if caching is enabled
}
_REMOVED_ENV = (
    "GMAIL_TOKEN_JSON",
    "GROQ_API_KEY",
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "AUTH_MODE",
    "AUTH_ALLOWED_EMAIL",
    "AUTH_GOOGLE_CLIENT_ID",
    "AUTH_SESSION_SECRET",
    "AUTH_SESSION_TTL_SECONDS",
    "FRONTEND_ORIGIN",
    "PUBLIC_BASE_URL",
    "API_HOST",
    "API_PORT",
)

# Recorded violations, inspected (and cleared) around every test by conftest.
network_attempts: list[str] = []
keyring_attempts: list[str] = []


class UnexpectedNetworkAccess(OSError):
    """Raised for a non-loopback connection or DNS lookup during tests."""


def _is_loopback_host(host: object) -> bool:
    if not isinstance(host, str):
        return False
    name = host.strip("[]").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _guard_address(address: object) -> None:
    if isinstance(address, str | bytes):  # AF_UNIX path
        return
    host = address[0] if isinstance(address, tuple) and address else address
    if not _is_loopback_host(host):
        network_attempts.append(f"connect {address!r}")
        raise UnexpectedNetworkAccess(f"test attempted network access to {address!r}")


def _install_socket_guard() -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: object) -> None:
        if self.family != getattr(socket, "AF_UNIX", object()):
            _guard_address(address)
        return real_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:
        if self.family != getattr(socket, "AF_UNIX", object()):
            _guard_address(address)
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    def getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host is not None and not _is_loopback_host(
            host.decode() if isinstance(host, bytes) else host
        ):
            network_attempts.append(f"resolve {host!r}")
            raise UnexpectedNetworkAccess(f"test attempted DNS lookup of {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo


def _install_keyring_guard() -> None:
    import keyring
    from keyring.backend import KeyringBackend
    from keyring.errors import NoKeyringError

    class ForbiddenKeyring(KeyringBackend):
        priority = 1  # type: ignore[assignment]

        def get_password(self, service: str, username: str) -> str | None:
            keyring_attempts.append(f"get {service}")
            raise NoKeyringError("keychain access is forbidden in tests; mock keyring instead")

        def set_password(self, service: str, username: str, password: str) -> None:
            keyring_attempts.append(f"set {service}")
            raise NoKeyringError("keychain access is forbidden in tests; mock keyring instead")

        def delete_password(self, service: str, username: str) -> None:
            keyring_attempts.append(f"delete {service}")
            raise NoKeyringError("keychain access is forbidden in tests; mock keyring instead")

    keyring.set_keyring(ForbiddenKeyring())


def install() -> None:
    os.environ.update(_FORCED_ENV)
    for name in _REMOVED_ENV:
        os.environ.pop(name, None)

    # backend/config.py calls load_dotenv() at import; a developer's .env must not leak in.
    import dotenv

    dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[assignment]

    _install_keyring_guard()
    _install_socket_guard()
