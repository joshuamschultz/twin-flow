# RM-03 calendar and resource guide

## Labor calendars

Add a `calendar` to a labor pool. Models without one remain always available.

```yaml
labor:
  pools:
    - name: machinists
      headcount: 2
      skills: [mill]
      calendar:
        timezone: America/Chicago
        origin: "2026-03-02T07:00:00-06:00"
        weekly:
          - {days: [mon, tue, wed, thu, fri], start: "07:00", end: "15:30"}
        exceptions:
          - {date: "2026-03-09", closed: true}
          - {date: "2026-03-10", start: "09:00", end: "17:00"}
```

`origin` is the timezone-aware instant represented by simulation second zero. Weekday
names are `mon` through `sun`. An end time at or before the start denotes an overnight
interval. Closed exceptions remove all work on a date; a start/end exception replaces
that date's recurring intervals. DST conversion uses elapsed UTC time, so a local
00:00–04:00 shift on a spring-forward Sunday contains three elapsed hours.

## Shift crossing

Each location may set one policy:

- `pause`: setup, load, run, and unload consume only working calendar time. The machine
  and job remain occupied between shifts.
- `finish_unattended`: setup and load require on-shift labor; processing continues
  through shift end; unload waits for the next shift. Labor attendance excludes the
  unattended run phase.
- `overtime`: once labor is acquired, work proceeds continuously. This is the default
  and preserves existing model behavior.

```yaml
locations:
  - name: mill
    machine: mill
    capacity: 2
    labor_skill: mill
    shift_crossing: pause
```

## Machine identity and evidence

`capacity: 2` expands machine `mill` into runtime identities `mill-1` and `mill-2`.
Capacity one retains `mill`. Every machine owns its setup state, so both cold machines
pay setup independently and later work reuses only the acquired machine's setup.

Every run writes `resource_usage.parquet` beside `events.parquet`. Its rows contain
location, machine, labor pool and skill, setup/run/release timestamps, nominal setup
and run seconds, and attended labor seconds. `KpiEngine` automatically uses this file
for per-machine hours and capacity-adjusted cell utilization when given a single run's
event path.

Run the included example:

```bash
twinflow run examples/hmlv-calendar/model.yaml \
  --plan examples/hmlv-calendar/plan.csv --reps 1
```

## Current limits of the legacy manufacturing DES

Calendar exceptions replace one whole local date; multiple replacement intervals per
exception date are not yet supported. Paused work holds a machine while off shift and
does not model an explicit safely parked/released machine. Breakdowns still act on the
location capacity pool rather than a named machine. Machine speed, maintenance
calendars, tooling, expiring qualifications, multi-resource atomic acquisition,
split/merge genealogy, route revisions, and outside processing are not modeled by this
configuration-driven DES. The separate typed production API covers multi-resource phase
scheduling, lot genealogy, qualifications, and outside processing; see the
[production capability guide](../../production/README.md). Those capabilities are
not automatically available through this legacy workspace adapter.

The result is a mechanics foundation. Credible customer promise dates still require
customer calendars, snapshot quality, calibration, and operational validation.
