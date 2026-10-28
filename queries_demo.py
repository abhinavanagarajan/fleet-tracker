"""The 12 important queries (DA-2 Q1-Q10 + Q11 $lookup + Q12 cost-per-km).            (owner: Rakshana V)

Each query is a SPEC (data). The text shown on screen is generated FROM the same spec that is executed,
so 'what you see is exactly what ran'.   Run all from the terminal:   python queries_demo.py
"""
import json
import time

from db import get_db, get_redis, require_dbs

DEFAULTS = {"bus_id": "BUS_101", "bus2": "BUS_104", "ref_bus": "BUS_103", "date": "2026-08-24",
            "start": "2026-08-01", "end": "2026-08-31"}
G = "computed_summary.gross_collection_inr"


# ------------------------------------------------------------------ query definitions
def Q(qid, title, db, tags, question, build):
    return {"id": qid, "title": title, "db": db, "tags": tags, "question": question, "build": build}


QUERIES = [
    Q("Q1", "Point lookup by (bus_id, date)", "MongoDB", ["find_one", "compound index ix_bus_date"],
      "What did one bus earn on one day, and what was its mileage?",
      lambda p: {"type": "find_one", "coll": "daily_bus_logs", "filter": {"bus_id": p["bus_id"], "date": p["date"]},
                 "projection": {"_id": 1, "date": 1, "day_type": 1, G: 1, "computed_summary.mileage_kmpl": 1,
                                "performance_evaluation.collection_status": 1}}),
    Q("Q2", "Underperforming-day / anomaly detection", "MongoDB",
      ["range filter", "nested field", "ix_perf_status", "sort"],
      "Which days did a bus fall BELOW its day-type baseline this month?",
      lambda p: {"type": "find", "coll": "daily_bus_logs",
                 "filter": {"bus_id": p["bus2"], "date": {"$gte": p["start"], "$lte": p["end"]},
                            "performance_evaluation.collection_status": "BELOW_AVERAGE"},
                 "projection": {"_id": 0, "date": 1, "day_type": 1, G: 1,
                                "performance_evaluation.baseline_avg_collection": 1},
                 "sort": [("date", 1)], "limit": 0}),
    Q("Q3", "Fleet-wide mileage-efficiency flag", "MongoDB", ["nested field filter", "limit"],
      "Which bus-days burned too much diesel for the distance (mileage LOW)?",
      lambda p: {"type": "find", "coll": "daily_bus_logs", "filter": {"performance_evaluation.mileage_status": "LOW"},
                 "projection": {"_id": 0, "bus_id": 1, "date": 1, "computed_summary.mileage_kmpl": 1,
                                "performance_evaluation.mileage_baseline_kmpl": 1},
                 "sort": [("date", 1), ("bus_id", 1)], "limit": 5}),
    Q("Q4", "Contextual day-type average (Sunday vs Weekday)", "MongoDB",
      ["aggregation", "$group", "$avg", "ix_daytype"],
      "Is a Sunday spike normal? Average collection per bus split by day type (Objective 4).",
      lambda p: {"type": "aggregate", "coll": "daily_bus_logs", "pipeline": [
          {"$group": {"_id": {"bus_id": "$bus_id", "day_type": "$day_type"},
                      "avg_gross": {"$avg": "$" + G}, "days_counted": {"$sum": 1}}},
          {"$sort": {"_id.bus_id": 1, "_id.day_type": 1}}]}),
    Q("Q5", "Peak-revenue single per route", "MongoDB", ["aggregation", "$unwind", "embedded array", "ix_route"],
      "Which trip slot (single) earns the most on each route?",
      lambda p: {"type": "aggregate", "coll": "daily_bus_logs", "pipeline": [
          {"$unwind": "$singles"},
          {"$group": {"_id": {"route_id": "$route_id", "trip_code": "$singles.trip_code"},
                      "avg_single_collection": {"$avg": "$singles.collection_inr"},
                      "total_passengers": {"$sum": "$singles.passenger_count"}}},
          {"$sort": {"avg_single_collection": -1}}, {"$limit": 5}]}),
    Q("Q6", "Top-3 most profitable bus-days, fleet-wide", "MongoDB", ["sort", "limit", "nested sort key"],
      "Which three bus-days made the highest net margin?",
      lambda p: {"type": "find", "coll": "daily_bus_logs", "filter": {},
                 "projection": {"_id": 0, "bus_id": 1, "date": 1, "computed_summary.net_margin_inr": 1},
                 "sort": [("computed_summary.net_margin_inr", -1)], "limit": 3}),
    Q("Q7", "Route margin intelligence", "MongoDB", ["aggregation", "$group", "$sum", "$avg", "ix_route"],
      "Which route is most profitable overall, and how efficient is its fuel use?",
      lambda p: {"type": "aggregate", "coll": "daily_bus_logs", "pipeline": [
          {"$group": {"_id": "$route_id", "total_net_margin": {"$sum": "$computed_summary.net_margin_inr"},
                      "avg_mileage": {"$avg": "$computed_summary.mileage_kmpl"}, "trip_days": {"$sum": 1}}},
          {"$sort": {"total_net_margin": -1}}]}),
    Q("Q8", "Cross-collection reference lookup (logs -> buses_master)", "MongoDB",
      ["referencing", "ix_bus_master_pk (unique)"],
      "Static bus details live ONCE in buses_master; fetch them for a log via bus_id.",
      lambda p: {"type": "ref", "bus_id": p["ref_bus"], "date": p["date"]}),
    Q("Q9", "Live top-earning-bus leaderboard", "Redis", ["sorted set", "ZREVRANGE"],
      "Who are today's top earners? (answered from RAM, no Mongo scan)",
      lambda p: {"type": "redis", "cmds": [["ZREVRANGE", f"fleet:rankings:{p['date']}", 0, 4, "WITHSCORES"]]}),
    Q("Q10", "Cached rolling stats + fleet daily summary", "Redis", ["hash", "HGETALL", "HINCRBYFLOAT"],
      "7d / 30d / Sunday averages for a bus and the fleet's running totals for the day.",
      lambda p: {"type": "redis", "cmds": [["HGETALL", f"bus:rolling:{p['bus_id']}"],
                                           ["HGETALL", f"fleet:summary:{p['date']}"]]}),
    Q("Q11", "$lookup join: day's logs enriched with master data", "MongoDB",
      ["aggregation", "$lookup", "$unwind", "$project", "referencing"],
      "One pipeline: every bus's log for a day joined with seating capacity and permit type.",
      lambda p: {"type": "aggregate", "coll": "daily_bus_logs", "pipeline": [
          {"$match": {"date": p["date"]}},
          {"$lookup": {"from": "buses_master", "localField": "bus_id", "foreignField": "bus_id", "as": "bus"}},
          {"$unwind": "$bus"},
          {"$project": {"_id": 0, "bus_id": 1, "gross": "$" + G, "seats": "$bus.seating_capacity",
                        "permit": "$bus.permit_type", "chassis": "$bus.chassis"}},
          {"$sort": {"gross": -1}}]}),
    Q("Q12", "Revenue-per-km and fuel-cost-per-km per bus", "MongoDB",
      ["aggregation", "$group", "$project", "$divide"],
      "Link distance, fuel and fare (siloed on paper): what does each km earn and cost?",
      lambda p: {"type": "aggregate", "coll": "daily_bus_logs", "pipeline": [
          {"$group": {"_id": "$bus_id", "gross": {"$sum": "$" + G}, "km": {"$sum": "$odometer.total_km"},
                      "fuel": {"$sum": "$fuel_fill.total_fuel_cost"}}},
          {"$project": {"gross": 1, "km": 1, "fuel": 1,
                        "revenue_per_km": {"$divide": ["$gross", "$km"]},
                        "fuel_cost_per_km": {"$divide": ["$fuel", "$km"]}}},
          {"$sort": {"revenue_per_km": -1}}]}),
]
BY_ID = {q["id"]: q for q in QUERIES}


