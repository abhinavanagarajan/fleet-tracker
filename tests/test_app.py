"""End-to-end tests through the Flask app. Uses whatever DBs db.py points at (real MongoDB + Redis in the lab).
Run:  python -m pytest -q       (reseeds the demo data)"""
import pytest

import generate_data
import redis_layer as rl
import service
from app import app
from db import get_db, get_redis, require_dbs

DOC = "LOG_TN09BC1002_20260901"


@pytest.fixture(scope="module")
def client():
    require_dbs()
    generate_data.seed(verbose=False)
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = False
    return app.test_client()


def test_all_pages_render(client):
    for url in ["/", "/submit", "/logs", "/logs/LOG_TN09BC1001_20260824", "/queries", "/indexes", "/redis",
                "/redis?key=fleet:rankings:2026-08-24", "/health", "/guide"]:
        r = client.get(url)
        assert r.status_code == 200, url


def test_all_queries_via_api(client):
    for i in range(1, 13):
        j = client.get(f"/api/queries/Q{i}").get_json()
        assert j["error"] is None and j["count"] > 0, f"Q{i}: {j['error']}"


def _form(**o):
    f = {"bus_id": "BUS_102", "date": "2026-09-01", "start_km": "153000", "end_km": "153380", "litres": "82", "rate": "94.5",
         "bunk": "X", "other_expense": "0", "trip_code": ["S1_UP", "S2_DN"], "departure_time": ["06:00", "10:30"],
         "collection": ["4800", "4300"], "passengers": ["46", "41"]}
    f.update(o)
    return f


def test_submit_upsert_is_idempotent_in_redis(client):
    client.post("/submit", data=_form())
    client.post("/submit", data=_form(collection=["5000", "4300"]))      # same day again -> replace, not double count
    assert get_db().daily_bus_logs.count_documents({"_id": DOC}) == 1
    assert float(rl.fleet_summary("2026-09-01")["gross_collection_inr"]) == 9300.0
    assert rl.fleet_summary("2026-09-01")["buses_reported"] == "1"
    assert rl.leaderboard("2026-09-01") == [("BUS_102", 9300.0)]
    assert rl.audit()["ok"]


def test_update_push_and_fuel(client):
    r = client.post(f"/logs/{DOC}/single", data={"collection": "3900", "passengers": "38"})
    assert r.status_code == 302
    d = get_db().daily_bus_logs.find_one({"_id": DOC})
    assert len(d["singles"]) == 3 and d["computed_summary"]["gross_collection_inr"] == 13200.0
    client.post(f"/logs/{DOC}/fuel", data={"litres": "70", "rate": "100", "bunk": "Y"})
    d = get_db().daily_bus_logs.find_one({"_id": DOC})
    assert d["fuel_fill"]["total_fuel_cost"] == 7000.0 and d["computed_summary"]["net_margin_inr"] == 6200.0
    assert float(rl.fleet_summary("2026-09-01")["net_margin_inr"]) == 6200.0


def test_validation_rejects_bad_input(client):
    n = get_db().daily_bus_logs.count_documents({})
    client.post("/submit", data=_form(date="2026-09-02", end_km="100"))              # end < start
    client.post("/submit", data=_form(date="2026-09-02", collection=["", ""]))        # no singles
    client.post("/submit", data=_form(date="2026-09-02", litres="abc"))
    assert get_db().daily_bus_logs.count_documents({}) == n


def test_delete_reverses_redis(client):
    client.post(f"/logs/{DOC}/delete")
    assert get_db().daily_bus_logs.find_one({"_id": DOC}) is None
    assert rl.leaderboard("2026-09-01") == [] and get_redis().exists("fleet:summary:2026-09-01") == 0
    assert rl.audit()["ok"]


def test_redis_down_degrades_gracefully(client, monkeypatch):
    import redis_layer
    monkeypatch.setattr(redis_layer, "leaderboard", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    lb = service.leaderboard("2026-08-24")
    assert lb["degraded"] and lb["rows"][0][0] in {"BUS_101", "BUS_103"}
    assert client.get("/").status_code == 200


def test_rebuild_redis_heals(client):
    r = get_redis()
    r.delete("fleet:summary:2026-08-24"); r.delete("bus:rolling:BUS_101")
    assert not rl.audit()["ok"]
    rl.rebuild_all()
    assert rl.audit()["ok"]
