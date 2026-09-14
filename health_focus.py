#!/usr/bin/env python3
"""health_focus — read an Apple Health export and say, briefly, what to work on.

Apple Health logs dozens of record types; most are noise. This reads only a
curated shortlist of high-signal metrics, compares each recent window to your
own baseline, and (where a defensible public reference exists) flags it
good / watch / focus. Where no universal "good/bad" exists (HRV), it reports
trend only. Observations from your own data + population references — NOT
medical advice. If anything here matters to you, take it to your care team.

The file is multi-GB, so this streams line by line (each <Record> is one line)
and never loads the whole thing. Usage:

    python3 health_focus.py [path/to/export.xml] [--days N]

Default path: ~/Downloads/apple_health_export/export.xml
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, date, timedelta

# ── Curated shortlist (everything else in the export is ignored) ──────────────
DENSE = {  # metrics with enough points for a 30d-vs-prior-90d window
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_hr",
    "HKQuantityTypeIdentifierStepCount": "steps",
    "HKQuantityTypeIdentifierAppleExerciseTime": "exercise",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv",
    "HKCategoryTypeIdentifierSleepAnalysis": "sleep",
}
SPARSE = {  # infrequent readings → wider windows
    "HKQuantityTypeIdentifierVO2Max": "vo2max",
    "HKQuantityTypeIdentifierBodyMass": "weight",
    "HKQuantityTypeIdentifierBodyMassIndex": "bmi",
}
WANTED = {**DENSE, **SPARSE}

# One regex to cheaply pre-filter the ~10M lines down to the ones we care about.
_types_alt = "|".join(re.escape(t) for t in WANTED)
LINE_RE = re.compile(r'type="(' + _types_alt + r')"')
ATTR_RE = {k: re.compile(k + r'="([^"]*)"') for k in ("startDate", "endDate", "value", "unit", "sourceName")}


def union_minutes(intervals):
    """Total minutes covered by a set of (start,end) datetimes, overlaps merged
    once (dedupes multi-source + unspecified/staged double-logging)."""
    ivs = sorted(i for i in intervals if i[0] and i[1] and i[1] > i[0])
    total = 0.0
    cur_s = cur_e = None
    for s, e in ivs:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        elif e > cur_e:
            cur_e = e
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return total / 60.0


def attr(line, name):
    m = ATTR_RE[name].search(line)
    return m.group(1) if m else None


def parse_dt(s):
    # Apple format: "2026-09-14 08:30:00 -0700"
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")
    except (ValueError, TypeError):
        return None


# ── Read the <Me> header for age/sex (needed for VO2max banding) ──────────────
def read_me(path):
    dob, sex = None, None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if "<Me " in line or "HKCharacteristicTypeIdentifier" in line:
                m = re.search(r'DateOfBirth="([^"]*)"', line)
                if m and m.group(1):
                    dob = m.group(1)[:10]
                m = re.search(r'BiologicalSex="HKBiologicalSex([^"]*)"', line)
                if m:
                    sex = m.group(1).lower()  # male / female / other
            if i > 1000 or "<Record " in line:
                break
    age = None
    if dob:
        try:
            b = datetime.strptime(dob, "%Y-%m-%d").date()
            age = (date.today() - b).days // 365
        except ValueError:
            pass
    return age, sex


# ── Stream the file, keeping only the shortlist ───────────────────────────────
def parse(path):
    points = defaultdict(list)          # metric -> [(date, value)] point readings
    # steps/exercise: metric -> day -> source -> summed value (dedupe by MAX across
    # sources per day, since iPhone + Watch each count ~the whole day).
    daily = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    sleep_iv = defaultdict(list)        # night(date) -> [(start,end)] asleep segments
    inbed_iv = defaultdict(list)        # fallback if no staged/asleep data exists
    weight_unit = {"unit": "kg"}
    n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            n += 1
            if n % 2_000_000 == 0:
                print(f"  …scanned {n:,} lines", file=sys.stderr)
            m = LINE_RE.search(line)
            if not m:
                continue
            metric = WANTED[m.group(1)]
            sd = attr(line, "startDate")
            if not sd:
                continue
            day = sd[:10]

            if metric == "sleep":
                val = attr(line, "value") or ""
                s, e = parse_dt(sd), parse_dt(attr(line, "endDate"))
                if not (s and e) or e <= s:
                    continue
                night = e.date().isoformat()  # attribute to wake day
                if "Asleep" in val:
                    sleep_iv[night].append((s, e))
                elif val.endswith("InBed"):
                    inbed_iv[night].append((s, e))
                continue

            v = attr(line, "value")
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue

            if metric in ("steps", "exercise"):
                src = attr(line, "sourceName") or "?"
                daily[metric][day][src] += v       # per-source daily sum (deduped later)
            else:
                points[metric].append((day, v))    # point-in-time readings
                if metric == "weight":
                    u = attr(line, "unit")
                    if u:
                        weight_unit["unit"] = u

    return points, daily, sleep_iv, inbed_iv, weight_unit["unit"], n


# ── Windowing helpers ─────────────────────────────────────────────────────────
def _latest(*day_iterables):
    days = [d for it in day_iterables for d in it]
    return max(days) if days else None


def window_avg(pairs, latest, recent_days, base_days):
    """pairs: list of (isodate, value). Returns (recent_mean, base_mean, n_recent)."""
    if not pairs:
        return None, None, 0
    rc = latest - timedelta(days=recent_days)
    bc = latest - timedelta(days=recent_days + base_days)
    rec = [v for d, v in pairs if date.fromisoformat(d) > rc]
    base = [v for d, v in pairs if bc < date.fromisoformat(d) <= rc]
    rmean = sum(rec) / len(rec) if rec else None
    bmean = sum(base) / len(base) if base else None
    return rmean, bmean, len(rec)


# ── VO2max reference bands (mL/kg/min), approx ACSM/Cooper norms ───────────────
VO2_BANDS = {  # (good_at_or_above, low_below) by sex + age bracket
    "male":   [(29, 48, 38), (39, 44, 34), (49, 42, 31), (59, 38, 28), (200, 35, 25)],
    "female": [(29, 42, 32), (39, 40, 30), (49, 37, 27), (59, 33, 23), (200, 30, 21)],
}


def vo2_band(age, sex):
    table = VO2_BANDS.get(sex)
    if not table or age is None:
        return None
    for ceil, good, low in table:
        if age <= ceil:
            return good, low
    return None


# ── Evaluate each metric → (rank, status, title, lines[]) ──────────────────────
# rank: 0 focus, 1 watch, 2 good, 3 info
def build_findings(points, daily, sleep_iv, inbed_iv, wunit, age, sex):
    latest = _latest(
        (d for d, _ in sum(points.values(), [])),
        *(dd.keys() for dd in daily.values()),
        sleep_iv.keys(), inbed_iv.keys(),
    )
    if latest is None:
        return [], None
    latest = date.fromisoformat(latest)
    F = []

    def add(rank, status, title, *lines):
        F.append((rank, status, title, list(lines)))

    # steps (per-day = MAX across sources, then avg/day)
    sp = [(d, max(sm.values())) for d, sm in daily["steps"].items()]
    r, b, nr = window_avg(sp, latest, 30, 90)
    if r is not None:
        trend = f" (was {b:,.0f}/day)" if b else ""
        if r >= 7500:
            add(2, "good", "Steps", f"{r:,.0f}/day{trend} — at/above the ~7.5k linked to lower mortality.")
        elif r >= 5000:
            add(1, "watch", "Steps", f"{r:,.0f}/day{trend} — below ~7.5k; nudging up has outsized payoff here.")
        else:
            add(0, "focus", "Steps", f"{r:,.0f}/day{trend} — in the sedentary range (<5k). Biggest, easiest lever.")

    # exercise minutes (per-day = MAX across sources → weekly avg)
    ex = [(d, max(sm.values())) for d, sm in daily["exercise"].items()]
    r, b, nr = window_avg(ex, latest, 30, 90)
    if r is not None:
        wk = r * 7
        bwk = f" (was {b*7:.0f})" if b else ""
        if wk >= 150:
            add(2, "good", "Exercise", f"~{wk:.0f} min/week{bwk} — meets the WHO 150–300 min guideline.")
        elif wk >= 90:
            add(1, "watch", "Exercise", f"~{wk:.0f} min/week{bwk} — under the WHO 150 min target.")
        else:
            add(0, "focus", "Exercise", f"~{wk:.0f} min/week{bwk} — well under 150 min/week.")

    # resting heart rate
    r, b, nr = window_avg(points["resting_hr"], latest, 30, 90)
    if r is not None:
        delta = (r - b) if b else 0
        arrow = f" ({'↑' if delta>0 else '↓'}{abs(delta):.0f} vs prior 90d)" if b else ""
        if b and delta >= 4:
            add(1, "watch", "Resting HR", f"{r:.0f} bpm{arrow} — a multi-week rise is worth noticing (sleep, stress, training load, illness).")
        elif r <= 70:
            add(2, "good", "Resting HR", f"{r:.0f} bpm{arrow} — comfortably in a healthy range.")
        else:
            add(2, "good", "Resting HR", f"{r:.0f} bpm{arrow}.")

    # sleep (per-night asleep hours = union of segments; fall back to in-bed)
    src = sleep_iv if sleep_iv else inbed_iv
    label = "asleep" if sleep_iv else "in bed"
    sl = [(d, union_minutes(ivs) / 60.0) for d, ivs in src.items()]
    r, b, nr = window_avg(sl, latest, 30, 90)
    if r is not None:
        bt = f" (was {b:.1f}h)" if b else ""
        if r >= 7:
            add(2, "good", "Sleep", f"{r:.1f}h/night {label}{bt} — within the 7–9h range.")
        elif r >= 6:
            add(1, "watch", "Sleep", f"{r:.1f}h/night {label}{bt} — a bit under 7h.")
        else:
            add(0, "focus", "Sleep", f"{r:.1f}h/night {label}{bt} — under 6h; high-impact to fix.")

    # VO2max (sparse → wider windows + age/sex band)
    r, b, nr = window_avg(points["vo2max"], latest, 120, 240)
    if r is not None:
        band = vo2_band(age, sex)
        drift = f" ({'↑' if (b and r>=b) else '↓'}{abs(r-b):.1f} vs prior)" if b else ""
        if band:
            good, low = band
            where = "above" if r >= good else ("below" if r < low else "around")
            status = 2 if r >= good else (0 if r < low else 1)
            rank = {2: 2, 1: 1, 0: 0}[status]
            sname = {2: "good", 1: "watch", 0: "focus"}[status]
            add(rank, sname, "VO₂max (cardio fitness)",
                f"{r:.1f} mL/kg/min{drift} — {where} typical for a {age}yo {sex} "
                f"(≈{good}+ good, <{low} low). One of the strongest longevity signals.")
        else:
            add(3, "info", "VO₂max (cardio fitness)", f"{r:.1f} mL/kg/min{drift} — trend only (age/sex unknown for banding).")

    # weight / BMI — trend-only context (no prescriptive target here)
    r, b, nr = window_avg(points["weight"], latest, 120, 240)
    if r is not None:
        drift = f" ({'↑' if (b and r>=b) else '↓'}{abs(r-b):.1f}{wunit} vs prior)" if b else ""
        bmi = ""
        rb, bb, _ = window_avg(points["bmi"], latest, 120, 240)
        if rb is not None:
            bmi = f", BMI ~{rb:.1f}"
        add(3, "info", "Weight", f"{r:.1f}{wunit}{drift}{bmi} — trend only.")

    # HRV — explicitly trend-only (no universal good/bad)
    r, b, nr = window_avg(points["hrv"], latest, 30, 90)
    if r is not None:
        drift = f" ({'↑' if (b and r>=b) else '↓'}{abs(r-b):.0f} vs prior 90d)" if b else ""
        add(3, "info", "HRV (SDNN)", f"{r:.0f} ms{drift} — no universal good/bad; watch your own trend, not the number.")

    F.sort(key=lambda x: x[0])
    return F, latest


def render(findings, latest, age, sex, path):
    out = []
    who = f"{age}yo {sex}" if age and sex else "unknown age/sex"
    out.append("=" * 68)
    out.append("  APPLE HEALTH — what to work on")
    out.append(f"  data through {latest} · {who}")
    out.append("=" * 68)
    if not findings:
        out.append("No shortlist metrics found in the export.")
    focus = [f for f in findings if f[1] in ("focus", "watch")]
    good = [f for f in findings if f[1] == "good"]
    info = [f for f in findings if f[1] == "info"]
    icon = {"focus": "🔴", "watch": "🟡", "good": "🟢", "info": "•"}
    if focus:
        out.append("\n🎯 FOCUS / WATCH")
        for _, st, title, lines in focus:
            out.append(f"  {icon[st]} {title}")
            for ln in lines:
                out.append(f"       {ln}")
    if good:
        out.append("\n✅ DOING WELL")
        for _, st, title, lines in good:
            out.append(f"  {icon[st]} {title}: {lines[0]}")
    if info:
        out.append("\nℹ️  TREND-ONLY / CONTEXT")
        for _, st, title, lines in info:
            out.append(f"  • {title}: {lines[0]}")
    out.append("\n" + "-" * 68)
    out.append("Observations from your own data vs. public population references —")
    out.append("NOT medical advice, and not personalized targets. Anything glucose/")
    out.append("insulin-related is your diabetes care team's call.")
    text = "\n".join(out) + "\n"
    print(text)
    with open(path, "w") as fh:
        fh.write(text)
    print(f"(saved to {path})", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Apple Health: what to work on")
    ap.add_argument("export", nargs="?",
                    default=os.path.expanduser("~/Downloads/apple_health_export/export.xml"))
    args = ap.parse_args()
    if not os.path.isfile(args.export):
        sys.exit(f"export.xml not found: {args.export}")

    age, sex = read_me(args.export)
    print(f"Parsing {args.export} (this is a big file; streaming)…", file=sys.stderr)
    points, daily, sleep_iv, inbed_iv, wunit, n = parse(args.export)
    print(f"  done — scanned {n:,} lines.", file=sys.stderr)
    findings, latest = build_findings(points, daily, sleep_iv, inbed_iv, wunit, age, sex)
    report = os.path.join(os.path.dirname(os.path.abspath(__file__)), "health_report.txt")
    render(findings, latest, age, sex, report)


if __name__ == "__main__":
    main()
