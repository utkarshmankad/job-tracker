"""Deciding what a collected observation means (wired to the identity resolver in the
resolution step of Phase 3; until then observations are stored pending)."""

from __future__ import annotations

from backend.collection.ingest import Decider, PendingDecider
from backend.db.data_store import DataStore


def collection_decider(db: DataStore) -> Decider:
    return PendingDecider()
