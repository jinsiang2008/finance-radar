"""Build the private, research-only Options Lab overview.

The first production slice intentionally has no option-chain adapter and never
turns an underlying price, portfolio row, or benchmark index into a contract
recommendation.  It exposes a versioned readiness ledger and a reproducible
official-index baseline so the UI can be useful while failing closed.

This module performs no network or database I/O.  Callers provide already
projected inputs and remain responsible for authentication.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = 1
METHOD_VERSION = "options-research-readiness-v1"

_MACRO_ACTION_LABELS = {
    "observe": "观察",
    "prepare_reduce": "准备降低风险",
    "reduce_candidate": "暂停新增短 Put",
    "exit_candidate": "停止新增短 Put",
}

_GENERAL_RESEARCH_UNIVERSE = (
    ("US:SPY", "SPY", "标普 500 ETF", "核心 ETF"),
    ("US:QQQ", "QQQ", "纳斯达克 100 ETF", "核心 ETF"),
    ("US:MSFT", "MSFT", "微软", "核心大盘股"),
    ("US:GOOGL", "GOOGL", "谷歌", "核心大盘股"),
    ("US:META", "META", "Meta", "核心大盘股"),
    ("US:NVDA", "NVDA", "英伟达", "核心大盘股"),
    ("US:JPM", "JPM", "摩根大通", "核心大盘股"),
    ("US:AMD", "AMD", "AMD", "半导体扩展"),
    ("US:AVGO", "AVGO", "博通", "半导体扩展"),
    ("US:QCOM", "QCOM", "高通", "半导体扩展"),
    ("US:TSM", "TSM", "台积电", "半导体扩展"),
    ("US:TSLA", "TSLA", "特斯拉", "高波动观察"),
    ("US:HOOD", "HOOD", "Robinhood", "高波动观察"),
    ("US:PLTR", "PLTR", "Palantir", "高波动观察"),
    ("US:MU", "MU", "美光", "高波动观察"),
    ("US:BABA", "BABA", "阿里巴巴", "高波动观察"),
)

_PUTWRITE_BASELINE = {
    "calculation_version": "putwrite-benchmark-v1",
    "report_date": "2026-09-06",
    "source_as_of": "2026-09-04",
    "range": {"start": "2007-01-03", "end": "2026-09-04"},
    "input_sha256": {
        "PUT": "de5e047788418441d6c65f3b5d55c80e2664c1406b9d017550b532d092209fdd",
        "PUTY": "b6bb0ab28d6d6b579a4e67bebc2a6bd19e04f69ccaeb1970430656db20cdefb8",
        "WPUT": "59edaa44e9a07c6b4ff941f0d524c324fdb1a8f42c0ba5ab9866d3aee7411c6c",
        "PUTD": "16c0bf9393faf8d11be28ba3bf76872e7603dc700989d5c30bb8cb1c1ab45776",
        "SPX": "863d6b0e3f716a5ada5c56e176fdade1d3a0d21fb53ba64efcb606d85e5f8b9b",
    },
    "scope": (
        "Cboe 官方日度策略指数的研究基线；不是个人账户逐合约回测，"
        "也不代表可执行成交。"
    ),
    "series": [
        {
            "key": "PUT",
            "label": "月度平值 PutWrite",
            "kind": "putwrite",
            "observations": 4950,
            "cagr": 0.0720,
            "annualized_daily_volatility": 0.1383,
            "daily_max_drawdown": -0.3709,
            "monthly_beta_to_spx": 0.601,
        },
        {
            "key": "PUTY",
            "label": "月度约 2% 虚值 PutWrite",
            "kind": "putwrite",
            "observations": 4950,
            "cagr": 0.0531,
            "annualized_daily_volatility": 0.1210,
            "daily_max_drawdown": -0.3304,
            "monthly_beta_to_spx": 0.455,
        },
        {
            "key": "WPUT",
            "label": "周度 PutWrite",
            "kind": "putwrite",
            "observations": 4947,
            "cagr": 0.0437,
            "annualized_daily_volatility": 0.1233,
            "daily_max_drawdown": -0.2862,
            "monthly_beta_to_spx": 0.528,
        },
        {
            "key": "PUTD",
            "label": "动态 PutWrite",
            "kind": "putwrite",
            "observations": 4950,
            "cagr": 0.0946,
            "annualized_daily_volatility": 0.1611,
            "daily_max_drawdown": -0.4503,
            "monthly_beta_to_spx": 0.760,
        },
        {
            "key": "SPX",
            "label": "标普 500 价格指数",
            "kind": "price_index",
            "observations": 4950,
            "cagr": 0.0900,
            "annualized_daily_volatility": 0.1967,
            "daily_max_drawdown": -0.5678,
            "monthly_beta_to_spx": 1.000,
        },
    ],
    "stress_windows": [
        {
            "key": "global_financial_crisis",
            "label": "全球金融危机",
            "start": "2007-10-09",
            "end": "2009-03-09",
            "returns": {
                "PUT": -0.3483,
                "PUTY": -0.3102,
                "WPUT": -0.2361,
                "PUTD": -0.4467,
                "SPX": -0.5678,
            },
        },
        {
            "key": "covid_drawdown",
            "label": "新冠急跌",
            "start": "2020-02-19",
            "end": "2020-03-23",
            "returns": {
                "PUT": -0.2892,
                "PUTY": -0.2731,
                "WPUT": -0.2530,
                "PUTD": -0.3142,
                "SPX": -0.3392,
            },
        },
        {
            "key": "rate_hikes_2022",
            "label": "2022 加息周期",
            "start": "2022-01-03",
            "end": "2022-12-30",
            "returns": {
                "PUT": -0.0785,
                "PUTY": -0.0163,
                "WPUT": -0.1453,
                "PUTD": -0.1527,
                "SPX": -0.1995,
            },
        },
    ],
    "limitations": [
        "策略指数不是个人账户可执行成交，不能据此选择今天的股票、行权价或张数。",
        "SPX 是价格指数，而 put-write 指数包含抵押现金收益，CAGR 不是严格同口径比较。",
        "该基线不覆盖单股财报跳空、提前指派、特殊交割物、点差、费用、税务或保证金变化。",
        "指数发布前的历史可能包含理论回测，不能等同于实盘业绩。",
    ],
    "sources": [
        {
            "label": "Cboe PutWrite 指数方法论",
            "url": "https://cdn.cboe.com/api/global/us_indices/governance/Cboe_PutWrite_Indices_Methodology.pdf",
        },
        {
            "label": "Cboe PUT 官方日度历史",
            "url": "https://cdn.cboe.com/api/global/us_indices/daily_prices/PUT_History.csv",
        },
        {
            "label": "OCC 标准化期权风险披露",
            "url": "https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document",
        },
    ],
}


def _served_at(now: datetime | None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def _safe_text(value: Any, *, maximum: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:maximum]


def _portfolio_readiness(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        return {
            "key": "portfolio_freshness",
            "label": "持仓新鲜度",
            "status": "unavailable",
            "blocking": True,
            "detail": "尚未读取到持仓快照；不能判断接货后的组合集中度。",
        }

    staleness = snapshot.get("staleness")
    staleness = staleness if isinstance(staleness, Mapping) else {}
    as_of = _safe_text(snapshot.get("as_of"), maximum=64)
    stale = staleness.get("is_stale") is not False
    clock_skew = staleness.get("clock_skew") is True
    if clock_skew:
        detail = "持仓快照时间异常；不能用于今天的期权研究。"
    elif stale:
        detail = "持仓快照已过期；只能作为历史资料，不能用于张数或组合容量判断。"
    else:
        detail = "持仓快照时间可核验；资金容量、期权权限和现有期权仓位仍未同步。"
    result: dict[str, Any] = {
        "key": "portfolio_freshness",
        "label": "持仓新鲜度",
        "status": "ready" if not stale and not clock_skew else "stale",
        "blocking": stale or clock_skew,
        "detail": detail,
    }
    if as_of:
        result["evidence_as_of"] = as_of
    return result


def _market_gate(public_macro: Any) -> dict[str, Any]:
    alerts = public_macro.get("market_alerts") if isinstance(public_macro, Mapping) else None
    markets = alerts.get("markets") if isinstance(alerts, Mapping) else None
    us_market = next(
        (
            item
            for item in markets
            if isinstance(item, Mapping)
            and str(item.get("market") or "").strip().upper() == "US"
        ),
        None,
    ) if isinstance(markets, list) else None
    if not isinstance(us_market, Mapping):
        return {
            "status": "unavailable",
            "action": "observe",
            "action_label": "等待宏观证据",
            "data_status": "insufficient",
            "abstain": True,
            "blocks_new_short_puts": True,
            "detail": "美股宏观闸门不可用；未知不能解释成低风险。",
        }

    raw_action = str(us_market.get("action") or "").strip().lower()
    valid_action = raw_action in _MACRO_ACTION_LABELS
    action = raw_action if valid_action else "observe"
    raw_data_status = str(us_market.get("data_status") or "").strip().lower()
    abstain = (
        not valid_action
        or us_market.get("abstain") is True
        or raw_data_status != "ok"
    )
    if abstain:
        status = "unavailable"
        blocks = True
        action = "observe"
        detail = "美股宏观证据不足或已过期；停止新增短 Put 研究候选。"
    elif action in {"reduce_candidate", "exit_candidate"}:
        status = "blocked"
        blocks = True
        detail = "宏观风险闸门要求暂停新增短 Put，只保留风险复核。"
    elif action == "prepare_reduce":
        status = "constrained"
        blocks = False
        detail = "宏观风险正在升高；后续实时版本应收紧标的范围和资金预算。"
    else:
        status = "ready"
        blocks = False
        detail = "宏观闸门允许继续研究；这不代表任何合约已经通过账户、事件或流动性门槛。"

    result: dict[str, Any] = {
        "status": status,
        "action": action,
        "action_label": _MACRO_ACTION_LABELS[action],
        "data_status": "insufficient" if abstain else "ok",
        "abstain": abstain,
        "blocks_new_short_puts": blocks,
        "detail": detail,
    }
    data_as_of = _safe_text(us_market.get("data_as_of"), maximum=64)
    if data_as_of:
        result["data_as_of"] = data_as_of
    return result


def _readiness_items(
    portfolio_snapshot: Any,
    market_gate: Mapping[str, Any],
) -> list[dict[str, Any]]:
    items = [
        {
            "key": "option_market_data",
            "label": "期权链与报价",
            "status": "unavailable",
            "blocking": True,
            "detail": "尚未接入有授权且时间语义明确的 Bid / Ask、Greeks、OI 与成交量。",
        },
        {
            "key": "funding_capacity",
            "label": "资金容量",
            "status": "unavailable",
            "blocking": True,
            "detail": "可用购买力与已占用担保尚未同步；不能计算现金担保张数。",
        },
        {
            "key": "options_permission",
            "label": "期权权限",
            "status": "unavailable",
            "blocking": True,
            "detail": "尚未确认是否允许现金担保卖 Put、费用规则与合约乘数。",
        },
        _portfolio_readiness(portfolio_snapshot),
        {
            "key": "event_calendar",
            "label": "事件日历",
            "status": "unavailable",
            "blocking": True,
            "detail": "点时财报、除息和公司行动日历尚未接入；不能排除到期前二元风险。",
        },
        {
            "key": "macro_gate",
            "label": "美股宏观闸门",
            "status": str(market_gate.get("status") or "unavailable"),
            "blocking": market_gate.get("blocks_new_short_puts") is True,
            "detail": _safe_text(market_gate.get("detail"), maximum=320),
            **(
                {"evidence_as_of": market_gate["data_as_of"]}
                if market_gate.get("data_as_of")
                else {}
            ),
        },
    ]
    return items


def _research_universe() -> list[dict[str, Any]]:
    return [
        {
            "asset_key": asset_key,
            "symbol": symbol,
            "name_zh": name_zh,
            "tier": tier,
            "status": "needs_user_confirmation",
            "status_label": "待用户确认",
            "note": "通用研究池，不表示用户当前熟悉、持有、支持期权或适合卖 Put。",
        }
        for asset_key, symbol, name_zh, tier in _GENERAL_RESEARCH_UNIVERSE
    ]


def build_options_overview(
    *,
    portfolio_snapshot: Any = None,
    public_macro: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a bounded, private-safe Options Lab research view model."""

    market_gate = _market_gate(public_macro)
    readiness_items = _readiness_items(portfolio_snapshot, market_gate)
    rejections = [
        {
            "code": item["key"],
            "label": item["label"],
            "detail": item["detail"],
        }
        for item in readiness_items
        if item["blocking"] is True
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "available": True,
        "mode": "research_only",
        "data_status": "insufficient",
        "decision_state": "abstain",
        "headline": "研究模式：暂无可执行候选",
        "summary": (
            "缺少实时期权链、资金容量、权限与事件日历；系统停止回答"
            "卖哪只、哪个行权价或多少张。"
        ),
        "served_at": _served_at(now),
        "market_gate": market_gate,
        "readiness": {
            "met": sum(item["status"] == "ready" for item in readiness_items),
            "total": len(readiness_items),
            "items": readiness_items,
        },
        "candidate_count": 0,
        "candidates": [],
        "rejections": rejections,
        "research_universe": _research_universe(),
        "benchmark": deepcopy(_PUTWRITE_BASELINE),
        "next_steps": [
            "同步最新持仓时间、资金容量、现有期权仓位和卖 Put 权限。",
            "明确每只标的的愿意接货价、不愿持有清单和组合资金上限。",
            "接入有合法使用权的实时或明确延迟期权链、财报、分红与公司行动。",
            "完成逐合约 Bid / Ask、费用、指派和样本外回测后，再生成纸面候选。",
        ],
        "risk_notice": "研究辅助，不构成投资建议；系统不会提交或执行任何订单。",
        "human_review_required": True,
        "automatic_execution": False,
        "trade_execution_available": False,
    }
