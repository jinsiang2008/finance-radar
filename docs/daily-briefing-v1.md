# Daily Briefing v1 导入契约

这个契约把 OpenClaw/Hermes 的采集结果和 Finance Radar 的公共只读页面隔开。
生产者负责采集并原子写出 JSON；导入器只校验、去重和持久化，不访问网络、不
运行定时任务，也不调用模型。

## 最小结构

六个栏目必须全部存在，允许使用空数组。事件时间均为带时区的 ISO 8601；仅
持仓截至日 `effective_at`（及其输入别名 `period_end`、`data_as_of`）可使用严格
的 `YYYY-MM-DD`：

```json
{
  "schema_version": 1,
  "snapshot_date": "2026-09-05",
  "generated_at": "2026-09-05T10:00:00+08:00",
  "source_as_of": "2026-09-05T09:58:00+08:00",
  "sections": {
    "macro": [],
    "world": [],
    "finance": [],
    "technology": [],
    "ai": [
      {
        "title": "NIST 发布新的 AI 风险管理资料",
        "source": "NIST",
        "source_url": "https://www.nist.gov/artificial-intelligence/example",
        "published_at": "2026-09-05T09:30:00+08:00",
        "fetched_at": "2026-09-05T09:34:00+08:00",
        "source_tier": "official",
        "summary": "基于原始文档的简短事实摘要。",
        "why_it_matters": "对市场或产业链的可核验影响。",
        "assets": ["THEME:AI"]
      }
    ],
    "investors": [
      {
        "title": "投资机构季度持仓披露",
        "source": "SEC",
        "source_url": "https://www.sec.gov/edgar/browse/example",
        "published_at": "2026-09-05T08:20:00+08:00",
        "fetched_at": "2026-09-05T08:25:00+08:00",
        "disclosed_at": "2026-09-05T08:20:00+08:00",
        "period_end": "2026-06-30",
        "source_tier": "official",
        "summary": "本次披露对应上一季度末持仓。",
        "why_it_matters": "披露日不等于交易发生日。",
        "assets": ["US:BRK.B"]
      }
    ]
  }
}
```

每条记录必须有 `title`、`source_url`、`source_tier`，并至少提供
`published_at` 或 `fetched_at`。`source`、`summary`、`why_it_matters` 和
`assets` 可选。投资披露还可提供带时区的 `disclosed_at`，以及
`effective_at`、`period_end`、`data_as_of` 三者之一作为持仓截至时间；多个别名
并存时必须表达同一时间。`source_tier` 只能是 `official`、`first_party`、
`media` 或 `discovery`；公共服务仍会根据 URL 与证据类型保守复核等级，生产者
的标签不能自行把转载升级成官方或一手原文。当前 `official` 仅信任政府与交易所
域名，`first_party` 仅信任来源账号与链接账号一致的 X / Truth Social 原帖，
普通非聚合文章可标为 `media`，其余降为 `discovery`。

### HN 与 AI 策展来源元数据

Hacker News、AI Digest 与 AI Brief 属于发现/策展层，不是对原始事实的独立
确认。三类记录必须使用以下严格元数据，公共 API 会逐字段二次校验；字段不一致
时整条记录 fail closed，不会只隐藏坏字段后继续展示：

| 字段 | 约束与语义 |
| --- | --- |
| `kind` | `hn_story`、`ai_digest`、`paper_digest` 之一 |
| `discovered_via` | 去重数组；值限于 `hacker_news_top`、`hacker_news_best`、`ai_digest_rss`、`ai_brief_rss`；必须包含与 `kind` 对应的主渠道，允许保留跨源合并后的其他渠道 |
| `publication_time_verified` | 布尔值；为 `true` 时必须有 `published_at`，为 `false` 时必须省略 `published_at` |
| `featured_at` | 带时区的精选/入榜时间，不能晚于 `fetched_at` 或本批 `source_as_of` |
| `original_url` | 可选的底层原始文章、官方发布或 arXiv 链；不能拿策展页冒充原链 |
| `discussion_url` | 仅在含 HN 渠道时使用，必须是与 `hn_id` 一致的 `https://news.ycombinator.com/item?id=...` |
| `hn_id`、`hn_score`、`hn_comments`、`hn_rank` | 含 HN 渠道时成套必填的非负有界整数；`hn_rank` 为 top/best 中的最优名次 |
| `heat_score` | 含 HN 渠道时必填，范围 `0..100`；只是同一发现层内的有界排序信号 |

