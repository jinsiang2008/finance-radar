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
        self.assertIn(
            'class="tab active" data-view="decision"', self.html
        )
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

    def test_evidence_and_market_validation_are_visually_separated(self) -> None:
        self.assertIn("机制证据（不是因果证明）", self.javascript)
        self.assertIn("市场验证", self.javascript)
        self.assertIn("相反证据与不确定性", self.javascript)
        self.assertIn("失效条件", self.javascript)


if __name__ == "__main__":
    unittest.main()
