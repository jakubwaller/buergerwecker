from datetime import datetime, time
from unittest.mock import patch
import pytest
from app.db import connect, init_schema
from app.models import Filter, Slot, Subscription
from app.digest import render_digest_text

def _sub(language="de"):
    return Subscription(
        id=1, email="a@x.com", city="leipzig", language=language,
        sub_filter=Filter(
            appointment_types=["svc-A"], locations="all",
            weekdays=[1,2,3,4,5,6,7],
            time_window_start=time(0,0), time_window_end=time(23,59),
        ),
        created_at=datetime(2026,5,1), confirmed_at=datetime(2026,5,1),
        last_notified_at=None,
        expires_at=datetime(2026,8,1),
        reminder_sent_at=None, heartbeat_30d_at=None, heartbeat_60d_at=None,
        deleted_at=None,
    )

def test_render_digest_de():
    slots = [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")]
    text = render_digest_text(_sub("de"), slots,
                              unsubscribe_url="https://x/unsubscribe/tok",
                              public_base_url="https://x", kofi_url="https://ko-fi.com/me")
    assert "2026-06-10" in text
    assert "10:30" in text
    assert "schneller Klick" in text  # burst-congestion line
    assert "https://x/unsubscribe/tok" in text

def test_render_digest_en():
    slots = [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")]
    text = render_digest_text(_sub("en"), slots,
                              unsubscribe_url="https://x/unsubscribe/tok",
                              public_base_url="https://x", kofi_url="https://ko-fi.com/me")
    assert "click wins" in text.lower()
