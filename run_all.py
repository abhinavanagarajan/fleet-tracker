"""One reproducible end-to-end run -> writes the evidence files into output/.     (owner: Vishnu Swaroop G)

    python run_all.py          # seed, CRUD, 12 queries, indexing, Redis<->Mongo audit  (+ PASS/FAIL summary)
"""
import contextlib
import io
import os
import sys

import crud_demo
import generate_data
import indexing_demo
import queries_demo
import redis_layer as rl
from db import health, require_dbs

OUT = "output"


def capture(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a)
    return buf.getvalue()


def main():
    require_dbs()
    os.makedirs(OUT, exist_ok=True)
    checks, log = [], []

    def section(title, text, fname):
        print(text)
        open(f"{OUT}/{fname}", "w", encoding="utf-8").write(text)
        log.append(f"\n{'#' * 78}\n# {title}\n{'#' * 78}\n{text}")

    h = health()
    section("0. ENVIRONMENT", f"MongoDB {h['mongo']['version']} | Redis {h['redis']['version']} | db={h['mongo']['db']}\n", "00_environment.txt")
    section("1. SEED", capture(generate_data.seed), "01_seed_output.txt")
    db = generate_data.get_db()
    checks += [("buses_master has 5 docs", db.buses_master.count_documents({}) == 5),
               ("daily_bus_logs has 150 docs", db.daily_bus_logs.count_documents({}) == 150),
               ("5 indexes exist", all(i["exists"] for i in indexing_demo.list_indexes(db))),
               ("Redis consistent after seed", rl.audit()["ok"])]
    section("2. CRUD", capture(crud_demo.main), "02_crud_output.txt")
    checks.append(("Redis consistent after CRUD", rl.audit()["ok"]))

    def q_all():
        for q in queries_demo.QUERIES:
            queries_demo.print_result(queries_demo.run_query(q["id"]))
    section("3. QUERIES (Q1-Q12)", capture(q_all), "03_queries_output.txt")
    checks += [(f"{q['id']} runs without error", queries_demo.run_query(q["id"])["error"] is None) for q in queries_demo.QUERIES]
    section("4. INDEXING", capture(indexing_demo.main), "04_indexing_output.txt")

    summary = "\n".join(f"  [{'PASS' if ok else 'FAIL'}] {name}" for name, ok in checks)
    section("5. SELF-CHECK", summary + "\n", "05_selfcheck.txt")
    open(f"{OUT}/full_run_output.txt", "w", encoding="utf-8").write("\n".join(log))
    bad = [n for n, ok in checks if not ok]
    print(f"\n{'ALL CHECKS PASSED' if not bad else 'FAILED: ' + ', '.join(bad)}  -> evidence in ./{OUT}/")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
