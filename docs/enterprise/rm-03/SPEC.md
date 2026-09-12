# RM-03: HMLV operational fidelity foundations

## Requirements

- RM03-R1: Labor pools may declare an aware origin, IANA timezone, recurring weekly
  working intervals, and dated closed or replacement-day exceptions.
- RM03-R2: Operations declare `shift_crossing` as `pause`, `finish_unattended`, or
  `overtime`. The default is `overtime` for compatibility.
- RM03-R3: A paused operation consumes only working time and resumes at the next valid
  interval across nights, weekends, exceptions, and DST transitions.
- RM03-R4: `capacity: N` expands into N stable machine identities. Each runtime machine
  owns independent setup state; two parallel cold machines each incur their own setup.
- RM03-R5: Resource evidence names the machine, labor pool, skill, setup interval, run
  interval, and release time for every firing without changing ProcessExecution v1.
- RM03-R6: Invalid timezone, origin, day/time, overlapping interval, exception, or
  crossing declarations fail model validation before compilation.
- RM03-R7: Existing models without calendar declarations retain always-available labor
  and their existing execution behavior.

## Public configuration

```yaml
labor:
  pools:
    - name: machinists
      headcount: 2
      skills: [mill]
      calendar:
        timezone: America/Chicago
        origin: 2026-03-06T00:00:00-06:00
        weekly:
          - {days: [mon, tue, wed, thu, fri], start: "07:00", end: "15:30"}
        exceptions:
          - {date: 2026-03-09, closed: true}
          - {date: 2026-03-10, start: "09:00", end: "17:00"}

locations:
  - name: mill
    machine: mill
    capacity: 2
    labor_skill: mill
    shift_crossing: pause
```

Intervals are local wall-clock intervals interpreted through `zoneinfo`. `origin` must
be timezone-aware and its zone offset must be valid for the declared timezone at that
instant. Overnight intervals use an end time earlier than their start time.

## Components and tasks

1. Add immutable calendar configuration and a `WorkingCalendar` runtime primitive.
2. Parse and validate calendar/crossing declarations at the model boundary.
3. Gate starts and elapsed work through calendar semantics.
4. Expand capacity into independent runtime machines and use the acquired machine's
   setup state during dispatch and execution.
5. Write `resource_usage.parquet` beside existing run evidence and derive per-machine
   metrics from it when supplied.
6. Add hand-calculated overnight, exception, DST, crossing, and parallel-setup tests.
7. Add a runnable calendar/capacity example plus coverage and usage documentation.

## Acceptance

- Friday 15:00 plus two attended hours on a 07:00–16:00 weekday calendar completes
  Monday 08:00; a closed Monday moves completion to Tuesday 08:00.
- A spring-forward Sunday interval has the correct elapsed UTC duration.
- `finish_unattended` starts only while labor is present and then crosses shift end;
  `overtime` preserves continuous legacy execution.
- Two jobs starting on two cold machines each pay setup; a later job on one already
  configured machine can avoid setup independently of the other machine's state.
- Resource usage rows name real stable machine IDs and their exact setup/run windows.
- All legacy tests remain green.

These foundations improve simulated operational fidelity. They do not establish a
validated promise date without customer data, calibration, and the later RM-05 gates.
