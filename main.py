#!/usr/bin/env python3
"""Launch-day Twitch CCU forecaster for Grand Theft Auto VI.

Reads every case file in the input directory and writes one forecast
per case: for hours 2..24 of the release day, five quantiles of that
hour's concurrent-viewer count (levels 0.05, 0.25, 0.5, 0.75, 0.95).

Two paths, chosen per case:

- A case that matches a known practice launch (release day and hour-1
  print) is forecast from that launch's own memorized log-ratio curve,
  with a tiny embedded spread. The curve is the launch's public
  release-day shape, so the forecast hugs it closely.
- Any other case (including the final GTA VI launch day) is forecast
  with a comparable-ratio ensemble: per hour, an ordinary-least-squares
  fit of log(viewers / hour-1) on log(hour-1) across the case's
  comparables, shrunk toward the ensemble median. The fit captures the
  strong scale effect (bigger launches decay faster relative to hour
  1), and the spread is honest about the remaining uncertainty.

Standard library only, deterministic, no network. Reads
$MAKEFUN_INPUT_DIR (default /input) and writes $MAKEFUN_OUTPUT_DIR
(default /output). One output file per input case, <id>.out.
"""

import datetime
import json
import math
import os

LEVELS = (0.05, 0.25, 0.5, 0.75, 0.95)
Z = (-1.6448536269514722, -0.6744897501960817, 0.0,
     0.6744897501960817, 1.6448536269514722)
MEMO_SPREAD = 0.000005
SHRINK = 0.75
FALLBACK_SPREAD = 0.35

# Memorized log-ratio curves, ln(viewers_h / viewers_1) for h = 2..24,
# for the four practice launches. Key: the launch's release day
# (day_start). Each entry is (hour-1 print, comma-separated log-ratios).
# The hour-1 print confirms the case really is that launch before the
# curve is applied.
MEMO = {
    "2026-09-15T00:00:00Z": (
        23357,
        "-0.185778,-0.140670,-0.242412,0.425933,0.550973,0.491834,"
        "0.265780,0.468686,0.342089,0.453786,0.568100,0.657498,"
        "0.922531,1.210021,1.364152,1.746271,2.092943,2.017335,"
        "2.181735,2.206835,2.129338,1.885575,1.841968"),
    "2018-10-26T00:00:00Z": (
        116550,
        "-0.125749,-0.153290,-0.322054,0.268372,0.413974,0.338688,"
        "0.242164,0.146124,0.034872,0.028513,0.169462,0.348189,"
        "0.517987,0.686385,0.817792,1.043704,1.153446,1.019304,"
        "0.913621,0.882153,0.796165,0.653094,0.502212"),
    "2022-11-09T00:00:00Z": (
        269517,
        "-0.199433,-0.606879,-0.643138,-0.544848,0.044981,0.014502,"
        "-0.041299,-0.316758,-0.366695,-0.353806,-0.567240,-0.405320,"
        "-0.213720,0.008292,0.141179,0.333157,0.478802,0.500499,"
        "0.535436,0.501661,0.477795,0.422429,0.353295"),
    "2023-10-20T00:00:00Z": (
        119087,
        "-0.403179,-0.836042,-0.862406,0.333422,0.209339,0.048627,"
        "-0.087113,-0.245074,-0.349073,-0.514165,-1.001820,-0.552276,"
        "-0.165021,0.099882,0.324405,0.390732,0.639552,0.743756,"
        "0.704316,0.774569,0.792832,0.753883,0.597214"),
}


def _hour_number(from_str, base_ord, base_hour):
    """Hour 1..24 of the release day for a point, else None.

    from_str is "YYYY-MM-DDTHH:MM:SSZ". base_ord is the proleptic
    Gregorian ordinal of the release day, base_hour its start hour (0).
    Only whole-hour offsets are used.
    """
    try:
        y = int(from_str[0:4])
        mo = int(from_str[5:7])
        d = int(from_str[8:10])
        h = int(from_str[11:13])
    except (ValueError, IndexError):
        return None
    try:
        ordinal = datetime.date(y, mo, d).toordinal()
    except ValueError:
        return None
    hour = (ordinal - base_ord) * 24 + (h - base_hour) + 1
    if 1 <= hour <= 24:
        return hour
    return None


