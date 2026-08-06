from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        cls.javascript = (ROOT / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        cls.css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    def test_decision_cockpit_is_default_and_accessible(self) -> None:
        self.assertIn('class="tab active"', self.html)
        self.assertIn('data-view="decision"', self.html)
        self.assertIn('class="view active" id="view-decision"', self.html)
        self.assertIn('aria-labelledby="impact-matrix-title"', self.html)
        self.assertIn("data-decision-key", self.javascript)
        self.assertIn("matrix-symbol", self.css)

    def test_private_mode_uses_password_form_and_private_endpoint(self) -> None:
        self.assertIn('type="password"', self.html)
        self.assertIn('autocomplete="current-password"', self.html)
        self.assertIn("api/private/decisions", self.javascript)
        self.assertIn("api/auth/login", self.javascript)
        self.assertIn("api/auth/logout", self.javascript)
        self.assertIn("decisionRequestGeneration", self.javascript)
        self.assertIn("clearDecisionView", self.javascript)
        self.assertIn("logoutPending", self.javascript)
        self.assertIn(
            "requestGeneration !== state.decisionRequestGeneration",
            self.javascript,
        )

    def test_frontend_does_not_depend_on_legacy_private_macro_fields(self) -> None:
        self.assertNotIn("affected_positions", self.javascript)
        self.assertNotIn("portfolio_impact", self.javascript)
        # Scenario exposure now reaches the UI through the public split tags,
        # which the collector derives from affected_assets/affected_markets.
        self.assertIn("tickers", self.javascript)
        self.assertIn("sectors", self.javascript)
        self.assertIn("market_impact", self.javascript)

    def test_feed_separates_publication_time_from_collection_time(self) -> None:
        self.assertIn('id="time-status-chips"', self.html)
        self.assertIn('data-time-status="verified"', self.html)
        self.assertIn('data-time-status="unverified"', self.html)
        self.assertIn("timeStatus: \"verified\"", self.javascript)
        self.assertIn("it.published_at", self.javascript)
        self.assertIn("发布时间未知", self.javascript)
        self.assertIn("抓取", self.javascript)
        self.assertIn("个独立来源", self.javascript)
        self.assertIn('id="time-window-basis"', self.html)
        self.assertIn("按发布时间筛选", self.javascript)
        self.assertIn("隔离区按首次抓取时间筛选", self.javascript)
        self.assertNotIn(
            "it.last_seen_at || it.fetched_at",
            self.javascript,
        )

    def test_macro_view_lists_monitored_events_with_time_provenance(self) -> None:
        self.assertIn('id="macro-events-block"', self.html)
        self.assertIn('id="macro-events"', self.html)
        self.assertIn("监控到的事件", self.html)
        self.assertIn("renderMonitoredEvents", self.javascript)
        self.assertIn("monitored_events", self.javascript)
        self.assertIn("时间待核验", self.javascript)
        self.assertIn(".event-time.unverified", self.css)

    def test_macro_cards_separate_tradeable_symbols_from_sectors(self) -> None:
        self.assertIn("assetTagRow", self.javascript)
        self.assertIn("item.tickers", self.javascript)
        self.assertIn("item.sectors", self.javascript)
        self.assertIn("tag-group-label", self.javascript)
        self.assertIn(".tag.sector", self.css)
        self.assertIn(".tagline-split", self.css)

    def test_macro_prefers_ofr_financial_stress_with_legacy_fallback(self) -> None:
        self.assertIn('"financial_stress"', self.javascript)
        self.assertIn("function financialStressMetric(stress)", self.javascript)
        self.assertIn("stress?.ofr_fsi", self.javascript)
        self.assertIn('"全球金融压力"', self.javascript)
        self.assertIn('critical: "压力极高"', self.javascript)
        self.assertIn('low: "低于长期均值"', self.javascript)
        self.assertIn("`信用 ${signedMetric(stress?.credit)}`", self.javascript)
        self.assertIn("`融资 ${signedMetric(stress?.funding)}`", self.javascript)
        self.assertIn("`截至 ${fmtMonthDay(stress?.observed_at)}`", self.javascript)
        self.assertIn('"OFR FSI"', self.javascript)
        self.assertIn('stale ? "数据延迟" : ""', self.javascript)
        self.assertIn('"高收益债利差"', self.javascript)
        self.assertIn("cs.hy_oas", self.javascript)
        self.assertIn('static/app.js?v=11', self.html)
        self.assertIn(".metric.is-stale", self.css)

    def test_macro_coverage_distinguishes_stale_and_unavailable_sources(self) -> None:
        self.assertIn("source?.data_status", self.javascript)
        self.assertIn("source?.status", self.javascript)
        self.assertIn("source?.stale === true", self.javascript)
        self.assertIn("source?.is_stale === true", self.javascript)
        self.assertIn('sourceStatus === "stale"', self.javascript)
        self.assertIn(
            'state: unavailable ? "off" : stale ? "stale" : "ok"',
            self.javascript,
        )
        self.assertIn(
            'statusLabel: unavailable ? "不可用" : stale ? "数据延迟" : ""',
            self.javascript,
        )
        self.assertIn(".cov-pill.stale", self.css)
        self.assertIn(".cov-pill.off", self.css)

    def test_evidence_and_market_validation_are_visually_separated(self) -> None:
        self.assertIn("机制证据（不是因果证明）", self.javascript)
        self.assertIn("市场验证", self.javascript)
        self.assertIn("相反证据与不确定性", self.javascript)
        self.assertIn("失效条件", self.javascript)

    def test_current_topic_keys_and_asset_labels_are_user_facing(self) -> None:
        for topic_key in (
            "inflation",
            "geopolitics_trade",
            "crypto",
            "financial_system",
            "china_markets",
            "market_risk",
        ):
            self.assertIn(f"{topic_key}:", self.javascript)
        for asset_key in (
            "US:SPY",
            "US:QQQ",
            "US:NVDA",
            "US:AVGO",
            "US:AMD",
            "US:TSM",
            "US:SOXL",
            "BOND:UST_LONG",
            "COMMODITY:GOLD",
            "COMMODITY:OIL",
            "CRYPTO:BTC",
            "CRYPTO:ETH",
            "CRYPTO:DOGE",
            "FX:JPY",
            "FX:CNY",
            "INDEX:HSI",
            "INDEX:CSI300",
            "THEME:EMERGING_MARKETS",
            "THEME:FINANCIALS",
            "THEME:GLOBAL_RISK_ASSETS",
        ):
            self.assertIn(f'"{asset_key}"', self.javascript)
        self.assertIn("function assetTicker", self.javascript)
        self.assertIn("function assetLabel", self.javascript)
        self.assertIn('title="${esc(card.asset_key)}"', self.javascript)

    def test_macro_history_drives_trend_and_decision_brief(self) -> None:
        self.assertIn("macroData: null", self.javascript)
        self.assertIn("macroHistory: []", self.javascript)
        self.assertIn('api("api/macro/history?limit=72")', self.javascript)
        self.assertIn("function renderMacroTrend(items)", self.javascript)
        self.assertIn('class="trend-card"', self.javascript)
        self.assertIn('sparklineSVG(trend.points, "trend-svg")', self.javascript)
        self.assertIn("关注优先级约 24 小时上涨", self.javascript)
        self.assertIn('class="situation-brief"', self.javascript)
        self.assertIn('class="brief-score"', self.javascript)
        self.assertIn('class="transmission-ribbon"', self.javascript)
        self.assertIn("信号 → 主题 → 资产", self.javascript)
        self.assertIn("本轮无已确认行动", self.javascript)
        self.assertIn(
            "if (state.decisionData) renderDecisionHero(state.decisionData)",
            self.javascript,
        )

    def test_decision_queue_and_matrix_default_to_priority_slices(self) -> None:
        self.assertIn("decisionQueueExpanded: false", self.javascript)
        self.assertIn("cards.slice(0, 10)", self.javascript)
        self.assertIn('id="decision-show-all"', self.javascript)
        self.assertIn("查看全部 ${cards.length} 条", self.javascript)
        self.assertIn("收起到重点信号", self.javascript)
        self.assertIn("matrixExpanded: false", self.javascript)
        self.assertIn("orderedColumns.slice(0, 8)", self.javascript)
        self.assertIn('id="matrix-show-all"', self.javascript)
        self.assertIn("收起到重点资产", self.javascript)

    def test_external_links_are_protocol_allowlisted(self) -> None:
        self.assertIn("function safeExternalUrl(value)", self.javascript)
        self.assertIn('parsed.protocol === "http:"', self.javascript)
        self.assertIn('parsed.protocol === "https:"', self.javascript)
        self.assertGreaterEqual(self.javascript.count("safeExternalUrl("), 3)

    def test_tabs_chips_and_refresh_follow_accessible_low_noise_contract(self) -> None:
        for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
            self.assertIn(f'event.key === "{key}"', self.javascript)
        self.assertIn('setAttribute("aria-pressed", "false")', self.javascript)
        self.assertIn('setAttribute("aria-pressed", "true")', self.javascript)
        self.assertIn("function refreshCurrentView()", self.javascript)
        self.assertIn("setInterval(refreshCurrentView, 300_000)", self.javascript)
        self.assertNotIn("setInterval(refreshAll", self.javascript)

    def test_system_status_reflects_macro_snapshot_freshness(self) -> None:
        self.assertIn('id="system-status"', self.html)
        self.assertIn('id="system-status-label"', self.html)
        self.assertIn("状态待确认", self.html)
        self.assertIn("function updateMacroStatus(snapshot)", self.javascript)
        self.assertIn("快照正常", self.javascript)
        self.assertIn("快照延迟", self.javascript)
        self.assertIn("快照异常", self.javascript)
        self.assertIn("is-warn", self.css)
        self.assertIn("is-error", self.css)

    def test_support_entry_restores_the_one_shot_floating_nudge(self) -> None:
        self.assertIn('class="footer-support"', self.html)
        self.assertIn('class="support-fab"', self.html)
        self.assertIn('id="support-fab"', self.html)
        self.assertIn("请我喝杯咖啡", self.html)
        self.assertIn('const NUDGE_DELAY = 25_000', self.javascript)
        self.assertIn("function scheduleNudge()", self.javascript)
        self.assertIn('sessionStorage.getItem(NUDGE_KEY)', self.javascript)
        self.assertIn('fab.classList.add("attention")', self.javascript)
        self.assertIn(".support-fab.attention", self.css)
        self.assertIn("@keyframes fab-bounce", self.css)
        self.assertNotIn("fab-bounce 1.15s ease infinite", self.css)
        self.assertIn("right: 28px; bottom: clamp(84px, 10vh, 112px)", self.css)
        self.assertIn(".support-fab::after", self.css)
        self.assertIn("right: -28px", self.css)
        self.assertIn(
            "bottom: calc(14px + env(safe-area-inset-bottom, 0px))",
            self.css,
        )

    def test_feed_prioritizes_high_impact_and_labels_source_nature(self) -> None:
        self.assertIn("stats: null", self.javascript)
        self.assertIn('highParams.set("impact", "high")', self.javascript)
        self.assertIn('highParams.set("limit", "50")', self.javascript)
        self.assertIn("mergePriorityEvents", self.javascript)
        self.assertIn("高影响已优先", self.javascript)
        self.assertIn("普通流仅展示前150条", self.javascript)
        self.assertIn("当前窗口采集记录", self.javascript)
        self.assertIn("本人动态", self.javascript)
        self.assertIn("媒体提及", self.javascript)
        self.assertIn('class="source-kind ${sourceNature.key}"', self.javascript)


if __name__ == "__main__":
    unittest.main()
