#!/usr/bin/env python3
"""
Macro risk snapshot collector.

Runs the risk radar (黑天鹅/灰犀牛预警) and stores one snapshot per run so the
dashboard can serve it instantly — a live run takes ~45s of network calls,
far too slow for a page load.

Usage:
    python3 macro_collect.py            # collect + store
    python3 macro_collect.py --dry-run  # print, don't store
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
# The release bundle uses <app>/lib; the source tree uses <repo>/lib.
for _cand in (
    os.environ.get("KOL_LIB_DIR"),
    _HERE / "lib",
    _HERE.parent / "lib",
):
    if _cand and Path(_cand, "risk_radar.py").is_file():
        sys.path.insert(0, str(_cand))
        break

import db  # noqa: E402
import macro_alert_service  # noqa: E402
import market_data  # noqa: E402


def _has_metric(payload: object, field: str) -> bool:
    """Return whether a source supplied a usable metric value."""
    return (
        isinstance(payload, Mapping)
        and payload.get(field) is not None
        and payload.get("data_status") not in {"unavailable", "stale"}
        and payload.get("stale") is not True
        and payload.get("is_stale") is not True
    )


def _financial_stress_available(market_data: Mapping) -> bool:
    """Prefer OFR FSI while accepting stored snapshots from the OAS era."""
    return _has_metric(market_data.get("financial_stress"), "ofr_fsi") or _has_metric(
        market_data.get("credit_spreads"), "hy_oas"
    )


def _gold_oil_available(market_data: Mapping) -> bool:
    payload = market_data.get("gold_oil")
    return (
        isinstance(payload, Mapping)
        and _has_metric(payload.get("gold"), "price")
        and _has_metric(payload.get("oil"), "price")
    )


def _alert_input(market_data: Mapping, key: str) -> Mapping:
    payload = market_data.get("alert_inputs")
    if not isinstance(payload, Mapping):
        return {}
    item = payload.get(key)
    return item if isinstance(item, Mapping) else {}


def _alert_input_available(market_data: Mapping, key: str) -> bool:
    payload = _alert_input(market_data, key)
    return (
        payload.get("status") == "available"
        and payload.get("data_status") == "ok"
        and payload.get("stale") is not True
        and payload.get("close") is not None
    )


# Which market_data fields must be present for a sub-score to be trustworthy.
_SOURCE_CHECKS = {
    "vix": lambda md: md.get("vix", {}).get("value") is not None,
    "treasury": lambda md: md.get("treasury", {}).get("10Y") is not None,
    "usd_cny": lambda md: md.get("usd_cny", {}).get("rate") is not None,
    "gold_oil": _gold_oil_available,
    "dxy": lambda md: md.get("dxy", {}).get("value") is not None,
    "financial_stress": _financial_stress_available,
    "us_equity_trend": lambda md: _alert_input_available(md, "us_equity"),
    "cn_equity_trend": lambda md: _alert_input_available(md, "cn_equity"),
}

_SOURCE_LABELS = {
    "vix": "VIX 恐慌指数",
    "treasury": "美债收益率曲线",
    "usd_cny": "美元/人民币",
    "gold_oil": "黄金 / 原油",
    "dxy": "美元指数 DXY",
    "financial_stress": "全球金融压力（OFR FSI）",
    "us_equity_trend": "标普 500 完成收盘趋势",
    "cn_equity_trend": "沪深 300 完成收盘趋势",
}

_COVERAGE_SOURCE_FIELDS = (
    "status",
    "data_status",
    "observed_at",
    "source_url",
    "stale",
    "is_stale",
    "note",
    "source",
    "provider",
    "market_date",
)


def _coverage_source_payload(market_data: Mapping, key: str) -> Mapping:
    """Choose the payload whose metadata describes the coverage decision."""
    if key == "us_equity_trend":
        return _alert_input(market_data, "us_equity")
    if key == "cn_equity_trend":
        return _alert_input(market_data, "cn_equity")
    if key != "financial_stress":
        payload = market_data.get(key)
        return payload if isinstance(payload, Mapping) else {}

    current = market_data.get("financial_stress")
    if _has_metric(current, "ofr_fsi"):
        return current
    legacy = market_data.get("credit_spreads")
    if _has_metric(legacy, "hy_oas"):
        return legacy
    return current if isinstance(current, Mapping) else {}


def annotate_coverage(report: dict) -> dict:
    """Mark which data sources came back, so the UI never implies false confidence."""
    md = report.get("market_data", {})
    if not isinstance(md, Mapping):
        md = {}
    sources = []
    ok = 0
    for key, check in _SOURCE_CHECKS.items():
        try:
            available = bool(check(md))
        except Exception:
            available = False
        ok += available
        source = {"key": key, "label": _SOURCE_LABELS[key], "available": available}
        payload = _coverage_source_payload(md, key)
        source.update(
            {
                field: payload[field]
                for field in _COVERAGE_SOURCE_FIELDS
                if field in payload
            }
        )
        sources.append(source)
    report["data_coverage"] = {
        "available": ok,
        "total": len(_SOURCE_CHECKS),
        "pct": round(100 * ok / len(_SOURCE_CHECKS)),
        "sources": sources,
    }
    return report


def _previous_macro_snapshot() -> dict | None:
    """Load the full prior snapshot once for moves and alert persistence."""
    try:
        db.init()
        previous = db.latest_macro()
    except Exception:
        return None
    if not isinstance(previous, dict):
        return None
    return previous


def collect_alert_inputs(
    *,
    history_fetcher=None,
    cn_history_fallback=None,
    now: datetime | None = None,
) -> dict[str, dict]:
    """Fetch the four bounded daily series used by the alert rule engine.

    Fetches run concurrently and degrade independently.  Only derived summaries
    are returned; raw provider bars are not stored in macro snapshots.
    """
    fetcher = history_fetcher or market_data.fetch_yahoo_history
    fallback = cn_history_fallback
    if fallback is None and history_fetcher is None:
        fallback = market_data.fetch_tencent_daily_history
    current = now or datetime.now(timezone.utc)
    specs = macro_alert_service.series_specs()
    output: dict[str, dict] = {}

    def fetch_one(key: str, asset_key: str) -> tuple[str, dict]:
        try:
            history = fetcher(
                asset_key,
                range_="6mo",
                interval="1d",
                timeout=8.0,
            )
        except Exception:
            history = {
                "status": "unavailable",
                "reason_code": "request_failed",
                "bars": [],
            }
        summary = macro_alert_service.summarize_daily_history(
            history,
            series_key=key,
            now=current,
        )
        if key == "cn_equity" and summary.get("data_status") != "ok" and fallback:
            try:
                fallback_history = fallback(
                    asset_key,
                    count=120,
                    timeout=8.0,
                )
            except Exception:
                fallback_history = {
                    "status": "unavailable",
                    "reason_code": "request_failed",
                    "bars": [],
                }
            fallback_summary = macro_alert_service.summarize_daily_history(
                fallback_history,
                series_key=key,
                now=current,
            )
            rank = {"unavailable": 0, "stale": 1, "ok": 2}
            if rank.get(fallback_summary.get("data_status"), 0) > rank.get(
                summary.get("data_status"), 0
            ):
                summary = fallback_summary
        return key, summary

    with ThreadPoolExecutor(max_workers=len(specs)) as executor:
        futures = {
            executor.submit(fetch_one, key, spec["asset_key"]): key
            for key, spec in specs.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                result_key, summary = future.result()
            except Exception:
                summary = macro_alert_service.summarize_daily_history(
                    {"status": "unavailable", "reason_code": "request_failed"},
                    series_key=key,
                    now=current,
                )
                result_key = key
            output[result_key] = summary
    return {key: output[key] for key in specs}


def collect(
    store: bool = True,
    *,
    history_fetcher=None,
    cn_history_fallback=None,
    now: datetime | None = None,
) -> dict:
    import risk_radar  # noqa: E402  — heavy import, only when actually collecting

    current = now or datetime.now(timezone.utc)
    previous = _previous_macro_snapshot()
    previous_market_data = (
        previous.get("market_data")
        if isinstance(previous, Mapping)
        and isinstance(previous.get("market_data"), dict)
        else None
    )
    report = risk_radar.generate_risk_report(previous_market_data)
    report_market_data = report.get("market_data")
    if not isinstance(report_market_data, dict):
        report_market_data = {}
        report["market_data"] = report_market_data
    report_market_data["alert_inputs"] = collect_alert_inputs(
        history_fetcher=history_fetcher,
        cn_history_fallback=cn_history_fallback,
        now=current,
    )
    report["market_alerts"] = macro_alert_service.build_market_alerts(
        report,
        previous_snapshot=previous,
        now=current,
    )
    report = annotate_coverage(report)
    if store:
        db.init()
        snap_id = db.save_macro_snapshot(report)
        report["snapshot_id"] = snap_id
    return report


def main() -> int:
    dry = "--dry-run" in sys.argv
    report = collect(store=not dry)
    cov = report["data_coverage"]
    cr = report["composite_risk"]
    summary = {
        "snapshot_id": report.get("snapshot_id"),
        "composite": f"{cr.get('level')} ({cr.get('score')})",
        "coverage": f"{cov['available']}/{cov['total']}",
        "black_swans": len(report.get("black_swan_scenarios", [])),
        "gray_rhinos": len(report.get("gray_rhinos", [])),
        "opportunities": len(report.get("opportunities", [])),
        "monitored_events": len(report.get("monitored_events", [])),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
