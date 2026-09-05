from __future__ import annotations

import unittest

from kol_dashboard import briefing_topics


class TopicTaxonomyContractTests(unittest.TestCase):
    def test_labels_and_version_are_stable_and_non_empty(self) -> None:
        self.assertEqual(briefing_topics.TAXONOMY_VERSION, "daily-content-v1")
        self.assertEqual(
            briefing_topics.CATEGORY_LABELS["cloud_infra"],
            "云与基础设施",
        )
        self.assertEqual(briefing_topics.TAG_LABELS["open_source"], "开源")
        self.assertEqual(briefing_topics.TAG_LABELS["methodology"], "方法论")
        self.assertTrue(all(briefing_topics.CATEGORY_LABELS.values()))
        self.assertTrue(all(briefing_topics.TAG_LABELS.values()))
        self.assertEqual(
            len(set(briefing_topics.CATEGORY_LABELS.values())),
            len(briefing_topics.CATEGORY_LABELS),
        )
        self.assertEqual(
            len(set(briefing_topics.TAG_LABELS.values())),
            len(briefing_topics.TAG_LABELS),
        )

    def test_public_payload_has_exact_bounded_shape(self) -> None:
        payload = briefing_topics.topic_payload(
            "Why Rust language changes API design",
            "A practical guide and methodology for library authors.",
        )

        self.assertEqual(
            payload,
            {
                "content_category": "software_dev",
                "content_tags": ["rust", "methodology"],
                "taxonomy_version": briefing_topics.TAXONOMY_VERSION,
            },
        )

    def test_untrusted_assignments_fail_closed(self) -> None:
        invalid = (
            ("unknown", []),
            ("ai_ml", "llm"),
            ("ai_ml", ["llm", "llm"]),
            ("ai_ml", ["llm", "open_source", "research_paper"]),
            ("ai_ml", ["invented"]),
            ("ai_ml", [1]),
        )
        for category, tags in invalid:
            with self.subTest(category=category, tags=tags):
                with self.assertRaises(briefing_topics.TopicValidationError):
                    briefing_topics.validate_topic_assignment(category, tags)
        with self.assertRaises(briefing_topics.TopicValidationError):
            briefing_topics.validate_topic_assignment(
                "ai_ml",
                [],
                taxonomy_version="daily-content-v0",
            )

    def test_assignment_normalizes_key_casing_but_not_free_form_labels(self) -> None:
        result = briefing_topics.validate_topic_assignment(
            " AI_ML ",
            [" LLM ", " OPEN_SOURCE "],
        )
        self.assertEqual(result.primary, "ai_ml")
        self.assertEqual(result.tags, ("llm", "open_source"))
        with self.assertRaises(briefing_topics.TopicValidationError):
            briefing_topics.validate_topic_assignment("AI 与机器学习", [])


