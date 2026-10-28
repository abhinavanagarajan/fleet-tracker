"""Run this first on the demo machine:  python check_env.py"""
import sys

from db import MONGO_URI, REDIS_URL, health, redact

h = health()
ok = True
print(f"MongoDB  {redact(MONGO_URI)}")
if h["mongo"]["ok"]:
    print(f"   OK  server {h['mongo']['version']}  collections: {h['mongo']['collections'] or '(empty - run: python generate_data.py)'}")
else:
    ok = False; print("   FAIL", h["mongo"]["error"])
print(f"Redis    {redact(REDIS_URL)}")
if h["redis"]["ok"]:
    print(f"   OK  server {h['redis']['version']}  keys: {h['redis']['dbsize']}")
else:
    ok = False; print("   FAIL", h["redis"]["error"])
if not ok:
    print("\nStart the databases:  docker compose up -d     (or start local mongod and redis-server)")
    sys.exit(1)
print("\nAll good. Next: python generate_data.py  &&  python app.py")
