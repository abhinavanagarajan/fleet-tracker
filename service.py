"""Application service: ONE code path for 'write to MongoDB, then update Redis'.   (owner: Vishnu Swaroop G)
Used by the Flask app, crud_demo.py and the tests, so what you demo is exactly what is tested.
Mongo is the source of truth: if Redis is down the write still succeeds and the UI says 'Redis out of sync'.
"""
import time

import logic
import redis_layer as rl
from db import get_db, get_redis


def timed(fn, *a, **kw):
    t = time.perf_counter()
    res = fn(*a, **kw)
    return res, round((time.perf_counter() - t) * 1000, 3)


def buses() -> list:
    return list(get_db().buses_master.find({}, {"_id": 0}).sort("bus_id", 1))


def master(bus_id: str):
    return get_db().buses_master.find_one({"bus_id": bus_id}, {"_id": 0})


def last_odometer(bus_id: str) -> int:
    d = get_db().daily_bus_logs.find_one({"bus_id": bus_id}, {"odometer.end_km": 1}, sort=[("date", -1)])
    return d["odometer"]["end_km"] if d else 0


def latest_date() -> str:
    d = get_db().daily_bus_logs.find_one({}, {"date": 1}, sort=[("date", -1)])
    return d["date"] if d else time.strftime("%Y-%m-%d")


def _redis_apply(fn, *args):
    try:
        return fn(*args), True, None
    except Exception as e:  # noqa: BLE001
        return [], False, f"{type(e).__name__}: {str(e)[:120]}"


# ---------------------------------------------------------------- CREATE / upsert
def submit_shift(p: dict) -> dict:
    errs = logic.validate_shift(p)
    m = master(p.get("bus_id", ""))
    if not m:
        errs.append(f"Unknown bus {p.get('bus_id')!r}.")
    if errs:
        raise ValueError(" ".join(errs))
    doc = logic.build_log(m, p["date"], p["start_km"], p["end_km"], p["singles"], p["litres"], p["rate"],
                          p.get("bunk"), p.get("other_expense", 0) or 0, bool(p.get("holiday")))
    logs = get_db().daily_bus_logs
    old = logs.find_one({"_id": doc["_id"]})
    res = logs.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    created = res.upserted_id is not None
    trace = [f"MongoDB  db.daily_bus_logs.replace_one({{_id: '{doc['_id']}'}}, <doc>, upsert=True)  ->  "
             f"{'INSERTED new day' if created else 'REPLACED existing day'} (acknowledged={res.acknowledged})"]
    cmds, ok, err = _redis_apply(rl.apply_submission, doc, old)
    trace += [f"Redis    {c}" for c in cmds] if ok else [f"Redis    FAILED ({err}) - Mongo write is safe; use 'Rebuild Redis'"]
    return {"doc": doc, "created": created, "trace": trace, "redis_ok": ok}


# ---------------------------------------------------------------- UPDATE (embedded array + recompute)
def _update(doc_id: str, mutate) -> dict:
    """Optimistic concurrency: the update only applies if gross is unchanged since we read it; retry otherwise."""
    logs = get_db().daily_bus_logs
    for _ in range(3):
        doc = logs.find_one({"_id": doc_id})
        if not doc:
            raise LookupError(f"No log {doc_id}")
        m = master(doc["bus_id"])
        import copy
        ops, new = mutate(copy.deepcopy(doc))
        cs, pe = logic.summarize(m, new["day_type"], new["singles"], new["odometer"], new["fuel_fill"],
                                 new.get("other_expense_inr", 0.0))
        ops.setdefault("$set", {}).update({"computed_summary": cs, "performance_evaluation": pe})
        if "fuel_fill" in ops["$set"]:
            ops["$set"]["fuel_fill"] = new["fuel_fill"]
        res = logs.update_one({"_id": doc_id, "computed_summary.gross_collection_inr":
                               doc["computed_summary"]["gross_collection_inr"]}, ops)
        if res.matched_count:
            fresh = logs.find_one({"_id": doc_id})
            cmds, ok, err = _redis_apply(rl.apply_submission, fresh, doc)
            trace = [f"MongoDB  db.daily_bus_logs.update_one({{_id: '{doc_id}'}}, {list(ops.keys())} ...)  ->  "
                     f"matched={res.matched_count} modified={res.modified_count}"]
            trace += [f"Redis    {c}" for c in cmds] if ok else [f"Redis    FAILED ({err})"]
            return {"doc": fresh, "trace": trace, "redis_ok": ok, "matched": res.matched_count,
                    "modified": res.modified_count}
    raise RuntimeError("Concurrent edit detected - please retry.")


