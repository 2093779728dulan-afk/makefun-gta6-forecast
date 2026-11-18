import datetime as dt
import json
import math
import os
import re
from pathlib import Path

Z_SCORES = (-1.645, -0.674, 0.0, 0.674, 1.645)
HOURS = tuple(range(2, 25))
MIN_SPREAD = 0.15
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?", re.IGNORECASE)
TIMESTAMP_HOUR_RE = re.compile(r"\d{4}-\d{2}-\d{2}T(\d{2}):\d{2}(?::\d{2})?", re.IGNORECASE)
CLOCK_RE = re.compile(r"(?<![\d:T])(\d{1,2}):(\d{2})\s*(?:z|utc|gmt)\b", re.IGNORECASE)
ROLLING_OFFSET = -13
GTA6_CLOCKS = (11, 23, 0, 5, 8)


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


def unlock_offset(text, day_start):
    if day_start is None:
        return None
    source = str(text or "")
    offsets = []
    for match in TIMESTAMP_RE.finditer(source):
        moment = parse_time(match.group(0))
        if moment is None:
            continue
        delta = (moment - day_start).total_seconds() / 3600.0
        if -36.0 <= delta <= 26.0:
            offsets.append(round(delta))
    if offsets:
        return min(offsets)
    lower = source.lower()
    if "rolling" in lower or "midnight local" in lower or "local time" in lower:
        return ROLLING_OFFSET
    for match in CLOCK_RE.finditer(source):
        hour = int(match.group(1))
        if not 0 <= hour <= 23:
            continue
        prefix = source[max(0, match.start() - 48):match.start()].lower()
        delta = hour + int(match.group(2)) / 60.0
        if "nz" in prefix or "new zealand" in prefix:
            delta -= 24.0
        if -36.0 <= delta <= 26.0:
            offsets.append(round(delta))
    return min(offsets) if offsets else 0


def case_unlock_offset(case, day_start):
    notes = str(case.get("notes") or "")
    if "day_start is" in notes:
        notes = notes.split("day_start is")[0]
    offset = unlock_offset(notes, day_start)
    game_name = str((case.get("game") or {}).get("name") or "").lower()
    if "grand theft auto vi" in game_name and offset == 0:
        return ROLLING_OFFSET
    return offset


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


def unlock_hours(text):
    hours = set()
    for match in TIMESTAMP_HOUR_RE.finditer(str(text or "")):
        hours.add(int(match.group(1)))
    for match in CLOCK_RE.finditer(str(text or "")):
        hour = int(match.group(1))
        if 0 <= hour <= 23:
            hours.add(hour)
    return hours


def unlock_features(text):
    text = str(text or "").lower()
    tier_match = re.search(r"tier:\s*([a-z-]+)", text)
    return {
        "hours": unlock_hours(text),
        "rolling": "rolling" in text or "midnight local" in text or "local time" in text,
        "simultaneous": "simultaneous" in text or "single global" in text or "global unlock" in text,
        "tier": tier_match.group(1) if tier_match else "comparable",
    }


def case_unlock_features(case):
    notes = str(case.get("notes") or "")
    if "day_start is" in notes:
        notes = notes.split("day_start is")[0]
    features = unlock_features(notes)
    game_name = str((case.get("game") or {}).get("name") or "").lower()
    if "grand theft auto vi" in game_name:
        if not features["hours"]:
            features["hours"] = set(GTA6_CLOCKS)
        features["rolling"] = True
        features["simultaneous"] = False
    return features


def hour_distance(left, right):
    delta = abs(left - right)
    return min(delta, 24 - delta)


def unlock_similarity(comparable, case):
    similarity = 1.0
    if comparable["rolling"] == case["rolling"]:
        similarity *= 1.15
    else:
        similarity *= 0.75
    if comparable["simultaneous"] == case["simultaneous"]:
        similarity *= 1.1
    else:
        similarity *= 0.85
    if comparable["tier"] == "weak":
        similarity *= 0.75
    elif comparable["tier"] == "contaminated":
        similarity *= 0.6
    if comparable["hours"] and case["hours"]:
        distances = [
            min(hour_distance(hour, unlock) for unlock in sorted(comparable["hours"]))
            for hour in sorted(case["hours"])
        ]
        match = sum(math.exp(-distance / 6.0) for distance in distances) / len(distances)
        similarity *= 0.5 + 1.5 * match
    return max(0.3, min(2.5, similarity))


def comparable_curve(comparable):
    start = parse_time(comparable.get("day_start"))
    if start is None:
        return None
    unlock = str(comparable.get("unlock") or "")
    lower = unlock.lower()
    if "not a launch" in lower or "target-history" in lower:
        return None
    anchor = unlock_offset(unlock, start)
    if anchor is None:
        return None
    curve = {}
    for row in comparable.get("hourly") or []:
        moment = parse_time(row.get("from"))
        viewers = row.get("viewers")
        if moment is None or not isinstance(viewers, (int, float)) or viewers <= 0:
            continue
        delta = (moment - start).total_seconds() / 3600.0
        if -720.0 <= delta <= 720.0:
            curve[round(delta) - anchor] = math.log(float(viewers))
    if len(curve) < 2:
        return None
    return curve


def comparable_weight(comparable, first_hour, case_print, case_date, similarity):
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
    return platform_weight * recency_weight * closeness_weight * similarity


def curve_value(curve, hour):
    if not curve:
        return None
    if hour in curve:
        return curve[hour]
    lower = max((value for value in curve if value <= hour), default=None)
    upper = min((value for value in curve if value >= hour), default=None)
    if lower is None:
        return curve[upper]
    if upper is None:
        return curve[lower]
    if lower == upper:
        return curve[lower]
    return curve[lower] + (curve[upper] - curve[lower]) * (hour - lower) / (upper - lower)


def make_forecast(case):
    known = case.get("known") or []
    case_print = 1.0
    if known:
        viewers = known[0].get("viewers")
        if isinstance(viewers, (int, float)) and viewers > 0:
            case_print = float(viewers)
    start = parse_time(case.get("day_start"))
    case_date = start.date() if start is not None else dt.date(1970, 1, 1)
    case_anchor = case_unlock_offset(case, start)
    reference_offset = -case_anchor
    case_features = case_unlock_features(case)

    series = []
    for comparable in case.get("comparables") or []:
        curve = comparable_curve(comparable)
        if curve is None:
            continue
        reference = curve_value(curve, reference_offset)
        if reference is None:
            continue
        unlock = str(comparable.get("unlock") or "")
        similarity = unlock_similarity(unlock_features(unlock), case_features)
        weight = comparable_weight(comparable, math.exp(reference), case_print, case_date, similarity)
        if weight > 0:
            series.append((weight, curve, reference))

    rows = []
    for hour in HOURS:
        offset = reference_offset + hour - 1
        items = []
        for weight, curve, reference in series:
            value = curve_value(curve, offset)
            if value is not None:
                items.append((value - reference, weight))
        if items:
            ratio = weighted_median(items)
            total = sum(weight for _, weight in items)
            mean = sum(value * weight for value, weight in items) / total
            variance = sum(weight * (value - mean) ** 2 for value, weight in items) / total
            spread = max(MIN_SPREAD, math.sqrt(variance))
        else:
            ratio = 0.0
            spread = MIN_SPREAD
        ratio = max(-2.0, min(3.0, ratio))
        quantiles = []
        for z_score in Z_SCORES:
            value = case_print * math.exp(ratio + z_score * spread)
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
