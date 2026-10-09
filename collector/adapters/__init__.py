"""Adapter registry. Only sources with an adapter here, or an employer portal with an
explicit [sources.employer] definition, can be collected; anything else is unsupported."""

from __future__ import annotations

from collector.adapters.base import Adapter
from collector.adapters.employer import EmployerDefinitionError, build_employer_adapter
from collector.adapters.sites import SITE_ADAPTERS

ADAPTERS: dict[str, type[Adapter]] = {cls.SOURCE_KEY: cls for cls in SITE_ADAPTERS}


class UnsupportedSource(LookupError):
    pass


def adapter_for(source_key: str, employer: dict[str, object] | None = None) -> Adapter:
    if source_key in ADAPTERS:
        return ADAPTERS[source_key]()
    if source_key.startswith("employer-") and employer:
        try:
            return build_employer_adapter(source_key, dict(employer))
        except EmployerDefinitionError as exc:
            raise UnsupportedSource(f"{source_key}: {exc}") from exc
    raise UnsupportedSource(source_key)