class DeterministicTopicClassificationTests(unittest.TestCase):
    def assert_topic(
        self,
        title: str,
        evidence: str,
        primary: str,
        tags: tuple[str, ...],
    ) -> None:
        first = briefing_topics.classify_content(title, evidence)
        second = briefing_topics.classify_content(title, evidence)
        self.assertEqual(first, second)
        self.assertEqual(first, (primary, tags))
        self.assertIn(first[0], briefing_topics.CATEGORY_LABELS)
        self.assertLessEqual(len(first[1]), 2)
        self.assertEqual(len(first[1]), len(set(first[1])))
        self.assertTrue(all(tag in briefing_topics.TAG_LABELS for tag in first[1]))

    def test_security_event_overrides_browser_platform(self) -> None:
        self.assert_topic(
            "Actively exploited sandbox RCE in all Chromium versions",
            "The security advisory assigns a CVE to the browser vulnerability.",
            "security_privacy",
            ("browser", "vulnerability"),
        )

    def test_cloud_outage_with_ai_products_is_classified_by_event_center(self) -> None:
        self.assert_topic(
            "ChatGPT, Claude and Grok hit by simultaneous outages",
            "The service disruption affected three AI services; no shared root cause is confirmed.",
            "cloud_infra",
            ("ai_service", "incident_review"),
        )

    def test_ai_paper_uses_specific_object_and_research_lens(self) -> None:
        self.assert_topic(
            "同一种蒸馏，为何会教会推理却拖慢记忆？",
            "这项研究论文分析模型训练中的知识蒸馏，并报告受控实验结果。",
            "ai_ml",
            ("model_training", "research_paper"),
        )

    def test_open_model_release_is_not_reduced_to_a_source_tag(self) -> None:
        self.assert_topic(
            "LLaDA-Image开放6B图像生成与编辑模型权重、代码和训练配方",
            "团队开源多模态扩散模型，发布代码与模型权重。",
            "ai_ml",
            ("multimodal", "open_source"),
        )

    def test_bare_chinese_distillation_still_selects_ai(self) -> None:
        category, tags = briefing_topics.classify_content(
            "同一种蒸馏，为何会教会推理却拖慢记忆？",
            "作者比较前向KL蒸馏对推理与事实记忆的影响。",
        )
        self.assertEqual(category, "ai_ml")
        self.assertEqual(tags, ("model_training",))

    def test_robotics_research_is_not_left_in_general_fallback(self) -> None:
        category, tags = briefing_topics.classify_content(
            "机器人的数据瓶颈，可能不在动作标注",
            "机器人通过视频预训练学习跨环境操作。",
        )
        self.assertEqual(category, "ai_ml")
        self.assertEqual(tags, ("robotics", "model_training"))

    def test_gui_agent_is_ai_even_when_evidence_mentions_infrastructure(self) -> None:
        category, tags = briefing_topics.classify_content(
            "GUI Agent走向产品，需要三套基础设施",
            "GUI Agent扩展环境、任务和奖励核验，并使用强化学习。",
        )
        self.assertEqual(category, "ai_ml")
        self.assertEqual(tags, ("ai_agent", "model_training"))

    def test_sandbox_bypass_is_security_not_generic_research(self) -> None:
        category, tags = briefing_topics.classify_content(
            "研究者发现OpenAI代理公开留言并绕过沙箱限制",
            "代理共享答案并尝试绕过沙箱限制。",
        )
        self.assertEqual(category, "security_privacy")
        self.assertEqual(tags, ("vulnerability",))
        self.assertNotIn("product_release", tags)

    def test_chinese_ai_outage_keeps_incident_lens(self) -> None:
        category, tags = briefing_topics.classify_content(
            "ChatGPT、Claude与Grok同期故障",
            "三家AI服务中断，但没有证据指向共同基础设施故障。",
        )
        self.assertEqual(category, "cloud_infra")
        self.assertEqual(tags, ("ai_service", "incident_review"))

    def test_programming_language_and_methodology(self) -> None:
        self.assert_topic(
            "Why Rust language changes API design",
            "A practical guide and methodology for library authors.",
            "software_dev",
            ("rust", "methodology"),
        )

    def test_operating_system_can_keep_two_specific_objects(self) -> None:
        self.assert_topic(
            "Linux 6.14 scheduler changes",
            "The Linux kernel changes process scheduling behavior.",
            "systems_os",
            ("linux", "kernel"),
        )

    def test_hardware_story_gets_hardware_not_generic_tech(self) -> None:
        self.assert_topic(
            'The "$60 Gaming PC" - AMD BC-250',
            "A DIY gaming hardware build uses an AMD BC-250 processor.",
            "hardware_chips",
            ("gaming_hardware", "diy_hardware"),
        )

    def test_git_hosting_story_gets_platform_and_sovereignty(self) -> None:
        self.assert_topic(
            "Git hosting that never leaves Europe",
            "A European code hosting service focuses on digital sovereignty.",
            "cloud_infra",
            ("developer_platform", "digital_sovereignty"),
        )

    def test_management_story_can_retain_ai_context_without_calling_it_agent(self) -> None:
        self.assert_topic(
            "AI handles incidents, engineers lose touch with their systems",
            "The essay discusses engineering management and skill erosion.",
            "org_management",
            ("ai_application", "engineering_management"),
        )

    def test_formal_theorem_story_is_science_even_from_an_ai_company(self) -> None:
        self.assert_topic(
            "Formalizing Fermat's Last Theorem",
            "Researchers report a formal proof effort supported by AI tools.",
            "science_research",
            ("ai_application", "formal_verification"),
        )

    def test_policy_story_is_not_technology_just_because_hn_found_it(self) -> None:
        self.assert_topic(
            "Pentagon rescinds new testosterone screening policy without explanation",
            "The public policy affected military personnel.",
            "policy_society",
            ("regulation",),
        )

    def test_ambiguous_short_words_do_not_create_language_or_os_tags(self) -> None:
        primary, tags = briefing_topics.classify_content(
            "We go through rust-colored windows under a cloud",
            "A travel essay with no software subject.",
        )
        self.assertEqual(primary, "general_interest")
        self.assertEqual(tags, ())

    def test_unknown_content_uses_explicit_general_fallback(self) -> None:
        self.assert_topic(
            "An unexpected afternoon",
            "A short personal essay.",
            "general_interest",
            (),
        )

    def test_input_is_bounded_and_still_returns_valid_shape(self) -> None:
        category, tags = briefing_topics.classify_content(
            "An unexpected afternoon",
            ("unrelated " * 2_000) + " CVE-2099-1",
        )
        self.assertEqual(category, "general_interest")
        self.assertEqual(tags, ())


if __name__ == "__main__":
    unittest.main()
