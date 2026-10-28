"""Connections, index definitions and health probes.      (owner: Vishnu Swaroop G - integration)

Environment variables (all optional):
  MONGO_URI  default mongodb://localhost:27017      (an Atlas mongodb+srv:// URI works too)
  REDIS_URL  default redis://localhost:6379/0
  FLEET_DB   default fleet_finance_db
"""
import os
import re

import redis
from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DB_NAME = os.getenv("FLEET_DB", "fleet_finance_db")

_mongo = None
_redis = None


def mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    return _mongo


def get_db():
    return mongo_client()[DB_NAME]


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True,
                                      socket_connect_timeout=2, socket_timeout=4)
    return _redis


# name, collection, keys, extra options  - the 5 indexes documented in DA-2
INDEXES = [
    ("ix_bus_date", "daily_bus_logs", [("bus_id", 1), ("date", -1)], {}),
    ("ix_route", "daily_bus_logs", [("route_id", 1)], {}),
    ("ix_daytype", "daily_bus_logs", [("day_type", 1)], {}),
    ("ix_perf_status", "daily_bus_logs", [("performance_evaluation.collection_status", 1)], {}),
    ("ix_bus_master_pk", "buses_master", [("bus_id", 1)], {"unique": True}),
]


def ensure_indexes(db=None):
    db = db if db is not None else get_db()
    for name, coll, keys, opts in INDEXES:
        db[coll].create_index(keys, name=name, **opts)


def redact(uri: str) -> str:
    return re.sub(r"://([^:/@]+):[^@]+@", r"://\1:****@", uri)


def health() -> dict:
    """Used by /health and check_env.py - this is the 'actual NoSQL database' proof."""
    out = {"mongo": {"ok": False}, "redis": {"ok": False}}
    try:
        c = mongo_client()
        info = c.server_info()
        db = c[DB_NAME]
        names = sorted(db.list_collection_names())
        out["mongo"] = {
            "ok": True, "version": info.get("version"), "uri": redact(MONGO_URI), "db": DB_NAME,
            "collections": {n: db[n].count_documents({}) for n in names},
            "indexes": {n: sorted(db[n].index_information().keys()) for n in names},
        }
    except Exception as e:  # noqa: BLE001
        out["mongo"]["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    try:
        r = get_redis()
        info = r.info()
        out["redis"] = {
            "ok": True, "version": info.get("redis_version"), "url": redact(REDIS_URL),
            "mode": info.get("redis_mode"), "used_memory": info.get("used_memory_human"),
            "uptime_s": info.get("uptime_in_seconds"), "dbsize": r.dbsize(),
        }
    except Exception as e:  # noqa: BLE001
        out["redis"]["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


def require_dbs():
    """Call at the top of every CLI script: friendly message instead of a 40-line stack trace."""
    h = health()
    bad = []
    if not h["mongo"]["ok"]:
        bad.append(f"  x MongoDB not reachable at {redact(MONGO_URI)}\n      {h['mongo'].get('error', '')}")
    if not h["redis"]["ok"]:
        bad.append(f"  x Redis not reachable at {redact(REDIS_URL)}\n      {h['redis'].get('error', '')}")
    if bad:
        raise SystemExit("\nDatabase check FAILED:\n" + "\n".join(bad) +
                         "\n\nFix:  docker compose up -d     (or start local mongod / redis-server)\n"
                         "Then: python check_env.py\n")
