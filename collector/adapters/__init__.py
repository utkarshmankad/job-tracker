"""Adapter registry. Only sources with an adapter here (or an explicitly configured
employer history-table definition) can be collected; anything else is unsupported."""

from __future__ import annotations

from collector.adapters.base import Adapter

ADAPTERS: dict[str, type[Adapter]] = {}


class UnsupportedSource(LookupError):
    pass


def adapter_for(source_key: str, employer: dict[str, object] | None = None) -> Adapter:
    if source_key in ADAPTERS:
        return ADAPTERS[source_key]()
    raise UnsupportedSource(source_key)
