"""The collector credential, stored in the operating-system keychain (macOS Keychain via
``keyring``). It is never written to the config file, logs or diagnostics, and is only read
when a request to the tracker needs it."""

from __future__ import annotations

from urllib.parse import urlsplit

import keyring
from keyring.errors import KeyringError

SERVICE = "job-tracker-collector"


class CredentialMissing(RuntimeError):
    pass


def _account(api_url: str) -> str:
    return urlsplit(api_url).netloc or api_url


def store_credential(api_url: str, credential: str) -> None:
    keyring.set_password(SERVICE, _account(api_url), credential)


def load_credential(api_url: str) -> str:
    try:
        value = keyring.get_password(SERVICE, _account(api_url))
    except KeyringError as exc:
        raise CredentialMissing("The keychain is not available.") from exc
    if not value:
        raise CredentialMissing("No collector credential for this tracker; run `enroll` first.")
    return value


def delete_credential(api_url: str) -> bool:
    try:
        keyring.delete_password(SERVICE, _account(api_url))
    except KeyringError:
        return False
    return True
