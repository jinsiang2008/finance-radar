#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Risk Radar — 黑天鹅/灰犀牛预警与系统性风险扫描

核心功能：
1. 黑天鹅预警 — 识别尾部风险、极端事件前兆
2. 灰犀牛识别 — 追踪明显但被忽视的系统性威胁
3. 系统性风险评分 — 跨资产类别的风险传导分析
4. 投资机会挖掘 — 恐慌中的错杀、极端定价机会

数据源策略：
- 优先使用已验证的本地数据源（macro_fetcher）
- 对需要搜索的数据，输出搜索关键词供 AI agent 使用
- 不依赖不可靠的 API（如 Yahoo Finance）

输出：JSON 格式的结构化风险报告
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from html import unescape
from typing import Any

# macro_fetcher 与本文件同目录，按相对位置导入以便整个 lib 目录可搬迁
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from macro_fetcher import (
    fetch_cnbc_quote,
    fetch_vix,
    fetch_treasury_yields,
    fetch_usd_cny,
    fetch_gold_oil,
    fetch_fed,
    fetch_fed_speeches,
    fetch_fomc,
    fetch_pboc,
    fetch_pboc_speech,
    fetch_cctv_news,
    http_get,
    CN_TZ,
    now_cn,
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 15
OFR_FSI_CSV_URL = "https://www.financialresearch.gov/financial-stress-index/data/fsi.csv"
OFR_FSI_SOURCE_URL = "https://www.financialresearch.gov/financial-stress-index/"
OFR_FSI_SOURCE = "U.S. Treasury OFR"
OFR_FSI_FALLBACK_MAX_AGE_DAYS = 10

_OFR_FSI_COLUMNS = {
    "ofr_fsi": "ofr fsi",
    "credit": "credit",
    "funding": "funding",
    "volatility": "volatility",
    "equity_valuation": "equity valuation",
    "safe_assets": "safe assets",
}

# ════════════════════════════════════════════
# 1. 核心风险指标采集
# ════════════════════════════════════════════

def fetch_vix_futures() -> dict[str, Any]:
    """VIX 期货期限结构 — 判断 contango/backwardation（恐慌/恐慌消退信号）"""
    result = {"status": "unknown", "contango": None, "spot": None, "next_month": None}
    try:
        html = http_get("https://www.cnbc.com/quotes/.VIX")
        # 提取 VIX 现货
        last_m = re.search(r'"last":\s*"([0-9.]+)"', html)
        if last_m:
            result["spot"] = float(last_m.group(1))
        # 尝试从 CBOE 获取期货数据
        fut_html = http_get("https://www.cboe.com/us/futures/market_statistics/", timeout=10)
        # 提取近月期货价格
        fut_m = re.search(r'VX[0-9]{2}\s*</a>\s*</td>\s*<td[^>]*>\s*([0-9.]+)', fut_html)
        if fut_m:
            result["next_month"] = float(fut_m.group(1))
        if result["spot"] and result["next_month"]:
            diff = result["next_month"] - result["spot"]
            result["contango"] = round(diff, 2)
            if diff > 2:
                result["status"] = "contango_steep"  # 远期溢价高 → 恐慌预期高
            elif diff > 0:
                result["status"] = "contango"
            elif diff > -2:
                result["status"] = "backwardation_mild"  # 远期折价 → 短期恐慌但预期缓解
            else:
                result["status"] = "backwardation_steep"  # 深度折价 → 极端恐慌
    except Exception:
        pass
    return result


def fetch_dxy() -> dict[str, Any]:
    """美元指数 DXY — Investing.com，失败时回落 CNBC。"""
    result = {"value": None, "change_pct": None}
    try:
        html = http_get(
            "https://www.investing.com/indices/usdollar-index",
            timeout=12,
        )
        m = re.search(r'data-test="instrument-price-last">([^<]+)', html)
        if m:
            result["value"] = float(m.group(1).strip().replace(",", ""))
        chg_m = re.search(r'data-test="instrument-price-change-percent">([^<]+)', html)
        if chg_m:
            pct_str = chg_m.group(1).strip().replace("%", "").replace("+", "")
            result["change_pct"] = float(pct_str)
    except Exception:
        pass

    if result["value"] is None:
        # Investing.com 对多数机房 IP 反爬；CNBC 报价可用。
        try:
            quote = fetch_cnbc_quote(".DXY")
            result["value"] = quote.get("price")
            result["change_pct"] = quote.get("change_pct")
        except Exception:
            pass
    return result


def _financial_stress_status(value: float | None) -> str:
    """Classify OFR FSI levels without treating a missing value as calm."""
    if value is None:
        return "unknown"
    if value > 5:
        return "critical"
    if value > 2:
        return "elevated"
    if value > 0:
        return "normal"
    return "low"


def _empty_financial_stress() -> dict[str, Any]:
    return {
        "ofr_fsi": None,
        "credit": None,
        "funding": None,
        "volatility": None,
        "equity_valuation": None,
        "safe_assets": None,
        "observed_at": None,
        "source": OFR_FSI_SOURCE,
        "source_url": OFR_FSI_SOURCE_URL,
        "unit": "index",
        "status": "unknown",
        "data_status": "unavailable",
    }


def fetch_financial_stress() -> dict[str, Any]:
    """Fetch the official OFR Financial Stress Index category decomposition.

    OFR occasionally publishes a partial final row while its daily file is being
    refreshed.  Walk the file in order and retain the last row whose date and all
    six index values are complete and finite, rather than blindly trusting the
    final physical line.
    """
    result = _empty_financial_stress()

    try:
        raw = http_get(
            OFR_FSI_CSV_URL,
            headers={"Accept": "text/csv,*/*;q=0.8"},
            timeout=TIMEOUT,
        )
        if not isinstance(raw, str) or not raw.strip():
            return result

        latest: tuple[str, dict[str, float]] | None = None
        reader = csv.DictReader(io.StringIO(raw.lstrip("\ufeff")))
        for row in reader:
            if not isinstance(row, dict):
                continue
            normalized = {
                str(key).strip().lower(): value
                for key, value in row.items()
                if key is not None
            }
            observed_raw = normalized.get("date")
            if not isinstance(observed_raw, str):
                continue
            observed_at = observed_raw.strip()
            try:
                datetime.strptime(observed_at, "%Y-%m-%d")
            except ValueError:
                continue

            values: dict[str, float] = {}
            valid = True
            for output_key, csv_key in _OFR_FSI_COLUMNS.items():
                raw_value = normalized.get(csv_key)
                if not isinstance(raw_value, str) or not raw_value.strip():
                    valid = False
                    break
                try:
                    value = float(raw_value.strip())
                except ValueError:
                    valid = False
                    break
                if not math.isfinite(value):
                    valid = False
                    break
                values[output_key] = value
            if valid:
                latest = observed_at, values

        if latest is None:
            return result
        observed_at, values = latest
        result.update(values)
        result["observed_at"] = observed_at
        result["status"] = _financial_stress_status(values["ofr_fsi"])
        result["data_status"] = "ok"
    except Exception:
        return result
    return result


def fetch_yield_curve_analysis() -> dict[str, Any]:
    """收益率曲线分析 — 倒挂/陡峭化信号"""
    result = {"status": "unknown", "signal": None, "details": {}}
    try:
        yields = fetch_treasury_yields()
        result["details"] = yields
        if yields.get("2Y") and yields.get("10Y"):
            spread = yields["10Y"] - yields["2Y"]
            result["spread_2y10y"] = round(spread, 3)
            if spread < -0.4:
                result["status"] = "deep_inverted"  # 深度倒挂 → 衰退信号
                result["signal"] = "recession_warning"
            elif spread < 0:
                result["status"] = "inverted"  # 倒挂中
                result["signal"] = "recession_watch"
            elif spread < 0.5:
                result["status"] = "flat"  # 平坦化
                result["signal"] = "uncertainty"
            elif spread < 1.5:
                result["status"] = "normal"  # 正常陡峭
                result["signal"] = "normal"
            else:
                result["status"] = "steep"  # 陡峭 → 增长预期
                result["signal"] = "growth_expectation"
        if yields.get("10Y") and yields.get("30Y"):
            result["spread_10y30y"] = round(yields["30Y"] - yields["10Y"], 3)
    except Exception:
        pass
    return result


# ════════════════════════════════════════════
# 2. 风险场景评分
# ════════════════════════════════════════════


def _stress_measure(
    financial_stress_data: Any,
    legacy_credit_data: Any = None,
) -> tuple[str | None, float | None]:
    """Read OFR FSI first, with HY OAS accepted only for stored old snapshots."""
    candidates = (financial_stress_data, legacy_credit_data)
    for payload in candidates:
        if not isinstance(payload, dict):
            continue
        if payload.get("data_status") == "unavailable":
            continue
        ofr_fsi = _numeric(payload.get("ofr_fsi"))
        if ofr_fsi is not None:
            return "ofr_fsi", ofr_fsi
        hy_oas = _numeric(payload.get("hy_oas"))
        if hy_oas is not None:
            return "legacy_hy_oas", hy_oas
    return None, None


def _score_completeness(inputs: dict[str, float | None]) -> tuple[str, list[str]]:
    missing = [name for name, value in inputs.items() if value is None]
    if not missing:
        return "ok", []
    if len(missing) == len(inputs):
        return "unavailable", missing
    return "partial", missing


_SCORE_INPUT_LABELS = {
    "vix": "VIX",
    "yield_curve": "收益率曲线",
    "dxy": "美元指数 DXY",
    "financial_stress": "OFR 金融压力",
}


def _missing_input_text(missing_inputs: list[str]) -> str:
    return "、".join(_SCORE_INPUT_LABELS.get(key, key) for key in missing_inputs)


def score_recession_risk(
    vix_data: dict,
    yield_curve: dict,
    financial_stress_data: dict | None = None,
    *,
    credit_data: dict | None = None,
) -> dict:
    """衰退风险评估 (0-100)"""
    score = 30  # 基准分
    signals = []

    # VIX 信号
    vix_val = _numeric(vix_data.get("value")) if isinstance(vix_data, dict) else None
    if vix_val is not None:
        if vix_val > 35:
            score += 30
            signals.append("VIX极度恐慌")
        elif vix_val > 28:
            score += 20
            signals.append("VIX恐慌加剧")
        elif vix_val > 20:
            score += 10
            signals.append("VIX偏高")

    # 收益率曲线信号
    spread = (
        _numeric(yield_curve.get("spread_2y10y"))
        if isinstance(yield_curve, dict)
        else None
    )
    if spread is not None:
        if spread < -0.4:
            score += 25
            signals.append("收益率曲线深度倒挂")
        elif spread < 0:
            score += 15
            signals.append("收益率曲线倒挂")
        elif spread < 0.3:
            score += 5
            signals.append("收益率曲线平坦化")

    # 新快照使用 OFR FSI；旧快照仍可按原 HY OAS 口径重算。
    stress_kind, stress_value = _stress_measure(financial_stress_data, credit_data)
    if stress_kind == "ofr_fsi" and stress_value is not None:
        if stress_value > 5:
            score += 25
            signals.append("OFR FSI > 5 → 系统性金融压力极高")
        elif stress_value > 2:
            score += 15
            signals.append("OFR FSI > 2 → 金融压力显著高于历史均值")
        elif stress_value > 0:
            score += 5
            signals.append("OFR FSI 高于历史均值")
    elif stress_kind == "legacy_hy_oas" and stress_value is not None:
        if stress_value > 500:
            score += 25
            signals.append("高收益债利差飙升")
        elif stress_value > 350:
            score += 15
            signals.append("高收益债利差扩大")
        elif stress_value > 200:
            score += 5

    score = min(score, 100)
    level = "critical" if score >= 75 else "high" if score >= 55 else "medium" if score >= 40 else "low"
    data_status, missing_inputs = _score_completeness({
        "vix": vix_val,
        "yield_curve": spread,
        "financial_stress": stress_value,
    })
    base_interpretation = {
        "critical": "🚨 衰退风险极高 — 多项指标同时触发预警",
        "high": "⚠️ 衰退风险偏高 — 需密切关注",
        "medium": "📌 衰退风险中等 — 部分指标发出预警",
        "low": "🟢 衰退风险较低 — 宏观环境正常",
    }.get(level, "")
    if missing_inputs:
        missing_text = _missing_input_text(missing_inputs)
        qualifier = (
            f"数据不完整（缺少 {missing_text}），当前分数仅反映可用指标，"
            "不足以确认宏观环境处于低风险状态"
        )
        interpretation = (
            f"⚪ {qualifier}" if level == "low"
            else f"{base_interpretation}；{qualifier}"
        )
    else:
        interpretation = base_interpretation

    return {
        "score": score,
        "level": level,
        "signals": signals,
        "data_status": data_status,
        "missing_inputs": missing_inputs,
        "interpretation": interpretation,
    }


def score_market_stress(
    vix_data: dict,
    dxy_data: dict,
    financial_stress_data: dict | None = None,
    *,
    credit_data: dict | None = None,
) -> dict:
    """市场压力/流动性风险评估 (0-100)"""
    score = 20
    signals = []

    vix_val = _numeric(vix_data.get("value")) if isinstance(vix_data, dict) else None
    if vix_val is not None:
        if vix_val > 35:
            score += 25
            signals.append("VIX > 35 → 市场极度恐慌")
        elif vix_val > 28:
            score += 15
            signals.append("VIX > 28 → 恐慌加剧")
        elif vix_val > 20:
            score += 5

    dxy_val = _numeric(dxy_data.get("value")) if isinstance(dxy_data, dict) else None
    dxy_chg = (
        _numeric(dxy_data.get("change_pct"))
        if isinstance(dxy_data, dict)
        else None
    )
    if dxy_val is not None:
        if dxy_val > 108:
            score += 20
            signals.append("美元指数 > 108 → 新兴市场压力")
        elif dxy_val > 105:
            score += 10
            signals.append("美元指数偏高")
        if dxy_chg is not None and abs(dxy_chg) > 1:
            score += 10
            signals.append(f"美元单日波动 {dxy_chg:+.2f}%")

    stress_kind, stress_value = _stress_measure(financial_stress_data, credit_data)
    if stress_kind == "ofr_fsi" and stress_value is not None:
        if stress_value > 5:
            score += 15
            signals.append("OFR FSI > 5 → 系统性金融压力极高")
        elif stress_value > 2:
            score += 10
            signals.append("OFR FSI > 2 → 金融压力显著高于历史均值")
        elif stress_value > 0:
            score += 5
            signals.append("OFR FSI 高于历史均值")
    elif (
        stress_kind == "legacy_hy_oas"
        and stress_value is not None
        and stress_value > 400
    ):
        score += 15
        signals.append("信用利差显著扩大")

    score = min(score, 100)
    level = "critical" if score >= 70 else "high" if score >= 50 else "medium" if score >= 35 else "low"
    data_status, missing_inputs = _score_completeness({
        "vix": vix_val,
        "dxy": dxy_val,
        "financial_stress": stress_value,
    })
    base_interpretation = {
        "critical": "🚨 市场压力极大 — 流动性紧缩风险",
        "high": "⚠️ 市场压力偏高 — 警惕流动性拐点",
        "medium": "📌 市场压力中等 — 正常波动范围",
        "low": "🟢 市场压力较低 — 流动性充裕",
    }.get(level, "")
    if missing_inputs:
        missing_text = _missing_input_text(missing_inputs)
        qualifier = (
            f"数据不完整（缺少 {missing_text}），当前分数仅反映可用指标，"
            "不足以确认流动性处于宽松状态"
        )
        interpretation = (
            f"⚪ {qualifier}" if level == "low"
            else f"{base_interpretation}；{qualifier}"
        )
    else:
        interpretation = base_interpretation

    return {
        "score": score,
        "level": level,
        "signals": signals,
        "data_status": data_status,
        "missing_inputs": missing_inputs,
        "interpretation": interpretation,
    }


def score_geopolitical_risk(fed_items: list, pboc_items: list, cctv_items: list) -> dict:
    """地缘政治风险评估 — 基于新闻内容关键词"""
    score = 20
    signals = []
    keywords_high = ["制裁", "sanctions", "关税", "tariff", "军事", "military",
                     "冲突", "conflict", "战争", "war", "核", "nuclear",
                     "脱钩", "decoupling", "封锁", "blockade"]
    keywords_med = ["贸易", "trade", "紧张", "tension", "抗议", "protest",
                    "限制", "restriction", "调查", "investigation"]

    all_text = ""
    for item in fed_items + pboc_items + cctv_items:
        all_text += (item.get("title", "") + " ") + (item.get("text", "") + " ")

    high_hits = sum(1 for kw in keywords_high if kw.lower() in all_text.lower())
    med_hits = sum(1 for kw in keywords_med if kw.lower() in all_text.lower())

    if high_hits >= 3:
        score += 35
        signals.append(f"检测到 {high_hits} 个高风险关键词")
    elif high_hits >= 1:
        score += 20
        signals.append(f"检测到 {high_hits} 个高风险关键词")

    if med_hits >= 5:
        score += 15
    elif med_hits >= 2:
        score += 5

    score = min(score, 100)
    level = "critical" if score >= 70 else "high" if score >= 50 else "medium" if score >= 35 else "low"

    return {
        "score": score,
        "level": level,
        "signals": signals,
        "interpretation": {
            "critical": "🚨 地缘风险极高 — 重大冲突/制裁事件",
            "high": "⚠️ 地缘风险偏高 — 关注局势升级",
            "medium": "📌 地缘风险中等 — 正常国际博弈",
            "low": "🟢 地缘风险较低 — 环境稳定",
        }.get(level, ""),
    }


def score_ai_bubble_risk() -> dict:
    """AI 泡沫风险评估 — 基于可获取的公开数据"""
    # 注意：此评估需要搜索最新数据，AI agent 应补充搜索
    result = {
        "score": 40,  # 默认中等偏高
        "level": "medium",
        "signals": ["需要搜索 NVDA/AVGO 估值、AI 板块资金流向等数据"],
        "search_queries": [
            "NVDA PE ratio forward 2026",
            "S&P 500 technology sector concentration 2026",
            "AI stocks valuation bubble analysis 2026",
            "Semiconductor ETF fund flows 2026",
        ],
        "interpretation": "需要 AI agent 搜索最新数据后补充评估",
    }
    return result


def score_china_risk(usd_cny: dict, pboc_items: list) -> dict:
    """中国系统性风险评估"""
    score = 25
    signals = []

    rate = usd_cny.get("rate")
    if rate:
        if rate > 7.30:
            score += 30
            signals.append(f"USD/CNY {rate} → 破7.3警戒线")
        elif rate > 7.20:
            score += 20
            signals.append(f"USD/CNY {rate} → 接近7.3")
        elif rate > 7.10:
            score += 10
            signals.append(f"USD/CNY {rate} → 偏弱")
        elif rate < 6.80:
            score -= 5
            signals.append(f"USD/CNY {rate} → 人民币走强")

    # 人行政策信号
    pboc_titles = [i.get("title", "") for i in pboc_items]
    pboc_text = " ".join(pboc_titles)
    if any(kw in pboc_text for kw in ["降准", "降息", "逆回购", "MLF", "LPR"]):
        score += 10
        signals.append("人行有货币政策调整信号")

    score = min(max(score, 0), 100)
    level = "critical" if score >= 70 else "high" if score >= 50 else "medium" if score >= 35 else "low"

    return {
        "score": score,
        "level": level,
        "signals": signals,
        "interpretation": {
            "critical": "🚨 中国系统性风险高 — 汇率/债务/通缩多重压力",
            "high": "⚠️ 中国风险偏高 — 需关注政策应对",
            "medium": "📌 中国风险中等 — 正常波动",
            "low": "🟢 中国风险较低 — 环境稳定",
        }.get(level, ""),
    }


# ════════════════════════════════════════════
# 3. 投资机会识别
# ════════════════════════════════════════════

def identify_opportunities(
    vix_data: dict,
    gold_oil: dict,
    yield_curve: dict,
    usd_cny: dict,
) -> list[dict]:
    """从极端定价中识别潜在投资机会"""
    opportunities = []

    vix_val = vix_data.get("value")

    # 机会1: VIX 极端恐慌 → 抄底信号
    if vix_val and vix_val > 35:
        opportunities.append({
            "type": "panic_buying",
            "asset": "美股大盘 (SPY/QQQ)",
            "signal": f"VIX {vix_val} → 极度恐慌，历史经验表明此时买入持有1年胜率>85%",
            "confidence": "high",
            "timeframe": "中长期 (6-12个月)",
            "risk": "需确认恐慌是否由结构性危机引发",
        })
    elif vix_val and vix_val > 28:
        opportunities.append({
            "type": "panic_buying",
            "asset": "美股大盘 (SPY/QQQ)",
            "signal": f"VIX {vix_val} → 恐慌加剧，可分批建仓",
            "confidence": "medium",
            "timeframe": "中长期 (3-6个月)",
            "risk": "等待恐慌进一步释放后再重仓",
        })

    # 机会2: 收益率曲线从倒挂转正 → 经济复苏信号
    spread = yield_curve.get("spread_2y10y")
    if spread is not None and -0.2 < spread < 0.3:
        opportunities.append({
            "type": "curve_normalization",
            "asset": "金融股 / 周期股",
            "signal": f"收益率曲线 {spread:+.3f}% → 接近正常化，银行利差改善",
            "confidence": "medium",
            "timeframe": "中期 (1-3个月)",
            "risk": "若倒挂加深则信号失效",
        })

    # 机会3: 黄金回调 → 避险配置机会
    gold_price = gold_oil.get("gold", {}).get("price")
    gold_chg = gold_oil.get("gold", {}).get("change_pct")
    if gold_price and gold_chg and gold_chg < -2:
        opportunities.append({
            "type": "dip_buying",
            "asset": "黄金 (IAU/GLD)",
            "signal": f"黄金回调 {gold_chg}% → 逢低配置避险资产",
            "confidence": "medium",
            "timeframe": "中长期 (3-12个月)",
            "risk": "美元走强可能继续压制金价",
        })

    # 机会4: 人民币贬值 → 出口/美元资产受益
    rate = usd_cny.get("rate")
    if rate and rate > 7.20:
        opportunities.append({
            "type": "currency_tailwind",
            "asset": "出口型企业 / 美元计价资产",
            "signal": f"USD/CNY {rate} → 人民币贬值利好出口和美元资产",
            "confidence": "medium",
            "timeframe": "短期 (1-3个月)",
            "risk": "人行可能干预汇率",
        })

    return opportunities


# ════════════════════════════════════════════
# 4. 黑天鹅场景推演
# ════════════════════════════════════════════

def generate_black_swan_scenarios(
    vix_data: dict,
    yield_curve: dict,
    fed_items: list,
    pboc_items: list,
) -> list[dict]:
    """基于当前数据推演可能的黑天鹅场景"""
    scenarios = []

    # 场景1: AI 泡沫破裂
    scenarios.append({
        "id": "bs_ai_bubble",
        "name": "AI 泡沫破裂",
        "probability": "medium",
        "impact": "severe",
        "description": "NVDA/AVGO 等 AI 龙头业绩不及预期，引发 AI 板块估值重置，半导体产业链暴跌",
        "trigger": "NVDA 财报 miss / 主要客户削减资本开支 / 竞争对手突破",
        "affected_assets": ["NVDA", "AVGO", "TSM", "SOXL", "NVDL", "AMD", "MU"],
        "hedge": "做空 SOXS / 买入 VIX 看涨 / 减仓半导体杠杆ETF",
        "timeframe": "1-3个月",
    })

    # 场景2: 美国衰退硬着陆
    scenarios.append({
        "id": "bs_us_recession",
        "name": "美国经济硬着陆",
        "probability": "low_to_medium",
        "impact": "severe",
        "description": "就业数据恶化 + 消费疲软 → 美联储被迫紧急降息 → 美股大幅回调",
        "trigger": "非农连续负增长 / 零售数据大幅低于预期 / 信用利差飙升",
        "affected_assets": ["TSLA", "MSFT", "META", "SPY", "TSLL", "NVDL", "几乎所有风险资产"],
        "hedge": "增持黄金/美债/VIX / 减仓杠杆ETF / 买入SPXS对冲",
        "timeframe": "3-6个月",
    })

    # 场景3: 中国通缩危机深化
    scenarios.append({
        "id": "bs_china_deflation",
        "name": "中国通缩螺旋深化",
        "probability": "medium",
        "impact": "high",
        "description": "房地产持续下行 → 通缩预期固化 → 消费投资全面萎缩 → 人民币承压",
        "trigger": "CPI持续负增长 / 房地产销售再创新低 / 信用事件频发",
        "affected_assets": ["BABA", "NIO", "BILI", "XPEV", "YINN", "CAF", "A股市场"],
        "hedge": "减仓中概股 / 增持高股息A股 / 配置黄金",
        "timeframe": "6-12个月",
    })

    # 场景4: 地缘冲突升级（台海）
    scenarios.append({
        "id": "bs_taiwan_conflict",
        "name": "台海地缘冲突升级",
        "probability": "low",
        "impact": "catastrophic",
        "description": "台海局势紧张 → 全球半导体供应链中断 → TSM 产能受影响 → 全球科技股暴跌",
        "trigger": "军事演习升级 / 外交关系恶化 / 美国对台军售激化",
        "affected_assets": ["TSM", "NVDA", "AVGO", "QCOM", "AMD", "SOXL", "全球科技股"],
        "hedge": "减仓半导体 / 增持黄金/石油 / 买入 VIX 期权",
        "timeframe": "不确定",
    })

    # 场景5: 美元流动性危机
    scenarios.append({
        "id": "bs_liquidity_crisis",
        "name": "美元流动性危机",
        "probability": "low",
        "impact": "severe",
        "description": "美国债务上限僵局 / 回购市场压力 → 美元荒 → 全球资产抛售",
        "trigger": "SOFR利率飙升 / 美联储紧急流动性工具启用 / 信用市场冻结",
        "affected_assets": ["几乎所有资产"],
        "hedge": "持有现金/短债 / 做多美元 / 减仓杠杆和新兴市场",
        "timeframe": "随时可能",
    })

    # 场景6: 加密货币黑天鹅
    scenarios.append({
        "id": "bs_crypto_crash",
        "name": "加密货币市场崩溃",
        "probability": "medium",
        "impact": "medium",
        "description": "主要交易所暴雷 / 监管严厉打击 / 稳定币脱锚 → 加密货币暴跌",
        "trigger": "交易所挤兑 / 美国CFTC/SEC联合行动 / 稳定币审计问题",
        "affected_assets": ["BTC", "ETH", "DOGE", "IBIT"],
        "hedge": "减仓加密资产 / 设置止损",
        "timeframe": "1-6个月",
    })

    # 基于当前数据调整概率
    vix_val = vix_data.get("value")
    if vix_val and vix_val > 25:
        # VIX 高 → 衰退概率上调
        for s in scenarios:
            if s["id"] in ("bs_us_recession", "bs_liquidity_crisis"):
                s["probability"] = "medium_to_high"

    spread = yield_curve.get("spread_2y10y")
    if spread is not None and spread < -0.3:
        for s in scenarios:
            if s["id"] == "bs_us_recession":
                s["probability"] = "high"

    return _attach_asset_tags(scenarios, "affected_assets")


# ════════════════════════════════════════════
# 4b. 标的与板块标签
# ════════════════════════════════════════════

_PARENS_TICKER_RE = re.compile(r"[（(]\s*([A-Z][A-Z0-9.\-]{0,5})\s*[)）]")
_BARE_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")


def split_asset_tags(items: Any) -> dict[str, list[str]]:
    """Separate tradeable symbols from sector and theme labels.

    The scenario lists mix the two — "NVDA" sits next to "区域银行ETF (KRE)"
    and "几乎所有资产" — so the UI cannot otherwise tell a reader which tags
    are directly tradeable.
    """
    tickers: list[str] = []
    sectors: list[str] = []
    if not isinstance(items, (list, tuple)):
        return {"tickers": tickers, "sectors": sectors}

    for raw in items:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text:
            continue
        parenthesized = _PARENS_TICKER_RE.search(text)
        if parenthesized:
            symbol = parenthesized.group(1)
            if symbol not in tickers:
                tickers.append(symbol)
            remainder = _PARENS_TICKER_RE.sub("", text).strip()
            if remainder and remainder not in sectors:
                sectors.append(remainder)
            continue
        if _BARE_TICKER_RE.match(text):
            if text not in tickers:
                tickers.append(text)
            continue
        if text not in sectors:
            sectors.append(text)

    return {"tickers": tickers, "sectors": sectors}


def _attach_asset_tags(entries: list[dict], *source_fields: str) -> list[dict]:
    """Add split tags to scenario entries without dropping the original lists."""
    for entry in entries:
        combined: list[str] = []
        for field in source_fields:
            value = entry.get(field)
            if isinstance(value, (list, tuple)):
                combined.extend(value)
        tags = split_asset_tags(combined)
        entry["tickers"] = tags["tickers"]
        entry["sectors"] = tags["sectors"]
    return entries


# ════════════════════════════════════════════
# 5. 灰犀牛事件识别
# ════════════════════════════════════════════

def identify_gray_rhinos() -> list[dict]:
    """识别正在逼近但被市场忽视的灰犀牛事件"""
    rhinos = []

    # 灰犀牛1: 美国商业地产债务到期潮
    rhinos.append({
        "id": "gr_commercial_real_estate",
        "name": "美国商业地产债务到期潮",
        "description": "2025-2027年约$2万亿商业地产贷款到期，利率高位下再融资困难，区域银行风险暴露",
        "visibility": "highly_visible",  # 明显但被忽视
        "urgency": "approaching",
        "affected_markets": ["区域银行ETF (KRE)", "商业地产REITs", "中小银行股"],
        "catalyst": "某中型银行因商业地产贷款违约而寻求救助",
        "market_impact": "可能由区域银行扩散为系统性信用收缩",
    })

    # 灰犀牛2: 美国国债可持续性
    rhinos.append({
        "id": "gr_us_debt",
        "name": "美国国债可持续性危机",
        "description": "美国国债/GDP超120%，利息支出超国防预算，长期美债需求端承压",
        "visibility": "highly_visible",
        "urgency": "gradual",
        "affected_markets": ["美债市场", "美元", "全球利率"],
        "catalyst": "美债拍卖需求疲软 / 外国央行减持 / 信用评级下调",
        "market_impact": "长期利率上升可能压制高估值科技股与成长资产",
    })

    # 灰犀牛3: AI 资本开支回报率
    rhinos.append({
        "id": "gr_ai_capex",
        "name": "AI 资本开支回报率质疑",
        "description": "科技巨头AI资本开支超$3000亿/年，但AI收入转化尚未清晰，若回报不及预期将引发资本开支削减",
        "visibility": "moderate",
        "urgency": "approaching",
        "affected_markets": ["NVDA", "AVGO", "半导体设备", "数据中心REITs"],
        "catalyst": "MSFT/GOOGL/AMZN财报中AI收入占比低于预期",
        "market_impact": "可能冲击AI半导体链，杠杆行业ETF会放大波动",
    })

    # 灰犀牛4: 日本央行加息冲击
    rhinos.append({
        "id": "gr_boj_hike",
        "name": "日本央行加息引发套利交易平仓",
        "description": "日元套利交易规模巨大（估计$1万亿+），日央行继续加息将引发carry trade大规模平仓",
        "visibility": "moderate",
        "urgency": "approaching",
        "affected_markets": ["日元", "日经", "全球风险资产", "新兴市场"],
        "catalyst": "日本CPI超预期 / 日央行鹰派表态",
        "market_impact": "短期冲击全球风险资产，类似日元套利交易集中平仓",
    })

    # 灰犀牛5: 全球贸易摩擦升级
    rhinos.append({
        "id": "gr_trade_war",
        "name": "全球贸易摩擦全面升级",
        "description": "中美/美欧贸易摩擦持续升温，关税范围扩大，供应链重构加速",
        "visibility": "highly_visible",
        "urgency": "gradual",
        "affected_markets": ["半导体", "新能源", "汽车", "消费品"],
        "catalyst": "新一轮关税宣布 / 技术出口管制加码",
        "market_impact": "可能压制跨境科技与出口链，并利好部分国产替代方向",
    })

    return _attach_asset_tags(rhinos, "affected_markets")


# ════════════════════════════════════════════
# 5b. 监控到的具体事件
# ════════════════════════════════════════════

# Central banks publish on a weekly-to-monthly cadence, so a tight window
# would hide the very releases worth monitoring. Recency is conveyed by
# sorting and by showing each event's age instead of by hiding it.
POLICY_EVENT_MAX_AGE_HOURS = 14 * 24
MONITORED_EVENT_LIMIT = 24

# PBoC announcement URLs embed the publication time as YYYYMMDDHHMMSS.
_URL_TIMESTAMP_RE = re.compile(r"/(\d{14})\d*/")

_POLICY_HIGH_WORDS = (
    "rate decision", "fomc", "emergency", "sanction", "tariff",
    "intervention", "downgrade", "default",
    "降息", "加息", "制裁", "关税", "干预", "降准", "违约", "评级下调",
)
_POLICY_MEDIUM_WORDS = (
    "inflation", "employment", "guidance", "outlook", "speech", "minutes",
    "通胀", "就业", "讲话", "纪要", "展望", "政策",
)

# Keyword to tradeable-symbol / sector mapping. Deterministic and explicit:
# a policy headline never implies a position, only an area to look at.
_POLICY_TAG_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    (("rate", "fomc", "interest", "降息", "加息", "利率", "lpr"),
     ("TLT", "SPY"), ("美债", "利率敏感板块")),
    (("inflation", "cpi", "通胀", "物价"),
     ("TIP", "GLD"), ("通胀受益板块",)),
    (("tariff", "trade", "关税", "贸易", "出口管制", "sanction", "制裁"),
     ("SOXL", "FXI"), ("半导体", "跨境贸易链")),
    (("employment", "payroll", "jobless", "就业", "非农"),
     ("SPY",), ("消费", "周期股")),
    (("yuan", "renminbi", "人民币", "汇率", "外汇"),
     ("CNY=X", "FXI"), ("中概股", "出口链")),
    (("bank", "liquidity", "银行", "流动性", "存款保险"),
     ("KRE",), ("区域银行", "信用市场")),
    (("housing", "real estate", "房地产", "楼市"),
     ("XHB",), ("地产链",)),
)


