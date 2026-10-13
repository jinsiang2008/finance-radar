"""Offline coverage and data-retention checks for third-party article feeds."""
import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from research.podcast_poc import poc, schedule_policy, wechat


SOURCE = {
    "id": "test-account", "name": "测试公众号", "category": "AI",
    "provider": "测试 RSS", "feed_url": "https://example.org/test-feed.xml",
}
BODY = "<p>模型在真实任务中的表现需要独立检验，测试结果不能替代完整业务评估。</p>" * 12


def make_item(*, url="https://mp.weixin.qq.com/s/test-article", body=BODY,
              description="来源简介", published="Sat, 03 Oct 2026 09:00:00 +0800", title="测试文章"):
    item = ET.Element("item")
    for name, value in (("link", url), ("title", title), ("description", description), ("pubDate", published)):
        if value is not None:
            ET.SubElement(item, name).text = value
    if body is not None:
        ET.SubElement(item, wechat.BODY_TAG).text = body
    return item


def rss(*items):
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    channel.extend(items)
    return ET.tostring(root, encoding="utf-8")


class WechatCoverageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_patch = patch.object(poc, "DATA", Path(self.directory.name))
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)

    def reviewed_article(self):
        article = wechat.article_from_item(make_item(), SOURCE)
        article.update({
            "status": "summarized", "summary_signature": "source-model-prompt-signature",
            "summary": {"overview": "对照文字后的复核摘要", "points": []},
            "model_draft_summary": {"overview": "未经复核的模型草稿", "points": []},
            "quality_review": {"status": "assistant_reviewed", "issues": ["原稿范围过大"]},
        })
        article["coverage"]["segments_processed"] = len(article["segments"])
        poc.save_episode(article)
        return article

    def test_long_description_without_content_is_only_metadata(self):
        article = wechat.article_from_item(make_item(body=None, description="非常长的文章简介。" * 300), SOURCE)
        self.assertEqual(article["source_kind"], "rss_metadata")
        self.assertEqual(article["status"], "metadata_only")
        self.assertEqual(article["coverage"]["scope"], "metadata_only")
        self.assertEqual(article["segments"], [])
        self.assertNotIn("summary", article)
        self.assertTrue(article["source_excerpt"])

    def test_body_removes_active_html_but_preserves_nested_inline_text(self):
        html = ('<p>模型<strong>增长<span>率</span></strong>为<strong>10%</strong>。<br>第二行。</p>'
                '<script>EXECUTE_ME()</script><style>HIDDEN_STYLE</style><iframe>FRAME_TEXT</iframe>'
                '<noscript>NO_SCRIPT_TEXT</noscript><svg><text>SVG_TEXT</text></svg>'
                '<img src="https://example.org/never-fetched.png" onerror="EXECUTE_ME()">')
        with patch.object(poc, "fetch", side_effect=AssertionError("HTML parsing must not fetch images")):
            lines, images = wechat.body_text(html)
        self.assertEqual(lines, ["模型增长率为10%。", "第二行。"])
        self.assertEqual(images, 1)
        self.assertFalse(any(token in "\n".join(lines) for token in ("EXECUTE_ME", "HIDDEN_STYLE", "FRAME_TEXT", "SVG_TEXT")))

    def test_table_cells_keep_numeric_boundaries(self):
        lines, _ = wechat.body_text('<table><tr><th>收入</th><th>成本</th></tr><tr><td>1</td><td><b>23</b></td></tr></table>')
        text = "\n".join(lines)
        self.assertIn("收入", text)
        self.assertIn("成本", text)
        self.assertRegex(text, r"\b1\s*\|\s*23\b")
        self.assertNotRegex(text, r"\b123\b")

    def test_wechat_tracking_links_resolve_to_one_article_identity(self):
        first = 'http://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1&sn=def&scene=2&from=timeline#wechat_redirect'
        second = 'https://mp.weixin.qq.com/s?sn=def&idx=1&mid=123&__biz=abc&scene=1&isappinstalled=0'
        a = wechat.article_from_item(make_item(url=first), SOURCE)
        b = wechat.article_from_item(make_item(url=second), SOURCE)
        self.assertEqual(a["canonical_url"], b["canonical_url"])
        self.assertEqual(a["id"], b["id"])
        wechat.upsert_article(a)
        wechat.upsert_article(b)
        self.assertEqual(len(list((poc.DATA / "articles").glob("*.json"))), 1)
        different = wechat.article_from_item(make_item(url=second.replace("mid=123", "mid=124")), SOURCE)
        self.assertNotEqual(a["id"], different["id"])

    def test_unchanged_body_keeps_summary_review_and_draft(self):
        original = self.reviewed_article()
        incoming = wechat.article_from_item(make_item(description="更新后的来源简介"), SOURCE)
        incoming["fetched_at"] = "2026-10-03T10:00:00+00:00"
        saved = wechat.upsert_article(incoming)
        self.assertEqual(saved["status"], "summarized")
        for key in ("summary", "quality_review", "model_draft_summary", "summary_signature"):
            self.assertEqual(saved[key], original[key])
        self.assertEqual(saved["fetched_at"], incoming["fetched_at"])
        self.assertEqual(saved["source_excerpt"], "更新后的来源简介")
        self.assertEqual(saved["coverage"]["segments_processed"], len(saved["segments"]))

    def test_changed_body_invalidates_previous_summary_and_review(self):
        original = self.reviewed_article()
        incoming = wechat.article_from_item(make_item(body=BODY + "<p>新增材料改变了前面的结论，不能沿用旧摘要。</p>"), SOURCE)
        saved = wechat.upsert_article(incoming)
        self.assertEqual(saved["id"], original["id"])
        self.assertEqual(saved["status"], "body_ready")
        self.assertNotEqual(saved["body_hash"], original["body_hash"])
        for key in ("summary", "quality_review", "model_draft_summary", "summary_signature"):
            self.assertNotIn(key, saved)
        self.assertEqual(saved["coverage"]["segments_processed"], 0)

    def test_metadata_downgrade_preserves_archived_body_review_and_body_timestamp(self):
        original = self.reviewed_article()
        incoming = wechat.article_from_item(make_item(body=None), SOURCE)
        incoming["fetched_at"] = "2026-10-08T10:00:00+00:00"
        saved = wechat.upsert_article(incoming)
        for key in ("segments", "summary", "quality_review", "body_hash", "fetched_at", "summary_signature"):
            self.assertEqual(saved[key], original[key])
        self.assertFalse(saved["source_body_available"])
        self.assertEqual(saved["source_checked_at"], incoming["fetched_at"])
        self.assertIn("上次保存", saved["source_body_note"])
        state = wechat.ingest_feed(SOURCE, rss(make_item(body=None)))
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["body_count"], 0)
        restored = wechat.upsert_article(wechat.article_from_item(make_item(), SOURCE))
        self.assertTrue(restored["source_body_available"])
        self.assertNotIn("source_body_note", restored)
        self.assertEqual(restored["quality_review"], original["quality_review"])

    def test_feed_order_does_not_override_latest_publication_selection(self):
        old = make_item(url="https://mp.weixin.qq.com/s/old", published="Thu, 01 Oct 2026 09:00:00 +0800", title="旧文章")
        unknown = make_item(url="https://mp.weixin.qq.com/s/no-date", published=None, title="无日期文章")
        latest = make_item(url="https://mp.weixin.qq.com/s/new", published="Sat, 03 Oct 2026 09:00:00 +0800", title="最新文章")
        state = wechat.ingest_feed(SOURCE, rss(old, unknown, latest), per_source=1)
        stored = [json.loads(path.read_text()) for path in (poc.DATA / "articles").glob("*.json")]
        self.assertEqual([a["title"] for a in stored], ["最新文章"])
        self.assertEqual(state["latest_published_at"], "2026-10-03T01:00:00+00:00")
        self.assertEqual(state["feed_item_count"], 3)

    def test_failed_refresh_keeps_existing_article_and_last_success(self):
        article = self.reviewed_article()
        before = poc.episode_path(article["id"]).read_bytes()
        last_success = "2026-10-03T01:30:00+00:00"
        poc.write_json(poc.DATA / "wechat-sources.json", {"sources": [{**SOURCE, "status": "ok", "last_success_at": last_success, "article_count": 1}]})
        with patch.object(wechat, "source_config", return_value=[SOURCE]), patch.object(poc, "fetch", side_effect=TimeoutError("test timeout")), contextlib.redirect_stdout(io.StringIO()):
            states = wechat.refresh()
        self.assertEqual(states[0]["status"], "failed")
        self.assertIn("test timeout", states[0]["error"])
        self.assertEqual(states[0]["last_success_at"], last_success)
        self.assertEqual(poc.episode_path(article["id"]).read_bytes(), before)
        self.assertEqual(len(wechat.payload()["articles"]), 1)

    def test_off_peak_pause_propagates_without_overwriting_source_health(self):
        previous = {"sources": [{**SOURCE, "status": "ok", "last_success_at": "2026-10-13T00:00:00+00:00"}]}
        state = poc.DATA / "wechat-sources.json"
        poc.write_json(state, previous)
        before = state.read_bytes()
        pause = schedule_policy.OffPeakDeferred(datetime.fromisoformat("2026-10-13T09:00:00+08:00"))
        with patch.object(wechat, "source_config", return_value=[SOURCE]), patch.object(poc, "fetch", side_effect=pause):
            with self.assertRaises(schedule_policy.OffPeakDeferred):
                wechat.refresh()
        self.assertEqual(state.read_bytes(), before)

    def test_podcast_discovery_uses_same_pause_exception_and_stops_remaining_sources(self):
        pause = schedule_policy.OffPeakDeferred(datetime.fromisoformat("2026-10-13T09:00:00+08:00"))
        with patch.object(poc, "fetch", side_effect=pause) as fetch:
            with self.assertRaises(schedule_policy.OffPeakDeferred):
                poc.discover(["sv101", "latepost"], limit=1)
        self.assertEqual(fetch.call_count, 1)

    def test_missing_invalid_or_timezone_free_dates_never_become_fetch_time(self):
        for published in (None, "not a date", "2026-10-03T09:00:00"):
            with self.subTest(published=published), patch.object(poc, "now", return_value="2026-10-03T12:34:56+00:00"):
                article = wechat.article_from_item(make_item(published=published), SOURCE)
            self.assertIsNone(article["published_at"])
            self.assertEqual(article["fetched_at"], "2026-10-03T12:34:56+00:00")
            self.assertEqual(article["published_at_raw"], published)

    def test_article_and_podcast_payloads_remain_separate(self):
        article = wechat.upsert_article(wechat.article_from_item(make_item(), SOURCE))
        podcast = {"id": "test-podcast", "status": "discovered", "show": "播客", "segments": []}
        poc.save_episode(podcast)
        with patch.object(wechat, "source_config", return_value=[SOURCE]):
            article_payload = wechat.payload()
        podcast_payload = poc.payload()
        self.assertEqual([a["id"] for a in article_payload["articles"]], [article["id"]])
        self.assertEqual([e["id"] for e in podcast_payload["episodes"]], [podcast["id"]])
        self.assertEqual(poc.episode_path(article["id"]).parent.name, "articles")
        self.assertEqual(poc.episode_path(podcast["id"]).parent.name, "episodes")


if __name__ == "__main__":
    unittest.main()