def add_single(doc_id: str, single: dict) -> dict:
    """$push a late single into the embedded array and $set the recomputed totals."""
    def mutate(d):
        s = logic.norm_single(single, len(d["singles"]) + 1)
        d["singles"].append(s)
        return {"$push": {"singles": s}}, d
    return _update(doc_id, mutate)


def set_fuel(doc_id: str, litres: float, rate: float, bunk: str | None = None) -> dict:
    def mutate(d):
        d["fuel_fill"].update({"litres": round(float(litres), 2), "fuel_rate_per_litre": round(float(rate), 2)})
        if bunk:
            d["fuel_fill"]["bunk_name"] = bunk
        return {"$set": {"fuel_fill": d["fuel_fill"]}}, d
    return _update(doc_id, mutate)


# ---------------------------------------------------------------- DELETE
def delete_log(doc_id: str) -> dict:
    logs = get_db().daily_bus_logs
    old = logs.find_one({"_id": doc_id})
    if not old:
        raise LookupError(f"No log {doc_id}")
    res = logs.delete_one({"_id": doc_id})
    cmds, ok, err = _redis_apply(rl.apply_deletion, old)
    trace = [f"MongoDB  db.daily_bus_logs.delete_one({{_id: '{doc_id}'}})  ->  deleted_count={res.deleted_count}"]
    trace += [f"Redis    {c}" for c in cmds] if ok else [f"Redis    FAILED ({err})"]
    return {"deleted": res.deleted_count, "trace": trace, "redis_ok": ok}


# ---------------------------------------------------------------- reads with graceful degradation
def leaderboard(date: str, n: int = 5) -> dict:
    try:
        rows, ms = timed(rl.leaderboard, date, n)
        return {"rows": rows, "source": "Redis ZREVRANGE", "ms": ms, "degraded": False}
    except Exception:  # noqa: BLE001  Redis down -> answer from MongoDB instead
        def q():
            return [(d["bus_id"], d["computed_summary"]["gross_collection_inr"]) for d in get_db().daily_bus_logs.find(
                {"date": date}, {"bus_id": 1, "computed_summary.gross_collection_inr": 1})
                .sort([("computed_summary.gross_collection_inr", -1)]).limit(n)]
        rows, ms = timed(q)
        return {"rows": rows, "source": "MongoDB fallback (Redis unavailable)", "ms": ms, "degraded": True}


def fleet_summary(date: str) -> dict:
    try:
        h, ms = timed(rl.fleet_summary, date)
        return {"data": h, "source": "Redis HGETALL", "ms": ms, "degraded": False}
    except Exception:  # noqa: BLE001
        def q():
            r = list(get_db().daily_bus_logs.aggregate([{"$match": {"date": date}}, {"$group": {
                "_id": None, "gross_collection_inr": {"$sum": "$computed_summary.gross_collection_inr"},
                "net_margin_inr": {"$sum": "$computed_summary.net_margin_inr"}, "buses_reported": {"$sum": 1}}}]))
            return {k: str(round(v, 2)) for k, v in r[0].items() if k != "_id"} if r else {}
        h, ms = timed(q)
        return {"data": h, "source": "MongoDB fallback (Redis unavailable)", "ms": ms, "degraded": True}


def rolling_all() -> dict:
    out, total, degraded = {}, 0.0, False
    for b in buses():
        try:
            h, ms = timed(rl.rolling, b["bus_id"])
        except Exception:  # noqa: BLE001
            h, ms = timed(rl.compute_rolling, get_db(), b["bus_id"]); degraded = True
        out[b["bus_id"]] = h; total += ms
    return {"data": out, "ms": round(total, 3), "source": "MongoDB fallback" if degraded else "Redis HGETALL x5",
            "degraded": degraded}
