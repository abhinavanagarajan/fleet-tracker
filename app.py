"""Flask web application: dashboard, shift submission, CRUD, query console, index lab, Redis explorer.
(owner: Vishnu Swaroop G - application <-> database integration)         run:  python app.py  ->  http://localhost:5000
"""
import json
import os

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

import generate_data
import indexing_demo as ix
import queries_demo as qd
import redis_layer as rl
import service
from db import get_db, health

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fleet-finance-demo")


@app.template_filter("inr")
def inr(v):
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return v


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return e
    name = type(e).__name__
    db_down = any(k in name for k in ("ServerSelection", "AutoReconnect", "ConnectionError", "Timeout", "NetworkTimeout"))
    app.logger.exception("request failed")
    return render_template("error.html", name=name, msg=str(e)[:400], db_down=db_down), 500


def _num(f, key, cast=float, default=None):
    v = (f.get(key) or "").strip()
    if v == "":
        return default
    return cast(v)


# ---------------------------------------------------------------- dashboard
@app.route("/")
def dashboard():
    db = get_db()
    date = request.args.get("date") or service.latest_date()
    lb = service.leaderboard(date, 10)
    summ = service.fleet_summary(date)
    roll = service.rolling_all()
    today = {d["bus_id"]: d for d in db.daily_bus_logs.find({"date": date})}
    rows = []
    for rank, (bus, gross) in enumerate(lb["rows"], 1):
        pe = today.get(bus, {}).get("performance_evaluation", {})
        base = pe.get("baseline_avg_collection")
        rows.append({"rank": rank, "bus": bus, "gross": gross, "status": pe.get("collection_status", "-"),
                     "mileage": pe.get("mileage_status", "-"), "base": base,
                     "delta": round((gross / base - 1) * 100, 1) if base else None})
    alerts = [d for d in today.values() if d["performance_evaluation"]["collection_status"] == "BELOW_AVERAGE"
              or d["performance_evaluation"]["mileage_status"] == "LOW"]
    recent = list(db.daily_bus_logs.find({}, {"bus_id": 1, "date": 1, "day_type": 1, "computed_summary": 1,
                                               "performance_evaluation": 1}).sort([("date", -1), ("bus_id", 1)]).limit(10))
    return render_template("dashboard.html", date=date, lb=lb, rows=rows, summ=summ, roll=roll, alerts=alerts,
                           recent=recent, trace=session.pop("trace", None), last=session.pop("last", None))


# ---------------------------------------------------------------- CREATE (shift submission)
@app.route("/submit", methods=["GET", "POST"])
def submit():
    init = {"singles": [], "form": {}}
    if request.method == "POST":
        f = request.form
        init["form"] = dict(f.items())
        try:
            singles = []
            for tc, dep, col, pax in zip(f.getlist("trip_code"), f.getlist("departure_time"),
                                         f.getlist("collection"), f.getlist("passengers")):
                init["singles"].append({"trip_code": tc, "departure_time": dep, "collection": col, "passengers": pax})
                if col.strip():
                    singles.append({"trip_code": tc.strip() or None, "departure_time": dep, "collection_inr": float(col),
                                    "passenger_count": int(pax or 0)})
            payload = {"bus_id": f.get("bus_id"), "date": f.get("date"), "start_km": _num(f, "start_km", int),
                       "end_km": _num(f, "end_km", int), "singles": singles, "litres": _num(f, "litres"),
                       "rate": _num(f, "rate"), "bunk": f.get("bunk"), "other_expense": _num(f, "other_expense", float, 0),
                       "holiday": f.get("holiday") == "on"}
            res = service.submit_shift(payload)
            session["trace"], session["last"] = res["trace"], res["doc"]["_id"]
            flash(f"{'Inserted' if res['created'] else 'Replaced'} {res['doc']['_id']}"
                  + ("" if res["redis_ok"] else "  - WARNING: Redis out of sync, use 'Rebuild Redis' on Health page"),
                  "ok" if res["redis_ok"] else "err")
            return redirect(url_for("dashboard", date=res["doc"]["date"]))
        except (ValueError, TypeError) as e:
            flash(str(e) if str(e) else "Please check the numbers entered.", "err")
    return render_template("submit.html", buses=service.buses(), init=init, today=service.latest_date())


@app.route("/api/bus/<bus_id>")
def api_bus(bus_id):
    m = service.master(bus_id)
    if not m:
        return jsonify({"error": "unknown bus"}), 404
    return jsonify({**m, "last_odometer": service.last_odometer(bus_id)})


# ---------------------------------------------------------------- READ / UPDATE / DELETE
@app.route("/logs")
def logs():
    a, q = request.args, {}
    if a.get("bus_id"): q["bus_id"] = a["bus_id"]
    if a.get("day_type"): q["day_type"] = a["day_type"]
    if a.get("status"): q["performance_evaluation.collection_status"] = a["status"]
    if a.get("from") or a.get("to"):
        q["date"] = {k: v for k, v in (("$gte", a.get("from")), ("$lte", a.get("to"))) if v}
    docs = list(get_db().daily_bus_logs.find(q, {"singles": 0}).sort([("date", -1), ("bus_id", 1)]).limit(200))
    return render_template("logs.html", docs=docs, buses=service.buses(), a=a, q=json.dumps(q), n=len(docs))


