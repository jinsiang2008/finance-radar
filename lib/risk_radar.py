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

import json
import math
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
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
    fetch_pboc,
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


def fetch_credit_spreads() -> dict[str, Any]:
    """信用利差 — 尝试从 FRED 获取（HY OAS, IG OAS）"""
    result = {
        "hy_oas": None,
        "ig_oas": None,
        "unit": "basis_points",
        "status": "unknown",
    }
    try:
        # 尝试 BAML HY OAS (FRED: BAMLH0A0HYM2)
        hy = http_get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?bgcolor=%23e1e9f0&chart_type=line&"
            "drp=0&fo=open%20sans&graph_bgcolor=%23ffffff&height=450&mode=fred&recession_bars=on&"
            "txtcolor=%23444444&ts=12&tts=12&width=1168&nt=0&thu=0&trc=0&show_legend=yes&"
            "show_axis_titles=yes&show_tooltip=yes&id=BAMLH0A0HYM2&scale=left&cosd=2025-01-01&"
            "coed=2026-12-31&line_color=%234572a7&link_values=false&line_style=solid&"
            "mark_type=none&mw=3&lw=2&ost=-99999&oet=99999&mma=0&fml=a&fq=Daily&fam=avg&"
            "fgst=lin&fgsnd=2020-02-01&line_index=1&transformation=lin&vintage_date=TODAY",
            timeout=10,
        )
        lines = hy.strip().split("\n")
        if len(lines) >= 2:
            last_line = lines[-1].strip()
            parts = last_line.split(",")
            if len(parts) >= 2 and parts[1]:
                result["hy_oas"] = round(float(parts[1]) * 100, 2)

        # 尝试 IG OAS (FRED: BAMLC0A0CM)
        ig = http_get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?bgcolor=%23e1e9f0&chart_type=line&"
            "drp=0&fo=open%20sans&graph_bgcolor=%23ffffff&height=450&mode=fred&recession_bars=on&"
            "txtcolor=%23444444&ts=12&tts=12&width=1168&nt=0&thu=0&trc=0&show_legend=yes&"
            "show_axis_titles=yes&show_tooltip=yes&id=BAMLC0A0CM&scale=left&cosd=2025-01-01&"
            "coed=2026-12-31&line_color=%234572a7&link_values=false&line_style=solid&"
            "mark_type=none&mw=3&lw=2&ost=-99999&oet=99999&mma=0&fml=a&fq=Daily&fam=avg&"
            "fgst=lin&fgsnd=2020-02-01&line_index=1&transformation=lin&vintage_date=TODAY",
            timeout=10,
        )
        lines = ig.strip().split("\n")
        if len(lines) >= 2:
            last_line = lines[-1].strip()
            parts = last_line.split(",")
            if len(parts) >= 2 and parts[1]:
                result["ig_oas"] = round(float(parts[1]) * 100, 2)

        if result["hy_oas"] and result["ig_oas"]:
            result["spread_diff"] = round(result["hy_oas"] - result["ig_oas"], 2)
            if result["hy_oas"] > 500:
                result["status"] = "critical"  # HY > 500bp → 信用恐慌
            elif result["hy_oas"] > 350:
                result["status"] = "elevated"
            elif result["hy_oas"] > 200:
                result["status"] = "normal"
            else:
                result["status"] = "low"
    except Exception:
        pass
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

def score_recession_risk(vix_data: dict, yield_curve: dict, credit_data: dict) -> dict:
    """衰退风险评估 (0-100)"""
    score = 30  # 基准分
    signals = []

    # VIX 信号
    vix_val = vix_data.get("value")
    if vix_val:
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
    spread = yield_curve.get("spread_2y10y")
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

    # 信用利差信号
    hy_oas = credit_data.get("hy_oas")
    if hy_oas:
        if hy_oas > 500:
            score += 25
            signals.append("高收益债利差飙升")
        elif hy_oas > 350:
            score += 15
            signals.append("高收益债利差扩大")
        elif hy_oas > 200:
            score += 5

    score = min(score, 100)
    level = "critical" if score >= 75 else "high" if score >= 55 else "medium" if score >= 40 else "low"

    return {
        "score": score,
        "level": level,
        "signals": signals,
        "interpretation": {
            "critical": "🚨 衰退风险极高 — 多项指标同时触发预警",
            "high": "⚠️ 衰退风险偏高 — 需密切关注",
            "medium": "📌 衰退风险中等 — 部分指标发出预警",
            "low": "🟢 衰退风险较低 — 宏观环境正常",
        }.get(level, ""),
    }


