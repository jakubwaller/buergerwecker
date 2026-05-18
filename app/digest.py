"""Digest-email composer. Filled in in Task 7.1."""
from __future__ import annotations
import sqlite3
from app.models import Subscription, Slot

def send_digest(*, conn: sqlite3.Connection, subscription: Subscription,
                matched_slots: list[Slot], cycle_id: str, cfg) -> None:
    raise NotImplementedError("digest emails wired up in Task 7.1")
