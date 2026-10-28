"""Redis live-metrics layer.                              (owner: Abhinavanagarajan A - Redis)

  fleet:summary:{date}   HASH        gross_collection_inr, net_margin_inr, buses_reported   (HINCRBYFLOAT / HINCRBY)
  bus:rolling:{bus_id}   HASH        avg_collection_7d / 30d / sunday / weekday, mileage...  (recomputed from Mongo)
  fleet:rankings:{date}  SORTED SET  member = bus_id, score = gross collection              (ZADD / ZREVRANGE)

MongoDB is the source of truth; Redis is a derived cache that can always be rebuilt (rebuild_all).
Every write is a DELTA (new - old) so re-submitting or editing a day never double-counts.
"""
from datetime import datetime

from db import get_db, get_redis

PATTERNS = ["fleet:summary:*", "bus:rolling:*", "fleet:rankings:*"]


def summary_key(d): return f"fleet:summary:{d}"
def rolling_key(b): return f"bus:rolling:{b}"
def rank_key(d): return f"fleet:rankings:{d}"


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def compute_rolling(db, bus_id: str) -> dict:
    """Pure-Mongo computation (uses index ix_bus_date). Also the fallback when Redis is down."""
    proj = {"date": 1, "day_type": 1, "computed_summary": 1}
    last30 = list(db.daily_bus_logs.find({"bus_id": bus_id}, proj).sort([("date", -1)]).limit(30))
    if not last30:
        return {}
    g = lambda d: d["computed_summary"]["gross_collection_inr"]
    sundays = list(db.daily_bus_logs.find({"bus_id": bus_id, "day_type": "SUNDAY"}, proj)
                   .sort([("date", -1)]).limit(8))
    out = {
        "avg_collection_7d": _avg([g(d) for d in last30[:7]]),
        "avg_collection_30d": _avg([g(d) for d in last30]),
        "avg_collection_sunday": _avg([g(d) for d in sundays]),
        "avg_collection_weekday": _avg([g(d) for d in last30 if d["day_type"] == "WEEKDAY"]),
        "avg_mileage_30d": _avg([d["computed_summary"].get("mileage_kmpl") for d in last30]),
        "logs_counted": len(last30),
        "last_log_date": last30[0]["date"],
    }
    return {k: v for k, v in out.items() if v is not None}


def refresh_rolling(bus_id: str, db=None, r=None) -> dict:
    db = db if db is not None else get_db()
    r = r or get_redis()
    data = compute_rolling(db, bus_id)
    if data:
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        r.hset(rolling_key(bus_id), mapping=data)
    else:
        r.delete(rolling_key(bus_id))
    return data


def apply_submission(new: dict, old: dict | None = None, db=None, r=None) -> list:
    """Called after every Mongo insert/update. Returns the list of Redis commands run (for the UI trace)."""
    r = r or get_redis()
    d, bus = new["date"], new["bus_id"]
    gn, nn = new["computed_summary"]["gross_collection_inr"], new["computed_summary"]["net_margin_inr"]
    go = old["computed_summary"]["gross_collection_inr"] if old else 0.0
    no = old["computed_summary"]["net_margin_inr"] if old else 0.0
    dg, dn, db_cnt = round(gn - go, 2), round(nn - no, 2), (0 if old else 1)
    pipe = r.pipeline(transaction=True)
    pipe.hincrbyfloat(summary_key(d), "gross_collection_inr", dg)
    pipe.hincrbyfloat(summary_key(d), "net_margin_inr", dn)
    pipe.hincrby(summary_key(d), "buses_reported", db_cnt)
    pipe.zadd(rank_key(d), {bus: gn})
    pipe.execute()
    refresh_rolling(bus, db, r)
    return [
        f"HINCRBYFLOAT {summary_key(d)} gross_collection_inr {dg}",
        f"HINCRBYFLOAT {summary_key(d)} net_margin_inr {dn}",
        f"HINCRBY {summary_key(d)} buses_reported {db_cnt}",
        f"ZADD {rank_key(d)} {gn} {bus}",
        f"HSET {rolling_key(bus)} <recomputed from MongoDB: 7d / 30d / sunday averages>",
    ]


def apply_deletion(old: dict, db=None, r=None) -> list:
    r = r or get_redis()
    d, bus = old["date"], old["bus_id"]
    dg, dn = -old["computed_summary"]["gross_collection_inr"], -old["computed_summary"]["net_margin_inr"]
    pipe = r.pipeline(transaction=True)
    pipe.hincrbyfloat(summary_key(d), "gross_collection_inr", dg)
    pipe.hincrbyfloat(summary_key(d), "net_margin_inr", dn)
    pipe.hincrby(summary_key(d), "buses_reported", -1)
    pipe.zrem(rank_key(d), bus)
    pipe.execute()
    if int(r.hget(summary_key(d), "buses_reported") or 0) <= 0:
        r.delete(summary_key(d), rank_key(d))
    refresh_rolling(bus, db, r)
    return [f"HINCRBYFLOAT {summary_key(d)} gross_collection_inr {dg}", f"HINCRBY {summary_key(d)} buses_reported -1",
            f"ZREM {rank_key(d)} {bus}", f"HSET {rolling_key(bus)} <recomputed>"]