def score_market_stress(vix_data: dict, dxy_data: dict, credit_data: dict) -> dict:
    """市场压力/流动性风险评估 (0-100)"""
    score = 20
    signals = []

    vix_val = vix_data.get("value")
    if vix_val:
        if vix_val > 35:
            score += 25
            signals.append("VIX > 35 → 市场极度恐慌")
        elif vix_val > 28:
            score += 15
            signals.append("VIX > 28 → 恐慌加剧")
        elif vix_val > 20:
            score += 5

    dxy_val = dxy_data.get("value")
    dxy_chg = dxy_data.get("change_pct")
    if dxy_val:
        if dxy_val > 108:
            score += 20
            signals.append("美元指数 > 108 → 新兴市场压力")
        elif dxy_val > 105:
            score += 10
            signals.append("美元指数偏高")
        if dxy_chg and abs(dxy_chg) > 1:
            score += 10
            signals.append(f"美元单日波动 {dxy_chg:+.2f}%")

    hy_oas = credit_data.get("hy_oas")
    if hy_oas and hy_oas > 400:
        score += 15
        signals.append("信用利差显著扩大")

    score = min(score, 100)
    level = "critical" if score >= 70 else "high" if score >= 50 else "medium" if score >= 35 else "low"

    return {
        "score": score,
        "level": level,
        "signals": signals,
        "interpretation": {
            "critical": "🚨 市场压力极大 — 流动性紧缩风险",
            "high": "⚠️ 市场压力偏高 — 警惕流动性拐点",
            "medium": "📌 市场压力中等 — 正常波动范围",
            "low": "🟢 市场压力较低 — 流动性充裕",
        }.get(level, ""),
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

    return scenarios


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

    return rhinos


# ════════════════════════════════════════════
# 6. 综合报告生成
# ════════════════════════════════════════════

def _safe_fetch(fn, default=None):
    """安全执行数据获取函数，异常时返回默认值"""
    try:
        return fn()
    except Exception:
        return default if default is not None else {}


def generate_risk_report() -> dict:
    """生成完整风险雷达报告"""
    # 采集数据（每个数据源独立超时保护）
    vix_data = _safe_fetch(fetch_vix)
    treasury_data = _safe_fetch(fetch_treasury_yields)
    usd_cny_data = _safe_fetch(fetch_usd_cny)
    gold_oil_data = _safe_fetch(fetch_gold_oil)
    dxy_data = _safe_fetch(fetch_dxy)
    credit_data = _safe_fetch(fetch_credit_spreads)
    yield_curve = _safe_fetch(fetch_yield_curve_analysis)
    fed_items = _safe_fetch(fetch_fed, default=[])
    pboc_items = _safe_fetch(fetch_pboc, default=[])
    cctv_items = _safe_fetch(fetch_cctv_news, default=[])

    # 评分
    recession = score_recession_risk(vix_data, yield_curve, credit_data)
    market_stress = score_market_stress(vix_data, dxy_data, credit_data)
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

    # 机会与场景
    opportunities = identify_opportunities(vix_data, gold_oil_data, yield_curve, usd_cny_data)
    black_swans = generate_black_swan_scenarios(vix_data, yield_curve, fed_items, pboc_items)
    gray_rhinos = identify_gray_rhinos()

    # 按概率排序黑天鹅
    prob_order = {"high": 0, "medium_to_high": 1, "medium": 2, "low_to_medium": 3, "low": 4}
    black_swans.sort(key=lambda x: prob_order.get(x["probability"], 99))

    report = {
        "public_schema_version": 1,
        "timestamp": now_cn(),
        "composite_risk": {
            "score": composite_score,
            "level": composite_level,
            "label": {
                "critical": "🚨 综合风险极高 — 系统性风险警报",
                "high": "⚠️ 综合风险偏高 — 需警惕",
                "medium": "📌 综合风险中等 — 正常关注",
                "low": "🟢 综合风险较低 — 环境良好",
            }.get(composite_level, ""),
        },
        "sub_scores": {
            "recession": recession,
            "market_stress": market_stress,
            "geopolitical": geopolitical,
            "china_risk": china_risk,
        },
        "market_data": {
            "vix": vix_data,
            "treasury": treasury_data,
            "yield_curve": yield_curve,
            "usd_cny": usd_cny_data,
            "dxy": dxy_data,
            "gold_oil": gold_oil_data,
            "credit_spreads": credit_data,
        },
        "black_swan_scenarios": black_swans,
        "gray_rhinos": gray_rhinos,
        "opportunities": opportunities,
        "search_queries": [
            "Fear and Greed Index today",
            "S&P 500 PE ratio forward 2026",
            "US high yield bond spread today",
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