def _parse_feed_time(value: Any) -> datetime | None:
    """Parse a feed timestamp, rejecting anything without a real offset."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        )
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _url_embedded_time(url: Any) -> datetime | None:
    """Recover the publication time PBoC encodes in its announcement paths.

    These pages carry no feed timestamp, so without this every Chinese
    central-bank notice would be quarantined as time-unverifiable.
    """
    if not isinstance(url, str):
        return None
    match = _URL_TIMESTAMP_RE.search(url)
    if not match:
        return None
    try:
        stamp = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return stamp.replace(tzinfo=CN_TZ).astimezone(timezone.utc)


def _classify_policy_severity(text: str) -> str:
    blob = text.lower()
    if any(word in blob for word in _POLICY_HIGH_WORDS):
        return "high"
    if any(word in blob for word in _POLICY_MEDIUM_WORDS):
        return "medium"
    return "low"


def _policy_tags(text: str) -> dict[str, list[str]]:
    blob = text.lower()
    tickers: list[str] = []
    sectors: list[str] = []
    for keywords, mapped_tickers, mapped_sectors in _POLICY_TAG_RULES:
        if not any(keyword in blob for keyword in keywords):
            continue
        for symbol in mapped_tickers:
            if symbol not in tickers:
                tickers.append(symbol)
        for sector in mapped_sectors:
            if sector not in sectors:
                sectors.append(sector)
    return {"tickers": tickers, "sectors": sectors}


def build_policy_events(
    items: Any,
    *,
    now: datetime | None = None,
    limit: int = MONITORED_EVENT_LIMIT,
) -> list[dict]:
    """Turn central-bank and policy feed items into monitored events.

    Undated items are kept but flagged, matching the KOL feed's rule that a
    missing timestamp is never silently treated as "just published".
    """
    current = now or datetime.now(timezone.utc)
    events: list[dict] = []
    seen: set[str] = set()

    for item in items if isinstance(items, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = unescape(str(item.get("url") or "")).strip()
        if not title or not url or url in seen:
            continue

        published = _parse_feed_time(item.get("date") or item.get("published_at"))
        if published is None:
            published = _url_embedded_time(url)
        if published is not None:
            age_hours = (current - published).total_seconds() / 3600
            if age_hours > POLICY_EVENT_MAX_AGE_HOURS or age_hours < -0.083:
                continue

        seen.add(url)
        tags = _policy_tags(title)
        events.append({
            "id": f"pol_{abs(hash(url)) % (10 ** 10):010d}",
            "kind": "policy",
            "title": title,
            "url": url,
            "source": str(item.get("source") or "").strip() or "未知来源",
            "published_at": published.isoformat() if published else None,
            "time_status": "verified" if published else "unknown",
            "severity": _classify_policy_severity(title),
            "tickers": tags["tickers"],
            "sectors": tags["sectors"],
        })
        if len(events) >= limit:
            break

    return events


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def detect_indicator_events(
    current_market: Any,
    previous_market: Any,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Report indicators that moved materially since the previous snapshot.

    Without a previous snapshot there is no move to report, so this abstains
    rather than presenting a level as if it were an event.
    """
    if not isinstance(current_market, dict) or not isinstance(previous_market, dict):
        return []
    if not previous_market:
        return []

    stamp = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    events: list[dict] = []

    def add(
        event_id,
        title,
        prev,
        curr,
        unit,
        severity,
        tickers,
        sectors,
        note,
        source="风险雷达指标监控",
    ):
        events.append({
            "id": event_id,
            "kind": "indicator",
            "title": title,
            "source": source,
            "published_at": stamp.isoformat(),
            "time_status": "verified",
            "severity": severity,
            "previous_value": prev,
            "current_value": curr,
            "unit": unit,
            "note": note,
            "tickers": list(tickers),
            "sectors": list(sectors),
        })

    vix_now = _numeric((current_market.get("vix") or {}).get("value"))
    vix_before = _numeric((previous_market.get("vix") or {}).get("value"))
    if vix_now is not None and vix_before is not None:
        change = vix_now - vix_before
        if abs(change) >= 3.0 or (vix_now >= 25 > vix_before):
            rising = change > 0
            add(
                "ind_vix_spike" if rising else "ind_vix_collapse",
                f"VIX {'跳升' if rising else '快速回落'} {abs(change):.1f} 点"
                f"（{vix_before:.1f} → {vix_now:.1f}）",
                vix_before, vix_now, "point",
                "high" if vix_now >= 25 else "medium",
                ("VXX", "SPY") if rising else ("SPY",),
                ("波动率", "美股大盘"),
                "恐慌指数快速变化通常先于风险资产重定价" if rising
                else "波动率回落，风险偏好可能修复",
            )

    current_financial_stress = current_market.get("financial_stress") or {}
    previous_financial_stress = previous_market.get("financial_stress") or {}
    ofr_now = (
        _numeric(current_financial_stress.get("ofr_fsi"))
        if isinstance(current_financial_stress, dict)
        and current_financial_stress.get("data_status") != "unavailable"
        else None
    )
    ofr_before = (
        _numeric(previous_financial_stress.get("ofr_fsi"))
        if isinstance(previous_financial_stress, dict)
        and previous_financial_stress.get("data_status") != "unavailable"
        else None
    )
    if ofr_now is not None and ofr_before is not None:
        change = ofr_now - ofr_before
        crossed_level = any(
            (ofr_now > threshold) != (ofr_before > threshold)
            for threshold in (0.0, 2.0, 5.0)
        )
        if change != 0 and (abs(change) >= 1.0 or crossed_level):
            rising = change > 0
            add(
                "ind_ofr_fsi_rise" if rising else "ind_ofr_fsi_fall",
                f"OFR FSI {'上升' if rising else '回落'} {abs(change):.2f} 点"
                f"（{ofr_before:.2f} → {ofr_now:.2f}）",
                ofr_before,
                ofr_now,
                "index",
                "high" if rising and ofr_now > 2 else "medium",
                ("HYG", "KRE", "SPY"),
                ("信用市场", "融资流动性", "系统性金融压力"),
                "OFR FSI 是相对历史均值衡量全球金融市场压力的综合指标",
                source=OFR_FSI_SOURCE,
            )

    # Backward compatibility for comparisons between snapshots from the OAS era.
    hy_now = _numeric((current_market.get("credit_spreads") or {}).get("hy_oas"))
    hy_before = _numeric((previous_market.get("credit_spreads") or {}).get("hy_oas"))
    if hy_now is not None and hy_before is not None:
        change = hy_now - hy_before
        if abs(change) >= 25:
            widening = change > 0
            add(
                "ind_credit_widening" if widening else "ind_credit_tightening",
                f"高收益债利差{'走阔' if widening else '收窄'} {abs(change):.0f}bp"
                f"（{hy_before:.0f} → {hy_now:.0f}bp）",
                hy_before, hy_now, "basis_points",
                "high" if widening and hy_now >= 450 else "medium",
                ("HYG", "KRE"),
                ("信用市场", "区域银行"),
                "信用利差是系统性压力最直接的先行指标",
            )

    curve_now = _numeric((current_market.get("yield_curve") or {}).get("spread_2y10y"))
    curve_before = _numeric((previous_market.get("yield_curve") or {}).get("spread_2y10y"))
    if curve_now is not None and curve_before is not None:
        if (curve_now < 0) != (curve_before < 0):
            inverting = curve_now < 0
            add(
                "ind_curve_inversion" if inverting else "ind_curve_normalization",
                f"2s10s 收益率曲线{'转为倒挂' if inverting else '结束倒挂'}"
                f"（{curve_before:+.2f} → {curve_now:+.2f}）",
                curve_before, curve_now, "percent",
                "high" if inverting else "medium",
                ("TLT", "KRE"),
                ("银行股", "利率敏感板块"),
                "曲线形态反转历来与衰退预期切换同步",
            )

    dxy_now = _numeric((current_market.get("dxy") or {}).get("value"))
    dxy_before = _numeric((previous_market.get("dxy") or {}).get("value"))
    if dxy_now is not None and dxy_before and dxy_before != 0:
        change_pct = (dxy_now - dxy_before) / dxy_before * 100
        if abs(change_pct) >= 1.0:
            strengthening = change_pct > 0
            add(
                "ind_dxy_move",
                f"美元指数{'走强' if strengthening else '走弱'} {abs(change_pct):.1f}%"
                f"（{dxy_before:.2f} → {dxy_now:.2f}）",
                dxy_before, dxy_now, "percent",
                "medium",
                ("UUP", "GLD"),
                ("新兴市场", "大宗商品"),
                "美元快速变动会重新定价新兴市场与大宗商品",
            )

    cny_now = _numeric((current_market.get("usd_cny") or {}).get("rate"))
    cny_before = _numeric((previous_market.get("usd_cny") or {}).get("rate"))
    if cny_now is not None and cny_before is not None:
        for threshold in (7.20, 7.30):
            if (cny_now >= threshold) != (cny_before >= threshold):
                add(
                    f"ind_usdcny_{str(threshold).replace('.', '')}",
                    f"美元/人民币{'升破' if cny_now >= threshold else '回落至'} "
                    f"{threshold:.2f}（{cny_before:.4f} → {cny_now:.4f}）",
                    cny_before, cny_now, "rate",
                    "high" if threshold >= 7.30 else "medium",
                    ("FXI", "CNY=X"),
                    ("中概股", "出口链"),
                    "汇率关键位切换会牵动中概与出口链定价",
                )
                break

    return events