`hn_story` 的 `source_url` 优先指向外部文章，Ask HN 等无外链帖子则指向
`discussion_url`；有外部文章时 `original_url` 必须和 `source_url` 一致。
`ai_digest` / `paper_digest` 的 `source_url` 必须分别指向
`ai-digest.liziran.com` / `ai-brief.liziran.com` 的策展条目；只有确认到唯一底层
来源时才填写一个不同的 `original_url`。主页、`/news`、`/blog`、`/research`、
`/papers`、`/company-announcements` 等通用落地页，以及没有 ID、日期、文档后缀
或结构上下文的单段 slug，不能作为事件原链；arXiv、带 identity query/数字 ID/
文档后缀的链接和至少两段的非泛化文章路径可以接受。无法确认唯一原链时必须
省略，让每个策展条目用自己的 `source_url` 保持独立。页面应把原链作为主要
核验入口、策展页作为“发现自”入口，不能互换标签。

三类记录无论链接最终落在媒体、官方或 arXiv，`source_tier` 都强制降为
`discovery`。`news.ycombinator.com`、`hacker-news.firebaseio.com`、
`ai-digest.liziran.com` 与 `ai-brief.liziran.com` 也始终按聚合/发现域处理。

### 中文阅读增强与内容标签

新采集的 HN、AI Digest 与 AI Brief 条目可带以下完整增强字段。原始 `title`
始终保留，并继续作为事件身份、去重与排序依据；中文字段只用于阅读展示，不能
改变来源等级、主栏目、交叉栏目或热度。

| 字段 | 约束与语义 |
| --- | --- |
| `title_zh` | 最多 180 字且含中文；`translated` 时必填。`source_zh` 不重复保存与原始标题等值的中文标题 |
| `summary_zh` | 最多 420 字且含中文；只有确有策展段落或 HN 自帖正文证据时才允许生成 |
| `summary_basis` | `title_only`、`curated_excerpt`、`self_post` 之一；即使翻译暂不可用也必须保留 |
| `content_category` | `daily-content-v1` 白名单中的唯一主类别，如 AI、云基础设施、软件开发、系统、管理等 |
| `content_tags` | 去重白名单数组，最多 2 个，如 `python`、`linux`、`methodology`、`engineering_management` |
| `taxonomy_version` | 当前必须严格等于 `daily-content-v1` |
| `translation_status` | `translated`、`source_zh`、`unavailable` 之一 |

证据边界采用 fail closed：`title_only` 禁止提供 `summary_zh`；
`curated_excerpt` 只适用于 `ai_digest` / `paper_digest`；`self_post` 只适用于没有
`original_url` 的 HN 自帖。`translated` 至少要有 `title_zh`，且有正文证据时还
必须有 `summary_zh`；`source_zh` 表示可展示中文来自来源自身，原始标题或来源
摘要至少一项必须含中文；`unavailable` 不得伪造中文字段，但仍保留证据边界与
确定性内容标签。

导入器遇到部分字段、未知标签、旧 taxonomy 或关系冲突时拒绝整批新输入。公共
读取服务会再次校验历史快照；若只发现增强字段损坏，会丢弃整个增强字段组但保留
核心故事。重复记录合并时只可从同来源信任层、同时间语义与同内容类型的记录原子
复制一组增强字段，不能把一条的中文标题与另一条的摘要依据或标签拼接。

## 时效与去重

