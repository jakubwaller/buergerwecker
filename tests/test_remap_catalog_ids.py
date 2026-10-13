"""The one-off remap that moves subscriptions onto a renumbered tenant's ids.

A TEVIS poll for a dead id returns nothing and raises nothing, so a
subscription left on the old ids goes silent. The script must touch exactly
the ids in the map, in the mapped city, on live rows, and nothing else.
"""
import json
import pathlib
import sys
from datetime import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts.remap_catalog_ids import main, remap  # noqa: E402

from app.db import connect, init_schema  # noqa: E402
from app.models import Filter  # noqa: E402
from app.repo import (insert_pending, insert_push_subscription,  # noqa: E402
                      register_device, soft_delete)

MAP = {"hagen": {"services": {"5595": "5836", "5599": "5837"},
                 "locations": {"491": "508", "492": "509"}}}


@pytest.fixture
def db(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn


def _filter(services, locations):
    return Filter(appointment_types=list(services), locations=locations,
                  weekdays=[1, 2, 3, 4, 5], time_window_start=time(8, 0),
                  time_window_end=time(12, 0), max_days_ahead=30)


def _sub(db, services, locations, city="hagen", email="a@example.com"):
    return insert_pending(db, email=email, city=city, language="de",
                          filter_=_filter(services, locations), ttl_days=90)


def _f(db, sid):
    return Filter.from_json(db.execute(
        "SELECT filters_json FROM subscriptions WHERE id=?", (sid,)).fetchone()[0])


def test_services_and_offices_are_remapped_and_nothing_else_moves(db):
    mail = _sub(db, ["5595", "5599"], ["491", "492"])
    device = register_device(db, platform="ios", token="tok", secret_hash="h",
                             language="de")
    app = insert_push_subscription(db, device_id=device, city="hagen",
                                   language="de", ttl_days=90,
                                   filter_=_filter(["5599"], "all"))
    other_city = _sub(db, ["5595"], ["491"], city="dresden")
    deleted = _sub(db, ["5595"], ["491"], email="b@example.com")
    soft_delete(db, deleted)
    unknown = _sub(db, ["5595", "4711"], ["491"], email="c@example.com")

    stats = remap(db, MAP, apply=True)

    assert _f(db, mail) == _filter(["5836", "5837"], ["508", "509"])
    assert _f(db, app) == _filter(["5837"], "all")
    assert _f(db, other_city) == _filter(["5595"], ["491"])
    assert _f(db, deleted) == _filter(["5595"], ["491"])
    assert _f(db, unknown) == _filter(["5836", "4711"], ["508"])
    assert stats == {"hagen": {"scanned": 3, "changed": 3, "unknown": 1}}


def test_dry_run_writes_nothing_and_a_second_apply_changes_nothing(db, tmp_path, capsys):
    sid = _sub(db, ["5595"], ["491"])
    before = db.execute("SELECT filters_json FROM subscriptions").fetchone()[0]
    map_path = tmp_path / "map.json"
    map_path.write_text(json.dumps(MAP))
    db_path = str(tmp_path / "t.db")

    main([str(map_path), "--db", db_path])
    assert db.execute("SELECT filters_json FROM subscriptions").fetchone()[0] == before
    assert "dry run" in capsys.readouterr().out

    main([str(map_path), "--db", db_path, "--apply"])
    assert _f(db, sid) == _filter(["5836"], ["508"])
    assert remap(db, MAP, apply=True)["hagen"]["changed"] == 0


def test_a_map_that_would_remap_its_own_output_is_refused(db):
    with pytest.raises(ValueError):
        remap(db, {"hagen": {"services": {"1": "2", "2": "3"}, "locations": {}}},
              apply=True)


def test_the_committed_map_is_accepted(db):
    path = (pathlib.Path(__file__).resolve().parents[1] / "scripts"
            / "catalog_remap_2026-10-10.json")
    stats = remap(db, json.loads(path.read_text()), apply=False)
    assert set(stats) == {"hagen", "dresden", "duesseldorf", "kassel",
                          "moenchengladbach"}
