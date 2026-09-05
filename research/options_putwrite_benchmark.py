#!/usr/bin/env python3
"""Compare official put-write index histories without inventing option fills.

The utility intentionally consumes local CSV files.  Downloading and licensing
market data are separate operational concerns, and keeping them outside the
calculation makes every reported result reproducible from an identified input
hash.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence


TRADING_DAYS = 252
MONTHS_PER_YEAR = 12
DEFAULT_STRESS_WINDOWS = (
    ("全球金融危机", date(2007, 10, 9), date(2009, 3, 9)),
    ("新冠急跌", date(2020, 2, 19), date(2020, 3, 23)),
    ("2022 加息周期", date(2022, 1, 3), date(2022, 12, 30)),
)


@dataclass(frozen=True)
class Observation:
    day: date
    value: float


@dataclass(frozen=True)
class Drawdown:
    value: float
    peak: date
    trough: date


@dataclass(frozen=True)
class SeriesMetrics:
    name: str
    start: str
    end: str
    observations: int
    input_sha256: str
    cagr: float
    annualized_daily_volatility: float
    daily_max_drawdown: float
    daily_drawdown_peak: str
    daily_drawdown_trough: str
    annualized_monthly_volatility: float
    month_end_max_drawdown: float
    worst_month: str | None
    worst_month_return: float | None
    positive_month_ratio: float | None


def _normalized_header(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _parse_date(raw: str) -> date:
    value = raw.strip()
    for pattern in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"unsupported date value: {raw!r}")


def _select_columns(fieldnames: Sequence[str], series_name: str) -> tuple[str, str]:
    if len(fieldnames) < 2:
        raise ValueError("CSV must contain a date column and a value column")

    normalized = {_normalized_header(field): field for field in fieldnames}
    date_column = next(
        (normalized[key] for key in ("date", "day", "tradedate") if key in normalized),
        fieldnames[0],
    )
    wanted = _normalized_header(series_name)
    value_column = next(
        (
            normalized[key]
            for key in (wanted, "close", "value", "indexvalue", "price")
            if key and key in normalized and normalized[key] != date_column
        ),
        None,
    )
    if value_column is None:
        remaining = [field for field in fieldnames if field != date_column]
        if len(remaining) != 1:
            raise ValueError(
                "could not identify one value column; name it after the series or Close"
            )
        value_column = remaining[0]
    return date_column, value_column


def load_series(path: Path, name: str) -> tuple[list[Observation], str]:
    """Load one positive-valued daily index series and return its input hash."""

    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise ValueError(f"{path}: missing CSV header")
    date_column, value_column = _select_columns(reader.fieldnames, name)
    points: dict[date, float] = {}
    for line_number, row in enumerate(reader, start=2):
        raw_day = (row.get(date_column) or "").strip()
        raw_value = (row.get(value_column) or "").replace(",", "").strip()
        if not raw_day and not raw_value:
            continue
        try:
            day = _parse_date(raw_day)
            value = float(raw_value)
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: {exc}") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{path}:{line_number}: index value must be positive")
        if day in points:
            raise ValueError(f"{path}:{line_number}: duplicate date {day.isoformat()}")
        points[day] = value
    if len(points) < 2:
        raise ValueError(f"{path}: at least two observations are required")
    return [Observation(day, points[day]) for day in sorted(points)], digest


def _returns(values: Sequence[float]) -> list[float]:
    return [current / previous - 1.0 for previous, current in zip(values, values[1:])]


def _sample_volatility(returns: Sequence[float], periods: int) -> float:
    if len(returns) < 2:
        return 0.0
    return statistics.stdev(returns) * math.sqrt(periods)


def _max_drawdown(points: Sequence[Observation]) -> Drawdown:
    peak = points[0]
    worst = Drawdown(0.0, peak.day, peak.day)
    for point in points:
        if point.value > peak.value:
            peak = point
        drawdown = point.value / peak.value - 1.0
        if drawdown < worst.value:
            worst = Drawdown(drawdown, peak.day, point.day)
    return worst


def _month_ends(points: Sequence[Observation]) -> list[Observation]:
    result: list[Observation] = []
    active_month: tuple[int, int] | None = None
    previous: Observation | None = None
    for point in points:
        month = (point.day.year, point.day.month)
        if active_month is not None and month != active_month:
            assert previous is not None
            result.append(previous)
        active_month = month
        previous = point
    assert previous is not None
    result.append(previous)
    return result


def _cagr(points: Sequence[Observation]) -> float:
    elapsed_days = (points[-1].day - points[0].day).days
    if elapsed_days <= 0:
        return 0.0
    return (points[-1].value / points[0].value) ** (365.2425 / elapsed_days) - 1.0


def compute_metrics(
    name: str,
    points: Sequence[Observation],
    input_sha256: str,
) -> SeriesMetrics:
    if len(points) < 2:
        raise ValueError("at least two observations are required")
    daily_returns = _returns([point.value for point in points])
    month_ends = _month_ends(points)
    month_returns = _returns([point.value for point in month_ends])
    daily_drawdown = _max_drawdown(points)
    monthly_drawdown = _max_drawdown(month_ends)
    worst_index = min(range(len(month_returns)), key=month_returns.__getitem__) if month_returns else None
    return SeriesMetrics(
        name=name,
        start=points[0].day.isoformat(),
        end=points[-1].day.isoformat(),
        observations=len(points),
        input_sha256=input_sha256,
        cagr=_cagr(points),
        annualized_daily_volatility=_sample_volatility(daily_returns, TRADING_DAYS),
        daily_max_drawdown=daily_drawdown.value,
        daily_drawdown_peak=daily_drawdown.peak.isoformat(),
        daily_drawdown_trough=daily_drawdown.trough.isoformat(),
        annualized_monthly_volatility=_sample_volatility(month_returns, MONTHS_PER_YEAR),
        month_end_max_drawdown=monthly_drawdown.value,
        worst_month=(month_ends[worst_index + 1].day.strftime("%Y-%m") if worst_index is not None else None),
        worst_month_return=(month_returns[worst_index] if worst_index is not None else None),
        positive_month_ratio=(
            sum(value > 0 for value in month_returns) / len(month_returns)
            if month_returns
            else None
        ),
    )


def _value_on_or_after(points: Sequence[Observation], target: date) -> Observation | None:
    return next((point for point in points if point.day >= target), None)


def _value_on_or_before(points: Sequence[Observation], target: date) -> Observation | None:
    return next((point for point in reversed(points) if point.day <= target), None)


def period_return(
    points: Sequence[Observation], start: date, end: date
) -> dict[str, str | float] | None:
    first = _value_on_or_after(points, start)
    last = _value_on_or_before(points, end)
    if first is None or last is None or first.day >= last.day:
        return None
    return {
        "start": first.day.isoformat(),
        "end": last.day.isoformat(),
        "return": last.value / first.value - 1.0,
    }


def trailing_cagr(points: Sequence[Observation], years: int) -> float | None:
    end = points[-1]
    try:
        target = end.day.replace(year=end.day.year - years)
    except ValueError:
        target = end.day.replace(year=end.day.year - years, day=28)
    first = _value_on_or_after(points, target)
    if first is None or first.day >= end.day:
        return None
    elapsed_days = (end.day - first.day).days
    return (end.value / first.value) ** (365.2425 / elapsed_days) - 1.0


def monthly_beta_and_correlation(
    points: Sequence[Observation], benchmark: Sequence[Observation]
) -> tuple[float | None, float | None]:
    subject_months = {
        point.day.strftime("%Y-%m"): point.value for point in _month_ends(points)
    }
    benchmark_months = {
        point.day.strftime("%Y-%m"): point.value for point in _month_ends(benchmark)
    }
    common = sorted(set(subject_months) & set(benchmark_months))
    if len(common) < 3:
        return None, None
    subject_returns = [
        subject_months[current] / subject_months[previous] - 1.0
        for previous, current in zip(common, common[1:])
    ]
    benchmark_returns = [
        benchmark_months[current] / benchmark_months[previous] - 1.0
        for previous, current in zip(common, common[1:])
    ]
    benchmark_variance = statistics.variance(benchmark_returns)
    if benchmark_variance == 0:
        return None, None
    subject_mean = statistics.mean(subject_returns)
    benchmark_mean = statistics.mean(benchmark_returns)
    covariance = sum(
        (subject - subject_mean) * (reference - benchmark_mean)
        for subject, reference in zip(subject_returns, benchmark_returns)
    ) / (len(subject_returns) - 1)
    beta = covariance / benchmark_variance
    if statistics.stdev(subject_returns) == 0:
        return beta, None
    correlation = covariance / (
        statistics.stdev(subject_returns) * statistics.stdev(benchmark_returns)
    )
    return beta, correlation


def _limit_range(
    points: Sequence[Observation], start: date | None, end: date | None
) -> list[Observation]:
    return [
        point
        for point in points
        if (start is None or point.day >= start) and (end is None or point.day <= end)
    ]


def build_report(
    loaded: dict[str, tuple[list[Observation], str]],
    *,
    benchmark_name: str | None = None,
    start: date | None = None,
    end: date | None = None,
    common_range: bool = True,
) -> dict:
    if not loaded:
        raise ValueError("at least one series is required")
    bounded_start = start
    bounded_end = end
    if common_range:
        common_start = max(points[0].day for points, _digest in loaded.values())
        common_end = min(points[-1].day for points, _digest in loaded.values())
        bounded_start = max(filter(None, (bounded_start, common_start)), default=None)
        bounded_end = min(filter(None, (bounded_end, common_end)), default=None)
    if bounded_start and bounded_end and bounded_start >= bounded_end:
        raise ValueError("selected date range contains fewer than two dates")

    series: dict[str, list[Observation]] = {}
    metrics: dict[str, SeriesMetrics] = {}
    for name, (points, digest) in loaded.items():
        selected = _limit_range(points, bounded_start, bounded_end)
        if len(selected) < 2:
            raise ValueError(f"{name}: selected date range has fewer than two observations")
        series[name] = selected
        metrics[name] = compute_metrics(name, selected, digest)

    if benchmark_name and benchmark_name not in series:
        raise ValueError(f"benchmark {benchmark_name!r} is not one of the loaded series")
    comparisons = {}
    if benchmark_name:
        benchmark = series[benchmark_name]
        for name, points in series.items():
            beta, correlation = monthly_beta_and_correlation(points, benchmark)
            comparisons[name] = {
                "monthly_beta": beta,
                "monthly_correlation": correlation,
            }

    stress = {}
    for label, window_start, window_end in DEFAULT_STRESS_WINDOWS:
        stress[label] = {
            name: period_return(points, window_start, window_end)
            for name, points in series.items()
        }

    trailing = {
        str(years): {name: trailing_cagr(points, years) for name, points in series.items()}
        for years in (1, 3, 5, 10)
    }
    return {
        "calculation_version": "putwrite-benchmark-v1",
        "common_range": common_range,
        "benchmark": benchmark_name,
        "series": {name: asdict(value) for name, value in metrics.items()},
        "monthly_comparison": comparisons,
        "stress_windows": stress,
        "trailing_cagr": trailing,
        "limitations": [
            "Index histories are benchmark levels, not executable fills for an individual account.",
            "SPX is a price index unless a total-return series is supplied, so its CAGR is not directly comparable with collateralized put-write indices.",
            "Published pre-launch index history may be backtested rather than live.",
        ],
    }


def _percentage(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def render_markdown(report: dict) -> str:
    lines = [
        "# Put-write benchmark report",
        "",
        f"Calculation: `{report['calculation_version']}`; common range: `{report['common_range']}`.",
        "",
        "| Series | Range | Obs. | CAGR | Daily vol. | Daily max DD | Month-end max DD | Worst month | Positive months |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, metric in report["series"].items():
        worst_month = (
            f"{metric['worst_month']} ({_percentage(metric['worst_month_return'])})"
            if metric["worst_month"]
            else "—"
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    name,
                    f"{metric['start']}–{metric['end']}",
                    str(metric["observations"]),
                    _percentage(metric["cagr"]),
                    _percentage(metric["annualized_daily_volatility"]),
                    _percentage(metric["daily_max_drawdown"]),
                    _percentage(metric["month_end_max_drawdown"]),
                    worst_month,
                    _percentage(metric["positive_month_ratio"]),
                )
            )
            + " |"
        )
    lines.extend(("", "## Stress windows", ""))
    names = list(report["series"])
    lines.append("| Window | " + " | ".join(names) + " |")
    lines.append("| --- | " + " | ".join("---:" for _name in names) + " |")
    for label, values in report["stress_windows"].items():
        lines.append(
            "| "
            + label
            + " | "
            + " | ".join(
                _percentage(values[name]["return"] if values[name] else None)
                for name in names
            )
            + " |"
        )
    lines.extend(("", "## Trailing CAGR", ""))
    lines.append("| Years | " + " | ".join(names) + " |")
    lines.append("| ---: | " + " | ".join("---:" for _name in names) + " |")
    for years, values in report["trailing_cagr"].items():
        lines.append(
            f"| {years} | "
            + " | ".join(_percentage(values[name]) for name in names)
            + " |"
        )
    if report["benchmark"]:
        lines.extend(("", f"## Monthly comparison with {report['benchmark']}", ""))
        lines.extend(("| Series | Beta | Correlation |", "| --- | ---: | ---: |"))
        for name, values in report["monthly_comparison"].items():
            beta = values["monthly_beta"]
            correlation = values["monthly_correlation"]
            lines.append(
                f"| {name} | {'—' if beta is None else f'{beta:.3f}'} | "
                f"{'—' if correlation is None else f'{correlation:.3f}'} |"
            )
    lines.extend(("", "## Input hashes", ""))
    for name, metric in report["series"].items():
        lines.append(f"- `{name}`: `{metric['input_sha256']}`")
    lines.extend(("", "## Limitations", ""))
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def _series_argument(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("use NAME=/path/to/history.csv")
    name, raw_path = value.split("=", 1)
    if not name.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("series name and path must both be non-empty")
    return name.strip(), Path(raw_path).expanduser()


def _optional_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--series",
        action="append",
        required=True,
        type=_series_argument,
        metavar="NAME=CSV",
        help="repeat for every official index history",
    )
    parser.add_argument("--benchmark", help="loaded series used for beta/correlation")
    parser.add_argument("--start", type=_optional_date)
    parser.add_argument("--end", type=_optional_date)
    parser.add_argument(
        "--independent-ranges",
        action="store_true",
        help="do not restrict every series to their common date range",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    loaded: dict[str, tuple[list[Observation], str]] = {}
    for name, path in args.series:
        if name in loaded:
            raise SystemExit(f"duplicate series name: {name}")
        loaded[name] = load_series(path, name)
    report = build_report(
        loaded,
        benchmark_name=args.benchmark,
        start=args.start,
        end=args.end,
        common_range=not args.independent_ranges,
    )
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
