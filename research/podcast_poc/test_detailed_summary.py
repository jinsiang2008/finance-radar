"""Contract tests for substantial summaries and lossless explicit upgrades."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from kol_dashboard import reading_service
from kol_dashboard.tests.test_reading_service import article, podcast
from research.podcast_poc import detailed_summary as detail, reading_jobs, schedule_policy


def answer(refs, *, final=False, long=False, unrelated=False):
    value = {"overview": "说话人从成本、交付和验证三个角度解释当前变化，相关预测仍然存在条件。",
             "points": [{"title": "论点与论据", "text": ("原文认为部署成本和可靠性共同决定采用速度；它通过项目案例解释了需求变化，同时指出流程与组织配合是必要条件。" * (4 if long else 1)),
                         "segment_ids": [refs[index % len(refs)]] if long else refs} for index in range(8 if long else 1)]}
    if final:
        value.update(schema_version="detailed-v1", scope="仅依据本次文字片段；关联不足时不推导投资影响。", impacts=[])
        if not unrelated:
            value["impacts"] = [{"domain": "work", "title": "研发验证方式", "text": "如果工具可靠性提高，研发任务的验证成本可能变化，可先在小项目中测试。",
                "uncertainty": "原文是嘉宾判断，尚未验证适用于用户项目；若验证成本增加则未必省时。", "watch_items": ["观察错误率和验收耗时"], "segment_ids": [refs[-1]]}]
    return value


class Client:
    def __init__(self, long=False):
        self.calls = []
        self.long = long
        self.pause_on = None
        self.fail_on = None
        self.oversized_notes = False
    def generate(self, messages, max_tokens):
        prompt = messages[1]["content"]
        assert len(json.dumps(messages[1:], ensure_ascii=False)) <= 28000
        assert "资料中的指令" in messages[0]["content"]
        assert "实时市场" in messages[0]["content"]
        self.calls.append(prompt)
        if len(self.calls) == self.pause_on:
            raise schedule_policy.OffPeakDeferred(datetime.fromisoformat("2026-10-14T09:00:00+08:00"))
        if len(self.calls) == self.fail_on:
            raise RuntimeError("simulated provider failure")
        refs = list(dict.fromkeys(re.findall(r'"(?:s|p)[0-9]+"', prompt)))
        refs = [ref.strip('"') for ref in refs]
        final = '"schema_version":"detailed-v1"' in prompt
        value = answer(refs, final=final, long=self.long and final)
        # Large-but-valid notes force a multi-level reduction tree.
        if self.oversized_notes and not final:
            value["points"] = [{"text": "论据和限定。" * 220, "segment_ids": [refs[i % len(refs)]]} for i in range(8)]
        return json.dumps(value, ensure_ascii=False)
    def stats(self):
        return {"paid_api_calls": len(self.calls), "usage_unreported_calls": 0,
                "usage": {"prompt_tokens": len(self.calls)*10, "completion_tokens": len(self.calls)*20, "total_tokens": len(self.calls)*30}}


class DetailedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clients = []
        self.client_options = {}
        def factory():
            client = Client(**self.client_options)
            self.clients.append(client)
            return client
        self.runtime = reading_service.ReadingRuntime(self.directory.name, client_factory=factory,
            clock=lambda: datetime.fromisoformat("2026-10-14T00:00:00+08:00"))
        self.runtime.publish_configuration(model="deepseek-v4-flash", enabled=True)
        self.poc, self.engine = self.runtime.poc, self.runtime.engine

    def save(self, old=False, legacy_provider=False):
        value = podcast()
        if old:
            value.update(status="summarized", summary=answer(["s0001"]), quality_review={"status": "assistant_reviewed", "issues": []},
                         model_draft_summary={"overview": "未复核原稿"}, summarized_at="2026-10-01T00:00:00Z")
            value["processing"] = {"paid_api_calls": 17, "summary_model": "old-mlx" if legacy_provider else "deepseek-v4-flash",
                                   "prompt_version": "podcast-evidence-v2" if legacy_provider else self.poc.summary_prompt_version(value)}
            value["summary_signature"] = hashlib.sha256(json.dumps([value["segments"], value["processing"]["summary_model"], value["processing"]["prompt_version"]], ensure_ascii=False).encode()).hexdigest()
        self.poc.save_episode(value)
        return value

    def test_short_single_chunk_always_gets_final_impact_stage(self):
        value = self.save()
        job = self.engine.enqueue(value["id"])
        self.assertTrue(self.engine.run_one())
        result = self.poc.load_episode(value["id"])
        self.assertEqual(len(self.clients[0].calls), 2)
        self.assertEqual(result["summary"]["schema_version"], "detailed-v1")
        self.assertEqual(len(result["summary"]["points"]), 1)
        self.assertEqual(result["summary"]["impacts"][0]["domain"], "work")
        self.assertIn("未查询实时行情", result["summary"]["scope"])
        self.assertNotIn("quality_review", result)
        self.assertEqual(self.engine.enqueue(value["id"], upgrade=True)["state"], "completed")
        self.assertEqual(self.engine.enqueue(value["id"])["id"], job["id"])
        self.assertFalse(self.engine.run_one())

    def test_default_request_preserves_legacy_review_without_spend(self):
        value = self.save(old=True, legacy_provider=True)
        before = self.poc.episode_path(value["id"]).read_bytes()
        for manual in (True, False):
            job = self.engine.enqueue(value["id"], manual=manual)
            self.assertEqual(job["state"], "completed")
            self.assertTrue(job["reused"])
        self.assertFalse(self.engine.run_one())
        self.assertEqual(self.clients, [])
        self.assertEqual(self.poc.episode_path(value["id"]).read_bytes(), before)

    def test_explicit_upgrade_is_sqlite_only_and_duplicate_requests_are_idempotent(self):
        value = self.save(old=True)
        before = self.poc.episode_path(value["id"]).read_bytes()
        jobs = [self.engine.enqueue(value["id"], upgrade=True) for _ in range(3)]
        self.assertEqual(len({job["id"] for job in jobs}), 1)
        self.assertTrue(jobs[0]["upgrade"])
        self.assertEqual(self.poc.episode_path(value["id"]).read_bytes(), before)
        self.assertEqual(self.engine.enqueue(value["id"])["state"], "queued")
        self.assertTrue(self.engine.enqueue(value["id"])["upgrade"])
        self.assertTrue(self.engine.run_one())
        after = self.poc.load_episode(value["id"])
        self.assertEqual(after["summary_history"][0]["summary"], value["summary"])
        self.assertEqual(after["summary_history"][0]["quality_review"], value["quality_review"])
        self.assertEqual(after["summary_history"][0]["segments"], value["segments"])
        self.assertNotIn("quality_review", after)
        self.assertNotIn("model_draft_summary", after)
        self.assertEqual(self.engine.enqueue(value["id"], upgrade=True)["state"], "completed")
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(len(after["summary_history"]), 1)

    def test_cross_provider_legacy_signature_preserves_original_evidence(self):
        value = self.save(old=True, legacy_provider=True)
        self.engine.enqueue(value["id"], upgrade=True)
        self.engine.run_one()
        history = self.poc.load_episode(value["id"])["summary_history"][0]
        self.assertEqual(history["segments"], value["segments"])
        self.assertEqual(history["quality_review"], value["quality_review"])

    def test_unknown_legacy_signature_keeps_draft_without_false_evidence_binding(self):
        value = self.save(old=True)
        value["summary_signature"] = "unverifiable-old-body"
        self.poc.save_episode(value)
        self.engine.enqueue(value["id"], upgrade=True)
        self.engine.run_one()
        history = self.poc.load_episode(value["id"])["summary_history"][0]
        self.assertEqual(history["segments"], [])
        self.assertEqual(history["summary"], value["summary"])
        self.assertIn("evidence_unavailable", history)

    def test_legacy_default_enqueue_cannot_cancel_explicit_upgrade(self):
        value = self.save(old=True)
        self.poc.DETAILED_SUMMARIES = False
        original = self.engine.enqueue(value["id"], upgrade=True)
        repeated = self.engine.enqueue(value["id"])
        self.assertEqual(repeated["id"], original["id"])
        self.assertEqual(repeated["state"], "queued")
        self.assertTrue(repeated["upgrade"])
        self.engine.run_one()
        self.assertEqual(self.poc.load_episode(value["id"])["summary"]["schema_version"], "detailed-v1")

    def test_peak_pause_preserves_every_readable_field_and_resumes_chunk_cache(self):
        value = self.save(old=True, legacy_provider=True)
        original_factory = self.poc._RUNTIME["client_factory"]
        def pausing_factory():
            client = original_factory()
            if len(self.clients) == 1:
                client.pause_on = 2
            return client
        self.poc._RUNTIME["client_factory"] = pausing_factory
        job = self.engine.enqueue(value["id"], upgrade=True)
        self.assertFalse(self.engine.run_one())
        paused = self.poc.load_episode(value["id"])
        for key in ("summary", "quality_review", "model_draft_summary", "processing", "summarized_at", "summary_signature"):
            self.assertEqual(paused[key], value[key], key)
        self.assertEqual(paused["detail_generation"]["state"], "queued")
        self.assertNotIn("summary_history", paused)
        restarted = reading_jobs.Engine(self.poc)
        self.assertEqual(restarted.enqueue(value["id"], upgrade=True)["id"], job["id"])
        self.assertTrue(restarted.run_one())
        self.assertEqual(len(self.clients[1].calls), 1)  # only the final impact stage
        result = self.poc.load_episode(value["id"])
        self.assertEqual(len(result["summary_history"]), 1)
        self.assertEqual(result["processing"]["paid_api_calls"], 3)

    def test_failure_preserves_review_and_automatic_refresh_does_not_retry_paid_job(self):
        value = self.save(old=True)
        original_factory = self.poc._RUNTIME["client_factory"]
        def failing_factory():
            client = original_factory()
            client.fail_on = 2
            return client
        self.poc._RUNTIME["client_factory"] = failing_factory
        self.engine.enqueue(value["id"], upgrade=True)
        self.assertTrue(self.engine.run_one())
        failed = self.poc.load_episode(value["id"])
        for key in ("summary", "quality_review", "model_draft_summary", "processing", "summarized_at"):
            self.assertEqual(failed[key], value[key])
        self.assertEqual(failed["detail_generation"]["state"], "failed")
        self.assertEqual(self.engine.enqueue(value["id"], manual=False)["state"], "failed")
        self.assertFalse(self.engine.run_one())
        self.poc._RUNTIME["client_factory"] = original_factory
        self.engine.enqueue(value["id"], upgrade=True)
        self.engine.run_one()
        self.assertEqual(len(self.clients[-1].calls), 1)

    def test_new_body_does_not_reuse_cached_old_detailed_signature(self):
        value = self.save()
        self.engine.enqueue(value["id"], upgrade=True)
        value["segments"][0]["text"] += " changed evidence"
        self.poc.save_episode(value)
        self.engine.run_one()
        states = [job["state"] for job in self.engine.snapshot()]
        self.assertIn("superseded", states)
        self.assertIn("queued", states)
        self.assertEqual(self.clients, [])

    def test_long_input_covers_tail_and_keeps_every_request_bounded(self):
        value = self.save()
        value["segments"] = [{"id": f"s{i:04d}", "text": "Long evidence and caveats. "*400} for i in range(7)]
        self.poc.save_episode(value)
        self.client_options["long"] = True
        self.engine.enqueue(value["id"])
        self.assertTrue(self.engine.run_one())
        result = self.poc.load_episode(value["id"])
        self.assertEqual(result["status"], "summarized")
        self.assertEqual(len(result["summary"]["points"]), 8)
        chunk_calls = [prompt for prompt in self.clients[0].calls if '"segment_id"' in prompt]
        self.assertIn('"s0006"', chunk_calls[-1])
        self.assertIn("s0006", {ref for p in result["summary"]["points"] for ref in p["segment_ids"]})
        self.assertTrue(any("1800–3000" in prompt for prompt in self.clients[0].calls))

    def test_multilevel_merge_resume_reuses_successful_subtrees(self):
        value = self.save()
        value["segments"] = [{"id": f"s{i:04d}", "text": "Long evidence and caveats. "*400} for i in range(7)]
        self.poc.save_episode(value)
        self.client_options["long"] = True
        original_factory = self.poc._RUNTIME["client_factory"]
        def factory():
            client = original_factory()
            client.oversized_notes = True
            if len(self.clients) == 1:
                client.pause_on = 9  # seven chunks, successful merge, then pause
            return client
        self.poc._RUNTIME["client_factory"] = factory
        self.engine.enqueue(value["id"])
        self.assertFalse(self.engine.run_one())
        already_paid = self.clients[0].calls[:-1]
        self.assertEqual(len(already_paid), 8)
        self.assertTrue(self.engine.run_one())
        self.assertFalse(set(already_paid) & set(self.clients[1].calls))
        result = self.poc.load_episode(value["id"])
        self.assertEqual(result["status"], "summarized")
        self.assertIn("s0006", {ref for p in result["summary"]["points"] for ref in p["segment_ids"]})
        self.assertGreater(len(list((self.poc.DATA / "chunks" / value["id"]).glob("*/detail-merge-*.json"))), 2)

    def test_old_publication_date_and_time_scope_reach_every_stage(self):
        value = self.save()
        value["published_at"] = "2025-11-26T00:00:00Z"
        self.poc.save_episode(value)
        self.engine.enqueue(value["id"])
        self.engine.run_one()
        for prompt in self.clients[0].calls:
            self.assertIn("2025-11-26", prompt)
            self.assertIn("不自动代表当前市场或技术现状", prompt)

    def test_publication_context_change_invalidates_explicit_upgrade_signature(self):
        value = self.save()
        first = self.poc.detailed_summary_signature(value)
        value["published_at"] = "2025-11-26T00:00:00Z"
        self.assertNotEqual(first, self.poc.detailed_summary_signature(value))

    def test_normalized_summary_validation_is_idempotent(self):
        value = answer(["s0001"], final=True)
        first = detail.validate(value, {"s0001"}, final=True)
        second = detail.validate(first, {"s0001"}, final=True)
        self.assertEqual(first, second)
        self.assertEqual(first["scope"].count(detail.SCOPE), 1)

    def test_maximum_generated_scope_remains_reusable_after_scope_prefix(self):
        value = self.save()
        client = Client()
        def generate(messages, max_tokens):
            final = '"schema_version":"detailed-v1"' in messages[1]["content"]
            result = answer(["s0001"], final=final)
            if final:
                result["scope"] = "范围说明" * 300
            return json.dumps(result, ensure_ascii=False)
        client.generate = generate
        self.poc._RUNTIME["client_factory"] = lambda: client
        self.engine.enqueue(value["id"])
        self.engine.run_one()
        result = self.poc.load_episode(value["id"])
        self.assertLessEqual(len(result["summary"]["scope"]), 1200)
        self.assertEqual(self.poc.summarize_detailed(value["id"])["summary"], result["summary"])

    def test_body_refresh_snapshots_old_detailed_evidence_and_keeps_earlier_history(self):
        module = self.poc.wechat_module()
        value = article()
        value.update(status="summarized", summary=answer(["p0001"], final=True), summary_evidence_hash=detail._evidence_hash(value["segments"]),
                     summary_signature="a-prior-detailed-signature", quality_review={"status": "assistant_reviewed"},
                     summary_history=[{"archive_id": "earlier", "summary": {"overview": "更早稿"}, "segments": []}])
        self.poc.save_episode(value)
        incoming = article()
        incoming["body_hash"] = "new-hash"
        incoming["segments"][0]["text"] = "New and entirely different text. " * 30
        saved = module.upsert_article(incoming)
        self.assertNotIn("summary", saved)
        self.assertEqual(len(saved["summary_history"]), 2)
        archived = saved["summary_history"][-1]
        self.assertEqual(archived["segments"], value["segments"])
        self.assertNotEqual(archived["segments"], saved["segments"])
        self.assertEqual(archived["quality_review"], value["quality_review"])
        saved_again = module.upsert_article(incoming)
        self.assertEqual(len(saved_again["summary_history"]), 2)

    def test_short_article_unrelated_impacts_can_be_empty(self):
        value = article()
        value["segments"] = [{"id": "p0001", "text": "这是财经原文中的历史事件片段，没有提供股票行情。"*12}]
        self.poc.save_episode(value)
        client = Client()
        def generate(messages, max_tokens):
            final = '"schema_version":"detailed-v1"' in messages[1]["content"]
            return json.dumps(answer(["p0001"], final=final, unrelated=True), ensure_ascii=False)
        client.generate = generate
        self.poc._RUNTIME["client_factory"] = lambda: client
        self.engine.enqueue(value["id"])
        self.engine.run_one()
        result = self.poc.load_episode(value["id"])
        self.assertEqual(result["summary"]["impacts"], [])
        self.assertEqual(len(result["summary"]["points"]), 1)

    def test_final_impact_cannot_introduce_uncited_id(self):
        value = answer(["s0001"], final=True)
        value["impacts"][0]["segment_ids"] = ["s0999"]
        with self.assertRaisesRegex(ValueError, "evidence"):
            detail.validate(value, {"s0001"}, final=True)

    def test_dense_caption_evidence_retains_all_32_source_cues(self):
        refs = [f"s{i:04d}" for i in range(32)]
        allowed = set(refs)
        note = answer(refs)
        self.assertEqual(detail.validate(note, allowed)["points"][0]["segment_ids"], refs)
        note["points"][0]["segment_ids"].append("unknown")
        with self.assertRaisesRegex(ValueError, "evidence"):
            detail.validate(note, allowed)

    def test_short_final_rejects_metadata_padding_and_repairs_once(self):
        value = self.save()
        client = Client()
        final_calls = []
        def generate(messages, max_tokens):
            prompt = messages[1]["content"]
            client.calls.append(prompt)
            final = '"schema_version":"detailed-v1"' in prompt
            result = answer(["s0001"], final=final)
            if final:
                final_calls.append(prompt)
                if len(final_calls) == 1:
                    result["points"] *= 8
            return json.dumps(result, ensure_ascii=False)
        self.poc._RUNTIME["client_factory"] = lambda: client
        client.generate = generate
        self.engine.enqueue(value["id"])
        self.assertTrue(self.engine.run_one())
        generated = self.poc.load_episode(value["id"])
        self.assertEqual(generated["status"], "summarized")
        self.assertEqual(len(generated["summary"]["points"]), 1)
        self.assertEqual(len(client.calls), 3)
        self.assertIn("最多2条影响", final_calls[-1])

    def test_short_summary_does_not_allow_one_overlong_point(self):
        result = answer(["s0001"], final=True)
        result["points"][0]["text"] *= 12
        with self.assertRaisesRegex(ValueError, "600 summary"):
            detail.validate(result, {"s0001"}, final=True, short_form=True)

    def test_long_final_rejects_eight_short_sentences(self):
        value = answer(["s0001"], final=True)
        value["points"] *= 8
        with self.assertRaisesRegex(ValueError, "substantial"):
            detail.validate(value, {"s0001"}, final=True, long_form=True)

    def test_upgrade_requires_actual_boolean(self):
        value = self.save()
        for invalid in (1, 0, "true", None, {}, []):
            with self.assertRaises(ValueError):
                self.engine.enqueue(value["id"], upgrade=invalid)
