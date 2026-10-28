"""CRUD demonstration - every operation goes through service.py (Mongo write + Redis update).   (owner: Vishnu Swaroop G)"""
import json

import service
from db import get_db, get_redis, require_dbs
import redis_layer as rl

BUS, DATE = "BUS_102", "2026-09-01"


def show(title, lines):
    print(f"\n--- {title} " + "-" * max(0, 70 - len(title)))
    for l in lines:
        print("  " + str(l))


def main():
    require_dbs()
    db, r = get_db(), get_redis()
    doc_id = "LOG_TN09BC1002_20260901"
    if db.daily_bus_logs.find_one({"_id": doc_id}):          # make the demo re-runnable
        service.delete_log(doc_id)

    singles = [{"trip_code": "S1_UP", "departure_time": "06:00", "collection_inr": 4800, "passenger_count": 46},
               {"trip_code": "S2_DN", "departure_time": "10:30", "collection_inr": 4300, "passenger_count": 41},
               {"trip_code": "S3_UP", "departure_time": "15:00", "collection_inr": 3250, "passenger_count": 31}]
    start = service.last_odometer(BUS)
    res = service.submit_shift({"bus_id": BUS, "date": DATE, "start_km": start, "end_km": start + 380, "singles": singles,
                                "litres": 82, "rate": 94.5, "bunk": "IOCL_Sriperumbudur"})
    show("CREATE  insert_one / upsert", [f"Inserted _id : {res['doc']['_id']}", f"created      : {res['created']}"] + res["trace"])

    show("READ  point lookup", [json.dumps(db.daily_bus_logs.find_one({"_id": doc_id}, {"singles": 0}), default=str)])
    show("READ  filtered query (Sundays of BUS_101)", [
        f"{d['date']} gross={d['computed_summary']['gross_collection_inr']} status={d['performance_evaluation']['collection_status']}"
        for d in db.daily_bus_logs.find({"bus_id": "BUS_101", "day_type": "SUNDAY"}).sort("date", 1).limit(2)])

    before = rl.rolling(BUS)
    up = service.add_single(doc_id, {"trip_code": "S4_DN", "departure_time": "19:30", "collection_inr": 3900, "passenger_count": 38})
    show("UPDATE  $push a late single + $set recomputed totals",
         [f"matched_count : {up['matched']}   modified_count : {up['modified']}",
          f"New gross_collection_inr : {up['doc']['computed_summary']['gross_collection_inr']}",
          f"singles count now        : {len(up['doc']['singles'])}",
          f"collection_status        : {up['doc']['performance_evaluation']['collection_status']}",
          f"Redis bus:rolling:{BUS} 7d avg: {before.get('avg_collection_7d')} -> {rl.rolling(BUS).get('avg_collection_7d')}"] + up["trace"])

    print("\nRedis audit before delete:", rl.audit()["ok"])
    de = service.delete_log(doc_id)
    show("DELETE  remove the test entry", [f"deleted_count : {de['deleted']}",
                                           f"Verify : find_one -> {db.daily_bus_logs.find_one({'_id': doc_id})}"] + de["trace"])
    a = rl.audit()
    print(f"\nRedis <-> Mongo audit after delete: {'CONSISTENT' if a['ok'] else a['problems']}")


if __name__ == "__main__":
    main()
