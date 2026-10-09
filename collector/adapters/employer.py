"""Generic adapter for an employer career portal's application-history table.

There is no universal employer-portal scraper. A portal is collected only when the user
writes an explicit definition for it in the config — the history page URL, the hosts it
may run on, the list container, the row selector and the field selectors — usually after
inspecting the page once. Without a definition the portal is unsupported. The definition
is validated strictly; a page that does not match it stops with ``selector_drift``.

Example (config.toml)::

    [[sources]]
    source_key = "employer-acme"
    [sources.employer]
    history_url = "https://careers.acme.example/candidate/applications"
    allowed_hosts = ["careers.acme.example"]
    list_container = "table#applications"
    row = "table#applications tbody tr"
    company = "td.company"           # or a fixed company name via company_name
    role = "td.title"
    status = "td.status"
    applied = "td.date"
    link = "td.title a"
    signed_out = ["form#login"]
    empty = [".no-applications"]
    next_selector = "a.next"
    status_map = { "submitted" = "applied", "not selected" = "rejected" }
    verified_on = ""                 # set to a date once you have checked it live
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from bs4 import Tag

from backend.collection.contract import CollectorStatus
from collector.adapters.base import Adapter, SelectorSet, StateMarkers
from collector.core import ExtractedItem

_HOST = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")
_ALLOWED_KEYS = {
    "history_url",
    "allowed_hosts",
    "list_container",
    "row",
    "company",
    "company_name",
    "role",
    "status",
    "applied",
    "link",
    "item_id_attr",
    "signed_out",
    "signed_out_urls",
    "empty",
    "next_selector",
    "status_map",
    "verified_on",
}


class EmployerDefinitionError(ValueError):
    pass


def build_employer_adapter(source_key: str, definition: dict[str, Any]) -> Adapter:
    unknown = set(definition) - _ALLOWED_KEYS
    if unknown:
        raise EmployerDefinitionError(f"Unknown employer adapter keys: {sorted(unknown)}")
    url = str(definition.get("history_url", ""))
    hosts = tuple(str(h).lower() for h in definition.get("allowed_hosts", []))
    if urlsplit(url).scheme != "https":
        raise EmployerDefinitionError("history_url must be https.")
    if not hosts or not all(_HOST.match(h) for h in hosts):
        raise EmployerDefinitionError("allowed_hosts must list the portal's hostnames.")
    if (urlsplit(url).hostname or "") not in hosts and not any(
        (urlsplit(url).hostname or "").endswith("." + h) for h in hosts
    ):
        raise EmployerDefinitionError("history_url must be on one of allowed_hosts.")
    for key in ("list_container", "row", "role"):
        if not definition.get(key):
            raise EmployerDefinitionError(f"{key} is required.")
    company_name = definition.get("company_name")
    if not definition.get("company") and not company_name:
        raise EmployerDefinitionError("Set company (a selector) or company_name (fixed text).")
    status_map = {str(k).lower(): str(v) for k, v in (definition.get("status_map") or {}).items()}
    valid = {s.value for s in CollectorStatus}
    if any(v not in valid for v in status_map.values()):
        raise EmployerDefinitionError(f"status_map values must be one of {sorted(valid)}.")

    selectors = SelectorSet(
        "primary",
        row=str(definition["row"]),
        company=str(definition.get("company") or ":scope"),
        role=str(definition["role"]),
        status=definition.get("status"),
        applied=definition.get("applied"),
        link=definition.get("link"),
        item_id_attr=definition.get("item_id_attr"),
    )
    markers = StateMarkers(
        signed_out=tuple(definition.get("signed_out", [])),
        signed_out_urls=tuple(definition.get("signed_out_urls", ["/login", "/signin"])),
        empty=tuple(definition.get("empty", [])),
        list_container=(str(definition["list_container"]),),
    )
    next_selector = definition.get("next_selector")
    verified = str(definition.get("verified_on") or "") or None

    class EmployerHistoryAdapter(Adapter):
        SOURCE_KEY = source_key
        VERSION = "generic-0.1.0"
        HISTORY_URL = url
        ALLOWED_HOSTS = hosts
        LIVE_VERIFIED = verified
        PAGINATION = "next" if next_selector else "none"
        NEXT_SELECTOR = next_selector
        SELECTORS = (selectors,)
        MARKERS = markers
        STATUS_MAP = status_map or {"applied": "applied", "submitted": "applied"}

        def _extract(self, row: Tag, sel: SelectorSet, today: date) -> ExtractedItem | None:
            item = super()._extract(row, sel, today)
            if item is not None and company_name:
                # A single employer's portal: the company is fixed, not read from the row,
                # so the role is what proves the row was read; without it the layout
                # changed (selector drift), not "an application with no title".
                if not item.role:
                    return None
                return replace(item, company=str(company_name))
            return item

    return EmployerHistoryAdapter()
