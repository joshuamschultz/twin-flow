"""Honest scoring of dated forecasts against explicit outcome maturity."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence

from twinflow.data.models import ActualRecord, BacktestReport, ForecastRecord, QuantileCoverage

SECONDS_PER_DAY = 86_400.0


def score_forecasts(
    forecasts: Sequence[ForecastRecord], actuals: Sequence[ActualRecord], *, by_segment: bool = True
) -> BacktestReport:
    """Score matched mature outcomes, retaining missing and censored counts."""
    actual_by_id = {actual.item_id: actual for actual in actuals}
    absolute_errors: list[float] = []
    baseline_errors: list[float] = []
    paired_forecast_errors: list[float] = []
    coverage_counts: dict[float, list[int]] = defaultdict(lambda: [0, 0])
    matched = 0
    missing = 0
    censored = 0
    for forecast in forecasts:
        actual = actual_by_id.get(forecast.item_id)
        if actual is None or (actual.observed_date is None and actual.censored_at is None):
            missing += 1
            continue
        matched += 1
        if actual.observed_date is None:
            censored += 1
            continue
        error = abs((forecast.point_date - actual.observed_date).total_seconds()) / SECONDS_PER_DAY
        absolute_errors.append(error)
        if forecast.baseline_date is not None:
            baseline_errors.append(
                abs((forecast.baseline_date - actual.observed_date).total_seconds())
                / SECONDS_PER_DAY
            )
            paired_forecast_errors.append(error)
        for quantile, predicted_date in forecast.quantiles.items():
            counts = coverage_counts[quantile]
            counts[1] += 1
            counts[0] += actual.observed_date <= predicted_date
    baseline_mean = statistics.fmean(baseline_errors) if baseline_errors else None
    paired_mean = statistics.fmean(paired_forecast_errors) if paired_forecast_errors else None
    improvement = None
    if baseline_mean is not None and paired_mean is not None and baseline_mean > 0:
        improvement = (baseline_mean - paired_mean) / baseline_mean
    segments: dict[str, BacktestReport] = {}
    if by_segment:
        groups: dict[str, list[ForecastRecord]] = defaultdict(list)
        for forecast in forecasts:
            groups[forecast.segment].append(forecast)
        if len(groups) > 1 or (groups and next(iter(groups)) != "all"):
            segments = {
                name: score_forecasts(group, actuals, by_segment=False)
                for name, group in sorted(groups.items())
            }
    return BacktestReport(
        forecast_count=len(forecasts),
        matched_count=matched,
        scored_count=len(absolute_errors),
        missing_count=missing,
        censored_count=censored,
        mean_absolute_error_days=statistics.fmean(absolute_errors) if absolute_errors else None,
        median_absolute_error_days=statistics.median(absolute_errors) if absolute_errors else None,
        quantile_coverage=tuple(
            QuantileCoverage(quantile, counts[0], counts[1])
            for quantile, counts in sorted(coverage_counts.items())
        ),
        baseline_count=len(baseline_errors),
        baseline_mean_absolute_error_days=baseline_mean,
        baseline_improvement_fraction=improvement,
        segments=segments,
    )


def backtest_to_dict(report: BacktestReport) -> dict[str, object]:
    """Convert a report to a JSON-ready dictionary."""
    return {
        "forecast_count": report.forecast_count,
        "matched_count": report.matched_count,
        "scored_count": report.scored_count,
        "missing_count": report.missing_count,
        "censored_count": report.censored_count,
        "mean_absolute_error_days": report.mean_absolute_error_days,
        "median_absolute_error_days": report.median_absolute_error_days,
        "quantile_coverage": [
            {
                "quantile": item.quantile,
                "covered": item.covered,
                "count": item.count,
                "empirical_coverage": item.empirical_coverage,
            }
            for item in report.quantile_coverage
        ],
        "baseline_count": report.baseline_count,
        "baseline_mean_absolute_error_days": report.baseline_mean_absolute_error_days,
        "baseline_improvement_fraction": report.baseline_improvement_fraction,
        "segments": {name: backtest_to_dict(item) for name, item in report.segments.items()},
    }
