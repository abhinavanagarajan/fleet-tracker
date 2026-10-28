# Fleet Finance Management System - final project (BCSE406L DA-3)

Hybrid NoSQL stack: **MongoDB** (durable documents, aggregation, indexes) + **Redis** (live hashes / sorted sets) behind a **Flask** app.

```
Browser -> Flask (app.py) -> service.py -> MongoDB  (source of truth: daily_bus_logs, buses_master)
                                        \-> Redis    (derived cache: fleet:summary / bus:rolling / fleet:rankings)
```
If Redis dies the app falls back to MongoDB; `Rebuild Redis` recreates the cache from MongoDB at any time.

## 1. Start the databases (pick ONE)
| Option | Command |
|---|---|
| **Docker (easiest)** | `docker compose up -d` |
| Local installs | Start `mongod` (MongoDB Community 6/7/8) and `redis-server` (Windows: Memurai or WSL). Defaults `localhost:27017` / `localhost:6379`. |
| MongoDB Atlas | `set MONGO_URI=mongodb+srv://user:pass@cluster/...` (PowerShell: `$env:MONGO_URI="..."`) |

## 2. Install and run
```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python check_env.py             # must say "All good"
python generate_data.py         # 5 buses, 150 logs, indexes, Redis keys
python app.py                   # http://localhost:5000
```
Evidence for the report + self-check (writes `output/*.txt`):
```bash
python run_all.py               # seed + CRUD + 12 queries + indexing + audit, prints PASS/FAIL
python -m pytest -q             # 12 tests (re-seeds data first)
python crud_demo.py | python queries_demo.py | python indexing_demo.py --bench 100000
```
Data is seeded with a fixed random seed: **every run gives identical numbers**, so report and live demo always match.

## 3. What is where
| File | Purpose | Owner (DA-2 split) |
|---|---|---|
| `redis_layer.py` | 3 Redis structures, HINCRBYFLOAT deltas, ZADD leaderboard, rolling-average refresh, rebuild + audit | Abhinavanagarajan A |
| `generate_data.py`, `queries_demo.py`, `indexing_demo.py`, `logic.py` | data generation, 12 queries + 4 pipelines, 5 indexes + explain + benchmark, business rules | Rakshana V |
| `app.py`, `service.py`, `crud_demo.py`, `run_all.py`, `db.py`, `templates/` | Flask app, Mongo+Redis service layer, CRUD, orchestration, connections | Vishnu Swaroop G |

## 4. Pages (open **Demo Guide** in the app for the 8-minute script)
Dashboard, Submit Shift (variable singles, auto-fill), Logs (CRUD: `$push`, `$set`, delete), **Query Console** (Q1-Q12: exact code, DB, ms, result), **Index Lab** (`explain()` IXSCAN vs COLLSCAN + 100k-doc benchmark), Redis Explorer, Health (real server versions, consistency check, rebuild/reset).

## 5. Screenshots to capture for the report (names match DA-2 evidence table)
`01_health` (Health page) - `02_dashboard` - `03_submit_trace` (green/red trace after a submit) - `04_log_update` - `05_query_console` (Q2, Q4, Q5, Q9) - `06_index_lab` (explain + benchmark) - `07_redis_explorer` - **Compass**: `daily_bus_logs` documents + Indexes tab - **mongosh / redis-cli**: `db.daily_bus_logs.getIndexes()`, `ZREVRANGE fleet:rankings:2026-08-24 0 4 WITHSCORES`.

## 6. GitHub (DA-2 said this was pending)
```bash
git init && git add . && git commit -m "Initial: db layer, data model, seed"
# then commit per milestone, e.g. redis_layer / queries / indexing / app / tests - one commit per member's module
git branch -M main && git remote add origin <your-repo-url> && git push -u origin main
```

## 7. Troubleshooting
| Symptom | Fix |
|---|---|
| `Database check FAILED` | start Mongo/Redis (`docker compose up -d`), rerun `python check_env.py` |
| Port 5000 busy | `set PORT=5001` then `python app.py` |
| Leaderboard says "MongoDB fallback" | Redis is down - start it, then Health -> *Rebuild Redis* |
| Data looks odd after live edits | Health -> *Reset demo data* (re-seeds the 150 logs) |
| Index Lab shows `n/a` plans | server is not genuine MongoDB (explain format differs); use real mongod/Atlas |