def assemble_monitored_events(
    *,
    policy_events: Any = None,
    indicator_events: Any = None,
    limit: int = MONITORED_EVENT_LIMIT,
) -> list[dict]:
    """Merge event streams, newest first, with undated records last."""
    merged: list[dict] = []
    for group in (indicator_events, policy_events):
        if isinstance(group, (list, tuple)):
            merged.extend(item for item in group if isinstance(item, dict))

    def is_dated(event: dict) -> bool:
        published = event.get("published_at")
        return isinstance(published, str) and bool(published)

    dated = sorted(
        (event for event in merged if is_dated(event)),
        key=lambda event: event["published_at"],
        reverse=True,
    )
    undated = [event for event in merged if not is_dated(event)]
    return (dated + undated)[:limit]


# ════════════════════════════════════════════
# 6. 综合报告生成
# ════════════════════════════════════════════

def _safe_fetch(fn, default=None):
    """安全执行数据获取函数，异常时返回默认值"""
    try:
        return fn()
    except Exception:
        return default if default is not None else {}


def _reuse_recent_financial_stress(
    current: Any,
    previous_market_data: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reuse one complete recent OFR point when today's download is unavailable."""
    if isinstance(current, dict) and _stress_measure(current)[0] == "ofr_fsi":
        return current
    fallback = (
        current
        if isinstance(current, dict) and current
        else _empty_financial_stress()
    )
    if not isinstance(previous_market_data, dict):
        return fallback
    previous = previous_market_data.get("financial_stress")
    if not isinstance(previous, dict) or previous.get("data_status") == "unavailable":
        return fallback

    observed_raw = previous.get("observed_at")
    if not isinstance(observed_raw, str):
        return fallback
    try:
        observed_date = datetime.strptime(observed_raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return fallback
    current_time = now or datetime.now(timezone.utc)
    age_days = (current_time.date() - observed_date).days
    if age_days < 0 or age_days > OFR_FSI_FALLBACK_MAX_AGE_DAYS:
        return fallback

    values = {
        key: _numeric(previous.get(key))
        for key in _OFR_FSI_COLUMNS
    }
    if any(value is None for value in values.values()):
        return fallback

    reused = {
        **values,
        "observed_at": observed_date.isoformat(),
        "source": OFR_FSI_SOURCE,
        "source_url": OFR_FSI_SOURCE_URL,
        "unit": "index",
        "status": _financial_stress_status(values["ofr_fsi"]),
        "data_status": "stale",
        "stale": True,
        "is_stale": True,
        "note": (
            "OFR download unavailable; reused the most recent complete point "
            f"({age_days} days old)"
        ),
    }
    return reused


def generate_risk_report(previous_market_data: dict | None = None) -> dict:
    """生成完整风险雷达报告。

    传入上一份快照的 market_data 才能判断指标是否发生异动；缺少历史时只输出
    政策事件，不把当前水平当成"刚刚发生的事件"。
    """
    # 采集数据（每个数据源独立超时保护）
    vix_data = _safe_fetch(fetch_vix)
    treasury_data = _safe_fetch(fetch_treasury_yields)
    usd_cny_data = _safe_fetch(fetch_usd_cny)
    gold_oil_data = _safe_fetch(fetch_gold_oil)
    dxy_data = _safe_fetch(fetch_dxy)
    financial_stress_data = _reuse_recent_financial_stress(
        _safe_fetch(fetch_financial_stress),
        previous_market_data,
    )
    yield_curve = _safe_fetch(fetch_yield_curve_analysis)
    fed_items = _safe_fetch(fetch_fed, default=[])
    pboc_items = _safe_fetch(fetch_pboc, default=[])
    cctv_items = _safe_fetch(fetch_cctv_news, default=[])

    # 评分
    recession = score_recession_risk(vix_data, yield_curve, financial_stress_data)
    market_stress = score_market_stress(vix_data, dxy_data, financial_stress_data)
    geopolitical = score_geopolitical_risk(fed_items, pboc_items, cctv_items)
    china_risk = score_china_risk(usd_cny_data, pboc_items)

    # 综合风险评分
    composite_score = round(
        recession["score"] * 0.30 +
        market_stress["score"] * 0.25 +
        geopolitical["score"] * 0.20 +
        china_risk["score"] * 0.25
    )
    composite_level = (
        "critical" if composite_score >= 70 else
        "high" if composite_score >= 55 else
        "medium" if composite_score >= 40 else
        "low"
    )
    composite_missing_inputs = list(dict.fromkeys(
        recession.get("missing_inputs", [])
        + market_stress.get("missing_inputs", [])
    ))
    composite_data_status = "partial" if composite_missing_inputs else "ok"
    composite_label = {
        "critical": "🚨 综合风险极高 — 系统性风险警报",
        "high": "⚠️ 综合风险偏高 — 需警惕",
        "medium": "📌 综合风险中等 — 正常关注",
        "low": "🟢 综合风险较低 — 环境良好",
    }.get(composite_level, "")
    if composite_level == "low" and composite_missing_inputs:
        composite_label = "⚪ 综合分仅反映可用指标 — 数据不完整，不足以确认低风险环境"

    # 机会与场景
    opportunities = identify_opportunities(vix_data, gold_oil_data, yield_curve, usd_cny_data)
    black_swans = generate_black_swan_scenarios(vix_data, yield_curve, fed_items, pboc_items)
    gray_rhinos = identify_gray_rhinos()

    # 按概率排序黑天鹅
    prob_order = {"high": 0, "medium_to_high": 1, "medium": 2, "low_to_medium": 3, "low": 4}
    black_swans.sort(key=lambda x: prob_order.get(x["probability"], 99))

    market_data = {
        "vix": vix_data,
        "treasury": treasury_data,
        "yield_curve": yield_curve,
        "usd_cny": usd_cny_data,
        "dxy": dxy_data,
        "gold_oil": gold_oil_data,
        "financial_stress": financial_stress_data,
    }

    # 具体监控到的事件：政策原文 + 指标异动
    fomc_items = _safe_fetch(fetch_fomc, default=[])
    fed_speeches = _safe_fetch(fetch_fed_speeches, default=[])
    pboc_speech = _safe_fetch(fetch_pboc_speech, default=[])
    monitored_events = assemble_monitored_events(
        policy_events=build_policy_events(
            list(fomc_items) + list(fed_items) + list(fed_speeches)
            + list(pboc_items) + list(pboc_speech)
        ),
        indicator_events=detect_indicator_events(market_data, previous_market_data),
    )

    report = {
        "public_schema_version": 1,
        "timestamp": now_cn(),
        "composite_risk": {
            "score": composite_score,
            "level": composite_level,
            "data_status": composite_data_status,
            "missing_inputs": composite_missing_inputs,
            "label": composite_label,
        },
        "sub_scores": {
            "recession": recession,
            "market_stress": market_stress,
            "geopolitical": geopolitical,
            "china_risk": china_risk,
        },
        "market_data": market_data,
        "monitored_events": monitored_events,
        "black_swan_scenarios": black_swans,
        "gray_rhinos": gray_rhinos,
        "opportunities": opportunities,
        "search_queries": [
            "Fear and Greed Index today",
            "S&P 500 PE ratio forward 2026",
            "Global PMI manufacturing June 2026",
            "Bitcoin fear and greed index",
            "US initial jobless claims latest",
            "China Caixin manufacturing PMI",
            "VIX futures term structure",
        ],
    }

    return report


# ════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════

def cmd_full() -> None:
    report = generate_risk_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_summary() -> None:
    """输出简洁摘要（用于紧急检查）"""
    report = generate_risk_report()
    cr = report["composite_risk"]
    print(f"RISK_RADAR:{cr['level']}:{cr['score']}")
    print(f"Time: {report['timestamp']}")
    print(f"Composite: {cr['label']}")
    for key, sub in report["sub_scores"].items():
        print(f"  {key}: {sub['level']} ({sub['score']})")
    print(f"Scenarios: {len(report['black_swan_scenarios'])} black swans, {len(report['gray_rhinos'])} gray rhinos")
    print(f"Opportunities: {len(report['opportunities'])}")


def cmd_scenarios() -> None:
    """输出黑天鹅场景"""
    report = generate_risk_report()
    for s in report["black_swan_scenarios"]:
        print(f"[{s['probability']}] {s['name']} — {s['description'][:100]}...")


def cmd_rhinos() -> None:
    """输出灰犀牛事件"""
    report = generate_risk_report()
    for r in report["gray_rhinos"]:
        print(f"[{r['urgency']}] {r['name']} — {r['description'][:100]}...")


def cmd_opportunities() -> None:
    """输出投资机会"""
    report = generate_risk_report()
    for o in report["opportunities"]:
        print(f"[{o['confidence']}] {o['asset']}: {o['signal'][:100]}...")


def cmd_help() -> None:
    print(__doc__)


COMMANDS = {
    "full": cmd_full,
    "summary": cmd_summary,
    "scenarios": cmd_scenarios,
    "rhinos": cmd_rhinos,
    "opportunities": cmd_opportunities,
    "help": cmd_help,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.stderr.write(__doc__ or "")
        return 1
    try:
        COMMANDS[sys.argv[1]]()
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
