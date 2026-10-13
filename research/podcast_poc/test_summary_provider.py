import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from research.podcast_poc import poc, inference, wechat
from research.podcast_poc.test_wechat import SOURCE, make_item


class SummaryProviderTests(unittest.TestCase):
    def test_resume_reuses_completed_merges_as_well_as_chunks(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(poc, "DATA", Path(directory)):
            poc.write_json(poc.DATA / "runtime/automation.json", {"provider": "deepseek"})
            article = wechat.article_from_item(make_item(), SOURCE)
            article["segments"] = [{"id": f"p{i:04d}", "text": "证" * 11_000} for i in range(5)]
            poc.save_episode(article)
            successful, attempted = [], []
            pause = True
            def answer(messages, max_tokens):
                nonlocal pause
                prompt = messages[1]["content"]
                attempted.append(prompt)
                refs = re.findall(r'"segment_id": "(p[0-9]+)"', prompt)
                if refs:
                    successful.append(prompt)
                    return json.dumps({"overview": "摘" * 5000, "points": [{"text": "要" * 5000, "segment_ids": refs}]})
                if pause and len(successful) == 6:
                    pause = False
                    raise RuntimeError("pause before the second merge")
                successful.append(prompt)
                refs = sorted(set(re.findall(r'"(p[0-9]+)"', prompt)))
                return json.dumps({"overview": "合并摘要", "points": [{"text": "跨段证据", "segment_ids": refs}]})
            with patch.object(inference, "Client") as client:
                client.return_value.generate.side_effect = answer
                client.return_value.stats.return_value = {"paid_api_calls": 0}
                with self.assertRaisesRegex(RuntimeError, "pause"):
                    poc.summarize(article["id"])
                already_completed = list(successful)
                result = poc.summarize(article["id"])
            self.assertEqual(len(already_completed), 6)
            for prompt in already_completed:
                self.assertEqual(attempted.count(prompt), 1)
            self.assertEqual(result["summary"]["points"][0]["segment_ids"], [f"p{i:04d}" for i in range(5)])

    def test_third_party_transcription_constraints_reach_model(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(poc, "DATA", Path(directory)):
            poc.write_json(poc.DATA / "runtime/automation.json", {"provider": "deepseek"})
            article = wechat.article_from_item(make_item(), SOURCE)
            article.update(content_kind="podcast", source_kind="third_party_transcript")
            article["coverage"]["scope"] = "partial"
            article["segments"][0]["source_notes"] = ["说话人标签待核验"]
            poc.save_episode(article)
            def answer(messages, max_tokens):
                self.assertIn("第三方自动转写", messages[1]["content"])
                self.assertIn("说话人标签待核验", messages[1]["content"])
                return json.dumps({"overview": "第三方稿摘要", "points": [{"text": "保留出处", "segment_ids": [article["segments"][0]["id"]]}]})
            with patch.object(inference, "Client") as client:
                client.return_value.generate.side_effect = answer
                client.return_value.stats.return_value = {"paid_api_calls": 0}
                result = poc.summarize(article["id"])
            self.assertEqual(result["coverage"]["scope"], "partial")
            self.assertIn("third-party-partial", result["processing"]["prompt_version"])

    def test_short_paragraph_overhead_is_split_before_the_shared_provider_limit(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(poc, "DATA", Path(directory)):
            poc.write_json(poc.DATA / "runtime/automation.json", {"provider": "deepseek"})
            article = wechat.article_from_item(make_item(), SOURCE)
            article["segments"] = [{"id": f"p{i:04d}", "text": "正文。" * 10} for i in range(350)]
            poc.save_episode(article)
            seen, prompts = set(), []
            def answer(messages, max_tokens):
                serialized = json.dumps(messages[1:], ensure_ascii=False)
                self.assertLessEqual(len(serialized), 32_000)
                prompts.append(serialized)
                refs = re.findall(r'"segment_id": "(p[0-9]+)"', messages[1]["content"])
                seen.update(refs)
                refs = refs[:1] or ["p0000"]
                return json.dumps({"overview": "分段摘要", "points": [{"text": "保留原始证据", "segment_ids": refs}]})
            with patch.object(inference, "Client") as client:
                client.return_value.generate.side_effect = answer
                client.return_value.stats.return_value = {"paid_api_calls": 0}
                result = poc.summarize(article["id"])
            self.assertEqual(result["status"], "summarized")
            self.assertEqual(seen, {s["id"] for s in article["segments"]})
            self.assertGreater(result["coverage"]["chunks_total"], 1)
            self.assertGreater(len(prompts), 2)

    def test_invalid_large_answer_does_not_expand_the_retry_request(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(poc, "DATA", Path(directory)):
            poc.write_json(poc.DATA / "runtime/automation.json", {"provider": "deepseek"})
            article = wechat.article_from_item(make_item(), SOURCE)
            poc.save_episode(article)
            calls = []
            def answer(messages, max_tokens):
                calls.append(messages)
                if len(calls) == 1:
                    return "not-json" * 10_000
                self.assertLessEqual(len(json.dumps(messages[1:], ensure_ascii=False)), 32_000)
                self.assertIn(article["segments"][0]["text"], messages[1]["content"])
                return json.dumps({"overview": "重试摘要", "points": [{"text": "保留原始证据", "segment_ids": [article["segments"][0]["id"]]}]})
            with patch.object(inference, "Client") as client:
                client.return_value.generate.side_effect = answer
                client.return_value.stats.return_value = {"paid_api_calls": 0}
                result = poc.summarize(article["id"])
            self.assertEqual(result["status"], "summarized")
            self.assertEqual(len(calls), 2)

    def test_long_document_merge_is_bounded_and_keeps_each_input_group(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(poc, "DATA", Path(directory)):
            poc.write_json(poc.DATA / "runtime/automation.json", {"provider": "deepseek"})
            article = wechat.article_from_item(make_item(), SOURCE)
            article["segments"] = [{"id": f"p{i:04d}", "text": "证" * 11_000} for i in range(5)]
            poc.save_episode(article)
            merges = []
            def answer(messages, max_tokens):
                self.assertLessEqual(len(json.dumps(messages[1:], ensure_ascii=False)), 32_000)
                prompt = messages[1]["content"]
                refs = re.findall(r'"segment_id": "(p[0-9]+)"', prompt)
                if refs:
                    return json.dumps({"overview": "摘" * 5000, "points": [{"text": "要" * 5000, "segment_ids": refs}]})
                refs = sorted(set(re.findall(r'"(p[0-9]+)"', prompt)))
                merges.append(refs)
                return json.dumps({"overview": "合并摘要", "points": [{"text": "跨段证据", "segment_ids": refs}]})
            with patch.object(inference, "Client") as client:
                client.return_value.generate.side_effect = answer
                client.return_value.stats.return_value = {"paid_api_calls": 0}
                result = poc.summarize(article["id"])
            self.assertGreater(len(merges), 1)
            self.assertEqual(result["summary"]["points"][0]["segment_ids"], [f"p{i:04d}" for i in range(5)])

    def test_deepseek_evidence_and_cache_without_mlx(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(poc, "DATA", Path(directory)):
            poc.write_json(poc.DATA / "runtime/automation.json", {"provider": "deepseek"})
            article = wechat.article_from_item(make_item(), SOURCE)
            poc.save_episode(article)
            summary = {"overview": "测试摘要", "points": [{"text": "需要独立检验", "segment_ids": [article["segments"][0]["id"]]}]}
            with patch.object(inference, "Client") as client, patch.object(poc, "setup_local_models", side_effect=AssertionError("No MLX")):
                client.return_value.generate.return_value = json.dumps(summary)
                client.return_value.stats.return_value = {"paid_api_calls": 1, "usage": {"total_tokens": 100}}
                result = poc.summarize(article["id"])
                self.assertEqual(result["processing"]["inference"], "deepseek")
                self.assertEqual(result["processing"]["paid_api_calls"], 1)
                self.assertEqual(result["summary"], summary)
                self.assertNotIn("quality_review", result)
                self.assertEqual(poc.summarize(article["id"])["summary_signature"], result["summary_signature"])
                self.assertEqual(client.return_value.generate.call_count, 1)

    def test_failed_attempt_usage_is_not_reported_as_free(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(poc, "DATA", Path(directory)):
            poc.write_json(poc.DATA / "runtime/automation.json", {"provider": "deepseek"})
            article = wechat.article_from_item(make_item(), SOURCE)
            poc.save_episode(article)
            with patch.object(inference, "Client") as client:
                client.return_value.generate.side_effect = inference.DeepSeekError("timeout")
                client.return_value.stats.return_value = {"paid_api_calls": 1, "usage_unreported_calls": 1}
                with self.assertRaises(inference.DeepSeekError):
                    poc.summarize(article["id"])
                result = poc.load_episode(article["id"])
                self.assertEqual(result["processing"]["paid_api_calls"], 1)
                self.assertEqual(result["processing"]["usage_unreported_calls"], 1)
                client.return_value.generate.side_effect = None
                client.return_value.generate.return_value = json.dumps({"overview": "重试摘要", "points": [
                    {"text": "需要独立检验", "segment_ids": [article["segments"][0]["id"]]}]})
                client.return_value.stats.return_value = {"paid_api_calls": 1, "usage": {"total_tokens": 100}}
                completed = poc.summarize(article["id"])
                self.assertEqual(completed["processing"]["paid_api_calls"], 2)
                self.assertEqual(completed["processing"]["usage_unreported_calls"], 1)
                self.assertEqual(completed["processing"]["usage"]["total_tokens"], 100)
