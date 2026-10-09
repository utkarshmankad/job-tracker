"""HTTP client for the tracker's collector API.

Sends only the scoped bearer credential (from the keychain) — never browser cookies. Every
write is idempotent on the server (run keys, batch keys), so a network failure is retried
with the same key; the server returns the stored result instead of processing twice.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from collector import COLLECTOR_VERSION


class ClientError(RuntimeError):
    """A tracker request failed. `code` is a fixed string; never the response body."""

    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code if status is None else f"{code} (HTTP {status})")
        self.code = code
        self.status = status


class TrackerClient:
    def __init__(
        self,
        api_url: str,
        credential: str | None,
        *,
        transport: httpx.BaseTransport | None = None,
        retries: int = 3,
        backoff_seconds: float = 2.0,
        sleep=time.sleep,
    ) -> None:
        headers = {"User-Agent": f"job-tracker-collector/{COLLECTOR_VERSION}"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        self._client = httpx.Client(
            base_url=f"{api_url.rstrip('/')}/api/v1",
            headers=headers,
            timeout=httpx.Timeout(30.0),
            transport=transport,
            follow_redirects=False,
        )
        self._retries = retries
        self._backoff = backoff_seconds
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(self._retries + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self._retries:
                    raise ClientError("network_error") from exc
                self._sleep(self._backoff * (2**attempt))
                continue
            if response.status_code in (502, 503, 504) and attempt < self._retries:
                self._sleep(self._backoff * (2**attempt))
                continue
            if response.status_code == 429:
                raise ClientError("rate_limited", 429)
            if response.status_code == 401:
                raise ClientError("unauthorized", 401)
            if response.status_code == 403:
                raise ClientError("forbidden_scope", 403)
            if response.status_code == 409:
                raise ClientError("conflict", 409)
            if response.status_code == 422:
                raise ClientError("rejected_payload", 422)
            if response.status_code >= 400:
                raise ClientError("server_error", response.status_code)
            return dict(response.json())
        raise ClientError("network_error")

    # -- endpoints -----------------------------------------------------------------

    def enroll(self, code: str) -> dict[str, Any]:
        return self._request("POST", "/collector/enroll", json={"code": code})

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/collector/me")

    def start_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/collector/runs", json=payload)

    def submit_batch(self, run_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/collector/runs/{run_key}/observations", json=payload)

    def finish_run(self, run_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/collector/runs/{run_key}/finish", json=payload)

    def metrics(self) -> dict[str, Any]:
        return self._request("GET", "/collector/metrics")