def rebuild_all(db=None, r=None) -> dict:
    """Wipe ONLY our three key namespaces and rebuild them from MongoDB (self-healing cache)."""
    db = db if db is not None else get_db()
    r = r or get_redis()
    removed = 0
    for pat in PATTERNS:
        for k in r.scan_iter(pat, count=500):
            r.delete(k); removed += 1
    pipe = r.pipeline(transaction=False)
    for row in db.daily_bus_logs.aggregate([{"$group": {
            "_id": "$date", "gross": {"$sum": "$computed_summary.gross_collection_inr"},
            "net": {"$sum": "$computed_summary.net_margin_inr"}, "n": {"$sum": 1}}}]):
        pipe.hset(summary_key(row["_id"]), mapping={"gross_collection_inr": round(row["gross"], 2),
                                                    "net_margin_inr": round(row["net"], 2), "buses_reported": row["n"]})
    for d in db.daily_bus_logs.find({}, {"bus_id": 1, "date": 1, "computed_summary.gross_collection_inr": 1}):
        pipe.zadd(rank_key(d["date"]), {d["bus_id"]: d["computed_summary"]["gross_collection_inr"]})
    pipe.execute()
    buses = db.daily_bus_logs.distinct("bus_id")
    for b in buses:
        refresh_rolling(b, db, r)
    return {"removed_keys": removed, "buses_rebuilt": len(buses),
            "summary_keys": len(list(r.scan_iter("fleet:summary:*"))),
            "ranking_keys": len(list(r.scan_iter("fleet:rankings:*"))),
            "rolling_keys": len(list(r.scan_iter("bus:rolling:*")))}


def leaderboard(date: str, n: int = 5, r=None) -> list:
    r = r or get_redis()
    return [(b, round(s, 2)) for b, s in r.zrevrange(rank_key(date), 0, n - 1, withscores=True)]


def fleet_summary(date: str, r=None) -> dict:
    r = r or get_redis()
    h = r.hgetall(summary_key(date))
    for k in ("gross_collection_inr", "net_margin_inr"):
        if k in h:
            h[k] = str(round(float(h[k]), 2))   # HINCRBYFLOAT accumulates float noise; show clean paise
    return h


def rolling(bus_id: str, r=None) -> dict:
    r = r or get_redis()
    return r.hgetall(rolling_key(bus_id))


def audit(db=None, r=None) -> dict:
    """Consistency check Mongo <-> Redis. Used by /health, run_all.py and the tests."""
    db = db if db is not None else get_db()
    r = r or get_redis()
    problems, checked = [], 0
    for row in db.daily_bus_logs.aggregate([{"$group": {
            "_id": "$date", "gross": {"$sum": "$computed_summary.gross_collection_inr"}, "n": {"$sum": 1}}}]):
        checked += 1
        d, h = row["_id"], r.hgetall(summary_key(row["_id"]))
        if not h:
            problems.append(f"{d}: Redis summary missing"); continue
        if abs(float(h.get("gross_collection_inr", 0)) - row["gross"]) > 0.05:
            problems.append(f"{d}: gross Redis={h.get('gross_collection_inr')} Mongo={round(row['gross'], 2)}")
        if int(h.get("buses_reported", -1)) != row["n"]:
            problems.append(f"{d}: buses_reported Redis={h.get('buses_reported')} Mongo={row['n']}")
        if r.zcard(rank_key(d)) != row["n"]:
            problems.append(f"{d}: leaderboard size {r.zcard(rank_key(d))} != {row['n']}")
    for b in db.daily_bus_logs.distinct("bus_id"):
        checked += 1
        want, got = compute_rolling(db, b), r.hgetall(rolling_key(b))
        for k in ("avg_collection_7d", "avg_collection_30d"):
            if k not in got or abs(float(got[k]) - want[k]) > 0.05:
                problems.append(f"{b}: {k} Redis={got.get(k)} Mongo={want.get(k)}")
    return {"ok": not problems, "checked": checked, "problems": problems[:20]}


def overview(r=None, sample: int = 6) -> list:
    """For the Redis explorer page: per key-pattern count + sample keys with type/ttl."""
    r = r or get_redis()
    out = []
    for pat in PATTERNS:
        keys = sorted(r.scan_iter(pat, count=500))
        out.append({"pattern": pat, "count": len(keys),
                    "type": r.type(keys[0]) if keys else "-",
                    "samples": [{"key": k, "ttl": r.ttl(k)} for k in keys[-sample:]]})
    return out


def dump_key(key: str, r=None):
    r = r or get_redis()
    t = r.type(key)
    if t == "hash": return t, r.hgetall(key)
    if t == "zset": return t, [{"member": m, "score": round(s, 2)} for m, s in r.zrevrange(key, 0, -1, withscores=True)]
    if t == "string": return t, r.get(key)
    return t, None
