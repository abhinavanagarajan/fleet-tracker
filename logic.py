"""Pure business rules (no DB access): day-type detection, mileage, margin, baseline evaluation.
Shared by the seeder, the web form and the CRUD service so every document is computed identically.
"""
from datetime import date as _date

DAY_FACTORS_DEFAULT = {"WEEKDAY": 1.0, "SUNDAY": 1.4, "HOLIDAY": 1.3}
ABOVE_AT = 1.05        # collection >= baseline * 1.05  -> ABOVE_AVERAGE
BELOW_AT = 0.95        # collection <= baseline * 0.95  -> BELOW_AVERAGE
MILEAGE_GOOD_AT = 1.0  # kmpl >= baseline               -> GOOD
MILEAGE_OK_AT = 0.95   # kmpl >= 95% of baseline        -> AVERAGE, else LOW


def day_type_for(date_str: str, holiday: bool = False) -> str:
    """Objective 4: Sundays / holidays are judged against their own baseline, not the weekday one."""
    if holiday:
        return "HOLIDAY"
    return "SUNDAY" if _date.fromisoformat(date_str).weekday() == 6 else "WEEKDAY"


def log_id(master: dict, date_str: str) -> str:
    return f"LOG_{master['registration_no'].replace('-', '')}_{date_str.replace('-', '')}"


def norm_single(s: dict, no: int) -> dict:
    return {
        "single_no": no,
        "trip_code": str(s.get("trip_code") or f"S{no}_{'UP' if no % 2 else 'DN'}"),
        "departure_time": str(s.get("departure_time") or ""),
        "collection_inr": round(float(s.get("collection_inr", 0)), 2),
        "passenger_count": int(s.get("passenger_count") or 0),
    }


def evaluate(master: dict, day_type: str, gross: float, mileage) -> dict:
    factors = master.get("day_type_factors") or DAY_FACTORS_DEFAULT
    base = round(master["baseline_avg_collection"] * factors.get(day_type, 1.0), 2)
    if gross >= base * ABOVE_AT:
        cstat = "ABOVE_AVERAGE"
    elif gross <= base * BELOW_AT:
        cstat = "BELOW_AVERAGE"
    else:
        cstat = "AVERAGE"
    mb = master["baseline_target_kmpl"]
    if mileage is None:
        mstat = "UNKNOWN"
    elif mileage >= mb * MILEAGE_GOOD_AT:
        mstat = "GOOD"
    elif mileage >= mb * MILEAGE_OK_AT:
        mstat = "AVERAGE"
    else:
        mstat = "LOW"
    return {"baseline_avg_collection": base, "collection_status": cstat,
            "mileage_baseline_kmpl": mb, "mileage_status": mstat}


def summarize(master: dict, day_type: str, singles: list, odometer: dict, fuel: dict, other_expense: float = 0.0):
    """-> (computed_summary, performance_evaluation)"""
    gross = round(sum(s["collection_inr"] for s in singles), 2)
    fuel_cost = round(fuel["litres"] * fuel["fuel_rate_per_litre"], 2)
    fuel["total_fuel_cost"] = fuel_cost
    expense = round(fuel_cost + other_expense, 2)
    km = odometer["total_km"]
    mileage = round(km / fuel["litres"], 2) if fuel["litres"] > 0 else None
    cs = {"gross_collection_inr": gross, "total_expense_inr": expense,
          "net_margin_inr": round(gross - expense, 2), "mileage_kmpl": mileage}
    return cs, evaluate(master, day_type, gross, mileage)


def build_log(master: dict, date_str: str, start_km: int, end_km: int, singles: list,
              litres: float, rate: float, bunk: str, other_expense: float = 0.0, holiday: bool = False) -> dict:
    singles = [norm_single(s, i + 1) for i, s in enumerate(singles)]
    day_type = day_type_for(date_str, holiday)
    odometer = {"start_km": int(start_km), "end_km": int(end_km), "total_km": int(end_km) - int(start_km)}
    fuel = {"litres": round(float(litres), 2), "fuel_rate_per_litre": round(float(rate), 2),
            "total_fuel_cost": 0.0, "bunk_name": bunk or master.get("default_bunk", "")}
    cs, pe = summarize(master, day_type, singles, odometer, fuel, float(other_expense))
    return {
        "_id": log_id(master, date_str), "bus_id": master["bus_id"],
        "registration_no": master["registration_no"], "route_id": master["route_id"],
        "date": date_str, "day_type": day_type, "odometer": odometer, "singles": singles,
        "fuel_fill": fuel, "other_expense_inr": round(float(other_expense), 2),
        "computed_summary": cs, "performance_evaluation": pe,
    }


def validate_shift(p: dict) -> list:
    """Returns a list of human-readable problems (empty = OK)."""
    errs = []
    try:
        _date.fromisoformat(p.get("date", ""))
    except ValueError:
        errs.append("Date must be YYYY-MM-DD.")
    try:
        if int(p["end_km"]) <= int(p["start_km"]):
            errs.append("Odometer end must be greater than start.")
    except (KeyError, TypeError, ValueError):
        errs.append("Odometer start/end must be whole numbers.")
    try:
        if float(p["litres"]) <= 0 or float(p["rate"]) <= 0:
            errs.append("Fuel litres and rate must be positive.")
    except (KeyError, TypeError, ValueError):
        errs.append("Fuel litres and rate must be numbers.")
    if not p.get("singles"):
        errs.append("Add at least one single (trip) with a collection.")
    for s in p.get("singles", []):
        if float(s.get("collection_inr", 0)) < 0:
            errs.append("Collections cannot be negative.")
            break
    return errs
