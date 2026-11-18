# Tune everything

![A control console with rows of sliders and dials, wired to a small factory](../images/headers/how-to-tune-everything.webp)

This guide explains that every number in twinflow has a default. It shows where to change the numbers, and where the full list lives.

## What this does

It shows the `experiment:` section of your plant file, where most run and race numbers are set. It links to the full table.

## Before you start

- You have a plant file. See [build a twin](build-a-twin.md).

## Steps

1. Find the `experiment:` section of your plant file. It can hold settings like these:

   ```yaml
   experiment:
     score: dollars
     race: {episodes: 200, seed: 0}
     checker: {time_limit_ms: 20}
   ```

2. Leave a setting out and twinflow uses its default. For example, if you leave out `race: {episodes: ...}`, the race runs 200 times for each rule.

3. Change a setting by editing its number or name. Change one setting at a time, so you know what made the difference.

4. Check the file after each change. See [check your twin](check-your-twin.md). The check names any setting twinflow does not know, and suggests the closest real name.

5. Read the full table of settings, with each default and what it does, in [Tune your twin](tune.md).

## The settings you will use most

- `experiment.score`: the score the race sorts rules by. The default is `dollars`.
- `experiment.race.episodes`: how many runs each rule gets in a race. The default is 200.
- `experiment.race.seed`: the seed for the random demand. The default is 0.
- `experiment.observation`: what the rules are allowed to see. The default is `full`.

Do not change the seed to make one rule win. Use the same seed for a fair test.

## What you get

- Nothing prints when you edit the file. The new numbers take effect the next time you run a command.

## If something goes wrong

- **"is not a setting twinflow knows. Did you mean ...?"**: A setting name has a typo. Use the name the check suggests.
- **A change has no effect**: Check that the setting is inside the `experiment:` section, and that the indent is right. Then check the file again.
- **You changed several settings and the numbers moved in a strange way**: Put the settings back. Change one, run the test, and read the result before the next change.

## Next

[How-to guides](README.md): go back to the list of guides, or read the [CLI reference](../cli.md) for every command and flag.