# ------------------------------------------------------------------ rendering (mongosh-style text)
def _j(o):
    return json.dumps(o, indent=2, default=str)


def render(spec: dict) -> str:
    t = spec["type"]
    if t == "find_one":
        return f"db.{spec['coll']}.findOne({_j(spec['filter'])},\n  {_j(spec['projection'])})"
    if t == "find":
        s = f"db.{spec['coll']}.find({_j(spec['filter'])},\n  {_j(spec['projection'])})"
        s += ".sort(" + json.dumps(dict(spec["sort"])) + ")"
        return s + (f".limit({spec['limit']})" if spec.get("limit") else "")
    if t == "aggregate":
        return f"db.{spec['coll']}.aggregate({_j(spec['pipeline'])})"
    if t == "ref":
        return (f"log    = db.daily_bus_logs.findOne({{bus_id: '{spec['bus_id']}', date: '{spec['date']}'}})\n"
                f"master = db.buses_master.findOne({{bus_id: log.bus_id}})      // reference, not embedded")
    if t == "redis":
        return "\n".join("redis> " + " ".join(str(x) for x in c) for c in spec["cmds"])
    return str(spec)


# ------------------------------------------------------------------ execution
def _flat(d, pre=""):
    out = {}
    for k, v in d.items():
        key = f"{pre}{k}"
        if isinstance(v, dict):
            out.update(_flat(v, key + "."))
        elif isinstance(v, list):
            out[key] = f"[{len(v)} items]"
        else:
            out[key] = round(v, 2) if isinstance(v, float) else v
    return out