- `snapshot_date` 必须与 `generated_at` 的北京时间日期一致。
- 输入的 `source_as_of` 表示本批数据实际覆盖到的时间，不能用文件复制或导入
  时间代替。公共 API 将它单独投影为 `source_coverage_as_of`；页面内容自身的
  最新证据时间是 `content_as_of`（并保留兼容字段 `source_as_of`）。两者不能
  相互冒充：新扫描不能把旧文章标新，空扫描也应明确显示“已扫描、暂无新增”。
  每条记录的抓取时间不得晚于本批覆盖时间；发布时间与披露时间只容许 5 分钟
  时钟偏差。
- 普通记录只有在 `published_at` 存在且位于最近 24 小时内时才能进入新闻栏目；
  只有 `fetched_at` 的普通记录标为 `fetched_only`，不得冒充发布时间。严格校验
  的 AI Digest/Brief 策展记录可用最近 24 小时内的 `featured_at` 进入栏目：未
  核验原始发布时间时标为 `featured_only`，页面只能显示“精选/发现时间”。HN
  不适用这个例外，必须始终按 `published_at` 中的帖子提交时间执行 24 小时门禁。
- HN 的 `published_at` 是官方 HN API 提供的帖子提交时间，
  `publication_time_verified=true` 只表示这个提交时间已核验，不代表外部原文的
  发布日期。AI Digest/Brief 页面或 RSS 的 datePublished/dateModified 只放入
  `featured_at`；除非另行核验到底层文章/论文时间，否则不得复制到
  `published_at`。AI Brief 的 T+3 论文当前只写入简报 `featured_at`；若日后
  另行核验论文真实发布时间，应把该旧时间保留在 `published_at`，以新的
  `featured_at` 表达“今天入选”，绝不能把论文重写成今天发布。
- 抓取、宏观快照生成或重新导入时间不会更新新闻的发布时间/证据截至时间，也
  不会把旧闻伪装成最新进展。
- 导入器移除常见跟踪参数；相同 canonical URL 也只有在北京时间事件日与保守
  规范化标题相容时才会合并，复用 landing page 的不同公告会保留。读取端只在
  实体、动作方向、数值、事实/预测语气与发布日期相容、且命中少量可审计事实
  句式时做跨语言事件合并；复杂从句默认保留。导入预处理只合并 canonical URL
  相同的相容记录；不同 URL 上的“Market Update”或“OpenAI Update”不会仅凭标题
  相同而合并。
  加息/降息、增持/减持、发布/召回、肯定/否定、预测/已发生不会合并。
- 同一事件只进入一个主栏目，AI 摘要只能补充 `cross_tags`、不能改变原始事实的
  主栏目；每栏默认显示 3 条，可展开至 6 条，不使用旧闻或低质量转载凑数。
  当前且发布时间已核验的官方/一手/媒体报道优先，陈旧官方记录不能挤掉当前
  媒体报道。
- 公共服务不直接信任生产者提交的 HN `heat_score`。独立 `hn_story` 排序时，会用
  已校验的 `hn_rank`、`hn_score`、`hn_comments`、top/best 双榜状态、HN 提交时间
  和当前时间按 8 小时半衰期指数衰减公式重新计算有界热度；生产者给出的
  `heat_score` 仅供展示。缺少独立 HN 提交时间的跨源策展代表仅展示社区指标，
  热度不参与排序。分数和评论数是社区热度，不是事实可信度或独立确认数。
- 最近 90 分钟内的官方/一手/媒体证据始终优先；最近 24 小时内且契约完整的 HN /
  AI 策展线索排在超过 90 分钟的旧媒体报道之前，避免 6 条旧报道完全隐藏当前
  热点。栏目“新鲜/陈旧”标签仍严格按最近 90 分钟计算。
- 页面显示的是“事件簇关联记录”，不把转载条数表述为独立确认数。
- 同一天、同一 schema 的再次导入只有在 `generated_at` 与 `source_as_of` 都不
  回退时才会更新；内容发生修订时至少推进其中一个时间，相同双时间的不同内容
  会被当作旧回放拒绝。读取端按 `source_as_of` 执行 24 小时门禁，并优先选实际
  来源时间最新的快照，而不是单看生成时间。陈旧或损坏快照自动回落到 Finance
  Radar 已落库数据。历史最多保留最近 45 天、64 条。

