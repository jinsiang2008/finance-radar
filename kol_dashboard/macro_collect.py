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

# Which market_data fields must be present for a sub-score to be trustworthy.
_SOURCE_CHECKS = {
    "vix": lambda md: md.get("vix", {}).get("value") is not None,
    "treasury": lambda md: md.get("treasury", {}).get("10Y") is not None,
    "usd_cny": lambda md: md.get("usd_cny", {}).get("rate") is not None,
    "gold_oil": lambda md: md.get("gold_oil", {}).get("gold", {}).get("price") is not None,
    "dxy": lambda md: md.get("dxy", {}).get("value") is not None,
    "credit_spreads": lambda md: md.get("credit_spreads", {}).get("hy_oas") is not None,
}

_SOURCE_LABELS = {
    "vix": "VIX 恐慌指数",
    "treasury": "美债收益率曲线",
    "usd_cny": "美元/人民币",
    "gold_oil": "黄金 / 原油",
    "dxy": "美元指数 DXY",
    "credit_spreads": "信用利差 (HY/IG OAS)",
}


def annotate_coverage(report: dict) -> dict:
    """Mark which data sources came back, so the UI never implies false confidence."""
    md = report.get("market_data", {})
    sources = []
    ok = 0
    for key, check in _SOURCE_CHECKS.items():
        try:
            available = bool(check(md))
        except Exception:
            available = False
        ok += available
        sources.append(
            {"key": key, "label": _SOURCE_LABELS[key], "available": available}
        )
    report["data_coverage"] = {
        "available": ok,
        "total": len(_SOURCE_CHECKS),
        "pct": round(100 * ok / len(_SOURCE_CHECKS)),
        "sources": sources,
    }
    return report


def _previous_market_data() -> dict | None:
    """Load the last snapshot's indicators so moves can be detected."""
    try:
        db.init()
        previous = db.latest_macro()
    except Exception:
        return None
    if not isinstance(previous, dict):
        return None
    market_data = previous.get("market_data")
    return market_data if isinstance(market_data, dict) else None


def collect(store: bool = True) -> dict:
    import risk_radar  # noqa: E402  — heavy import, only when actually collecting

    report = annotate_coverage(
        risk_radar.generate_risk_report(_previous_market_data())
    )
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
