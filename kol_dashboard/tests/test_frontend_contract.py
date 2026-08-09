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
        self.assertIn("function fmtBeijingDateTime", self.javascript)
        self.assertIn("function fmtRelativeTime", self.javascript)
        self.assertIn('timeZone: "Asia/Shanghai"', self.javascript)
        self.assertIn('second: "2-digit"', self.javascript)
        self.assertIn("function publicationTimeView", self.javascript)
        self.assertIn("const relative = fmtRelativeTime(iso);", self.javascript)
        self.assertIn("（北京时间）", self.javascript)
        self.assertIn('datetime="${esc(', self.javascript)
        self.assertIn('aria-label="${esc(', self.javascript)
        self.assertIn("white-space: nowrap", self.css)

    def test_macro_view_lists_monitored_events_with_time_provenance(self) -> None:
        self.assertIn('id="macro-events-block"', self.html)
        self.assertIn('id="macro-events"', self.html)
        self.assertIn('aria-labelledby="macro-events-title"', self.html)
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
        self.assertIn('static/app.js?v=18', self.html)
        self.assertIn('static/style.css?v=18', self.html)
        self.assertNotIn('?v=17', self.html)
        self.assertIn(".metric.is-stale", self.css)

    def test_macro_events_render_compact_ai_digest_and_bounded_highlights(self) -> None:
        self.assertIn("function renderMacroAiDigest(list)", self.javascript)
        self.assertIn('class="macro-ai-digest"', self.javascript)
        self.assertIn("AI 态势摘录", self.javascript)
        self.assertIn('role="status" aria-live="polite"', self.javascript)
        self.assertIn("statusCounts.ready", self.javascript)
        self.assertIn(".slice(0, 3)", self.javascript)
        self.assertIn("高影响", self.javascript)
        self.assertIn("中影响", self.javascript)
        self.assertIn("待生成", self.javascript)
        self.assertIn("待重试", self.javascript)
        self.assertIn("不可用", self.javascript)
        self.assertIn("function macroAiImpactEligible(event)", self.javascript)
        self.assertIn("function hasSubstantiveMacroEvidence(enrichment)", self.javascript)
        self.assertIn("confidence >= 0.65", self.javascript)
        self.assertIn("低置信待核验", self.javascript)
        self.assertIn("正文或指标", self.javascript)
        self.assertIn("qualifiedCount", self.javascript)
        self.assertIn("可用于研判", self.javascript)
        self.assertIn("处理完成", self.javascript)
        self.assertNotIn("<small>已解读</small>", self.javascript)
        self.assertIn(".macro-ai-digest", self.css)
        self.assertIn(".macro-ai-points", self.css)
        self.assertIn(".macro-ai-readiness-item", self.css)

    def test_macro_event_cards_use_ai_copy_and_native_disclosure(self) -> None:
        for field in (
            "ai_status",
            "ai_enrichment",
            "headline_zh",
            "summary_zh",
            "why_it_matters_zh",
            "impact_path",
            "tags",
            "assets",
            "evidence_basis",
            "confidence",
            "model",
            "generated_at",
            "category",
            "content_status",
            "content_excerpt",
            "content_source_url",
            "evidence_sections",
            "official_body",
        ):
            self.assertIn(field, self.javascript)
        self.assertIn("function macroEventCopy(event)", self.javascript)
        self.assertIn(
            "const enrichment = macroAiImpactEligible(event) ? rawEnrichment : null;",
            self.javascript,
        )
        self.assertIn("compactMacroSourceText(event.content_excerpt)", self.javascript)
        self.assertIn("function macroAiStateHTML(event)", self.javascript)
        self.assertIn("主标题、摘要和完整研判未采用", self.javascript)
        self.assertIn("function renderMacroEventInsight(event, copy)", self.javascript)
        self.assertIn('aria-labelledby="${titleId}"', self.javascript)
        self.assertIn('<details class="event-insight">', self.javascript)
        self.assertIn("展开完整解读", self.javascript)
        self.assertIn("为何重要", self.javascript)
        self.assertIn("传导路径", self.javascript)
        self.assertIn("可能受影响的资产", self.javascript)
        self.assertIn("原始标题", self.javascript)
        self.assertIn("仅标题证据", self.javascript)
        self.assertIn("事件类别重要度", self.javascript)
        self.assertIn("AI 核验影响", self.javascript)
        self.assertIn("官方正文已读取", self.javascript)
        self.assertIn("系统尚未读取正文", self.javascript)
        self.assertIn("来源暂不支持正文抓取", self.javascript)
        self.assertIn("正文抓取暂不可用，系统将重试", self.javascript)
        self.assertIn("function renderOfficialEvidence(event)", self.javascript)
        self.assertIn('<details class="event-source-evidence">', self.javascript)
        self.assertIn("展开官方正文摘录", self.javascript)
        self.assertIn("不公开原始 HTML", self.javascript)
        self.assertIn("AI 解读暂不可用", self.javascript)
        self.assertIn("AI 解读将在稍后重试", self.javascript)
        self.assertIn(".event-insight > summary:focus-visible", self.css)
        self.assertIn(".event-source-evidence > summary:focus-visible", self.css)
        self.assertIn(".content-evidence-status.is-ready", self.css)
        self.assertIn(".content-evidence-status.is-missing", self.css)
        self.assertIn(".content-evidence-status.is-unsupported", self.css)
        self.assertIn(".content-evidence-status.is-unavailable", self.css)
        self.assertIn("min-height: 44px", self.css)

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

    def test_decision_lenses_filter_queue_and_matrix_with_bounded_full_load(
        self,
    ) -> None:
        hero = self.html.index('id="decision-hero"')
        lenses = self.html.index('id="decision-lenses"')
        layout = self.html.index('class="decision-layout"')
        self.assertLess(hero, lenses)
        self.assertLess(lenses, layout)
        for lens in ("all", "candidate", "portfolio", "watchlist"):
            self.assertIn(f'data-decision-lens="{lens}"', self.html)
        self.assertIn('decisionLens: "all"', self.javascript)
        self.assertIn("function decisionMatchesLens", self.javascript)
        self.assertIn("function decisionLensCards", self.javascript)
        self.assertIn("function renderDecisionLenses(data)", self.javascript)
        self.assertIn("const allowedKeys = new Set(lensCards.map(decisionKey));", self.javascript)
        self.assertIn("await loadFullDecisions();", self.javascript)
        self.assertIn("state.fullDecisionLoadPromise", self.javascript)
        self.assertIn("fetchJSON(url, 15000)", self.javascript)
        self.assertIn("state.decisionLensLoading", self.javascript)
        load_start = self.javascript.index("async function loadDecisions")
        load_end = self.javascript.index("async function loadFullDecisions", load_start)
        load_contract = self.javascript[load_start:load_end]
        summary_branch = load_contract.index("if (data?.summary === true)")
        self.assertGreater(
            load_contract.index('state.decisionLens = "all"', summary_branch),
            summary_branch,
        )
        self.assertIn("const preserveFullPublicContext", load_contract)
        self.assertIn("state.decisionData.summary !== true", load_contract)
        self.assertIn('? "api/decisions"', load_contract)
        self.assertIn(".decision-lenses", self.css)
        self.assertIn(".decision-lens", self.css)

    def test_watchlist_persists_only_bounded_public_asset_keys(self) -> None:
        for text in (
            "DECISION_WATCHLIST_STORAGE_KEY",
            "DECISION_WATCHLIST_LIMIT = 50",
            "PUBLIC_ASSET_KEY_PATTERN",
            "function loadDecisionWatchlist()",
            "function persistDecisionWatchlist()",
            "localStorage.getItem(DECISION_WATCHLIST_STORAGE_KEY)",
            "localStorage.setItem(",
            "JSON.stringify(Array.from(state.watchAssets)",
            "key.length <= 80",
        ):
            self.assertIn(text, self.javascript)
        self.assertIn("[A-Z0-9._\\/-]", self.javascript)
        self.assertIn("Array.from(\n        new Set(", self.javascript)
        self.assertLess(
            self.javascript.rindex("loadDecisionWatchlist();"),
            self.javascript.index("await ensureViewLoaded(state.view)"),
        )
        self.assertIn('data-watch-asset="${esc(card.asset_key)}"', self.javascript)
        self.assertIn('aria-pressed="${String(watched)}"', self.javascript)
        self.assertIn("仅公开 asset_key 保存在本机浏览器", self.javascript)
        self.assertIn("不上传持仓、账户或成本信息", self.html)
        persist_start = self.javascript.index("function persistDecisionWatchlist")
        persist_end = self.javascript.index("function isWatchedAsset", persist_start)
        persist_contract = self.javascript[persist_start:persist_end]
        for private_field in ("matched_positions", "account", "quantity", "avg_cost"):
            self.assertNotIn(private_field, persist_contract)

        clear_start = self.javascript.index("function clearDecisionView")
        clear_end = self.javascript.index("async function loadDecisions", clear_start)
        clear_contract = self.javascript[clear_start:clear_end]
        self.assertIn('state.decisionLens = "all"', clear_contract)
        self.assertIn("[data-lens-count]", clear_contract)
        self.assertIn("私人决策与组合命中已从当前页面清除", clear_contract)
        self.assertNotIn("watchAssets.clear", clear_contract)
        toggle_start = self.javascript.index("function toggleWatchAsset")
        toggle_end = self.javascript.index("function evidenceStatusInfo", toggle_start)
        toggle_contract = self.javascript[toggle_start:toggle_end]
        self.assertIn("requestAnimationFrame", toggle_contract)
        self.assertIn("replacement || selectedCard || lensButton", toggle_contract)

    def test_market_status_prioritizes_applicability_and_pending_before_failure(
        self,
    ) -> None:
        start = self.javascript.index("function marketStatusInfo")
        end = self.javascript.index(
            "const DECISION_SNAPSHOT_MAX_AGE_SECONDS", start
        )
        contract = self.javascript[start:end]
        failure = contract.index("market.degraded === true")
        for predicate in (
            'applicabilityReason === "no_event_anchor"',
            'applicabilityReason === "direction_missing"',
            '"direction_unavailable"',
            "marketIsPurePending(market)",
        ):
            self.assertIn(predicate, contract)
            self.assertLess(contract.index(predicate), failure)
        for reason in (
            "follow_up_unavailable",
            "insufficient_follow_up",
            "no_records",
            "request_failed",
            "unsupported_benchmark",
        ):
            self.assertIn(reason, contract)
        self.assertIn("这不是行情链路故障", contract)
        self.assertIn("重试行情无法解决此问题", contract)
        self.assertIn("未来窗口不构成数据故障", contract)
        self.assertIn('state: "data_failure"', contract)
        reason_start = self.javascript.index("function marketReasonCount")
        reason_end = self.javascript.index("function decisionNextReview", reason_start)
        reason_contract = self.javascript[reason_start:reason_end]
        self.assertIn("market?.reason_counts", reason_contract)
        self.assertIn("record?.reason_code || record?.reason", reason_contract)
        pending_start = self.javascript.index("function marketIsPurePending")
        pending_end = self.javascript.index("function earliestMarketDue", pending_start)
        pending_contract = self.javascript[pending_start:pending_end]
        self.assertIn('marketReasonCount(market, "window_not_due")', pending_contract)
        self.assertIn("marketReasonTotal(market)", pending_contract)
        self.assertIn("market?.degraded !== true", pending_contract)
        self.assertIn("unavailableWindows === 0", pending_contract)

    def test_decision_runway_portfolio_summary_and_next_review_are_explicit(
        self,
    ) -> None:
        for field in (
            "portfolio_overview",
            "position_count",
            "matched_position_count",
            "impacted_asset_count",
            "candidate_matched_decisions",
            "leveraged_match_count",
            "stale_position_count",
            "market?.next_review_at",
            "record?.next_due_at",
        ):
            self.assertIn(field, self.javascript)
        self.assertIn('class="decision-runway"', self.javascript)
        self.assertIn("我的资产", self.javascript)
        self.assertIn("下一复核", self.javascript)
        self.assertIn("现在需复核", self.javascript)
        self.assertIn('<time datetime="${esc(nextReview.datetime)}">', self.javascript)
        self.assertIn("尚无持仓快照", self.javascript)
        self.assertIn("间接暴露尚未计算，不代表组合无风险", self.javascript)
        self.assertIn("多源证据", self.javascript)
        self.assertIn("单一来源 · 待交叉核验", self.javascript)
        self.assertIn(".decision-runway", self.css)
        self.assertIn(".decision-next-review", self.css)
        self.assertIn(".decision-watch-btn", self.css)
        review_start = self.javascript.index("function decisionNextReview")
        review_end = self.javascript.index("function marketSourceScopeLabel", review_start)
        review_contract = self.javascript[review_start:review_end]
        self.assertLess(
            review_contract.index('reason === "direction_missing"'),
            review_contract.index("const declared = earliestMarketDue(market)"),
        )
        self.assertIn("技术 / 数据重试，不是市场窗口确认", review_contract)
        self.assertIn('phase.startsWith("confirmed_")', review_contract)
        self.assertIn("market.required_window_complete !== true", review_contract)
        self.assertIn("等待所需确认窗口", review_contract)

    def test_decision_priority_keeps_stage_first_then_private_and_watchlist(
        self,
    ) -> None:
        start = self.javascript.index("function orderedDecisions")
        end = self.javascript.index("function renderDecisionQueue", start)
        contract = self.javascript[start:end]
        self.assertLess(contract.index("const stageRank"), contract.index("const portfolioRank"))
        self.assertLess(contract.index("const portfolioRank"), contract.index("const watchRank"))
        self.assertIn("cardHasPortfolioMatch", contract)
        self.assertIn("isWatchedAsset", contract)
        self.assertIn('class="decision-card ${', self.javascript)
        self.assertIn('watched ? "is-watched"', self.javascript)

    def test_decision_controls_are_keyboard_mobile_and_reduced_motion_safe(
        self,
    ) -> None:
        self.assertIn('id="decision-lenses" role="group"', self.html)
        self.assertIn('aria-pressed="true"', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('event.key === "ArrowRight"', self.javascript)
        self.assertIn('event.key === "ArrowLeft"', self.javascript)
        self.assertIn('window.matchMedia?.("(prefers-reduced-motion: reduce)")', self.javascript)
        self.assertIn('behavior: reduceMotion ? "auto" : "smooth"', self.javascript)
        self.assertIn(".decision-lens { min-height: 44px", self.css)
        self.assertIn(".decision-watch-btn { min-height: 44px", self.css)
        forbidden = self.html + self.javascript
        for phrase in ("买入", "卖出", "自动交易", "自动下单", "一键下单"):
            self.assertNotIn(phrase, forbidden)

    def test_decision_boundary_exposes_market_degradation_without_false_green(
        self,
    ) -> None:
        self.assertIn("function decisionBoundaryState(data)", self.javascript)
        self.assertIn("function declaredBusinessDegraded(data)", self.javascript)
        self.assertIn("market.degraded === true", self.javascript)
        self.assertIn("市场验证暂不可用", self.javascript)
        self.assertIn(
            "仅可核验证据，不能据此确认交易动作",
            self.javascript,
        )
        self.assertIn("decision-boundary-rail is-${esc(boundary.tone)}", self.javascript)
        self.assertIn('aria-label="证据状态与市场状态"', self.javascript)
        self.assertIn('class="decision-card-boundary"', self.javascript)
        self.assertIn("setSystemSignal(", self.javascript)
        self.assertIn("applySystemStatus", self.javascript)
        self.assertIn(".decision-boundary-rail.is-error", self.css)
        self.assertIn(".decision-card-boundary", self.css)
        self.assertIn(".validation-boundary.is-error", self.css)

    def test_decision_boundary_fails_closed_on_snapshot_age_and_market_phase(
        self,
    ) -> None:
        self.assertIn("DECISION_SNAPSHOT_MAX_AGE_SECONDS = 90 * 60", self.javascript)
        self.assertIn("function decisionSnapshotFreshness(data)", self.javascript)
        self.assertIn("metadata.stale === true", self.javascript)
        self.assertIn("metadata.age_seconds", self.javascript)
        self.assertIn("metadata.generated_at", self.javascript)
        self.assertIn("computedAgeSeconds", self.javascript)
        self.assertIn("computedAgeSeconds < -300", self.javascript)
        self.assertIn(
            "Math.max(rawAgeSeconds, liveAgeSeconds)",
            self.javascript,
        )
        self.assertIn(
            "ageSeconds > DECISION_SNAPSHOT_MAX_AGE_SECONDS",
            self.javascript,
        )
        self.assertIn("决策快照延迟", self.javascript)
        self.assertIn("快照时间待核验", self.javascript)
        self.assertIn('status === "complete"', self.javascript)
        self.assertIn("market.abstain === false", self.javascript)
        self.assertIn("market.veto === false", self.javascript)
        self.assertIn("directionConfirmed === true", self.javascript)
        self.assertIn('phase === "contrary"', self.javascript)
        self.assertIn("市场反向，候选停止并复核", self.javascript)
        self.assertIn("市场方向中性或不一致", self.javascript)

    def test_partial_business_degradation_and_macro_failure_remain_visible(
        self,
    ) -> None:
        self.assertIn("function businessHealthSeverity(data)", self.javascript)
        self.assertIn("rawAvailableRecords", self.javascript)
        self.assertIn("availableRecords <= 0", self.javascript)
        self.assertIn("fallbackAllUnavailable", self.javascript)
        self.assertIn("!businessHealth.declared", self.javascript)
        self.assertIn('partial: severity === "warn"', self.javascript)
        self.assertIn('unavailable: severity === "error"', self.javascript)
        self.assertIn("市场验证部分降级", self.javascript)
        self.assertIn('state.systemSignals.macro?.kind === "error"', self.javascript)
        self.assertIn("宏观快照刷新失败", self.javascript)
        self.assertIn(
            "if (state.decisionData) renderDecisionHero(state.decisionData)",
            self.javascript,
        )

    def test_human_review_actions_remain_candidates_in_every_decision_surface(
        self,
    ) -> None:
        self.assertIn("candidate_reduce_or_hedge", self.javascript)
        self.assertIn("candidate_scale_in", self.javascript)
        self.assertIn("候选减仓 / 对冲", self.javascript)
        self.assertIn("候选分批布局", self.javascript)
        self.assertIn("候选行动 · 待人工确认", self.javascript)
        self.assertIn("候选行动，待人工确认", self.javascript)
        self.assertNotIn("已确认行动", self.javascript)
        self.assertNotIn("已进入分级行动", self.javascript)

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
        self.assertIn("本轮暂无候选行动", self.javascript)
        self.assertIn(
            "if (state.decisionData) renderDecisionHero(state.decisionData)",
            self.javascript,
        )

    def test_decision_queue_and_matrix_default_to_priority_slices(self) -> None:
        self.assertIn("decisionQueueExpanded: false", self.javascript)
        self.assertIn("cards.slice(0, 10)", self.javascript)
        self.assertIn('id="decision-show-all"', self.javascript)
        self.assertIn("查看全部 ${total} 条", self.javascript)
        self.assertIn("收起到重点信号", self.javascript)
        self.assertIn("matrixExpanded: false", self.javascript)
        self.assertIn("orderedColumns.slice(0, 8)", self.javascript)
        self.assertIn('id="matrix-show-all"', self.javascript)
        self.assertIn("收起到重点资产", self.javascript)
        self.assertIn("if (data?.summary === true)", self.javascript)
        self.assertGreaterEqual(
            self.javascript.count("state.decisionQueueExpanded = false"),
            2,
        )
        self.assertGreaterEqual(
            self.javascript.count("state.matrixExpanded = false"),
            2,
        )

    def test_full_decision_failure_restores_controls_and_reports_in_place(
        self,
    ) -> None:
        self.assertIn("fullDecisionLoadError", self.javascript)
        self.assertIn("function showDecisionExpansionError", self.javascript)
        self.assertIn("decision-inline-error", self.javascript)
        self.assertIn("if (more.isConnected)", self.javascript)
        self.assertIn("more.disabled = false", self.javascript)
        self.assertIn("if (matrixMore.isConnected)", self.javascript)
        self.assertIn("matrixMore.disabled = false", self.javascript)
        self.assertIn("finally", self.javascript)
        self.assertIn(".decision-inline-error", self.css)

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
        self.assertIn("function refreshCurrentView(", self.javascript)
        self.assertIn("function ensureViewLoaded(view", self.javascript)
        self.assertIn('document.addEventListener("visibilitychange"', self.javascript)
        self.assertIn("state.refreshTimer = setTimeout", self.javascript)
        self.assertNotIn("setInterval(refreshCurrentView", self.javascript)
        self.assertNotIn("setInterval(refreshAll", self.javascript)

    def test_failed_view_refresh_keeps_last_good_and_is_not_cached(self) -> None:
        self.assertIn("viewLastGoodAt", self.javascript)
        self.assertIn("viewLoadErrors", self.javascript)
        self.assertIn("function renderViewLoadState(view)", self.javascript)
        self.assertIn('data-view-retry="${esc(view)}"', self.javascript)
        self.assertIn("viewLastGoodDataAt", self.javascript)
        self.assertIn("function recordViewLastGoodDataAt", self.javascript)
        self.assertIn("payload?.generated_at", self.javascript)
        self.assertIn("payload?.source_as_of", self.javascript)
        self.assertIn("继续显示数据截至 ${fmtAbsoluteTime", self.javascript)
        self.assertNotIn(
            "fmtAbsoluteTime(new Date(lastGoodAt).toISOString())",
            self.javascript,
        )
        self.assertIn("criticalSucceeded", self.javascript)
        self.assertIn("state.viewLoadedAt[view] = loadedNow", self.javascript)
        self.assertIn("state.viewLoadedAt[view] = 0", self.javascript)
        self.assertIn("eventsResult.value === true", self.javascript)
        self.assertIn("decisionResult.value === true", self.javascript)
        self.assertNotIn("已清除上一份页面数据", self.javascript)
        self.assertIn(".view-load-state", self.css)
        self.assertIn(".view-retry-btn", self.css)

    def test_decision_detail_409_retries_once_then_requires_user_action(self) -> None:
        self.assertIn("conflictRetryCount = 0", self.javascript)
        self.assertIn("if (conflictRetryCount >= 1)", self.javascript)
        self.assertIn('loadDecisions({ autoSelect: false })', self.javascript)
        self.assertIn("conflictRetryCount: 1", self.javascript)
        self.assertIn("function renderDecisionDetailConflict", self.javascript)
        self.assertIn("已停止自动重试", self.javascript)
        self.assertIn("data-decision-detail-retry", self.javascript)
        self.assertIn("人工重试证据链", self.javascript)
        self.assertIn(".decision-detail-conflict", self.css)

    def test_first_screen_uses_summary_lazy_detail_and_public_revalidation(self) -> None:
        self.assertIn('"api/decisions/summary"', self.javascript)
        self.assertIn("api/decisions/detail?${params}", self.javascript)
        self.assertIn("decisionDetailCache: new Map()", self.javascript)
        self.assertIn('cache: options.cache || "no-cache"', self.javascript)
        self.assertIn("feedAbortController: null", self.javascript)
        self.assertIn("requestController.signal", self.javascript)
        self.assertIn("loadFullDecisions()", self.javascript)
        self.assertNotIn("loadAuthStatus().then(refreshAll)", self.javascript)
        self.assertNotIn("setInterval(refreshCurrentView", self.javascript)
        self.assertIn("void loadSupportFacts();", self.javascript)

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

    def test_feed_prefers_bounded_ai_fields_and_opens_evidence_drawer(self) -> None:
        for field in (
            "ai_enrichment",
            "headline_zh",
            "summary_zh",
            "why_it_matters_zh",
            "impact_path",
            "tags",
            "assets",
            "cluster_key",
            "evidence_basis",
        ):
            self.assertIn(field, self.javascript)
        self.assertIn("function eventEnrichment(item)", self.javascript)
        self.assertIn("function foldEventClusters(items)", self.javascript)
        self.assertIn('data-event-detail="${esc(', self.javascript)
        self.assertIn("查看证据链", self.javascript)
        self.assertIn("AI 解读生成中", self.javascript)
        self.assertIn("AI 解读暂不可用", self.javascript)
        self.assertIn("仅标题证据", self.javascript)
        self.assertIn("同簇相关报道", self.javascript)
        self.assertIn("function renderIntelAssets", self.javascript)
        self.assertIn("function renderIntelSources", self.javascript)
        self.assertIn("function renderIntelRelated", self.javascript)
        self.assertIn("规则关联用于发现线索；市场相关不等于因果", self.javascript)

    def test_event_clusters_keep_unverified_items_separate_and_expand_in_place(
        self,
    ) -> None:
        fold_start = self.javascript.index("function foldEventClusters(items)")
        fold_end = self.javascript.index("function renderEvents(items)", fold_start)
        fold_contract = self.javascript[fold_start:fold_end]

        self.assertIn("timeStatus", fold_contract)
        self.assertIn('timeStatus === "verified"', fold_contract)
        self.assertLess(
            fold_contract.index('timeStatus === "verified"'),
            fold_contract.index("cluster_key"),
        )
        self.assertIn("relatedItems", fold_contract)
        self.assertIn("expandedClusters", self.javascript)
        self.assertIn("data-cluster-toggle", self.javascript)
        self.assertIn('aria-expanded="${group.isExpanded', self.javascript)
        self.assertIn("state.expandedClusters.add(clusterKey)", self.javascript)
        self.assertIn("state.expandedClusters.delete(clusterKey)", self.javascript)

    def test_event_detail_request_preserves_selected_kol_sighting(self) -> None:
        self.assertIn("drawerKol", self.javascript)
        self.assertIn("drawerSourceUrl", self.javascript)
        self.assertIn("data-event-kol", self.javascript)
        self.assertIn("data-event-source-url", self.javascript)
        self.assertIn('params.set("kol", state.drawerKol)', self.javascript)
        self.assertIn(
            'params.set("source_url", state.drawerSourceUrl)',
            self.javascript,
        )
        self.assertIn("trigger?.dataset?.eventKol", self.javascript)
        self.assertIn("trigger?.dataset?.eventSourceUrl", self.javascript)
        self.assertIn('query ? `?${query}` : ""', self.javascript)

    def test_event_intelligence_drawer_is_accessible_and_mobile_safe(self) -> None:
        self.assertIn('id="intel-drawer-shell" hidden', self.html)
        self.assertIn('id="intel-drawer" role="dialog" aria-modal="true"', self.html)
        self.assertIn('aria-labelledby="intel-drawer-title"', self.html)
        self.assertIn('id="intel-drawer-live" role="status" aria-live="polite"', self.html)
        self.assertIn("function openIntelDrawer", self.javascript)
        self.assertIn("function closeIntelDrawer", self.javascript)
        self.assertIn("function bindIntelDrawer", self.javascript)
        self.assertIn("bindIntelDrawer();", self.javascript)
        self.assertIn('api(`api/events/${encodeURIComponent(eventId)}`)', self.javascript)
        self.assertIn('event.key === "Escape"', self.javascript)
        self.assertIn('event.key !== "Tab"', self.javascript)
        self.assertIn("node.inert = true", self.javascript)
        self.assertIn("node.inert = false", self.javascript)
        self.assertIn("drawerReturnFocus", self.javascript)
        self.assertIn(".intel-drawer", self.css)
        self.assertIn("height: 100dvh", self.css)


if __name__ == "__main__":
    unittest.main()