## URL 与文件安全

- 仅接受 HTTP(S) 公网地址；localhost、私网、回环、链路本地、多播、保留地址、
  用户名密码、反斜杠/空白、Unicode 或百分号编码主机、协议不匹配端口和非标准
  端口全部拒绝。
- URL 中出现 token、API key、签名、session、credential 等敏感查询参数时整条
  记录拒绝；常见 tracking 参数和 fragment 会从公开链接中移除。
- 导入文件必须是 2 MiB 以内的普通非软链 UTF-8 JSON；读取使用有界文件描述符，
  单栏最多 80 条、总计最多 300 条，未知字段与重复 JSON key 均拒绝。

## 旧 Daily 输入的栏目迁移

旧脚本的临时输入只能作为采集线索，迁移时还必须补齐原链、发布时间和抓取时间：

| 旧输入 | v1 主栏目 |
| --- | --- |
| `world` | `world` |
| `finance`、`markets`、TraderKing 摘要 | `finance` |
| Hacker News、The Verge | `technology` |
| AI digest、AI brief、论文 | `ai` |
| gurus、段永平 | `investors` |
| 央行、通胀、就业、增长与财政官方发布 | `macro` |

天气属于辅助上下文，不应为了填满六栏伪装成市场新闻。英文标题可提供中文摘要，
但必须保留原始标题所对应的直接链接；付费内容只保存元数据、短摘要和原链。

## 运行边界

```bash
python3 kol_dashboard/briefing_collect.py \
  --output /path/to/daily-briefing-latest.json \
  --import --db /path/to/kol_dashboard.db

python3 kol_dashboard/briefing_import.py /path/to/daily-briefing.json \
  --db /path/to/kol_dashboard.db
```

内置 producer 的资讯来源只限 HN Top/Best、AI Digest RSS 与 AI Brief RSS。
CLI 默认在采集后尝试生成中文阅读增强；模型未配置或暂时失败时会保留原始资讯，
以 `translation_status=unavailable` 明确降级，使用 `--no-ai-enrichment` 可关闭模型
调用但仍执行确定性内容分类。HN 两个榜单根接口都必须成功且至少产生一条当前
有效 story；两个 AI feed 的根地址也都必须抓取成功并解析为有效 RSS。若某个 AI
feed 成功完成扫描、但最近 24 小时没有新条目，这是有效空扫描，不应伪造条目或
阻断其他新资讯发布。根抓取或 RSS 解析失败仍会在写文件和导入前退出，保留
last-good 快照。网络读取同时受单请求和整批墙钟上限约束。中文增强只能使用采集
墙钟的剩余预算，且自身默认最多占用 24 秒（环境变量
`KOL_DAILY_ENRICHMENT_DEADLINE_SECONDS` 可调但硬上限 40 秒）；超时条目按
`unavailable` 降级，不得阻塞新快照。

producer 按一次采集、一次退出的 CLI 运行模型设计；`collect.sh daily` 每次都会启动
独立进程。生产 `deploy.sh` 会安装应用自管的 `kol-collect-daily.service/.timer`，
在每小时第 5 分钟加不超过 90 秒随机延迟后运行，并通过 `Persistent=true` 补跑
错过的周期。部署候选必须先成功执行一次 collector；快照验收继续要求部署期内的
当前数据、完整六栏目和至少一条 HN story，但不要求每个已成功空扫描的 AI feed
当天必须产出文章。不要在常驻 Web 进程内无限循环调用采集 library API，底层 DNS
或系统网络调用若无法取消，应交给下一次独立任务重试。

输出先写入同目录临时文件，完成 JSON 校验后再原子改名，然后执行导入。自定义
OpenClaw/Hermes producer、跨主机传输和 Slack 通知仍属于独立运维变更；若启用
外部 producer，应先与应用自管 timer 合并为单一写入链路或停用其中一个，避免
两个 producer 竞争覆盖同一天的完整快照。
