# Add people and shifts

This guide sets who runs the machines, and when they work.

## What this does

It shows the `labor:` part of the CNC shop file. You set the people in each group, their shifts and their days off.

## Before you start

- You did [Model your floor](model-your-floor.md).
- Know how many people you have in each skill group, and when they work.

## Steps

1. Open `examples/showcase/cnc-job-shop/shop.yaml` and find `labor:`. Each group of people is a pool.

   ```yaml
   - name: machinists
     headcount: 2                  # people on each shift
     skills: [saw, lathe, mill]
   ```

2. Match a step to a pool. A step with `labor_skill: mill` needs one person from a pool that lists `mill`.

3. Set the shifts in `calendar`. `origin` is time zero of your order book. `weekly` lists the days and hours.

   ```yaml
   calendar:
     timezone: America/Chicago
     origin: "2026-11-16T06:00:00-06:00"
     weekly:
       - {days: [mon, tue, wed, thu, fri], start: "06:00", end: "22:00"}
   ```

4. Add days off in `exceptions`. This shop is closed on Thanksgiving.

   ```yaml
   exceptions:
     - {date: "2026-11-26", closed: true}
   ```

5. Check the file.

   ```bash
   twinflow validate examples/showcase/cnc-job-shop/shop.yaml
   ```

   No message means it passed.

## What you get

- People who work only in their shift hours.
- Closed days that stay closed. Work stops at the end of a shift and picks up at the next one.

## If something goes wrong

- **"labor_skill 'mill' is not declared in any labor pool"**: No pool lists that skill. Add it to a pool.
- **"time data '6am' does not match format '%H:%M'"**: Use 24-hour times in quotes, like `"06:00"`.
- **Dates look shifted**: Check `timezone` and the `-06:00` in `origin`.

## Next

[Add jobs](add-jobs.md): write the orders file.
