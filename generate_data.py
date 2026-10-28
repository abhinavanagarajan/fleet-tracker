"""Seed data: 5 buses x 30 days (Aug 1-30, 2026) = 150 logs, 3-5 embedded singles each.   (owner: Rakshana V)
Deterministic (seed 2026): every run produces the SAME numbers, so your report matches your live demo.

    python generate_data.py            # reset + seed MongoDB, rebuild Redis
"""
import random
from datetime import date, timedelta

import logic
import redis_layer as rl
from db import ensure_indexes, get_db, get_redis, require_dbs

START, DAYS = date(2026, 8, 1), 30

BUSES = [  # bus_id, reg, chassis, seats, route, kmpl, baseline weekday collection, bunk
    ("BUS_101", "TN-09-BC-1001", "CH-101-2019", 52, "RT_CHN_VLR", 4.6, 17800.0, "HPCL_Walajapet"),
    ("BUS_102", "TN-09-BC-1002", "CH-102-2019", 49, "RT_CHN_TPT", 4.5, 17000.0, "IOCL_Sriperumbudur"),
    ("BUS_103", "TN-09-BC-1003", "CH-103-2019", 52, "RT_CHN_VLR", 4.7, 18200.0, "BPCL_Kanchipuram"),
    ("BUS_104", "TN-09-BC-1004", "CH-104-2020", 45, "RT_CHN_TPT", 4.4, 14800.0, "IOCL_Sriperumbudur"),
    ("BUS_105", "TN-09-BC-1005", "CH-105-2020", 52, "RT_CHN_SLM", 4.8, 15500.0, "HPCL_Krishnagiri"),
]
ROUTES = {"RT_CHN_VLR": ("Chennai-Vellore", 105.0, 90), "RT_CHN_TPT": ("Chennai-Tirupati", 118.0, 100),
          "RT_CHN_SLM": ("Chennai-Salem", 135.0, 85)}           # name, avg fare/passenger, km per single
SLOTS = ["06:00", "10:30", "15:00", "19:30", "22:30"]
DOW = [1.04, 0.94, 0.97, 1.0, 1.0, 1.08, 1.0]                   # Mon..Sun mild weekday effect


def masters():
    return [{"bus_id": b, "registration_no": reg, "chassis": ch, "permit_type": "STAGE_CARRIAGE",
             "seating_capacity": seats, "route_id": rt, "route_name": ROUTES[rt][0],
             "baseline_target_kmpl": kmpl, "baseline_avg_collection": base,
             "day_type_factors": dict(logic.DAY_FACTORS_DEFAULT), "default_bunk": bunk}
            for b, reg, ch, seats, rt, kmpl, base, bunk in BUSES]


def make_logs(ms):
    rng = random.Random(2026)
    logs = []
    for bi, m in enumerate(ms):
        odo = 140000 + bi * 1500
        _, fare0, km0 = ROUTES[m["route_id"]]
        for i in range(DAYS):
            d = START + timedelta(days=i)
            dstr, dt = d.isoformat(), logic.day_type_for(d.isoformat())
            target = (m["baseline_avg_collection"] * m["day_type_factors"][dt] * DOW[d.weekday()]
                      * min(1.5, max(0.6, rng.gauss(1, 0.11))))
            n = rng.choice([4, 5, 5]) if dt == "SUNDAY" else rng.choice([3, 4, 4, 5])      # flexible array length
            w = [rng.uniform(0.8, 1.2) for _ in range(n)]
            singles, kms = [], 0
            for k in range(n):
                part, fare = target * w[k] / sum(w), fare0 * rng.uniform(0.97, 1.03)
                pax = max(18, min(m["seating_capacity"] + 6, round(part / fare)))
                singles.append({"trip_code": f"S{k + 1}_{'UP' if k % 2 == 0 else 'DN'}", "departure_time": SLOTS[k],
                                "collection_inr": float(round(pax * fare / 10) * 10), "passenger_count": pax})
                kms += round(km0 * rng.uniform(0.97, 1.03))
            kmpl = m["baseline_target_kmpl"] * rng.gauss(1, 0.045)
            litres = round(kms / kmpl * 2) / 2
            logs.append(logic.build_log(m, dstr, odo, odo + kms, singles, litres,
                                        round(rng.uniform(93.9, 95.4), 2), m["default_bunk"]))
            odo += kms
    return logs


def seed(reset: bool = True, verbose: bool = True) -> dict:
    db, r = get_db(), get_redis()
    if reset:
        db.daily_bus_logs.drop(); db.buses_master.drop()
    ms = masters()
    ensure_indexes(db)
    db.buses_master.insert_many([dict(m) for m in ms])
    logs = make_logs(ms)
    db.daily_bus_logs.insert_many(logs)
    redis_stats = rl.rebuild_all(db, r)
    stats = {"buses_master": len(ms), "daily_bus_logs": len(logs),
             "embedded_singles": sum(len(l["singles"]) for l in logs), "redis": redis_stats}
    if verbose:
        print(f"Seeded {stats['buses_master']} buses, {stats['daily_bus_logs']} daily logs, "
              f"{stats['embedded_singles']} embedded singles")
        print(f"Redis rebuilt: {redis_stats}")
    return stats


if __name__ == "__main__":
    require_dbs()
    seed()
