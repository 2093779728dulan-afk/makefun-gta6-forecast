import datetime as dt
import json
import math
import os
from pathlib import Path

Z_SCORES = (-1.645, -0.674, 0.0, 0.674, 1.645)
HOURS = range(2, 25)


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return moment


def weighted_median(items):
    ordered = sorted(items)
    total = sum(weight for _, weight in ordered)
    if total <= 0:
        return 0.0
    accumulated = 0.0
    for value, weight in ordered:
        accumulated += weight
        if accumulated >= total / 2:
            return value
    return ordered[-1][0]


def comparable_profile(comparable):
    start = parse_time(comparable.get("day_start"))
    if start is None:
        return None
    release_date = start.date()
    levels = {}
    for row in comparable.get("hourly") or []:
        moment = parse_time(row.get("from"))
        if moment is None or moment.date() != release_date:
            continue
        hour = moment.hour + 1
        viewers = row.get("viewers")
        if 1 <= hour <= 24 and isinstance(viewers, (int, float)) and viewers > 0:
            levels[hour] = float(viewers)
    if 1 not in levels:
        return None
    first_hour = levels[1]
    ratios = {hour: math.log(viewers / first_hour) for hour, viewers in levels.items()}
    return first_hour, ratios


def comparable_weight(comparable, first_hour, case_print, case_date):
    platforms = str(comparable.get("platforms") or "").lower()
    if any(name in platforms for name in ("playstation 5", "ps5", "ps/5", "ps4/5", "ps4/ps5")):
        platform_weight = 3.0
    elif any(name in platforms for name in ("playstation", "ps4", "xbox", "switch", "ps,", "ps/")):
        platform_weight = 2.0
    else:
        platform_weight = 1.0
    start = parse_time(comparable.get("day_start"))
    age = (start.date() - case_date).days if start is not None else 0
    recency_weight = math.exp(max(-4000, min(4000, age)) / 1000.0)
    closeness_weight = math.exp(-abs(math.log(first_hour / case_print)) / 1.5)
    return platform_weight * recency_weight * closeness_weight


def make_forecast(case):
    known = case.get("known") or []
    case_print = 1.0
    if known:
        viewers = known[0].get("viewers")
        if isinstance(viewers, (int, float)) and viewers > 0:
            case_print = float(viewers)
    start = parse_time(case.get("day_start"))
    case_date = start.date() if start is not None else dt.date(1970, 1, 1)

    series = []
    for comparable in case.get("comparables") or []:
        profile = comparable_profile(comparable)
        if profile is None:
            continue
        first_hour, ratios = profile
        weight = comparable_weight(comparable, first_hour, case_print, case_date)
        if weight > 0:
            series.append((weight, ratios))

    rows = []
    previous_median = 0.0
    for hour in HOURS:
        items = [(ratios[hour], weight) for weight, ratios in series if hour in ratios]
        if items:
            median = weighted_median(items)
            total = sum(weight for _, weight in items)
            mean = sum(value * weight for value, weight in items) / total
            variance = sum(weight * (value - mean) ** 2 for value, weight in items) / total
            dispersion = math.sqrt(variance)
        else:
            median = previous_median
            dispersion = 0.0
        previous_median = median
        spread = max(dispersion, 0.15) * (1.0 + 0.01 * (hour - 2))
        quantiles = []
        for z_score in Z_SCORES:
            value = case_print * math.exp(median + z_score * spread)
            quantiles.append(round(min(1e9, max(0.01, value)), 2))
        rows.append({"hour": hour, "q": quantiles})
    return {"hours": rows}


def main():
    input_dir = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
    output_dir = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(input_dir.glob("*.in")):
        with source.open("r", encoding="utf-8") as handle:
            case = json.load(handle)
        result = make_forecast(case)
        destination = output_dir / (str(case.get("id", source.stem)) + ".out")
        with destination.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, separators=(",", ":"))


if __name__ == "__main__":
    main()
