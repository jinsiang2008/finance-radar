"""Conservative content taxonomy for Daily Briefing records.

The six Daily rails answer *where an event belongs* (macro, world, finance,
technology, AI or investors).  This module answers a different question:
*what the article is about*.  It intentionally does not use publisher names,
Hacker News popularity or producer-supplied free-form labels.

Classification is deterministic and bounded.  One stable primary category is
always returned, together with at most two allow-listed detail tags.  Exact
technology names are preferred over generic labels, and an object tag is
paired with one useful lens (for example ``rust`` + ``methodology``) when the
evidence supports both.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


TAXONOMY_VERSION: Final = "daily-content-v1"

# Keep keys stable: they are persisted and exposed by the public Daily API.
# These categories are deliberately topic-level and do not repeat the six
# editorial rail names.
CATEGORY_LABELS = MappingProxyType(
    {
        "ai_ml": "AI 与机器学习",
        "cloud_infra": "云与基础设施",
        "software_dev": "软件开发",
        "systems_os": "系统与操作系统",
        "security_privacy": "安全与隐私",
        "data_database": "数据与数据库",
        "hardware_chips": "芯片与硬件",
        "product_business": "产品与商业",
        "org_management": "组织与管理",
        "policy_society": "政策与社会",
        "science_research": "科学与研究",
        "general_interest": "综合议题",
    }
)

# Detail tags mix a concrete object/stack with a useful reading lens.  The
# selector below emits no more than one from each group when both are present.
TAG_LABELS = MappingProxyType(
    {
        # AI and automation objects
        "llm": "大模型",
        "ai_agent": "AI Agent",
        "ai_application": "AI 应用",
        "ai_service": "AI 服务",
        "multimodal": "多模态",
        "model_training": "模型训练",
        "robotics": "具身智能",
        # Cloud and infrastructure objects
        "kubernetes": "Kubernetes",
        "serverless": "Serverless",
        "distributed_systems": "分布式系统",
        "sre": "SRE",
        "observability": "可观测性",
        "networking": "网络",
        # Programming languages and developer tooling
        "python": "Python",
        "rust": "Rust",
        "go": "Go",
        "javascript": "JavaScript / TypeScript",
        "java": "Java / JVM",
        "cpp": "C / C++",
        "compiler": "编译器",
        "web_dev": "Web 开发",
        "developer_platform": "开发平台",
        # Systems
        "linux": "Linux",
        "windows": "Windows",
        "apple_platform": "Apple 平台",
        "android": "Android",
        "browser": "浏览器",
        "kernel": "内核",
        # Data
        "database": "数据库",
        "data_engineering": "数据工程",
        "vector_search": "向量检索",
        # Hardware
        "gpu": "GPU / 加速器",
        "cpu": "CPU",
        "semiconductor": "半导体",
        "gaming_hardware": "游戏硬件",
        "edge_device": "边缘设备",
        # Reading lenses
        "open_source": "开源",
        "methodology": "方法论",
        "research_paper": "论文研究",
        "formal_verification": "形式化验证",
        "architecture": "架构设计",
        "engineering_practice": "工程实践",
        "performance": "性能优化",
        "reliability": "可靠性",
        "vulnerability": "安全漏洞",
        "privacy": "隐私",
        "incident_review": "故障复盘",
        "product_release": "产品发布",
        "business_model": "商业模式",
        "engineering_management": "工程管理",
        "regulation": "政策监管",
        "digital_sovereignty": "数字主权",
        "diy_hardware": "DIY 硬件",
    }
)

# Compatibility aliases use the more descriptive names discussed during the
# Daily integration.  New callers should prefer CATEGORY_LABELS/TAG_LABELS.
PRIMARY_TOPIC_LABELS = CATEGORY_LABELS
DETAIL_TAG_LABELS = TAG_LABELS

_MAX_TITLE_CHARS: Final = 600
_MAX_EVIDENCE_CHARS: Final = 6_000


class TopicValidationError(ValueError):
    """A persisted or model-proposed taxonomy assignment is invalid."""


@dataclass(frozen=True)
class TopicClassification:
    primary: str
    tags: tuple[str, ...] = ()
    version: str = TAXONOMY_VERSION

    def __post_init__(self) -> None:
        _validate_normalized(self.primary, self.tags, self.version)

    @property
    def primary_label(self) -> str:
        return CATEGORY_LABELS[self.primary]

    @property
    def tag_labels(self) -> tuple[str, ...]:
        return tuple(TAG_LABELS[tag] for tag in self.tags)

    def to_payload(self) -> dict[str, object]:
        return {
            "content_category": self.primary,
            "content_tags": list(self.tags),
            "taxonomy_version": self.version,
        }


@dataclass(frozen=True)
class _Signal:
    pattern: re.Pattern[str]
    title_weight: int
    evidence_weight: int


def _pattern(expression: str, *, case_sensitive: bool = False) -> re.Pattern[str]:
    flags = re.UNICODE if case_sensitive else re.UNICODE | re.IGNORECASE
    return re.compile(expression, flags)


def _signal(
    expression: str,
    title_weight: int,
    evidence_weight: int,
    *,
    case_sensitive: bool = False,
) -> _Signal:
    return _Signal(
        _pattern(expression, case_sensitive=case_sensitive),
        title_weight,
        evidence_weight,
    )


_CATEGORY_ORDER = tuple(CATEGORY_LABELS)
_CATEGORY_RULES: dict[str, tuple[_Signal, ...]] = {
    "security_privacy": (
        _signal(
            r"\bCVE-[0-9]{4}-[0-9]+\b|\bRCE\b|remote code execution|"
            r"actively exploited|zero[ -]day|sandbox (?:escape|breakout)|"
            r"安全漏洞|远程代码执行|正在利用|零日漏洞|沙箱逃逸|"
            r"绕过.{0,8}沙箱|沙箱.{0,8}(?:绕过|限制)",
            20,
            8,
        ),
        _signal(
            r"\b(?:vulnerabilit(?:y|ies)|exploit|malware|ransomware|"
            r"cybersecurity|security advisory|data breach)\b|"
            r"(?:安全事件|网络安全|恶意软件|勒索软件|数据泄露|隐私风险)",
            10,
            4,
        ),
    ),
    "policy_society": (
        _signal(
            r"\b(?:law|legislation|regulation|regulator|government policy|"
            r"public policy|sanction|tariff|export control|ban)\b|"
            r"(?:法律|立法|监管|政府政策|公共政策|制裁|关税|出口管制|禁令)",
            15,
            6,
        ),
        _signal(
            r"\b(?:Pentagon|government corruption|geopolitical|election|"
            r"digital sovereignty)\b|(?:五角大楼|政府腐败|地缘政治|选举|数字主权)",
            10,
            4,
        ),
    ),
    "org_management": (
        _signal(
            r"engineers?.{0,36}(?:lose touch|deskilling|skill erosion)|"
            r"(?:工程师|开发者).{0,20}(?:技能退化|失去掌控|脱离系统)",
            20,
            8,
        ),
        _signal(
            r"\b(?:engineering management|management|manager|leadership|"
            r"organization(?:al)?|team culture|hiring|layoffs?|workplace|"
            r"developer productivity)\b|(?:工程管理|组织管理|领导力|团队文化|"
            r"招聘|裁员|职场|开发者效率)",
            12,
            5,
        ),
    ),
    "cloud_infra": (
        _signal(
            r"\b(?:outages?|downtime|service disruption|incident response|"
            r"postmortem)\b|(?:服务中断|服务故障|系统故障|同期故障|大面积故障|"
            r"宕机|事故响应|故障复盘)",
            16,
            6,
        ),
        _signal(
            r"\b(?:cloud computing|cloud platform|Amazon Web Services|AWS|"
            r"Microsoft Azure|Google Cloud|GCP|Kubernetes|K8s|serverless|"
            r"site reliability engineering|SRE)\b|(?:云计算|云平台|云服务|"
            r"容器编排|无服务器|站点可靠性)",
            12,
            5,
        ),
        _signal(
            r"\b(?:distributed systems?|microservices?|observability|"
            r"infrastructure|data cent(?:er|re)|git hosting|code hosting)\b|"
            r"(?:分布式系统|微服务|可观测性|基础设施|数据中心|代码托管)",
            10,
            4,
        ),
    ),
    "ai_ml": (
        _signal(
            r"(?<![a-z0-9])(?:AI|LLM|VLM)(?![a-z0-9])|"
            r"\b(?:artificial intelligence|machine learning|deep learning|"
            r"large language models?|foundation models?|neural networks?|"
            r"transformers?|diffusion models?|agentic AI|AI agents?)\b|"
            r"(?<![a-z0-9])GUI agents?(?![a-z0-9])|"
            r"(?:人工智能|机器学习|深度学习|大语言模型|大模型|基础模型|神经网络|"
            r"扩散模型|生成模型|智能体|机器人|图像生成|图像编辑)",
            12,
            5,
        ),
        _signal(
            r"\b(?:OpenAI|Anthropic|ChatGPT|Claude|Gemini|Grok|Hugging Face)\b|"
            r"(?:模型训练|模型推理|知识蒸馏|蒸馏|强化学习|预训练|后训练|多模态|"
            r"具身智能)",
            8,
            3,
        ),
    ),
    "data_database": (
        _signal(
            r"\b(?:database|PostgreSQL|MySQL|SQLite|DuckDB|Redis|ClickHouse|"
            r"data warehouse|data lake|query engine|vector database)\b|"
            r"(?:数据库|数据仓库|数据湖|查询引擎|向量数据库)",
            12,
            5,
        ),
        _signal(
            r"\b(?:ETL|data engineering|data pipeline|stream processing|"
            r"vector search|retrieval augmented generation|RAG)\b|"
            r"(?:数据工程|数据管道|流处理|向量检索|检索增强生成)",
            9,
            4,
        ),
    ),
    "systems_os": (
        _signal(
            r"\b(?:operating system|Linux kernel|FreeBSD|OpenBSD|macOS|iOS|"
            r"Android OS|Microsoft Windows|Windows (?:10|11|Server)|"
            r"device driver)\b|(?:操作系统|系统内核|设备驱动)",
            13,
            5,
        ),
        _signal(
            r"\b(?:Chromium|Chrome|Firefox|WebKit|web browser|browser engine)\b|"
            r"(?:浏览器|浏览器内核)",
            9,
            3,
        ),
    ),
    "hardware_chips": (
        _signal(
            r"\b(?:GPU|CPU|NPU|TPU|semiconductor|microchip|chiplet|processor|"
            r"accelerator|RISC-V|gaming PC|keyboard)\b|"
            r"(?:芯片|半导体|处理器|加速器|显卡|游戏电脑|键盘|硬件)",
            13,
            5,
        ),
        _signal(
            r"\b(?:AMD|NVIDIA|Intel|Arm)\s+(?:GPU|CPU|APU|processor|chip|"
            r"accelerator|BC-[0-9]+)\b",
            10,
            4,
        ),
    ),
    "software_dev": (
        _signal(
            r"\b(?:programming language|software engineering|compiler|"
            r"interpreter|runtime|developer tools?|SDK|API design|Nitter)\b|"
            r"(?:编程语言|软件工程|编译器|解释器|运行时|开发工具|接口设计)",
            12,
            5,
        ),
        _signal(
            r"\b(?:Git|GitHub|GitLab|Codeberg|rustc|Cargo|Golang|TypeScript|"
            r"JavaScript|OpenJDK|JVM)\b|(?:代码仓库|源码托管)",
            9,
            3,
        ),
        _signal(
            r"\b(?:Python|Rust|Java)\s+(?:[0-9]+(?:\.[0-9]+)*|language|"
            r"compiler|runtime|package|library|framework)\b|"
            r"\b(?:written|built|implemented) in (?:Python|Rust|Go|Java|C\+\+)\b",
            10,
            4,
        ),
    ),
    "product_business": (
        _signal(
            r"\b(?:pricing|subscription|business model|acquisition|revenue|"
            r"monetization|go-to-market|startup funding|venture funding)\b|"
            r"(?:定价|订阅|商业模式|收购|营收|商业化|市场进入|创业融资|风险融资)",
            12,
            5,
        ),
        _signal(
            r"\b(?:product launch|product strategy|user experience|customer adoption)\b|"
            r"(?:产品发布|产品策略|用户体验|客户采用)",
            7,
            3,
        ),
    ),
    "science_research": (
        _signal(
            r"\b(?:theorem|formal proof|formalizing|mathematics|physics|"
            r"chemistry|biology|clinical trial)\b|(?:定理|形式化证明|数学|"
            r"物理学|化学(?:研究|实验|反应|材料|学科)|生物学|临床试验)",
            15,
            6,
        ),
        _signal(
            r"\b(?:research paper|peer review|scientific study)\b|"
            r"(?:研究论文|同行评审|科学研究)",
            7,
            3,
        ),
    ),
}


_SUBJECT_TAG_ORDER = (
    "llm",
    "ai_agent",
    "ai_application",
    "ai_service",
    "multimodal",
    "model_training",
    "robotics",
    "kubernetes",
    "serverless",
    "distributed_systems",
    "sre",
    "observability",
    "networking",
    "python",
    "rust",
    "go",
    "javascript",
    "java",
    "cpp",
    "compiler",
    "web_dev",
    "developer_platform",
    "linux",
    "windows",
    "apple_platform",
    "android",
    "browser",
    "kernel",
    "database",
    "data_engineering",
    "vector_search",
    "gpu",
    "cpu",
    "semiconductor",
    "gaming_hardware",
    "edge_device",
)

_LENS_TAG_ORDER = (
    "open_source",
    "methodology",
    "research_paper",
    "formal_verification",
    "architecture",
    "engineering_practice",
    "performance",
    "reliability",
    "vulnerability",
    "privacy",
    "incident_review",
    "product_release",
    "business_model",
    "engineering_management",
    "regulation",
    "digital_sovereignty",
    "diy_hardware",
)

_TAG_RULES: dict[str, tuple[_Signal, ...]] = {
    "llm": (
        _signal(
            r"(?<![a-z0-9])LLMs?(?![a-z0-9])|\blarge language models?\b|"
            r"(?:大语言模型|大模型)",
            10,
            4,
        ),
    ),
    "ai_agent": (
        _signal(
            r"\b(?:AI|LLM|autonomous)[ -]?agents?\b|\bagentic\b|"
            r"(?<![a-z0-9])GUI agents?(?![a-z0-9])|"
            r"(?:AI智能体|智能体|代理系统)",
            10,
            4,
        ),
    ),
    "ai_application": (
        _signal(
            r"\bAI[- ](?:assisted|powered|enabled|driven)\b|\bAI tools?\b|"
            r"\bAI (?:handles?|writes?|reviews?|operates?|manages?)\b|"
            r"(?:AI辅助|AI驱动|AI应用)",
            8,
            3,
        ),
    ),
    "ai_service": (
        _signal(
            r"\b(?:ChatGPT|Claude|Gemini|Grok)\b|(?:AI服务|模型服务)",
            8,
            3,
        ),
    ),
    "multimodal": (
        _signal(
            r"\b(?:multimodal|vision-language|text-to-image|image generation)\b|"
            r"(?:多模态|视觉语言|文生图|图像生成|图像编辑)",
            9,
            4,
        ),
    ),
    "model_training": (
        _signal(
            r"\b(?:model training|fine-tuning|distillation|reinforcement learning|"
            r"pretraining|post-training)\b|(?:模型训练|微调|蒸馏|强化学习|"
            r"预训练|后训练)",
            9,
            4,
        ),
    ),
    "robotics": (
        _signal(r"\b(?:robotics?|embodied AI)\b|(?:机器人|具身智能)", 9, 4),
    ),
    "kubernetes": (_signal(r"\b(?:Kubernetes|K8s)\b", 10, 4),),
    "serverless": (_signal(r"\bserverless\b|无服务器", 9, 4),),
    "distributed_systems": (
        _signal(r"\bdistributed systems?\b|分布式系统", 9, 4),
    ),
    "sre": (
        _signal(r"\b(?:SRE|site reliability engineering)\b|站点可靠性", 9, 4),
    ),
    "observability": (_signal(r"\bobservability\b|可观测性", 9, 4),),
    "networking": (
        _signal(r"\b(?:networking|network protocol|TCP|QUIC|DNS|BGP)\b|网络协议", 8, 3),
    ),
    "python": (
        _signal(
            r"\bPython\s+(?:[0-9]+(?:\.[0-9]+)*|language|interpreter|package|"
            r"library|framework)\b|\b(?:CPython|PyPI)\b|Python语言",
            10,
            4,
        ),
    ),
    "rust": (
        _signal(
            r"\bRust\s+(?:[0-9]+(?:\.[0-9]+)*|language|compiler|crate|library|"
            r"framework)\b|\b(?:rustc|Cargo)\b|Rust语言",
            10,
            4,
        ),
    ),
    "go": (
        _signal(
            r"\bGolang\b|\bGo (?:programming language|language|compiler|runtime|"
            r"module|package|toolchain|code)\b|Go语言",
            10,
            4,
        ),
    ),
    "javascript": (
        _signal(r"\b(?:JavaScript|TypeScript|Node\.js|Deno|Bun)\b", 10, 4),
    ),
    "java": (
        _signal(r"\b(?:OpenJDK|JVM|JDK|Java [0-9]+|Java language)\b|Java语言", 10, 4),
    ),
    "cpp": (
        _signal(r"(?<![A-Za-z0-9])(?:C\+\+|C language)(?![A-Za-z0-9])|C语言", 10, 4),
    ),
    "compiler": (_signal(r"\bcompiler\b|编译器", 8, 3),),
    "web_dev": (
        _signal(r"\b(?:web development|web framework|frontend|browser API)\b|Web开发", 8, 3),
    ),
    "developer_platform": (
        _signal(
            r"\b(?:Git|code) hosting\b|\b(?:GitHub|GitLab|Codeberg)\b|"
            r"(?:代码托管|开发者平台)",
            9,
            4,
        ),
    ),
    "linux": (
        _signal(r"\bLinux(?: kernel| distribution| distro| [0-9])?\b", 10, 4),
    ),
    "windows": (
        _signal(r"\b(?:Microsoft Windows|Windows (?:10|11|Server|kernel|driver))\b", 10, 4),
    ),
    "apple_platform": (_signal(r"\b(?:macOS|iOS|iPadOS|visionOS|SwiftUI)\b", 10, 4),),
    "android": (_signal(r"\bAndroid(?: OS| [0-9]| app| platform)?\b", 10, 4),),
    "browser": (
        _signal(r"\b(?:Chromium|Chrome|Firefox|WebKit|web browser|browser engine)\b|浏览器", 10, 4),
    ),
    "kernel": (_signal(r"\bkernel\b|系统内核", 9, 4),),
    "database": (
        _signal(
            r"\b(?:database|PostgreSQL|MySQL|SQLite|DuckDB|Redis|ClickHouse)\b|数据库",
            10,
            4,
        ),
    ),
    "data_engineering": (
        _signal(r"\b(?:ETL|data engineering|data pipeline|stream processing)\b|数据工程|数据管道", 9, 4),
    ),
    "vector_search": (
        _signal(r"\b(?:vector search|vector database|RAG)\b|向量检索|向量数据库", 9, 4),
    ),
    "gpu": (_signal(r"\b(?:GPU|NPU|TPU|graphics processor|AI accelerator)\b|显卡|AI加速器", 10, 4),),
    "cpu": (_signal(r"\b(?:CPU|central processing unit)\b|中央处理器", 10, 4),),
    "semiconductor": (
        _signal(r"\b(?:semiconductor|microchip|chiplet|chip fabrication)\b|芯片|半导体", 9, 4),
    ),
    "gaming_hardware": (_signal(r"\b(?:gaming PC|gaming hardware)\b|游戏电脑|游戏硬件", 9, 4),),
    "edge_device": (_signal(r"\b(?:edge device|on-device|embedded device)\b|边缘设备|端侧", 8, 3),),
    "open_source": (
        _signal(
            r"\b(?:open[ -]source|source available|MIT licen[cs]e|Apache[- ]2|GPL)\b|"
            r"\breleas(?:e|es|ed|ing) (?:the )?(?:source code|code and weights|model weights)\b|"
            r"(?:开源|开放.{0,12}(?:代码|源码|模型权重|权重))",
            10,
            4,
        ),
    ),
    "methodology": (
        _signal(
            r"\b(?:methodology|playbook|best practices?|practical guide)\b|"
            r"(?:方法论|实践指南|最佳实践|经验总结)",
            9,
            4,
        ),
    ),
    "research_paper": (
        _signal(
            r"\b(?:research paper|paper proposes|authors? (?:show|report|find)|"
            r"peer-reviewed study)\b|(?:研究论文|这项工作|论文提出|作者(?:发现|报告))",
            8,
            4,
        ),
    ),
    "formal_verification": (
        _signal(r"\b(?:formal verification|formal proof|formalizing)\b|形式化验证|形式化证明", 10, 4),
    ),
    "architecture": (_signal(r"\b(?:software|system) architecture\b|架构设计|系统架构", 8, 3),),
    "engineering_practice": (
        _signal(r"\b(?:engineering practice|implementation guide|lessons learned)\b|工程实践|实现指南", 8, 3),
    ),
    "performance": (
        _signal(r"\b(?:performance optimization|benchmarking|latency|throughput)\b|性能优化|延迟|吞吐量", 8, 3),
    ),
    "reliability": (
        _signal(r"\b(?:reliability|resilien(?:ce|t)|fault toleran(?:ce|t))\b|可靠性|韧性|容错", 8, 3),
    ),
    "vulnerability": (
        _signal(
            r"\bCVE-[0-9]{4}-[0-9]+\b|\bRCE\b|remote code execution|"
            r"actively exploited|zero[ -]day|\bvulnerabilit(?:y|ies)\b|"
            r"安全漏洞|远程代码执行|零日漏洞|绕过.{0,8}沙箱|"
            r"沙箱.{0,8}(?:绕过|限制)",
            12,
            5,
        ),
    ),
    "privacy": (_signal(r"\bprivacy\b|隐私", 8, 3),),
    "incident_review": (
        _signal(
            r"\b(?:outages?|downtime|incident response|postmortem)\b|"
            r"(?:服务中断|服务故障|系统故障|同期故障|宕机|事故响应|故障复盘)",
            10,
            4,
        ),
    ),
    "product_release": (
        _signal(
            r"\b(?:launches?|releases?|unveils?)\b|"
            r"(?:(?:产品|模型|版本|工具|服务).{0,8}(?:发布|推出|上线)|"
            r"(?:发布|推出|上线).{0,8}(?:产品|模型|版本|工具|服务))",
            7,
            3,
        ),
    ),
    "business_model": (
        _signal(r"\b(?:pricing|subscription|business model|monetization)\b|定价|订阅|商业模式|商业化", 9, 4),
    ),
    "engineering_management": (
        _signal(
            r"\b(?:engineering management|developer productivity|team culture|"
            r"engineers?.{0,30}(?:lose touch|deskilling))\b|"
            r"(?:工程管理|开发者效率|团队文化|技能退化)",
            10,
            4,
        ),
    ),
    "regulation": (
        _signal(r"\b(?:law|regulation|regulator|public policy|export control)\b|法律|监管|公共政策|出口管制", 9, 4),
    ),
    "digital_sovereignty": (
        _signal(
            r"\b(?:digital sovereignty|data sovereignty|never leaves Europe|"
            r"hosted in Europe|European hosting)\b|数字主权|数据主权|欧洲托管",
            10,
            4,
        ),
    ),
    "diy_hardware": (_signal(r"\b(?:DIY|homebuilt|self-built)\b|自行组装|DIY硬件", 8, 3),),
}


def _clean_text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip()[:maximum]


def _scores(
    rules: dict[str, tuple[_Signal, ...]],
    title: str,
    evidence: str,
) -> dict[str, int]:
    scores: dict[str, int] = {}
    for key, signals in rules.items():
        score = 0
        for signal in signals:
            if title and signal.pattern.search(title):
                score += signal.title_weight
            if evidence and signal.pattern.search(evidence):
                score += signal.evidence_weight
        if score:
            scores[key] = score
    return scores


def _ordered_matches(
    candidates: Sequence[str],
    scores: dict[str, int],
) -> list[str]:
    order = {key: index for index, key in enumerate(candidates)}
    return sorted(
        (key for key in candidates if scores.get(key, 0) > 0),
        key=lambda key: (-scores[key], order[key]),
    )


def classify_content(title: str, evidence: str = "") -> tuple[str, tuple[str, ...]]:
    """Return one primary category and up to two stable detail-tag keys.

    ``evidence`` should be a bounded source excerpt or evidence-backed summary,
    not comments, engagement metrics or a publisher label.  Low-confidence
    material is labelled ``general_interest`` instead of inheriting a Daily
    rail or inventing a technical topic.
    """

    clean_title = _clean_text(title, _MAX_TITLE_CHARS)
    clean_evidence = _clean_text(evidence, _MAX_EVIDENCE_CHARS)

    category_scores = _scores(_CATEGORY_RULES, clean_title, clean_evidence)
    if category_scores:
        highest_category_score = max(category_scores.values())
        category = next(
            key
            for key in _CATEGORY_ORDER
            if category_scores.get(key, 0) == highest_category_score
        )
    else:
        category = "general_interest"

    tag_scores = _scores(_TAG_RULES, clean_title, clean_evidence)
    subjects = _ordered_matches(_SUBJECT_TAG_ORDER, tag_scores)
    lenses = _ordered_matches(_LENS_TAG_ORDER, tag_scores)
    selected: list[str] = []
    if subjects:
        selected.append(subjects[0])
    if lenses:
        selected.append(lenses[0])
    if len(selected) < 2:
        remaining = _ordered_matches(
            _SUBJECT_TAG_ORDER + _LENS_TAG_ORDER,
            tag_scores,
        )
        available = [tag for tag in remaining if tag not in selected]
        selected.extend(available[: 2 - len(selected)])

    return category, tuple(selected[:2])


def _validate_normalized(
    category: str,
    tags: tuple[str, ...],
    taxonomy_version: str,
) -> None:
    if taxonomy_version != TAXONOMY_VERSION:
        raise TopicValidationError("unsupported taxonomy_version")
    if category not in CATEGORY_LABELS:
        raise TopicValidationError("unsupported content_category")
    if len(tags) > 2:
        raise TopicValidationError("content_tags cannot contain more than 2 items")
    if len(set(tags)) != len(tags):
        raise TopicValidationError("content_tags cannot contain duplicates")
    if any(tag not in TAG_LABELS for tag in tags):
        raise TopicValidationError("content_tags contains an unsupported value")


def validate_topic_assignment(
    category: object,
    tags: object,
    *,
    taxonomy_version: object = TAXONOMY_VERSION,
) -> TopicClassification:
    """Validate untrusted persisted/model output and return normalized keys."""

    if not isinstance(category, str):
        raise TopicValidationError("content_category must be a string")
    normalized_category = category.strip().casefold()
    if not isinstance(taxonomy_version, str):
        raise TopicValidationError("taxonomy_version must be a string")
    normalized_version = taxonomy_version.strip()
    if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes, bytearray)):
        raise TopicValidationError("content_tags must be an array")
    if len(tags) > 2:
        raise TopicValidationError("content_tags cannot contain more than 2 items")
    normalized_tags: list[str] = []
    for value in tags:
        if not isinstance(value, str):
            raise TopicValidationError("content_tags values must be strings")
        normalized = value.strip().casefold()
        if not normalized:
            raise TopicValidationError("content_tags values cannot be empty")
        normalized_tags.append(normalized)
    return TopicClassification(
        primary=normalized_category,
        tags=tuple(normalized_tags),
        version=normalized_version,
    )


def classify_topic(title: str, evidence_summary: str = "") -> TopicClassification:
    """Object-oriented wrapper around :func:`classify_content`."""

    category, tags = classify_content(title, evidence_summary)
    return TopicClassification(category, tags)


def topic_payload(title: str, evidence: str = "") -> dict[str, object]:
    """Return the public-contract fields for deterministic classification."""

    return classify_topic(title, evidence).to_payload()


__all__ = [
    "CATEGORY_LABELS",
    "DETAIL_TAG_LABELS",
    "PRIMARY_TOPIC_LABELS",
    "TAG_LABELS",
    "TAXONOMY_VERSION",
    "TopicClassification",
    "TopicValidationError",
    "classify_content",
    "classify_topic",
    "topic_payload",
    "validate_topic_assignment",
]