@app.route("/logs/<doc_id>")
def log_detail(doc_id):
    doc = get_db().daily_bus_logs.find_one({"_id": doc_id})
    if not doc:
        flash("Log not found.", "err")
        return redirect(url_for("logs"))
    return render_template("log_detail.html", d=doc, raw=json.dumps(doc, indent=2, default=str),
                           trace=session.pop("trace", None))


def _act(fn, doc_id, ok_msg, back_to_list=False):
    try:
        res = fn()
        session["trace"] = res["trace"]
        flash(ok_msg, "ok" if res["redis_ok"] else "err")
    except (LookupError, ValueError, RuntimeError, TypeError) as e:
        flash(str(e) or "Invalid input", "err")
    return redirect(url_for("logs") if back_to_list else url_for("log_detail", doc_id=doc_id))


@app.post("/logs/<doc_id>/single")
def log_add_single(doc_id):
    f = request.form
    try:
        s = {"trip_code": f.get("trip_code") or None, "departure_time": f.get("departure_time", ""),
             "collection_inr": float(f["collection"]), "passenger_count": int(f.get("passengers") or 0)}
    except (KeyError, ValueError):
        flash("Enter a numeric collection.", "err")
        return redirect(url_for("log_detail", doc_id=doc_id))
    return _act(lambda: service.add_single(doc_id, s), doc_id, "Late single pushed ($push) and totals recomputed ($set).")


@app.post("/logs/<doc_id>/fuel")
def log_set_fuel(doc_id):
    f = request.form
    try:
        litres, rate = float(f["litres"]), float(f["rate"])
        if litres <= 0 or rate <= 0:
            raise ValueError("Litres and rate must be positive.")
    except (KeyError, ValueError) as e:
        flash(str(e) or "Enter numeric litres and rate.", "err")
        return redirect(url_for("log_detail", doc_id=doc_id))
    return _act(lambda: service.set_fuel(doc_id, litres, rate, f.get("bunk")), doc_id, "Fuel fill updated ($set) and totals recomputed.")


@app.post("/logs/<doc_id>/delete")
def log_delete(doc_id):
    return _act(lambda: service.delete_log(doc_id), doc_id, f"Deleted {doc_id}.", back_to_list=True)


# ---------------------------------------------------------------- query console
@app.route("/queries")
def queries():
    return render_template("queries.html", qs=qd.QUERIES, buses=service.buses(), d=qd.DEFAULTS)


@app.route("/api/queries/<qid>")
def api_query(qid):
    if qid not in qd.BY_ID:
        return jsonify({"error": "unknown query"}), 404
    return jsonify(qd.run_query(qid, {k: v for k, v in request.args.items() if v}))


# ---------------------------------------------------------------- indexes
@app.route("/indexes")
def indexes():
    db = get_db()
    return render_template("indexes.html", idx=ix.list_indexes(db), plans=ix.plan_comparison(db))


@app.route("/api/timing")
def api_timing():
    return jsonify(ix.timing(runs=100))


@app.post("/api/bench")
def api_bench():
    return jsonify(ix.bench(int((request.get_json(silent=True) or {}).get("n", 100000))))


# ---------------------------------------------------------------- redis explorer, health, admin
@app.route("/redis")
def redis_explorer():
    key = request.args.get("key")
    t, val = rl.dump_key(key) if key else (None, None)
    return render_template("redis.html", ov=rl.overview(), key=key, ktype=t, val=val)


@app.route("/health")
def health_page():
    return render_template("health.html", h=health(), audit=None)


@app.post("/health/audit")
def health_audit():
    return render_template("health.html", h=health(), audit=rl.audit())


@app.post("/admin/rebuild-redis")
def admin_rebuild():
    flash(f"Redis rebuilt from MongoDB: {rl.rebuild_all()}", "ok")
    return redirect(url_for("health_page"))


@app.post("/admin/reseed")
def admin_reseed():
    s = generate_data.seed(verbose=False)
    flash(f"Demo data reset: {s['buses_master']} buses, {s['daily_bus_logs']} logs, {s['embedded_singles']} singles.", "ok")
    return redirect(url_for("health_page"))


@app.route("/guide")
def guide():
    return render_template("guide.html")


@app.route("/api/leaderboard")
def api_leaderboard():
    return jsonify(service.leaderboard(request.args.get("date") or service.latest_date(), int(request.args.get("n", 5))))


@app.route("/api/summary")
def api_summary():
    return jsonify(service.fleet_summary(request.args.get("date") or service.latest_date()))


if __name__ == "__main__":
    from db import require_dbs
    require_dbs()
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")), debug=False)
