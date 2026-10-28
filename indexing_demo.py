"""Indexing evidence: index list, explain() plans (IXSCAN vs COLLSCAN) and a scale benchmark.   (owner: Rakshana V)

    python indexing_demo.py              # plans + timings on the 150-doc seed
    python indexing_demo.py --bench 100000   # build N throw-away docs, time query with/without index
"""
import random
import statistics
import sys
import time

from db import INDEXES, get_db, require_dbs

# (label, collection, filter, sort, expected index)
PLAN_TESTS = [
    ("bus + recency", "daily_bus_logs", {"bus_id": "BUS_101"}, [("date", -1)], "ix_bus_date"),
    ("route_id", "daily_bus_logs", {"route_id": "RT_CHN_VLR"}, None, "ix_route"),
    ("day_type", "daily_bus_logs", {"day_type": "SUNDAY"}, None, "ix_daytype"),
    ("nested collection_status", "daily_bus_logs",
     {"performance_evaluation.collection_status": "BELOW_AVERAGE"}, None, "ix_perf_status"),
    ("master lookup by bus_id", "buses_master", {"bus_id": "BUS_103"}, None, "ix_bus_master_pk"),
    ("NON-indexed: bunk_name", "daily_bus_logs", {"fuel_fill.bunk_name": "HPCL_Walajapet"}, None, "(none)"),
]


def list_indexes(db=None) -> list:
    db = db if db is not None else get_db()
    rows = []
    for name, coll, keys, opts in INDEXES:
        info = db[coll].index_information().get(name)
        rows.append({"name": name, "collection": coll, "key": dict(keys), "unique": bool(opts.get("unique")),
                     "exists": info is not None})
    return rows


def _walk(o, key):
    """collect every value stored under `key` anywhere in a (version-dependent) explain document."""
    found = []
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key and isinstance(v, str):
                found.append(v)
            found += _walk(v, key)
    elif isinstance(o, list):
        for v in o:
            found += _walk(v, key)
    return found


def explain(db, coll, flt, sort=None, force_scan=False) -> dict:
    cmd = {"find": coll, "filter": flt}
    if sort:
        cmd["sort"] = dict(sort)
    if force_scan:
        cmd["hint"] = {"$natural": 1}
    try:
        ex = db.command({"explain": cmd, "verbosity": "executionStats"})
    except Exception as e:  # noqa: BLE001
        return {"stage": "n/a", "index": "", "examined": None, "returned": None, "ms": None, "error": str(e)[:150]}
    return parse_explain(ex)


def parse_explain(ex: dict) -> dict:
    """Reads MongoDB's documented explain('executionStats') format (classic and SBE 'queryPlan' variants)."""
    st = ex.get("executionStats", {})
    stages = _walk(ex.get("queryPlanner", {}).get("winningPlan", {}), "stage")
    names = _walk(ex.get("queryPlanner", {}).get("winningPlan", {}), "indexName")
    if "IXSCAN" in stages:
        stage = "IXSCAN"
    elif "COLLSCAN" in stages:
        stage = "COLLSCAN"
    else:
        stage = stages[0] if stages else "n/a (non-MongoDB plan format)"
    return {"stage": stage, "index": names[0] if names else "", "examined": st.get("totalDocsExamined"),
            "keys": st.get("totalKeysExamined"), "returned": st.get("nReturned"),
            "ms": st.get("executionTimeMillis"), "error": None}


def plan_comparison(db=None) -> list:
    db = db if db is not None else get_db()
    out = []
    for label, coll, flt, sort, want in PLAN_TESTS:
        out.append({"label": label, "filter": flt, "expected": want,
                    "planner": explain(db, coll, flt, sort), "forced_scan": explain(db, coll, flt, sort, True)})
    return out


def timing(db=None, runs: int = 300) -> list:
    db = db if db is not None else get_db()
    rows = []
    for label, coll, flt, _s, want in PLAN_TESTS:
        ts = []
        for _ in range(runs):
            t = time.perf_counter(); n = len(list(db[coll].find(flt))); ts.append((time.perf_counter() - t) * 1000)
        rows.append({"label": label, "indexed": want != "(none)", "matched": n, "median_ms": round(statistics.median(ts), 3)})
    return rows


def bench(n_docs: int = 100_000) -> dict:
    """Throw-away collection: same query WITHOUT then WITH an index, to show the effect at realistic scale."""
    db, name, rng = get_db(), "daily_bus_logs_bench", random.Random(7)
    n_docs = max(1000, min(int(n_docs), 500_000))
    db.drop_collection(name)
    try:
        coll, t0 = db[name], time.perf_counter()
        for off in range(0, n_docs, 10_000):
            coll.insert_many([{"bus_id": f"BUS_{rng.randint(1, 500):03d}", "date": f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                               "gross": rng.randint(8000, 30000)} for _ in range(min(10_000, n_docs - off))])
        load_s = round(time.perf_counter() - t0, 2)
        flt, sort = {"bus_id": "BUS_250"}, [("date", -1)]

        def measure():
            ts = []
            for _ in range(7):
                t = time.perf_counter(); n = len(list(coll.find(flt).sort(sort))); ts.append((time.perf_counter() - t) * 1000)
            return round(statistics.median(ts), 2), n

        before_ms, matched = measure(); before = explain(db, name, flt, sort)
        t = time.perf_counter(); coll.create_index([("bus_id", 1), ("date", -1)], name="ix_bus_date_bench")
        build_s = round(time.perf_counter() - t, 2)
        after_ms, _ = measure(); after = explain(db, name, flt, sort)
        return {"docs": n_docs, "load_s": load_s, "index_build_s": build_s, "matched": matched,
                "before": {"ms": before_ms, **before}, "after": {"ms": after_ms, **after},
                "speedup": round(before_ms / after_ms, 1) if after_ms else None}
    finally:
        db.drop_collection(name)


def main():
    require_dbs()
    db = get_db()
    print("INDEXES"); [print("  ", r) for r in list_indexes(db)]
    print("\nEXPLAIN: planner's choice vs forced collection scan ($natural hint)")
    for r in plan_comparison(db):
        p, f = r["planner"], r["forced_scan"]
        print(f"  {r['label']:<26} planner={p['stage']:<8} idx={p['index'] or '-':<17} examined={p['examined']}  |  "
              f"forced={f['stage']:<8} examined={f['examined']}")
    print("\nTIMING (median of 300 runs)"); [print("  ", r) for r in timing(db)]
    if "--bench" in sys.argv:
        n = int(sys.argv[sys.argv.index("--bench") + 1]) if len(sys.argv) > sys.argv.index("--bench") + 1 else 100000
        print(f"\nBENCH {n} docs ..."); print(bench(n))


if __name__ == "__main__":
    main()
