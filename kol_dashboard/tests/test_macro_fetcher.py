from __future__ import annotations

import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


LIB = Path(__file__).resolve().parents[2] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import macro_fetcher  # noqa: E402


FED_STATEMENT_URL = (
    "https://www.federalreserve.gov/newsevents/pressreleases/"
    "monetary20260729a.htm"
)
PBOC_OPERATION_URL = (
    "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/125475/"
    "2026080608581014813/index.html"
)


class OfficialPolicyContentTests(unittest.TestCase):
    def test_extracts_fed_rate_range_and_vote_from_official_body(self) -> None:
        html = """
        <!doctype html>
        <html><body>
          <nav>Markets About the Fed</nav>
          <input class="site-search" type="search">
          <img class="header-logo" src="logo.svg" alt="Federal Reserve">
          <main id="article">
            <h1>Federal Reserve issues FOMC statement</h1>
            <p>
              The Committee decided to maintain the target range for the
              federal funds rate at 3-1/2 to 3-3/4 percent.
            </p>
            <p>
              The vote was 9–3. Three members preferred to raise the target
              range by 1/4 percentage point at this meeting.
            </p>
          </main>
          <footer>Board of Governors of the Federal Reserve System</footer>
        </body></html>
        """.encode("utf-8")

        with mock.patch.object(
            macro_fetcher,
            "_download_official_html",
            return_value=(html, FED_STATEMENT_URL, "utf-8"),
        ) as download:
            result = macro_fetcher.fetch_official_policy_content(FED_STATEMENT_URL)

        download.assert_called_once_with(FED_STATEMENT_URL)
        self.assertEqual(result["content_status"], "ready")
        self.assertEqual(result["content_source_url"], FED_STATEMENT_URL)
        self.assertIn("3-1/2 to 3-3/4 percent", result["content_excerpt"])
        self.assertIn("9–3", result["content_excerpt"])
        self.assertIn("1/4 percentage point", result["content_excerpt"])
        self.assertNotIn("Markets About the Fed", result["content_excerpt"])
        self.assertTrue(result["evidence_sections"])
        self.assertTrue(
            all(section["kind"] == "paragraph" for section in result["evidence_sections"])
        )

    def test_prefers_utf8_for_pboc_body_and_preserves_table_rows(self) -> None:
        html = """
        <!doctype html>
        <html><body>
          <div id="zoom">
            <p>为保持银行体系流动性充裕，中国人民银行以固定利率、数量招标方式开展了逆回购操作。</p>
            <table>
              <tr><th>期限</th><th>投标量</th><th>中标量</th><th>中标利率</th></tr>
              <tr><td>7天</td><td>10亿元</td><td>10亿元</td><td>1.40%</td></tr>
            </table>
          </div>
          <div class="footer">网站主办单位：中国人民银行</div>
        </body></html>
        """.encode("utf-8")

        # The real PBoC response has historically advertised an unreliable
        # legacy charset. UTF-8 must still win when the bytes are valid UTF-8.
        with mock.patch.object(
            macro_fetcher,
            "_download_official_html",
            return_value=(html, PBOC_OPERATION_URL, "gb2312"),
        ):
            result = macro_fetcher.fetch_official_policy_content(PBOC_OPERATION_URL)

        self.assertEqual(result["content_status"], "ready")
        self.assertIn("中国人民银行", result["content_excerpt"])
        self.assertIn("10亿元", result["content_excerpt"])
        self.assertIn("1.40%", result["content_excerpt"])
        table_rows = [
            section["text"]
            for section in result["evidence_sections"]
            if section["kind"] == "table_row"
        ]
        self.assertIn("期限 | 投标量 | 中标量 | 中标利率", table_rows)
        self.assertIn("7天 | 10亿元 | 10亿元 | 1.40%", table_rows)
        self.assertNotIn("网站主办单位", result["content_excerpt"])

    def test_official_url_allowlist_accepts_only_known_article_paths(self) -> None:
        supported = (
            FED_STATEMENT_URL,
            "https://www.federalreserve.gov/newsevents/speech/powell20260730a.htm",
            PBOC_OPERATION_URL,
            PBOC_OPERATION_URL.replace("https://", "http://"),
        )
        rejected = (
            "http://127.0.0.1/newsevents/pressreleases/monetary20260729a.htm",
            "https://localhost/newsevents/pressreleases/monetary20260729a.htm",
            "https://www.federalreserve.gov.evil.example/newsevents/pressreleases/monetary20260729a.htm",
            "https://attacker@www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm",
            "https://www.federalreserve.gov:8443/newsevents/pressreleases/monetary20260729a.htm",
            "https://www.federalreserve.gov/newsevents/%2e%2e/pressreleases/monetary20260729a.htm",
            "https://www.federalreserve.gov/feeds/press_monetary.xml",
            "https://www.pbc.gov.cn/zhengcehuobisi/index.html",
        )

        for url in supported:
            with self.subTest(url=url):
                self.assertTrue(macro_fetcher.is_supported_official_policy_url(url))
        self.assertEqual(
            macro_fetcher._normalized_official_policy_url(
                PBOC_OPERATION_URL.replace("https://", "http://")
            ),
            PBOC_OPERATION_URL,
        )
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(macro_fetcher.is_supported_official_policy_url(url))

    def test_dns_guard_rejects_non_public_resolution_before_opening(self) -> None:
        with (
            mock.patch.object(macro_fetcher, "_host_resolves_publicly", return_value=False),
            mock.patch.object(macro_fetcher.urllib.request, "build_opener") as build_opener,
        ):
            with self.assertRaises(urllib.error.URLError):
                macro_fetcher._download_official_html(FED_STATEMENT_URL)

        build_opener.assert_not_called()

    def test_unsupported_and_download_failures_have_explicit_status(self) -> None:
        with mock.patch.object(macro_fetcher, "_download_official_html") as download:
            unsupported = macro_fetcher.fetch_official_policy_content(
                "https://evil.example/newsevents/pressreleases/monetary20260729a.htm"
            )
        download.assert_not_called()
        self.assertEqual(unsupported, {"content_status": "unsupported"})

        with mock.patch.object(
            macro_fetcher,
            "_download_official_html",
            side_effect=TimeoutError("official site timed out"),
        ):
            unavailable = macro_fetcher.fetch_official_policy_content(FED_STATEMENT_URL)

        self.assertEqual(unavailable["content_status"], "unavailable")
        self.assertEqual(unavailable["content_source_url"], FED_STATEMENT_URL)
        self.assertNotIn("content_excerpt", unavailable)
        self.assertNotIn("evidence_sections", unavailable)

    def test_non_substantive_official_page_is_unavailable(self) -> None:
        raw = b"<html><body><nav>Navigation</nav><p>Short</p></body></html>"
        with mock.patch.object(
            macro_fetcher,
            "_download_official_html",
            return_value=(raw, FED_STATEMENT_URL, "utf-8"),
        ):
            result = macro_fetcher.fetch_official_policy_content(FED_STATEMENT_URL)

        self.assertEqual(result["content_status"], "unavailable")
        self.assertNotIn("content_excerpt", result)

    def test_http_200_challenge_page_is_never_treated_as_official_body(self) -> None:
        raw = b"""
        <html><main id="content"><h1>Access Denied</h1>
        <p>Your request was blocked by a security check. Enable cookies and try again.</p>
        </main></html>
        """
        with mock.patch.object(
            macro_fetcher,
            "_download_official_html",
            return_value=(raw, FED_STATEMENT_URL, "utf-8"),
        ):
            result = macro_fetcher.fetch_official_policy_content(FED_STATEMENT_URL)

        self.assertEqual(result["content_status"], "unavailable")
        self.assertNotIn("content_excerpt", result)

    def test_final_evidence_url_must_be_the_same_official_article(self) -> None:
        other_url = FED_STATEMENT_URL.replace("monetary20260729a", "monetary20260617a")
        raw = (
            b'<html><main id="article"><p>The Committee maintained the target '
            b'range and published a sufficiently detailed policy statement.</p></main></html>'
        )
        with mock.patch.object(
            macro_fetcher,
            "_download_official_html",
            return_value=(raw, other_url, "utf-8"),
        ):
            result = macro_fetcher.fetch_official_policy_content(FED_STATEMENT_URL)

        self.assertEqual(result["content_status"], "unavailable")
        self.assertNotIn("content_excerpt", result)

    def test_priority_container_direct_text_falls_back_to_bounded_body(self) -> None:
        raw = """
        <html><div id="zoom"><h1>公告</h1><div>
        中国人民银行决定开展一项公开市场操作，以维护银行体系流动性合理充裕，
        具体期限、操作规模和中标利率以本公告列示的数据为准。
        </div></div></html>
        """.encode("utf-8")
        with mock.patch.object(
            macro_fetcher,
            "_download_official_html",
            return_value=(raw, PBOC_OPERATION_URL, "utf-8"),
        ):
            result = macro_fetcher.fetch_official_policy_content(PBOC_OPERATION_URL)

        self.assertEqual(result["content_status"], "ready")
        self.assertIn("流动性合理充裕", result["content_excerpt"])

    def test_pboc_candidates_sort_by_embedded_time_and_cross_year(self) -> None:
        listing = """
        <a href="/zhengcehuobisi/1/2026123110000012345/index.html">旧年度政策公告</a>
        <a href="/zhengcehuobisi/1/2027010209000012345/index.html">新年度政策公告</a>
        """.encode("utf-8")
        with mock.patch.object(
            macro_fetcher,
            "http_get_raw",
            return_value=listing,
        ) as get:
            items = macro_fetcher.fetch_pboc()

        get.assert_called_once_with(
            "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/index.html"
        )
        self.assertEqual(
            [item["title"] for item in items],
            ["新年度政策公告", "旧年度政策公告"],
        )
        self.assertTrue(all(item["url"].startswith("https://") for item in items))


