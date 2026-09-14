# health-focus

Reads an Apple Health export and says, briefly, **what to work on** — cutting a
multi-GB firehose of records down to a curated shortlist of high-signal metrics,
each compared to your own baseline and (where a defensible public reference
exists) flagged good / watch / focus.

> Observations from your own data vs. public population references — **not
> medical advice**, and not personalized targets. Anything glucose/insulin
> related is your diabetes care team's call.

## Why

Apple Health logs dozens of record types; most are noise, and it's not obvious
what's good or bad. This ignores the firehose and reports only the vital few.

## The shortlist

**Has a public reference → good / watch / focus:**
- **VO₂max** (cardio fitness) — age/sex percentile bands (approx ACSM/Cooper); strongest longevity signal
- **Resting heart rate** — healthy range + multi-week rise as a flag
- **Steps/day** — ~7.5k linked to lower mortality
- **Exercise minutes** — WHO 150–300 min/week
- **Sleep duration** — 7–9h

**Trend-only (no universal good/bad):**
- **HRV (SDNN)** — hugely individual; your own trend, not the number
- **Weight / BMI** — trend + context (BMI caveats)

Everything else in the export is deliberately ignored. Blood glucose / insulin
are intentionally left out — that's a care-team-calibrated, greenfield lane.

## Usage

```bash
# Health app → your profile → Export All Health Data → unzip
python3 health_focus.py [path/to/export.xml]      # default: ~/Downloads/apple_health_export/export.xml
```

Prints a ranked report and writes `health_report.txt` (gitignored).

## Notes

- **Streams the file.** Exports run to several GB / tens of millions of lines;
  this scans line by line (each `<Record>` is one line) and never loads it all.
- **De-dupes multi-source data.** iPhone + Apple Watch + apps log the same steps
  and sleep; a naive sum double/triple-counts. Steps/exercise take the **max
  across sources** per day; sleep uses an **interval union** per night.
- **Local-only.** Health data is about as sensitive as it gets. `export.xml`,
  `health_report.txt`, and run logs are gitignored and never committed.