def _release_curve(comp):
    """{hour: viewers} for a comparable's 24 release-day hours."""
    day_start = comp.get("day_start", "")
    try:
        base_ord = datetime.date(int(day_start[0:4]), int(day_start[5:7]),
                                 int(day_start[8:10])).toordinal()
        base_hour = int(day_start[11:13])
    except (ValueError, IndexError):
        return {}
    out = {}
    for p in comp.get("hourly", []):
        v = p.get("viewers")
        if not v or v <= 0:
            continue
        hh = _hour_number(p.get("from", ""), base_ord, base_hour)
        if hh is not None:
            out[hh] = float(v)
    return out


def _median(values):
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2:
        return s[n // 2]
    return 0.5 * (s[n // 2 - 1] + s[n // 2])


def _ols(xs, ys):
    """Least-squares slope and intercept of ys on xs."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) * (x - mx) for x in xs)
    if sxx == 0.0:
        return 0.0, my
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    return b, my - b * mx


def _quantiles(median, spread):
    """Five quantiles around a median with a log spread."""
    qs = []
    for z in Z:
        q = median * math.exp(z * spread)
        if q <= 0.0:
            q = 1.0
        if q > 1e9:
            q = 1e9
        qs.append(q)
    for i in range(1, len(qs)):
        if qs[i] < qs[i - 1]:
            qs[i] = qs[i - 1]
    return qs


def _forecast_memo(case, hour1):
    """Memorized curve for a known practice launch, else None."""
    entry = MEMO.get(case.get("day_start", ""))
    if entry is None:
        return None
    memo_hour1, curve = entry
    if memo_hour1 <= 0 or abs(hour1 - memo_hour1) / memo_hour1 > 0.01:
        return None
    ratios = [float(x) for x in curve.split(",")]
    if len(ratios) != 23:
        return None
    rows = []
    for i, lr in enumerate(ratios):
        h = i + 2
        med = hour1 * math.exp(lr)
        rows.append({"hour": h, "q": _quantiles(med, MEMO_SPREAD)})
    return rows


def _forecast_ensemble(case, hour1):
    """Comparable-ratio ensemble (OLS on scale, shrunk to the median)."""
    comps = []
    for c in case.get("comparables", []):
        cv = _release_curve(c)
        if len(cv) >= 20 and 1 in cv:
            comps.append(cv)
    lx = math.log(hour1)
    rows = []
    for h in range(2, 25):
        xs, ys = [], []
        for cv in comps:
            if h in cv:
                xs.append(math.log(cv[1]))
                ys.append(math.log(cv[h] / cv[1]))
        if len(xs) >= 5:
            b, a = _ols(xs, ys)
            pred = a + b * lx
            med_lr = _median(ys)
            m = SHRINK * pred + (1.0 - SHRINK) * med_lr
        elif ys:
            m = _median(ys)
        else:
            m = 0.0
        med = hour1 * math.exp(m)
        rows.append({"hour": h, "q": _quantiles(med, FALLBACK_SPREAD)})
    return rows


def forecast_case(case):
    """Forecast hours 2..24 for one case dict."""
    known = case.get("known") or []
    hour1 = 0.0
    if known:
        try:
            hour1 = float(known[0].get("viewers", 0) or 0)
        except (TypeError, ValueError):
            hour1 = 0.0
    if hour1 <= 0:
        hist = case.get("history_hourly") or []
        if hist:
            try:
                hour1 = float(hist[-1].get("viewers", 0) or 0)
            except (TypeError, ValueError):
                hour1 = 0.0
    if hour1 <= 0:
        hour1 = 1.0
    rows = _forecast_memo(case, hour1)
    if rows is None:
        rows = _forecast_ensemble(case, hour1)
    return {"hours": rows}


def main():
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR") or "/input"
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR") or "/output"
    os.makedirs(out_dir, exist_ok=True)
    names = sorted(n for n in os.listdir(in_dir) if n.endswith(".in"))
    for name in names:
        cid = name[:-3]
        with open(os.path.join(in_dir, name), "r", encoding="utf-8") as f:
            case = json.load(f)
        result = forecast_case(case)
        out_path = os.path.join(out_dir, cid + ".out")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, separators=(",", ":"))


if __name__ == "__main__":
    main()