class PolicyRssTests(unittest.TestCase):
    def test_fomc_rss_uses_xml_categories_and_drops_title_only_description(self) -> None:
        rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
          <item>
            <title>Federal Reserve issues FOMC statement</title>
            <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm</link>
            <description><![CDATA[<p>Federal Reserve issues FOMC statement</p>]]></description>
            <pubDate>Wed, 29 Jul 2026 18:00:00 GMT</pubDate>
          </item>
          <item>
            <title>Minutes of the Federal Open Market Committee, June 17-18, 2026</title>
            <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260708a.htm</link>
            <description><![CDATA[Committee participants discussed inflation and labor market conditions.]]></description>
            <pubDate>Wed, 08 Jul 2026 18:00:00 GMT</pubDate>
          </item>
        </channel></rss>"""

        with mock.patch.object(macro_fetcher, "http_get", return_value=rss) as get:
            items = macro_fetcher.fetch_fomc()

        get.assert_called_once_with("https://www.federalreserve.gov/feeds/press_monetary.xml")
        self.assertEqual([item["category"] for item in items], ["fomc_statement", "fomc_minutes"])
        self.assertNotIn("snippet", items[0])
        self.assertEqual(
            items[1]["snippet"],
            "Committee participants discussed inflation and labor market conditions.",
        )
        self.assertEqual(items[0]["date"], "Wed, 29 Jul 2026 18:00:00 GMT")

    def test_speech_rss_is_classified_as_fed_speech(self) -> None:
        rss = """<rss><channel><item>
          <title>Speech by Chair Powell on the economic outlook</title>
          <link>https://www.federalreserve.gov/newsevents/speech/powell20260730a.htm</link>
          <description>A detailed discussion of inflation, employment, and policy.</description>
          <pubDate>Thu, 30 Jul 2026 14:00:00 GMT</pubDate>
        </item></channel></rss>"""

        with mock.patch.object(macro_fetcher, "http_get", return_value=rss):
            items = macro_fetcher.fetch_fed_speeches()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "fed_speech")
        self.assertIn("inflation", items[0]["snippet"])

    def test_malformed_or_empty_rss_fails_closed(self) -> None:
        for raw in ("", "<rss><channel><item>"):
            with self.subTest(raw=raw):
                with mock.patch.object(macro_fetcher, "http_get", return_value=raw):
                    self.assertEqual(macro_fetcher.fetch_fed(), [])

    def test_all_press_feed_drops_application_and_enforcement_noise(self) -> None:
        rss = """<rss><channel>
        <item><title>Federal Reserve Board announces approval of the application by Example Bancshares</title>
        <link>https://www.federalreserve.gov/newsevents/pressreleases/orders20260801a.htm</link>
        <pubDate>Sat, 01 Aug 2026 14:00:00 GMT</pubDate></item>
        <item><title>Federal Reserve Board issues enforcement action with Example Bank</title>
        <link>https://www.federalreserve.gov/newsevents/pressreleases/enforcement20260802a.htm</link>
        <pubDate>Sun, 02 Aug 2026 14:00:00 GMT</pubDate></item>
        <item><title>Federal Reserve Board requests comment on liquidity requirements</title>
        <link>https://www.federalreserve.gov/newsevents/pressreleases/bcreg20260803a.htm</link>
        <pubDate>Mon, 03 Aug 2026 14:00:00 GMT</pubDate></item>
        <item><title>Federal Reserve issues FOMC statement</title>
        <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260804a.htm</link>
        <pubDate>Tue, 04 Aug 2026 18:00:00 GMT</pubDate></item>
        </channel></rss>"""

        with mock.patch.object(macro_fetcher, "http_get", return_value=rss):
            items = macro_fetcher.fetch_fed()

        self.assertEqual(
            [item["title"] for item in items],
            [
                "Federal Reserve Board requests comment on liquidity requirements",
                "Federal Reserve issues FOMC statement",
            ],
        )


if __name__ == "__main__":
    unittest.main()
