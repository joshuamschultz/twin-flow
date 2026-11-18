"""Random daily demand that follows the week and the year.

Each day's usual rate is multiplied by that weekday's number in `weekly`
(day 0 of the run is the first entry), and by a yearly wave that peaks on
`peak_day` and is `yearly_amplitude` above (and below) normal at its top
(and bottom). Then the units are drawn at random around that rate.
"""

import math

NAME = "seasonal"

# Change these to fit your plant. Weekly numbers average 1, so the year's total stays the same.
SETTINGS = {
    "weekly": [1.2, 1.1, 1.0, 1.0, 1.2, 0.8, 0.7],
    "yearly_amplitude": 0.2,
    "peak_day": 150,
}


def demand(period, rng, model, weekly=(1.0,) * 7, yearly_amplitude=0.0, peak_day=0):
    # How busy this day is: the weekday's number times the yearly wave.
    wave = 1 + yearly_amplitude * math.cos(2 * math.pi * (period - peak_day) / 365)
    busy = weekly[period % len(weekly)] * wave

    rates = [model.demand_rate[p] * busy for p in model.products]
    units = rng.poisson(rates)
    return {p: int(units[i]) for i, p in enumerate(model.products)}