def _execute(spec: dict):
    """-> list of dict rows."""
    db, t = get_db(), spec["type"]
    if t == "find_one":
        d = db[spec["coll"]].find_one(spec["filter"], spec["projection"])
        return [d] if d else []
    if t == "find":
        cur = db[spec["coll"]].find(spec["filter"], spec["projection"]).sort(spec["sort"])
        if spec.get("limit"):
            cur = cur.limit(spec["limit"])
        return list(cur)
    if t == "aggregate":
        return list(db[spec["coll"]].aggregate(spec["pipeline"]))
    if t == "ref":
        log = db.daily_bus_logs.find_one({"bus_id": spec["bus_id"], "date": spec["date"]})
        m = db.buses_master.find_one({"bus_id": log["bus_id"]}, {"_id": 0}) if log else None
        if not log or not m:
            return []
        return [{"log_id": log["_id"], "log_route": log["route_id"], "seating_capacity": m["seating_capacity"],
                 "permit_type": m["permit_type"], "chassis": m["chassis"], "registration_no": m["registration_no"],
                 "baseline_target_kmpl": m["baseline_target_kmpl"]}]
    if t == "redis":
        r, rows = get_redis(), []
        for c in spec["cmds"]:
            if c[0] == "ZREVRANGE":
                rows += [{"rank": i + 1, "member": m, "score": round(float(sc), 2)}
                         for i, (m, sc) in enumerate(r.zrevrange(c[1], c[2], c[3], withscores=True))]
            elif c[0] == "HGETALL":
                rows += [{"key": c[1], "field": k, "value": (round(float(v), 2) if k.endswith("inr") else v)}
                         for k, v in r.hgetall(c[1]).items()]
            else:
                raise ValueError(f"unsupported redis command {c[0]}")
        return rows
    raise ValueError(t)


def run_query(qid: str, params: dict | None = None) -> dict:
    q, p = BY_ID[qid], {**DEFAULTS, **(params or {})}
    spec = q["build"](p)
    res = {"id": qid, "title": q["title"], "db": q["db"], "tags": q["tags"], "question": q["question"],
           "code": render(spec), "params": p}
    try:
        t0 = time.perf_counter()
        raw = _execute(spec)
        res["ms"] = round((time.perf_counter() - t0) * 1000, 3)
        rows = [_flat({k: (str(v) if k == "_id" and not isinstance(v, dict) else v) for k, v in r.items()}) for r in raw]
        res["rows"], res["count"] = rows, len(rows)
        rows = [dict(sorted(r.items(), key=lambda kv: not kv[0].startswith("_id"))) for r in rows]  # _id.* first
        res["rows"] = rows
        res["columns"] = list(dict.fromkeys(c for r in rows for c in r))
        res["error"] = None
    except Exception as e:  # noqa: BLE001
        res.update(rows=[], columns=[], count=0, ms=None, error=f"{type(e).__name__}: {str(e)[:300]}")
    return res


def print_result(r: dict, out=print):
    out(f"\n{'=' * 78}\n{r['id']} - {r['title']}   [{r['db']}]\n  Q: {r['question']}\n{'-' * 78}\n{r['code']}\n{'-' * 78}")
    if r["error"]:
        out("ERROR: " + r["error"]); return
    for row in r["rows"][:8]:
        out("  " + "  ".join(f"{k}={v}" for k, v in row.items()))
    if r["count"] > 8:
        out(f"  ... ({r['count']} rows total)")
    out(f"  -> {r['count']} row(s) in {r['ms']} ms")


if __name__ == "__main__":
    require_dbs()
    for qq in QUERIES:
        print_result(run_query(qq["id"]))
