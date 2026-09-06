from __future__ import annotations

import unittest
from pathlib import Path

from kol_dashboard import briefing_topics


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
        self.assertIn('class="decision-layout decision-master-detail"', self.html)
        queue = self.html.index('class="decision-panel action-panel"')
        detail = self.html.index('id="decision-detail"')
        matrix = self.html.index('class="decision-panel matrix-panel"')
        self.assertLess(queue, detail)
        self.assertLess(detail, matrix)
        self.assertIn("data-decision-key", self.javascript)
        self.assertIn("matrix-symbol", self.css)

    def test_daily_briefing_is_a_separate_lazy_loaded_evidence_view(self) -> None:
        self.assertIn('id="tab-daily"', self.html)
        self.assertIn('data-view="daily"', self.html)
        self.assertIn('id="view-daily"', self.html)
        self.assertIn('aria-labelledby="tab-daily"', self.html)
        self.assertIn('id="daily-highlights-title"', self.html)
        self.assertIn('id="daily-firsthand-title"', self.html)
        self.assertIn('id="daily-watchpoints-title"', self.html)
        self.assertIn("dailyData: null", self.javascript)
        self.assertIn('view === "daily"', self.javascript)
        self.assertIn('api("api/briefings/latest")', self.javascript)
        self.assertIn("function renderDaily(data)", self.javascript)
        self.assertIn("function loadDaily()", self.javascript)
        self.assertIn('location.hash === "#daily"', self.javascript)

    def test_daily_briefing_keeps_source_directness_and_verification_explicit(self) -> None:
        for label in (
            "官方正文",
            "一手原文",
            "媒体报道",
            "聚合线索",
            "发布时间待核验",
            "仅标题证据",
            "AI 摘要已绑定当前证据",
            "关联记录，不代表独立确认",
        ):
            self.assertIn(label, self.javascript)
        self.assertIn("source_tier", self.javascript)
        self.assertIn("related_records", self.javascript)
        self.assertIn("evidence_basis", self.javascript)
        self.assertIn("item?.ai_summary_used === true", self.javascript)
        self.assertIn("data-daily-event-detail", self.javascript)
        self.assertIn("safeExternalUrl", self.javascript)
        self.assertIn('target="_blank"', self.javascript)
        self.assertIn('rel="noopener noreferrer"', self.javascript)
        self.assertIn('source.key === "first_party"', self.javascript)
        self.assertNotIn("本人原文", self.javascript)
        self.assertIn("条时间语义已核验", self.javascript)
        self.assertNotIn("条发布时间已核验", self.javascript)
        self.assertNotIn("${section.verified_count} 条已核验", self.javascript)
        self.assertIn("`${view.short} 北京时间", self.javascript)
        self.assertIn("const visible = compact", self.javascript)

    def test_daily_briefing_uses_responsive_editorial_hierarchy(self) -> None:
        for selector in (
            ".daily-lead-band",
            ".daily-signal-axis",
            ".daily-firsthand",
            ".daily-coverage-grid",
            ".daily-watch-grid",
        ):
            self.assertIn(selector, self.css)
        self.assertIn("grid-template-columns: 1fr", self.css)
        self.assertIn("min-height: 44px", self.css)

    def test_daily_briefing_exposes_six_deduplicated_editorial_sections(self) -> None:
        for key, label in (
            ("macro", "宏观信息"),
            ("world", "全球要闻"),
            ("finance", "金融要闻"),
            ("technology", "科技前沿"),
            ("ai", "AI 前沿"),
            ("investors", "投资大师动态"),
        ):
            self.assertIn(f'href="#daily-section-{key}"', self.html)
            self.assertIn(f'key: "{key}", label: "{label}"', self.javascript)
        for contract in (
            "data?.sections",
            "function dailyStoryKey(item)",
            "function dailyUniqueItems(items)",
            "function dailyNormalizedSections(data)",
            "story_key",
            "primary_section",
            "cross_tags",
            ".slice(0, 6)",
            "index >= 3",
        ):
            self.assertIn(contract, self.javascript)
        self.assertIn("同一故事只进入一个主栏目", self.javascript)
        self.assertIn("不会使用旧闻或重复转载填充本栏", self.javascript)

    def test_daily_sections_have_freshness_empty_and_expand_states(self) -> None:
        for contract in (
            "source_as_of",
            "verified_count",
            "total_count",
            "section.stale",
            'status === "empty"',
            'status === "stale"',
            '["partial", "limited"].includes(section.status)',
            "data-daily-section-toggle",
            'aria-expanded="false"',
            "setDailySectionExpanded",
            "自动刷新未接通",
            "refresh_schedule_status",
            "source_coverage_as_of",
            "source_coverage_stale",
            "内容证据截至",
            "已扫描至",
            "本轮扫描完成，暂无达到门槛的新事件",
            "不会使用旧闻或重复转载填充版面",
        ):
            self.assertIn(contract, self.javascript)
        for selector in (
            ".daily-status-band",
            ".daily-status-columns",
            ".daily-stream-section",
            ".daily-section-empty",
            ".daily-stale-note",
            ".daily-section-toggle",
            ".daily-cluster-marker",
        ):
            self.assertIn(selector, self.css)

    def test_daily_collapsed_rows_are_really_hidden_and_toggleable(self) -> None:
        hidden_rule = ".daily-stream-item[hidden] { display: none; }"
        self.assertIn(hidden_rule, self.css)
        self.assertLess(
            self.css.index(".daily-stream-item {"),
            self.css.index(hidden_rule),
        )
        toggle_start = self.javascript.index(
            "function setDailySectionExpanded(section, expanded)"
        )
        toggle_end = self.javascript.index(
            "function announceDailyStatus", toggle_start
        )
        toggle_contract = self.javascript[toggle_start:toggle_end]
        self.assertIn("item.hidden = !expanded", toggle_contract)
        self.assertIn(
            'button.setAttribute("aria-expanded", String(expanded))',
            toggle_contract,
        )

    def test_daily_investor_cards_separate_disclosure_and_holding_dates(self) -> None:
        for contract in (
            "function dailyInvestorDatesHTML(item)",
            "const disclosed = dailyDateView(item?.disclosed_at)",
            "const published = dailyDateView(item?.published_at)",
            "item?.effective_at || item?.period_end || item?.data_as_of",
            "const evidenceAt = dailyDateView(item?.data_as_of)",
            "证据截至：",
            "披露日期待核验",
            "持仓日期待核验",
            "来源发布",
            "披露不等于当日交易",
        ):
            self.assertIn(contract, self.javascript)
        self.assertIn("daily-investor-dates", self.css)

    def test_daily_uses_one_small_live_status_instead_of_a_live_document(self) -> None:
        announcer_start = self.html.index('id="daily-live-status"')
        announcer_end = self.html.index(">", announcer_start)
        announcer = self.html[announcer_start:announcer_end]
        self.assertIn('role="status"', announcer)
        self.assertIn('aria-live="polite"', announcer)
        self.assertIn('aria-atomic="true"', announcer)
        stage_start = self.html.index('class="daily-stage"')
        stage_end = self.html.index(">", stage_start)
        self.assertNotIn("aria-live", self.html[stage_start:stage_end])
        self.assertIn("function announceDailyStatus(message)", self.javascript)
        self.assertIn("简报已更新，共 ${total} 条去重记录。", self.javascript)

    def test_daily_unavailable_state_keeps_six_column_health_visible(self) -> None:
        unavailable_start = self.javascript.index("if (!data?.available)")
        unavailable_end = self.javascript.index(
            "if (jumpNav) jumpNav.hidden = false", unavailable_start
        )
        unavailable_contract = self.javascript[unavailable_start:unavailable_end]
        self.assertIn("const unavailableSections = sections.map", unavailable_contract)
        self.assertIn(
            'refresh_schedule_status: data?.refresh_schedule_status || "unconfigured"',
            unavailable_contract,
        )
        self.assertIn("next_refresh_at: data?.next_refresh_at || null", unavailable_contract)
        self.assertIn(
            "dailyStatusBandHTML(unavailableData, unavailableSections",
            unavailable_contract,
        )
        self.assertIn("linkable: false", unavailable_contract)

    def test_daily_first_load_failure_keeps_health_band_and_retry(self) -> None:
        load_start = self.javascript.index("async function loadDaily()")
        load_end = self.javascript.index(
            "// ─── Decision cockpit", load_start
        )
        load_contract = self.javascript[load_start:load_end]
        self.assertIn("renderDaily({", load_contract)
        self.assertIn("available: false", load_contract)
        self.assertIn('refresh_schedule_status: "unconfigured"', load_contract)
        self.assertIn("本轮无法形成可信简报，未使用旧闻补位", load_contract)
        self.assertNotIn("errorHTML(error, url)", load_contract)
        self.assertIn('setViewLoadError("daily", error, url)', load_contract)
        self.assertIn('data-view-retry="${esc(view)}"', self.javascript)

    def test_daily_nested_evidence_sections_use_third_level_headings(self) -> None:
        self.assertIn('<h3 id="daily-firsthand-title">', self.javascript)
        self.assertIn('<h3 id="daily-watchpoints-title">', self.javascript)
        self.assertIn(".daily-subsection-head h3", self.css)

    def test_daily_mobile_navigation_and_controls_are_touch_accessible(self) -> None:
        self.assertIn('aria-label="简报栏目"', self.html)
        self.assertIn("overflow-x: auto", self.css)
        self.assertIn("scroll-snap-type: x proximity", self.css)
        self.assertIn(".daily-status-columns { grid-template-columns: repeat(2", self.css)
        self.assertIn(".daily-section-toggle { grid-column: 1", self.css)
        self.assertIn(".daily-overview-item > a { min-height: 44px", self.css)
        self.assertIn(".daily-stream-summary { font-size: 14px; }", self.css)
        self.assertIn(".daily-stream-why { font-size: 13px; }", self.css)
        self.assertIn("(prefers-reduced-motion: reduce)", self.css)
        self.assertNotIn("DAILY INTELLIGENCE", self.html)
        self.assertNotIn("60 SECOND READ", self.javascript)
        self.assertNotIn("DIRECT SOURCES", self.javascript)
        self.assertNotIn("NEXT CHECK", self.javascript)

    def test_daily_assets_share_the_v36_cachebuster(self) -> None:
        self.assertIn('static/app.js?v=36', self.html)
        self.assertIn('static/style.css?v=36', self.html)
        self.assertEqual(self.html.count("?v=36"), 2)
        self.assertNotIn("?v=35", self.html)
        self.assertNotIn("?v=26", self.html)

    def test_options_lab_is_an_accessible_login_only_research_view(self) -> None:
        for contract in (
            'id="tab-options"',
            'data-view="options"',
            'aria-label="期权实验室"',
            'aria-controls="view-options"',
            'id="view-options" role="tabpanel"',
            'aria-labelledby="tab-options"',
            'id="options-subnav" role="tablist"',
            'aria-label="期权研究栏目"',
            'id="options-tab-policy"',
            'data-options-panel="policy"',
            'aria-controls="options-panel-policy"',
            "接货政策",
            'id="options-live-status" role="status"',
            "研究模式",
            "不会自动下单",
            "未登录时不会请求期权接口",
            "data-options-unlock",
        ):
            self.assertIn(contract, self.html)

        options_html_start = self.html.index('id="view-options"')
        options_html_end = self.html.index("<!-- ══════════════ KOL", options_html_start)
        static_options = self.html[options_html_start:options_html_end]
        self.assertNotIn("US:", static_options)
        self.assertNotIn("SPY", static_options)
        self.assertNotIn("熟悉标的", static_options)

        load_start = self.javascript.index("async function loadOptions()")
        load_end = self.javascript.index("// ─── Macro view", load_start)
        load_contract = self.javascript[load_start:load_end]
        self.assertLess(
            load_contract.index("if (!state.authenticated)"),
            load_contract.index('api("api/private/options/overview")'),
        )
        self.assertIn("renderOptionsLocked();", load_contract)
        self.assertIn("return true;", load_contract)
        self.assertIn("const data = await requestJSON(url)", load_contract)

        ensure_start = self.javascript.index("async function ensureViewLoaded(view")
        ensure_end = self.javascript.index("function switchView", ensure_start)
        ensure_contract = self.javascript[ensure_start:ensure_end]
        self.assertIn('view === "options"', ensure_contract)
        self.assertIn("if (!state.authStatusLoaded) await loadAuthStatus()", ensure_contract)
        self.assertIn("if (!state.authenticated)", ensure_contract)
        self.assertIn("loadOptions()", ensure_contract)

    def test_options_private_state_is_cleared_on_logout_and_unauthorized(self) -> None:
        locked_start = self.javascript.index("function renderOptionsLocked")
        clear_start = self.javascript.index("function clearOptionsView")
        clear_end = self.javascript.index("function optionNumberFrom", clear_start)
        clear_contract = self.javascript[clear_start:clear_end]
        cleanup_contract = self.javascript[locked_start:clear_end]
        self.assertIn("state.optionsData = null", self.javascript)
        self.assertIn("state.optionsPolicyDraft = null", cleanup_contract)
        self.assertIn("state.optionsPolicySaving = false", cleanup_contract)
        self.assertIn("state.optionsPolicySaveGeneration += 1", cleanup_contract)
        self.assertIn("state.optionsRequestGeneration += 1", clear_contract)
        self.assertIn("state.viewLastGoodAt.options = 0", clear_contract)
        self.assertIn("renderOptionsLocked(message)", clear_contract)

        expired_start = self.javascript.index("function handlePrivateSessionExpired")
        expired_end = self.javascript.index("async function loadDecisions", expired_start)
        self.assertIn(
            'clearOptionsView("私人会话已过期；重新解锁后才会读取期权研究")',
            self.javascript[expired_start:expired_end],
        )

        logout_start = self.javascript.index("async function lockPrivateMode")
        logout_end = self.javascript.index("async function submitAuth", logout_start)
        logout_contract = self.javascript[logout_start:logout_end]
        self.assertLess(
            logout_contract.index("clearOptionsView("),
            logout_contract.index('requestJSON(api("api/auth/logout")'),
        )

        load_start = self.javascript.index("async function loadOptions()")
        load_end = self.javascript.index("// ─── Macro view", load_start)
        options_contract = self.javascript[load_start:load_end]
        self.assertIn(
            "if (error?.status === 401 || error?.status === 403)",
            options_contract,
        )
        self.assertIn("handlePrivateSessionExpired();", options_contract)
        self.assertNotIn("localStorage", options_contract)
        self.assertNotIn("sessionStorage", options_contract)
        self.assertNotIn("console.", options_contract)
        self.assertIn(
            "旧私人结果已隐藏；不会把历史候选继续显示为当前结论。",
            self.javascript,
        )

    def test_options_zero_candidates_and_contract_values_fail_closed(self) -> None:
        start = self.javascript.index("// ─── Options research lab")
        end = self.javascript.index("// ─── Macro view", start)
        contract = self.javascript[start:end]
        for copy in (
            "今天没有通过全部门槛的卖 Put",
            "这是风控结论，不是页面故障",
            "不会为了填满列表而放宽标准",
            "把研究推进到候选，需要",
            "完成输入也不代表一定入选",
            "宏观、财报、流动性与集中度仍可否决交易",
            "合约明细尚未随响应提供",
            "不会显示推测的行权价、Delta 或权利金",
            "响应未同时提供现价、行权价、权利金和合约乘数",
            "系统不会补造数据",
            "研究结果必须人工确认；本页面不会提交订单",
        ):
            self.assertIn(copy, contract)
        self.assertIn("data?.next_steps", contract)
        self.assertIn('class="options-empty-next"', contract)
        for boundary in (
            "data.schema_version === 2",
            'data.method_version === "options-policy-readiness-v1"',
            "data.available === true",
            'data.mode === "research_only"',
            'data.decision_state === "abstain"',
            "candidateCount === 0",
            "candidates.length === 0",
            "data.human_review_required === true",
            "data.automatic_execution === false",
            "data.trade_execution_available === false",
            "capabilities.policy_configuration === true",
            "capabilities.live_option_chain === false",
            "capabilities.broker_capacity === false",
            "capabilities.event_calendar === false",
            "capabilities.candidate_generation === false",
            "capabilities.trade_execution === false",
            "Number.isInteger(policy.revision)",
            "OPTIONS_READY_DATA_STATES.has(dataStatus)",
            "Number.isFinite(parsed)",
            "multiplier == null",
        ):
            self.assertIn(boundary, contract)
        self.assertNotIn("|| 100", contract)

    def test_options_uses_research_universe_without_implying_holdings(self) -> None:
        self.assertIn("data?.research_universe", self.javascript)
        self.assertNotIn("familiar_universe", self.javascript)
        self.assertNotIn("familiar_universe", self.html)
        for copy in (
            "通用研究池 · 用户政策状态",
            "通用研究池",
            "不表示持有或适合交易",
            "系统不会使用示例代码补足候选",
            "状态来自接货政策",
            "不表示当前持有、支持期权或适合卖 Put",
            "未确认项不会进入后续候选",
            "愿意研究",
            "明确不做",
            "未确认",
        ):
            self.assertIn(copy, self.javascript)
        self.assertIn("const grouped = new Map()", self.javascript)
        self.assertIn('optionTextFrom(item, ["tier"])', self.javascript)
        self.assertIn('class="options-universe-groups"', self.javascript)
        self.assertIn("saved?.decision || item.status", self.javascript)
        self.assertIn('class="options-universe-status"', self.javascript)
        self.assertIn(".options-universe-groups > section", self.css)
        self.assertIn('grid-template-areas: "ledger analysis" "universe analysis"', self.css)
        self.assertIn('grid-template-areas: "ledger" "analysis" "universe"', self.css)

    def test_options_benchmark_formats_ratios_and_provenance_exactly(self) -> None:
        start = self.javascript.index("function optionRatioPercent")
        end = self.javascript.index("function renderOptions(data)", start)
        contract = self.javascript[start:end]
        self.assertIn("(numeric * 100).toFixed(2)", contract)
        for field in (
            "daily_max_drawdown",
            "annualized_daily_volatility",
            "monthly_beta_to_spx",
            "source_as_of",
            "calculation_version",
            "input_sha256",
        ):
            self.assertIn(field, contract)
        self.assertIn('optionTextFrom(benchmark?.range || {}, ["start"])', contract)
        self.assertIn('optionTextFrom(benchmark?.range || {}, ["end"])', contract)
        self.assertIn("`${rangeStart} — ${rangeEnd}`", contract)
        self.assertIn("/^[a-f0-9]{64}$/i.test(value)", contract)
        self.assertIn("function optionsStressWindowsHTML(benchmark)", contract)
        self.assertIn("benchmark?.stress_windows", contract)
        self.assertIn("window.returns", contract)
        self.assertIn("optionRatioPercent(value)", contract)
        self.assertIn("压力期表现", contract)
        self.assertIn("窗口收益 · 不是未来预测", contract)
        self.assertIn("本窗口没有通过有限数校验的收益", contract)
        benchmark_start = self.javascript.index("function optionsBenchmarkPanelHTML")
        benchmark_end = self.javascript.index("function renderOptions(data)", benchmark_start)
        self.assertNotIn("served_at", self.javascript[benchmark_start:benchmark_end])

    def test_options_layout_is_editorial_responsive_and_readable(self) -> None:
        for contract in (
            "#view-options { font-size: 16px; }",
            ".options-gate-rail",
            "grid-template-columns: repeat(4, minmax(0, 1fr))",
            "grid-template-columns: minmax(0, 1.38fr) minmax(340px, 1fr)",
            ".options-chart-path { fill: none; stroke: var(--accent)",
            ".options-subnav",
            ".options-stress-grid",
            ".options-policy-form",
            "grid-template-columns: minmax(0, 1.38fr) minmax(360px, 1fr)",
            "scroll-snap-type: x proximity",
        ):
            self.assertIn(contract, self.css)
        self.assertIn(
            ".options-benchmark-grid {\n  display: grid; grid-template-columns: repeat(5, minmax(0, 1fr))",
            self.css,
        )
        self.assertIn(
            "grid-template-columns: repeat(3, minmax(0, 1fr))",
            self.css[self.css.index(".options-stress-grid {") :],
        )
        mobile_start = self.css.index("@media (max-width: 700px)")
        mobile = self.css[mobile_start:]
        self.assertIn(".options-gate-rail { grid-template-columns: 1fr; }", mobile)
        self.assertIn(".options-stress-grid,", mobile)
        self.assertIn(".options-masthead { grid-template-columns: 1fr", mobile)
        self.assertIn(".options-policy-form { grid-template-columns: 1fr; }", self.css)
        self.assertIn(".options-policy-tier { grid-template-columns: 1fr; }", mobile)
        self.assertIn(".options-policy-underlying {", mobile)
        self.assertIn("min-height: 44px", mobile)
        self.assertIn(".tabs::-webkit-scrollbar { display: none; }", mobile)
        self.assertIn("flex: 0 0 auto; min-width: 74px; min-height: 44px", mobile)
        self.assertIn("font-size: 12px; scroll-snap-align: start", mobile)

    def test_options_policy_is_an_explicit_revisioned_research_boundary(self) -> None:
        start = self.javascript.index("function optionsPolicyPanelHTML")
        end = self.javascript.index("function optionsReadinessItems", start)
        policy_contract = self.javascript[start:end]
        for copy in (
            "人工确认的研究边界",
            "研究预算不是券商可用现金",
            "不读取购买力、保证金或实时账户余额",
            "三态只表达研究意愿",
            "未确认项不会提交",
            "保存也不会生成候选或下单",
            "实时期权链、券商资金、事件日历和执行能力仍未接入",
            "最高接货价",
            "总担保上限",
            "单标的上限",
            "最低现金缓冲",
            "每周新增合约上限",
            "指派后的复核计划",
            "仅研究现金担保 Put",
            "已理解指派风险",
            "未勾选也可保存为未就绪政策",
            "复核日期",
        ):
            self.assertIn(copy, policy_contract)
        self.assertIn('id="options-panel-policy"', policy_contract)
        self.assertIn('data-options-panel-view="policy"', policy_contract)
        self.assertIn('id="options-policy-form"', policy_contract)
        self.assertIn('["unconfirmed", "未确认"]', policy_contract)
        self.assertIn('["willing", "愿意研究"]', policy_contract)
        self.assertIn('["exclude", "明确不做"]', policy_contract)
        self.assertLess(
            policy_contract.index('class="options-policy-map"'),
            policy_contract.index('class="options-policy-terms"'),
        )
        for status, label in (
            ("ready", "已配置"),
            ("review_due", "到期复核"),
            ("acknowledgement_required", "待风险确认"),
            ("no_willing_underlyings", "未选愿意研究"),
        ):
            self.assertIn(status, self.javascript)
            self.assertIn(label, self.javascript)

    def test_options_policy_put_is_exact_fail_safe_and_conflict_aware(self) -> None:
        start = self.javascript.index("function optionsPolicyPayloadFromForm")
        end = self.javascript.index("function optionsReadinessItems", start)
        save_contract = self.javascript[start:end]
        for contract in (
            'api("api/private/options/policy")',
            'method: "PUT"',
            '"X-Finance-Radar-Action": OPTIONS_POLICY_ACTION',
            "schema_version: 1",
            "expected_revision: draft.expectedRevision",
            'strategy: "cash_secured_put"',
            "assignment_budget_ceiling_usd: budget",
            "max_total_reserved_bps:",
            "max_single_underlying_bps:",
            "minimum_cash_buffer_bps:",
            "max_new_contracts_per_week:",
            "assignment_plan: draft.assignmentPlan",
            "underlyings,",
            "cash_secured_only: draft.acknowledgements.cashSecuredOnly",
            "assignment_risk_reviewed: draft.acknowledgements.assignmentRiskReviewed",
            'item.decision === "willing" || item.decision === "exclude"',
            "OPTIONS_POLICY_MAX_SELECTIONS",
            "state.optionsPanel = \"policy\"",
            "state.optionsPolicyDraftDirty = false",
            "state.optionsPolicyDraft = null",
            "const reloaded = await loadOptions()",
            "接货政策已保存并重新读取",
            "另一页面已更新，已重新加载",
            "error?.status === 422",
            "请检查金额、比例、标的选择与风险确认",
            "error?.status === 401 || error?.status === 403",
            "handlePrivateSessionExpired()",
        ):
            self.assertIn(contract, save_contract)
        self.assertIn(
            'const OPTIONS_POLICY_ACTION = "update-options-policy"',
            self.javascript,
        )
        self.assertNotIn("error.payload", save_contract)
        self.assertNotIn("error.message", save_contract)
        self.assertNotIn("localStorage", save_contract)
        self.assertNotIn("sessionStorage", save_contract)
        self.assertNotIn("URLSearchParams", save_contract)

    def test_options_policy_dirty_draft_pauses_automatic_refresh(self) -> None:
        options_start = self.javascript.index("// ─── Options research lab")
        options_end = self.javascript.index("// ─── Macro view", options_start)
        options_contract = self.javascript[options_start:options_end]
        refresh_start = self.javascript.index("async function refreshCurrentView")
        refresh_end = self.javascript.index("function scheduleRefresh", refresh_start)
        refresh_contract = self.javascript[refresh_start:refresh_end]
        self.assertIn("optionsPolicyDraftDirty: false", self.javascript)
        self.assertIn("state.optionsPolicyDraftDirty = true", options_contract)
        self.assertIn("const preserveUnsavedPolicy = Boolean(", options_contract)
        self.assertIn(
            "validBoundary && state.optionsPolicyDraftDirty && state.optionsPolicyDraft",
            options_contract,
        )
        self.assertIn("if (!preserveUnsavedPolicy)", options_contract)
        self.assertIn("未保存草稿已保留", options_contract)
        self.assertIn('state.view === "options" && state.optionsPolicyDraftDirty', refresh_contract)
        self.assertIn('state.view === "options" && state.optionsPolicySaving', refresh_contract)
        self.assertIn("正在保存，请等待服务端响应后再刷新", refresh_contract)
        self.assertIn("if (!showSpinner)", refresh_contract)
        self.assertIn("已暂停自动刷新", refresh_contract)
        self.assertIn("window.confirm(", refresh_contract)
        self.assertIn("刷新会放弃当前页面内尚未保存的接货政策草稿", refresh_contract)
        self.assertLess(
            refresh_contract.index("state.optionsPolicyDraftDirty"),
            refresh_contract.index("await ensureViewLoaded"),
        )
        self.assertIn("await refreshCurrentView();", self.javascript)
        self.assertIn("void refreshCurrentView();", self.javascript)

    def test_options_policy_dirty_draft_survives_transient_service_failure(self) -> None:
        load_start = self.javascript.index("async function loadOptions()")
        load_end = self.javascript.index("// ─── Macro view", load_start)
        load_contract = self.javascript[load_start:load_end]
        recovery_start = self.javascript.index("function optionsPolicyRecoveryHTML")
        recovery_end = self.javascript.index("function optionsCapturePolicyDraft", recovery_start)
        recovery_contract = self.javascript[recovery_start:recovery_end]
        self.assertIn(
            "stage && !state.optionsData && !state.optionsPolicyDraftDirty",
            load_contract,
        )
        self.assertIn("const preserveDirtyPolicy = Boolean(", load_contract)
        self.assertIn(
            "state.optionsPolicyDraftDirty && state.optionsPolicyDraft",
            load_contract,
        )
        self.assertIn("if (!preserveDirtyPolicy)", load_contract)
        self.assertLess(
            load_contract.index("error?.status === 401 || error?.status === 403"),
            load_contract.index("const preserveDirtyPolicy"),
        )
        self.assertIn("state.optionsData = null", load_contract)
        self.assertIn("optionsPolicyRecoveryHTML()", load_contract)
        self.assertIn('optionsApplyPanel("policy")', load_contract)
        self.assertIn("旧候选已隐藏；未保存草稿仍保留在本页内存", load_contract)
        for copy in (
            "期权研究服务暂不可用，未保存草稿仍在",
            "候选、准备度与旧服务端视图已隐藏",
            "草稿只保留在当前页面内存",
            "请勿刷新或关闭页面",
            "保留草稿并重试服务",
        ):
            self.assertIn(copy, recovery_contract)
        self.assertIn('data-view-retry="options"', recovery_contract)
        self.assertIn("safeDraftProjection", recovery_contract)
        self.assertNotIn("state.optionsData", recovery_contract)
        render_start = self.javascript.index("function renderOptions(data)")
        render_end = self.javascript.index("async function loadOptions()", render_start)
        invalid_boundary_contract = self.javascript[render_start:render_end]
        self.assertIn("if (!optionsBoundaryIsValid(data))", invalid_boundary_contract)
        self.assertIn("state.optionsPolicyDraft = null", invalid_boundary_contract)
        self.assertIn("state.optionsPolicyDraftDirty = false", invalid_boundary_contract)

    def test_options_policy_save_locks_all_form_editing_until_settled(self) -> None:
        start = self.javascript.index("function optionsCapturePolicyDraft")
        end = self.javascript.index("function optionsReadinessItems", start)
        contract = self.javascript[start:end]
        self.assertIn("state.optionsPolicySaving) return", contract)
        self.assertIn('form.setAttribute("aria-busy", busy ? "true" : "false")', contract)
        self.assertIn('form.toggleAttribute("inert", busy)', contract)
        self.assertIn('form.classList.toggle("is-saving", busy)', contract)
        self.assertIn("optionsPolicySetFormBusy(form, true)", contract)
        self.assertIn('optionsPolicySetFormBusy($("#options-policy-form"), false)', contract)
        self.assertLess(
            contract.index("const result = optionsPolicyPayloadFromForm(form)"),
            contract.index("optionsPolicySetFormBusy(form, true)"),
        )
        self.assertIn('aria-busy="${', self.javascript)
        self.assertIn('state.optionsPolicySaving ? " inert" : ""', self.javascript)

    def test_options_policy_draft_is_memory_only_and_cleared_across_sessions(self) -> None:
        render_locked_start = self.javascript.index("function renderOptionsLocked")
        clear_end = self.javascript.index("function optionNumberFrom", render_locked_start)
        cleanup_contract = self.javascript[render_locked_start:clear_end]
        for contract in (
            "state.optionsPolicyDraft = null",
            "state.optionsPolicyDraftDirty = false",
            "state.optionsPolicySaving = false",
            "state.optionsPolicySaveGeneration += 1",
            "renderOptionsLocked(message)",
        ):
            self.assertIn(contract, cleanup_contract)
        self.assertIn('new BroadcastChannel("finance-radar-private-session")', self.javascript)
        self.assertIn('"private-session-locked"', self.javascript)
        self.assertIn("broadcastPrivateSessionLocked(\"logout\")", self.javascript)
        self.assertIn('window.addEventListener("pageshow"', self.javascript)
        pageshow_start = self.javascript.index('window.addEventListener("pageshow"')
        pageshow_contract = self.javascript[pageshow_start:]
        self.assertIn("if (!event.persisted) return", pageshow_contract)
        self.assertIn("state.authenticated = false", pageshow_contract)
        self.assertIn("state.authStatusLoaded = false", pageshow_contract)
        self.assertIn("旧接货政策已同步清除", pageshow_contract)
        self.assertIn("clearOptionsView(", pageshow_contract)
        self.assertLess(
            pageshow_contract.index("clearOptionsView("),
            pageshow_contract.index("await loadAuthStatus()"),
        )
        self.assertIn('ensureViewLoaded("options", { force: true })', self.javascript)
        options_start = self.javascript.index("// ─── Options research lab")
        options_end = self.javascript.index("// ─── Macro view", options_start)
        options_contract = self.javascript[options_start:options_end]
        self.assertNotIn("localStorage", options_contract)
        self.assertNotIn("sessionStorage", options_contract)

    def test_options_policy_first_configuration_has_no_financial_anchors(self) -> None:
        draft_start = self.javascript.index("function optionsPolicyDraftFromData")
        draft_end = self.javascript.index("function optionsPolicyStatusLabel", draft_start)
        draft_contract = self.javascript[draft_start:draft_end]
        panel_start = self.javascript.index("function optionsPolicyPanelHTML")
        panel_end = self.javascript.index("function optionsCapturePolicyDraft", panel_start)
        panel_contract = self.javascript[panel_start:panel_end]
        for anchored_default in ('"50000.00"', '"30"', '"15"', '"20"', ': "2"'):
            self.assertNotIn(anchored_default, draft_contract)
        self.assertIn('fallback = ""', self.javascript)
        self.assertGreaterEqual(panel_contract.count('placeholder="请自行设定"'), 4)
        self.assertIn("页面不会预填示例数值", panel_contract)
        for label in ("总担保上限", "单标的上限", "最低现金缓冲"):
            self.assertIn('${label}（百分比）', panel_contract)
            self.assertIn('aria-label="${label}，百分比"', panel_contract)
        self.assertIn('aria-label="${label}，百分比"', panel_contract)

    def test_options_policy_layout_has_touch_targets_and_mobile_reading_order(self) -> None:
        for contract in (
            ".options-policy-form {",
            "grid-template-columns: minmax(0, 1.38fr) minmax(360px, 1fr)",
            ".options-policy-tiers::before",
            ".options-policy-underlying {",
            ".options-policy-decisions span {",
            "min-height: 44px",
            ".options-policy-save {",
            "min-height: 48px",
            ".options-policy-terms { position: sticky",
        ):
            self.assertIn(contract, self.css)
        mobile = self.css[self.css.index("@media (max-width: 700px)") :]
        self.assertIn(".options-policy-meta { grid-template-columns: repeat(2", mobile)
        self.assertIn(".options-policy-tier { grid-template-columns: 1fr; }", mobile)
        self.assertIn("grid-template-columns: 1fr; min-inline-size: 0", mobile)
        self.assertIn(".options-policy-fields { grid-template-columns: 1fr", mobile)
        self.assertIn(".options-policy-feedback { padding-inline: 13px; font-size: 16px", mobile)
        self.assertIn(".options-policy-decisions span { min-height: 44px; font-size: 14px; }", mobile)

    def test_other_views_share_the_daily_editorial_reading_system(self) -> None:
        self.assertIn(".wrap { max-width: 1240px", self.css)
        self.assertIn("#view-kol .card-title {", self.css)
        self.assertIn("font-size: 20px; line-height: 1.5", self.css)
        self.assertIn("#view-kol .card-snippet {", self.css)
        self.assertIn("font-size: 14px; line-height: 1.75", self.css)
        self.assertIn("#view-macro .event-title {", self.css)
        self.assertIn("font-family: var(--font-display); font-size: 19px", self.css)
        self.assertIn("#view-decision .decision-card-trigger", self.css)
        self.assertIn("#view-decision .spine-step-head h3", self.css)

    def test_spacex_uses_canonical_listed_asset_label(self) -> None:
        self.assertIn('"US:SPCX": "SpaceX"', self.javascript)
        self.assertIn(
            "const label = ASSET_CN[key] ? assetLabel(key) : suppliedLabel",
            self.javascript,
        )

    def test_daily_desktop_reading_layout_has_a_compact_rail_and_split_impact(self) -> None:
        self.assertIn(
            "grid-template-columns: clamp(184px, 17vw, 212px) minmax(0, 1fr)",
            self.css,
        )
        self.assertIn("@media (min-width: 1101px)", self.css)
        self.assertIn(".daily-stream-narrative.has-impact", self.css)
        self.assertIn(
            'class="daily-stream-narrative${why ? " has-impact" : ""}"',
            self.javascript,
        )
        self.assertIn("--daily-copy-size: 14px", self.css)
        self.assertIn("overflow-wrap: anywhere; font-size: 21px", self.css)
        tablet_start = self.css.index("@media (max-width: 960px)")
        tablet_end = self.css.index("@media (max-width: 700px)", tablet_start)
        tablet_contract = self.css[tablet_start:tablet_end]
        self.assertIn(".daily-stream-copy > header { display: block; }", tablet_contract)
        self.assertIn(".daily-cluster-marker {", tablet_contract)
        self.assertIn("text-overflow: clip; white-space: normal", tablet_contract)

    def test_daily_prefers_chinese_copy_and_keeps_content_tags_distinct(self) -> None:
        for contract in (
            "function dailyLocalizedCopy(item)",
            "item?.title_zh",
            "item?.summary_zh",
            "当前只取得标题，尚不能可靠概括文章正文",
            "function dailyOriginalTitleHTML",
            "function dailyHasChinese",
            "function dailySourceExcerptHTML",
            "daily-original-copy",
            "daily-source-excerpt-copy",
            'lang="en"',
            "function dailyContentTagsHTML",
            "DAILY_CONTENT_CATEGORY_LABELS",
            "DAILY_CONTENT_TAG_LABELS",
            "仅根据标题中文整理，未读取全文",
            "根据策展摘要整理，仍需核对原文",
            "根据作者自帖整理，不等同外链全文",
        ):
            self.assertIn(contract, self.javascript)
        for selector in (
            ".daily-original-title",
            ".daily-content-tags",
            ".daily-content-category",
            ".daily-content-tag",
            ".daily-summary-basis",
            ".daily-source-excerpt",
        ):
            self.assertIn(selector, self.css)
        self.assertIn('aria-label="内容标签"', self.javascript)
        self.assertIn('aria-label="可能受影响的资产"', self.javascript)
        self.assertIn("当前仅有英文标题，中文主旨摘要待取得可靠正文后生成", self.javascript)
        self.assertIn("来源摘录 · 未翻译", self.javascript)
        for key, label in {
            **briefing_topics.CATEGORY_LABELS,
            **briefing_topics.TAG_LABELS,
        }.items():
            self.assertIn(f'{key}: "{label}"', self.javascript)


    def test_daily_discovery_sources_keep_heat_curation_and_time_semantics(self) -> None:
        for contract in (
            '"hn_story", "ai_digest", "paper_digest"',
            '"HN 社区热点"',
            '"策展/发现源"',
            "function dailyHnHeatHTML(item",
            "function dailyHasHnSignal(item)",
            "hacker_news_",
            "item?.hn_rank",
            "item?.hn_score",
            "item?.hn_comments",
            "Hacker News 社区热度",
            "function dailyHnSubmittedTimeHTML(item)",
            "HN 提交",
            "不是原文发布时间",
            "function dailyCuratedDatesHTML(item)",
            "item?.featured_at",
            "publication_time_verified",
            "入选简报",
            "论文发布",
            "论文发布时间待核验",
            "入选不等于今日发表",
            "function dailyExternalActionsHTML(item)",
            "item?.original_url",
            "item?.discussion_url",
            "打开原始来源",
            "HN 讨论",
            "策展条目",
            "仅根据标题中文整理，未读取全文",
            "根据策展摘要整理，仍需核对原文",
        ):
            self.assertIn(contract, self.javascript)
        highlight_start = self.javascript.index("function dailyHighlightHTML(item, index)")
        highlight_end = self.javascript.index(
            "function dailyFirsthandHTML(items)", highlight_start
        )
        highlight_contract = self.javascript[highlight_start:highlight_end]
        self.assertIn("dailyLocalizedCopy(item)", highlight_contract)
        self.assertIn("copy.basis", highlight_contract)
        self.assertIn("dailyContentTagsHTML(item)", highlight_contract)
        self.assertIn("daily-hn-heat", self.css)
        self.assertIn("daily-curated-dates", self.css)
        self.assertIn("daily-context-link", self.css)
        self.assertIn("原始来源优先", self.html)

    def test_daily_event_clusters_do_not_claim_independent_sources(self) -> None:
        self.assertIn("事件簇 · ${sourceCount} 条关联记录 · 不代表独立确认", self.javascript)
        self.assertNotIn("事件簇 · ${sourceCount} 个来源", self.javascript)
        self.assertIn("证据截至 ${esc(lastUpdated)}", self.javascript)
        self.assertNotIn(" · 更新 ${esc(lastUpdated)}", self.javascript)

    def test_private_mode_uses_password_form_and_private_endpoint(self) -> None:
        self.assertIn('type="password"', self.html)
        self.assertIn('autocomplete="current-password"', self.html)
        self.assertIn("api/private/decisions", self.javascript)
        self.assertIn("api/auth/login", self.javascript)
        self.assertIn("api/auth/logout", self.javascript)
        self.assertIn("decisionRequestGeneration", self.javascript)
        self.assertIn("clearDecisionView", self.javascript)
        self.assertIn("logoutPending", self.javascript)
        close_start = self.javascript.index("function closeAuth()")
        close_end = self.javascript.index(
            "async function lockPrivateMode", close_start
        )
        close_contract = self.javascript[close_start:close_end]
        self.assertIn('passcodeInput.value = ""', close_contract)
        self.assertIn('authError.textContent = ""', close_contract)
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
        self.assertIn("关联记录", self.javascript)
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
        self.assertIn('static/app.js?v=36', self.html)
        self.assertIn('static/style.css?v=36', self.html)
        self.assertNotIn('?v=26', self.html)
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
        self.assertIn("原始事实 / 关联记录", self.javascript)
        self.assertIn("规则关联", self.javascript)
        self.assertIn("条件性关联不是因果证明", self.javascript)
        self.assertIn("市场观察", self.javascript)
        self.assertIn("相反证据与不确定性", self.javascript)
        self.assertIn("失效条件", self.javascript)

    def test_decision_detail_is_a_six_step_evidence_spine(self) -> None:
        start = self.javascript.index("function renderDecisionDetail(card, policy)")
        end = self.javascript.index("function renderDecisionDetailConflict", start)
        contract = self.javascript[start:end]
        for key in ("facts", "rules", "model", "exposure", "market", "review"):
            self.assertIn(f'data-spine-step="{key}"', contract)
        for index in range(1, 7):
            self.assertIn(f'<span class="spine-index">{index:02d}</span>', contract)
        for heading in (
            "原始事实 / 关联记录",
            "规则关联",
            "模型传导假设",
            "资产暴露",
            "市场观察",
            "复核门槛",
        ):
            self.assertIn(heading, contract)
        for field in (
            "relation.rationale",
            "relation.relation_type",
            "relation.direction",
            "relation.horizon",
            "relation.method",
        ):
            self.assertIn(field, contract)
        self.assertIn("structuredModelSteps(card)", contract)
        self.assertIn("当前未提供结构化模型路径", contract)
        self.assertIn("不展示模型隐藏思维过程", contract)
        self.assertIn('class="source-record-grid"', contract)
        self.assertIn("detail.snippet", contract)
        self.assertIn("detail.published_at", contract)
        self.assertIn("safeExternalUrl(detail.url)", contract)
        self.assertIn('target="_blank" rel="noopener noreferrer"', contract)
        self.assertIn('class="evidence-spine"', contract)
        self.assertIn(".evidence-spine::before", self.css)
        self.assertNotIn("relation-chain", self.javascript)
        self.assertNotIn("relation-chain", self.css)

    def test_evidence_spine_uses_counted_progressive_disclosure(self) -> None:
        start = self.javascript.index("function renderDecisionDetail(card, policy)")
        end = self.javascript.index("function renderDecisionDetailConflict", start)
        contract = self.javascript[start:end]
        self.assertIn("const evidencePreviewLimit = 4", contract)
        self.assertIn("const relationPreviewLimit = 3", contract)
        self.assertIn("const contraryPreviewLimit = 3", contract)
        self.assertIn("const marketPreviewLimit = 3", contract)
        self.assertIn("查看其余 ${evidence.length - evidencePreviewLimit} 条关联记录", contract)
        self.assertIn("查看其余 ${relations.length - relationPreviewLimit} 条规则关联", contract)
        self.assertIn("条反证或不确定记录", contract)
        self.assertIn("条市场窗口", contract)
        self.assertIn("marketObservationScore(right.row)", contract)
        self.assertIn("可计算 ${completedMarketCount} / ${records.length}", contract)
        self.assertIn('class="spine-disclosure"', contract)
        self.assertIn(".spine-disclosure > summary", self.css)
        self.assertIn("min-height: 44px", self.css)

    def test_contrary_evidence_uses_readable_structured_fields(self) -> None:
        start = self.javascript.index("const renderContraryRow")
        end = self.javascript.index("const modelPathHTML", start)
        contract = self.javascript[start:end]
        self.assertIn('typeof detail === "object"', contract)
        self.assertIn("structuredDetail.snippet || structuredDetail.title", contract)
        self.assertIn("item.rationale || readableDetail", contract)
        self.assertIn("decisionDirectionLabel(item.direction)", contract)

    def test_ai_evidence_confidence_is_not_labeled_as_rule_matching(self) -> None:
        start = self.javascript.index("function evidenceConfidenceLabel")
        end = self.javascript.index("function intelSection", start)
        contract = self.javascript[start:end]
        self.assertIn("证据充分度", contract)
        self.assertIn("非概率", contract)
        self.assertNotIn("匹配度", contract)

    def test_decision_copy_distinguishes_counts_scores_and_market_observations(self) -> None:
        evidence_start = self.javascript.index("function evidenceStatusInfo")
        evidence_end = self.javascript.index("function marketReasonCount", evidence_start)
        evidence_contract = self.javascript[evidence_start:evidence_end]
        self.assertIn("关联记录 ${sourceCount} 条", evidence_contract)
        self.assertNotIn("个独立来源", evidence_contract)
        self.assertNotIn("多源证据", evidence_contract)

        detail_start = self.javascript.index("function renderDecisionDetail(card, policy)")
        detail_end = self.javascript.index("function renderDecisionDetailConflict", detail_start)
        detail_contract = self.javascript[detail_start:detail_end]
        self.assertIn("关注优先级", detail_contract)
        self.assertIn("规则匹配度", detail_contract)
        self.assertIn("不是概率", detail_contract)
        self.assertNotIn("决策分", detail_contract)
        self.assertNotIn("<span>置信", detail_contract)
        self.assertIn("价格观察点", detail_contract)
        self.assertIn("资产绝对收益", detail_contract)
        self.assertIn("基准收益", detail_contract)
        self.assertIn("相对基准超额", detail_contract)
        self.assertIn("market-direction-summary", detail_contract)
        self.assertIn("marketExpectedPerformanceLabel(row)", detail_contract)
        self.assertIn("marketObservedPerformanceLabel(row)", detail_contract)
        self.assertIn("事件预期与实际相对表现", detail_contract)
        self.assertIn("row.benchmark_asset_key", detail_contract)
        self.assertIn("row.provider", detail_contract)
        self.assertIn("row.window", detail_contract)
        self.assertIn("观察锚点", detail_contract)
        self.assertIn("row.source_id", detail_contract)
        self.assertIn("function localizeDecisionTerms", self.javascript)
        self.assertIn('replace(/\\babstain\\b/gi, "保持观察")', self.javascript)
        self.assertIn('replace(/\\bpending\\b/gi, "等待中")', self.javascript)
        self.assertIn("const evidencePolicy = localizeDecisionTerms", self.javascript)
        self.assertIn("重复转载不等于独立确认", self.html)

    def test_market_observation_calls_out_absolute_relative_divergence(self) -> None:
        start = self.javascript.index("function marketBenchmarkLabel")
        end = self.javascript.index("function marketTimestampRange", start)
        contract = self.javascript[start:end]
        self.assertIn("function expectedRelativePerformanceLabel", contract)
        self.assertIn("row?.evaluated_direction || row?.expected_direction", contract)
        self.assertIn("row?.benchmark_asset_key", contract)
        self.assertIn('row?.asset_return', contract)
        self.assertIn('row?.abnormal_return', contract)
        self.assertIn('row?.direction_confirmed === true', contract)
        self.assertIn('"市场表现支持事件预期"', contract)
        self.assertIn('"市场表现未验证事件预期"', contract)
        self.assertIn('`跑赢 ${benchmark} ${pct(value)}`', contract)
        self.assertIn('`跑输 ${benchmark} ${pct(value)}`', contract)
        self.assertIn('`资产虽上涨，但仍跑输 ${benchmark}`', contract)
        self.assertIn('`资产虽下跌，但仍跑赢 ${benchmark}`', contract)
        self.assertIn("return-divergence", self.javascript)
        self.assertIn(".return-divergence", self.css)
        self.assertIn(".market-direction-summary", self.css)

    def test_market_observation_uses_plain_language_in_every_surface(self) -> None:
        self.assertIn("市场表现未验证事件预期", self.javascript)
        self.assertIn("市场表现支持事件预期", self.javascript)
        self.assertIn("市场表现尚无一致结论", self.javascript)
        self.assertIn("这不是反向交易信号", self.javascript)
        self.assertIn("预期 ${esc(expectedPerformance)}", self.javascript)
        self.assertIn("实际 ${esc(observedPerformance)}", self.javascript)
        self.assertNotIn("相对基准与规则方向反向", self.javascript)
        self.assertNotIn("相对基准方向反向", self.javascript)
        self.assertNotIn("相对同向", self.javascript)
        self.assertNotIn("相对反向", self.javascript)

    def test_market_observation_formats_unix_second_timestamps(self) -> None:
        start = self.javascript.index("function marketTimestampRange")
        end = self.javascript.index("function renderDecisionDetail", start)
        contract = self.javascript[start:end]
        self.assertIn("const numeric = Number(raw)", contract)
        self.assertIn("Math.abs(numeric) < 1e12 ? numeric * 1000 : numeric", contract)
        self.assertIn("const parsed = new Date(milliseconds)", contract)
        self.assertIn("Number.isNaN(parsed.getTime())", contract)
        self.assertIn('"时间待核验"', contract)
        self.assertIn("fmtAbsoluteTime(parsed.toISOString())", contract)
        self.assertIn("formatTimestamp(timestamps.start)", contract)
        self.assertIn("formatTimestamp(timestamps.end)", contract)

    def test_decision_labels_are_legible_and_mobile_fab_does_not_cover_evidence(
        self,
    ) -> None:
        self.assertIn("--muted: #566f73", self.css)
        self.assertIn(".boundary-cell small {", self.css)
        self.assertIn(".decision-card-boundary small {", self.css)
        self.assertIn("font-size: 10.5px", self.css)
        mobile = self.css.index("@media (max-width: 700px)")
        mobile_contract = self.css[mobile:]
        fab = mobile_contract.index(".support-fab {")
        self.assertIn("display: none", mobile_contract[fab : fab + 80])
        self.assertIn('class="footer-support" data-support-open', self.html)

    def test_master_detail_selection_and_detail_updates_are_accessible(self) -> None:
        self.assertIn('aria-label="当前决策详情"', self.html)
        detail_tag = self.html[
            self.html.index('id="decision-detail"') : self.html.index(">", self.html.index('id="decision-detail"'))
        ]
        self.assertNotIn("aria-live", detail_tag)
        self.assertIn('id="decision-detail-title"', self.html)
        self.assertIn('aria-pressed="${String(key === state.selectedDecisionKey)}"', self.javascript)
        self.assertIn('aria-controls="decision-detail"', self.javascript)
        self.assertIn('node.setAttribute("aria-pressed", String(selected))', self.javascript)
        self.assertIn('id="decision-detail-title" tabindex="-1"', self.html)
        self.assertIn('$("#decision-detail-title")?.focus({ preventScroll: true })', self.javascript)
        self.assertIn('role="status" aria-live="polite" aria-busy="true"', self.javascript)
        self.assertIn(".action-panel { position: sticky", self.css)
        self.assertIn(".action-panel { position: static", self.css)
        self.assertIn("body { font-size: 14px; overflow-x: hidden; }", self.css)
        self.assertIn(".source-record-card p, .rule-relation-card > p", self.css)
        self.assertIn("font-size: 14px; line-height: 1.72", self.css)
        tablet = self.css.index("@media (max-width: 960px)")
        self.assertIn(".action-queue { max-height: none; overflow: visible; }", self.css[tablet:])

    def test_decision_lenses_filter_queue_and_matrix_with_bounded_full_load(
        self,
    ) -> None:
        hero = self.html.index('id="decision-hero"')
        lenses = self.html.index('id="decision-lenses"')
        layout = self.html.index('class="decision-layout decision-master-detail"')
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

    def test_kol_feed_supports_persistent_accessible_multi_selection(self) -> None:
        for text in (
            'aria-label="按 KOL 筛选，可多选"',
            'data-kol="" class="chip active" aria-pressed="true"',
            'id="kol-filter-status" role="status" aria-live="polite"',
        ):
            self.assertIn(text, self.html)
        for text in (
            "KOL_SELECTION_STORAGE_KEY",
            "KOL_SELECTION_LIMIT = 20",
            "PUBLIC_KOL_KEY_PATTERN",
            "selectedKols: new Set()",
            "function loadKolSelection()",
            "function persistKolSelection()",
            "localStorage.getItem(KOL_SELECTION_STORAGE_KEY)",
            "localStorage.setItem(",
            "function toggleKolSelection(rawKey)",
            "function clearKolSelection()",
            "function kolFilterSignature()",
            'loadedKolFilterSignature: ""',
            "kolSelectionPersisted: true",
            "kolCatalogLoaded: false",
            "viewLoadGeneration: { decision: 0, daily: 0, macro: 0, options: 0, kol: 0 }",
            "已选 ${selectedCount} 位 · 仅看所选 KOL",
            "最多选择 ${KOL_SELECTION_LIMIT} 位 KOL",
        ):
            self.assertIn(text, self.javascript)

        toggle_start = self.javascript.index("function toggleKolSelection")
        toggle_end = self.javascript.index("function clearKolSelection", toggle_start)
        toggle_contract = self.javascript[toggle_start:toggle_end]
        self.assertIn("state.selectedKols.delete(key)", toggle_contract)
        self.assertIn("state.selectedKols.add(key)", toggle_contract)
        self.assertIn("persistKolSelection()", toggle_contract)
        self.assertIn("loadEvents()", toggle_contract)

        clear_start = toggle_end
        clear_end = self.javascript.index("async function loadStats", clear_start)
        clear_contract = self.javascript[clear_start:clear_end]
        self.assertIn("state.selectedKols.clear()", clear_contract)
        self.assertIn("updateKolSelectionUi()", clear_contract)

        load_start = self.javascript.index("async function loadEvents")
        load_end = self.javascript.index("// ─── Support", load_start)
        load_contract = self.javascript[load_start:load_end]
        self.assertIn(
            'p.set("kols", selectedKolKeys().join(","))',
            load_contract,
        )
        self.assertNotIn('p.set("kol",', load_contract)

        reconcile_start = self.javascript.index("function reconcileKolSelection")
        reconcile_end = self.javascript.index(
            "function toggleKolSelection", reconcile_start
        )
        reconcile_contract = self.javascript[reconcile_start:reconcile_end]
        self.assertIn(
            "previous.filter((key) => available.has(key))",
            reconcile_contract,
        )
        self.assertIn("if (changed) persistKolSelection()", reconcile_contract)
        self.assertIn(
            "list.filter((item) => item?.configured !== false)",
            self.javascript,
        )
        self.assertIn("state.kolCatalogLoaded = true", self.javascript)
        self.assertIn("state.kolCatalogLoaded = false", self.javascript)
        self.assertIn("KOL 列表加载失败 · 可重试当前视图", self.javascript)
        self.assertIn("throw new Error(\"invalid_kol_catalog\")", self.javascript)
        self.assertIn("throw new Error(\"empty_kol_catalog\")", self.javascript)
        self.assertIn("偏好存于本机，筛选项随请求发送", self.javascript)
        self.assertIn("当前会话有效，筛选项随请求发送", self.javascript)
        self.assertIn("仍按本机已选 ${selectedCount} 位筛选", self.javascript)
        self.assertIn(
            "state.loadedKolFilterSignature === kolFilterSignature()",
            self.javascript,
        )
        self.assertIn(
            "state.loadedKolFilterSignature = requestFilterSignature",
            self.javascript,
        )
        self.assertIn("const loadGeneration = ++state.viewLoadGeneration[view]", self.javascript)
        self.assertIn("state.viewLoadGeneration.kol += 1", self.javascript)
        self.assertLess(
            self.javascript.rindex("loadKolSelection();"),
            self.javascript.index("await ensureViewLoaded(state.view)"),
        )
        self.assertIn("max-height: 148px", self.css)
        self.assertIn("min-height: 44px", self.css)

    def test_kol_feed_progressively_supplements_high_impact_results(self) -> None:
        load_start = self.javascript.index("async function loadEvents")
        load_end = self.javascript.index("// ─── Support", load_start)
        contract = self.javascript[load_start:load_end]

        # The regular feed owns the critical path; the optional high-impact
        # supplement must never delay its first successful render.
        self.assertNotIn("Promise.allSettled", contract)
        regular_fetch = contract.index(
            "const regularData = await fetchJSON(url, 12000"
        )
        regular_render = contract.index("renderEvents(regularItems)")
        supplement_fetch = contract.index(
            "const highData = await fetchJSON(highUrl, 12000"
        )
        supplement_render = contract.index("renderEvents(items)", supplement_fetch)
        self.assertLess(regular_fetch, regular_render)
        self.assertLess(regular_render, supplement_fetch)
        self.assertLess(supplement_fetch, supplement_render)

        # A filtered impact view stays a single request. An unfiltered view
        # only spends the second request when the 150-row page can hide highs.
        self.assertIn("!state.impact", contract)
        self.assertIn("regularItems.length >= 150", contract)
        self.assertIn("regularHighCount < 50", contract)
        self.assertIn("if (!shouldSupplementHighImpact) {", contract)
        self.assertEqual(contract.count("await fetchJSON("), 2)

        # A capped feed is only marked filter-complete after its optional
        # supplement succeeds.  If that request is aborted or fails, a later
        # view activation may retry instead of treating the partial feed as
        # fresh.
        no_supplement_start = contract.index(
            "if (!shouldSupplementHighImpact) {"
        )
        supplement_params = contract.index(
            "const highParams = new URLSearchParams(p)",
            no_supplement_start,
        )
        self.assertIn(
            "state.loadedKolFilterSignature = requestFilterSignature",
            contract[no_supplement_start:supplement_params],
        )
        self.assertIn(
            'state.loadedKolFilterSignature = ""',
            contract[no_supplement_start:supplement_params],
        )
        supplement_success = contract[
            supplement_fetch:contract.index("} catch (highError) {", supplement_fetch)
        ]
        self.assertLess(
            supplement_success.index("renderEvents(items)"),
            supplement_success.index(
                "state.loadedKolFilterSignature = requestFilterSignature"
            ),
        )

        # Both render phases are guarded against a superseded generation,
        # controller, aborted view switch, or changed KOL/filter signature.
        guard_start = contract.index("const requestIsCurrent")
        guard_end = contract.index("const feed =", guard_start)
        guard = contract[guard_start:guard_end]
        self.assertIn("generation === state.feedRequestGeneration", guard)
        self.assertIn("state.feedAbortController === requestController", guard)
        self.assertIn("!requestController.signal.aborted", guard)
        self.assertIn(
            "requestFilterSignature === kolFilterSignature()",
            guard,
        )
        self.assertGreaterEqual(
            contract.count("if (!requestIsCurrent()) return false"),
            3,
        )

        # A current supplement failure keeps the already-rendered regular
        # result successful and never enters the critical-request error path.
        supplement_catch = contract.index("} catch (highError) {", supplement_fetch)
        outer_catch = contract.index("} catch (e) {", supplement_catch)
        failure_contract = contract[supplement_catch:outer_catch]
        self.assertIn('console.warn("high impact feed", highError)', failure_contract)
        self.assertIn("return true", failure_contract)
        self.assertNotIn("setViewLoadError", failure_contract)
        self.assertNotIn("renderEvents([]", failure_contract)
        self.assertNotIn("loadedKolFilterSignature", failure_contract)

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
        self.assertIn("关联记录 ${sourceCount} 条 · 独立性待核验", self.javascript)
        self.assertIn("关联记录 1 条 · 待交叉核验", self.javascript)
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
        decision_html_start = self.html.index('id="view-decision"')
        decision_html_end = self.html.index('id="view-daily"', decision_html_start)
        decision_js_start = self.javascript.index("// ─── Decision cockpit")
        decision_js_end = self.javascript.index(
            "// ─── Options research lab", decision_js_start
        )
        forbidden = (
            self.html[decision_html_start:decision_html_end]
            + self.javascript[decision_js_start:decision_js_end]
        )
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
        self.assertIn("const hasPendingWindow", self.javascript)
        self.assertIn("const hasAlternativeValidation", self.javascript)
        self.assertIn("部分市场观察未完成", self.javascript)
        self.assertIn("部分项目需补充验证条件", self.javascript)
        self.assertIn("不适用事件后市场窗口", self.javascript)
        self.assertIn("不能代表全部事件", self.javascript)
        self.assertIn("市场表现未验证事件预期", self.javascript)
        self.assertIn("市场表现尚无一致结论", self.javascript)

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
        self.assertIn('class="lead-context-grid"', self.javascript)
        self.assertIn("验证门槛", self.javascript)
        self.assertNotIn('class="transmission-ribbon"', self.javascript)
        self.assertNotIn("信号 → 主题 → 资产", self.javascript)
        self.assertNotIn("当前首要传导", self.javascript)
        self.assertNotIn("资产传导", self.javascript)
        self.assertNotIn("宏观覆盖 ${coverage}%", self.javascript)
        self.assertIn("本轮暂无候选行动", self.javascript)
        self.assertIn(
            "if (state.decisionData) renderDecisionHero(state.decisionData)",
            self.javascript,
        )

    def test_macro_position_alerts_are_between_hero_and_trend(self) -> None:
        hero = self.html.index('id="macro-hero"')
        alerts = self.html.index('id="macro-alerts-block"')
        trend = self.html.index('id="macro-trend-block"')
        self.assertLess(hero, alerts)
        self.assertLess(alerts, trend)
        self.assertIn('id="macro-alerts-title"', self.html)
        self.assertIn("减仓与清仓预警", self.html)
        self.assertIn("规则试运行", self.html)
        self.assertIn("所有候选行动均需人工确认，系统不会执行任何订单", self.html)
        self.assertIn('id="macro-alert-live-status" role="status"', self.html)
        alerts_host = self.html[
            self.html.index('id="macro-alerts"') :
            self.html.index('id="macro-alert-live-status"')
        ]
        self.assertNotIn("aria-live", alerts_host)

    def test_macro_position_alerts_render_only_the_strict_public_contract(
        self,
    ) -> None:
        self.assertIn("function renderMarketAlerts(payload)", self.javascript)
        self.assertIn("renderMarketAlerts(d.market_alerts)", self.javascript)
        self.assertNotIn("renderMarketAlerts(d)", self.javascript)
        for contract_check in (
            'payload.schema_version === 1',
            'payload.method_version === "macro-de-risk-trial-v1"',
            'payload.mode === "trial"',
            'payload.human_review_required === true',
            'payload.automatic_execution === false',
            'candidate?.market === marketCode',
        ):
            self.assertIn(contract_check, self.javascript)
        for action in (
            "observe",
            "prepare_reduce",
            "reduce_candidate",
            "exit_candidate",
        ):
            self.assertIn(f'key: "{action}"', self.javascript)
        for field in (
            "gate_progress",
            "gates",
            "triggered_signals",
            "counter_signals",
            "upgrade_conditions",
            "invalidation_conditions",
            "missing_sources",
            "rule_version",
        ):
            self.assertIn(f"market.{field}", self.javascript)
        render_start = self.javascript.index("function renderMarketAlerts(payload)")
        render_end = self.javascript.index("function renderHero(d)", render_start)
        render_contract = self.javascript[render_start:render_end]
        self.assertNotIn("composite_risk", render_contract)
        self.assertNotIn("market_data", render_contract)

    def test_macro_position_alerts_fail_closed_and_are_accessible(self) -> None:
        for copy in (
            "预警数据尚未形成，等待下一次宏观采集",
            "暂不形成减仓或清仓判断",
            "证据不足，系统保持观望",
            "触发证据",
            "反向证据",
            "升级条件",
            "解除条件",
            "时间待核验",
        ):
            self.assertIn(copy, self.javascript)
        self.assertIn("macroAlertAnnouncementSignature: null", self.javascript)
        self.assertIn(
            "if (state.macroAlertAnnouncementSignature === signature) return",
            self.javascript,
        )
        self.assertIn('aria-current="step"', self.javascript)
        self.assertIn('rel="noopener noreferrer"', self.javascript)
        self.assertIn("safeExternalUrl(signal.source_url)", self.javascript)
        self.assertIn(".macro-alert-grid {", self.css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", self.css)
        self.assertIn(".macro-alert-grid { grid-template-columns: 1fr; }", self.css)
        self.assertIn(".macro-alert-evidence > summary {", self.css)
        self.assertIn("min-height: 44px", self.css)

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
        responsive = self.css.index("@media (max-width: 1680px)")
        responsive_contract = self.css[responsive:]
        self.assertIn(".support-fab { display: none; }", responsive_contract)
        self.assertIn(".support-topbar { display: grid; }", responsive_contract)
        mobile = self.css.index("@media (max-width: 700px)")
        mobile_contract = self.css[mobile:]
        fab = mobile_contract.index(".support-fab {")
        self.assertIn("display: none", mobile_contract[fab : fab + 80])

    def test_feed_prioritizes_high_impact_and_labels_source_nature(self) -> None:
        self.assertIn("stats: null", self.javascript)
        self.assertIn('highParams.set("impact", "high")', self.javascript)
        self.assertIn('highParams.set("limit", "50")', self.javascript)
        self.assertIn("mergePriorityEvents", self.javascript)
        self.assertIn("高影响已优先", self.javascript)
        self.assertIn("普通流仅展示前150条", self.javascript)
        self.assertIn("当前窗口采集记录", self.javascript)
        self.assertIn("本人动态", self.javascript)
        self.assertIn("本人被提及", self.javascript)
        self.assertIn("关联公司动态", self.javascript)
        self.assertIn('basis === "person_mention"', self.javascript)
        self.assertIn('basis === "company_mention"', self.javascript)
        self.assertIn("媒体提及", self.javascript)
        self.assertIn('class="source-kind ${sourceNature.key}"', self.javascript)
        self.assertIn(
            "const candidates = [item?.source_url, item?.canonical_url, item?.url]",
            self.javascript,
        )

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

    def test_manual_ai_requests_are_scoped_idempotent_and_accessible(self) -> None:
        self.assertIn('api("api/private/ai-requests")', self.javascript)
        self.assertIn('api("api/private/ai-requests/status")', self.javascript)
        self.assertIn('"X-Finance-Radar-Action": AI_REQUEST_ACTION_HEADER', self.javascript)
        self.assertIn('subject_type: subjectType', self.javascript)
        self.assertIn('subject_id: subjectId', self.javascript)
        self.assertIn("aiRequestInFlight: new Map()", self.javascript)
        self.assertIn("if (state.aiRequestInFlight.has(key))", self.javascript)
        self.assertIn('aiRequestControl("event", it.id, it, "card")', self.javascript)
        self.assertIn("primaryAiSubject || event", self.javascript)
        self.assertIn(
            'data-ai-request-key="${esc(key)}"',
            self.javascript,
        )
        self.assertIn('aiRequestControl("macro_event", e.id, e, "macro")', self.javascript)
        self.assertIn('String(item?.ai_status || "pending")', self.javascript)
        self.assertIn('=== "ready") return ""', self.javascript)
        self.assertIn("相同证据只会处理一次", self.javascript)
        self.assertIn("本次没有额外消耗 Token", self.javascript)
        self.assertIn("function aiRequestEligible(item)", self.javascript)
        self.assertIn("AI 已归并到主证据", self.javascript)
        self.assertIn("AI 只处理事件主证据，避免重复消耗 Token", self.javascript)
        self.assertIn("primary_ai_subject", self.javascript)
        self.assertIn("primaryAiSubject?.relations ?? payload?.relations", self.javascript)
        self.assertIn(
            "primaryAiSubject?.market_reactions ?? payload?.market_reactions",
            self.javascript,
        )
        self.assertIn("AI 解读绑定事件主证据", self.javascript)
        self.assertIn("本次没有重新调用模型", self.javascript)
        self.assertIn("if (!aiRequestEligible(item))", self.javascript)
        self.assertIn("当前证据 · ${impactText}", self.javascript)
        self.assertIn(".primary-ai-context", self.css)
        self.assertIn("同一事件只对主证据生成一次 AI 解读", self.javascript)
        self.assertIn("不能提前绕过退避", self.javascript)
        self.assertIn('role="status" aria-live="polite"', self.javascript)
        self.assertIn('aria-describedby="${esc(statusId)}"', self.javascript)
        self.assertIn("clearAllAiRequestPolls", self.javascript)
        self.assertIn("AI_REQUEST_POLL_DELAYS", self.javascript)
        self.assertIn('rawStatus === "retry"', self.javascript)
        self.assertIn("scheduleAiRequestStatusCheck", self.javascript)
        self.assertIn("aiRequestRetryDelay", self.javascript)
        self.assertIn("handlePrivateSessionExpired();", self.javascript)
        self.assertIn(
            'closeIntelDrawer({ restoreFocus: false })',
            self.javascript,
        )
        self.assertIn(
            'openAuth(returnFocus, { purpose: "ai" })',
            self.javascript,
        )
        self.assertIn(
            'function closeIntelDrawer({ restoreFocus = true } = {})',
            self.javascript,
        )
        self.assertIn("placement", self.javascript)
        self.assertIn('button.setAttribute("aria-busy"', self.javascript)
        self.assertIn(".ai-request-rail", self.css)
        self.assertIn("min-height: 44px", self.css)
        self.assertNotIn("重新解析", self.javascript)

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
