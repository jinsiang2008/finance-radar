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

## 时效与去重

- `snapshot_date` 必须与 `generated_at` 的北京时间日期一致。
- 输入的 `source_as_of` 表示本批数据实际覆盖到的时间，不能用文件复制或导入
  时间代替。公共 API 将它单独投影为 `source_coverage_as_of`；页面内容自身的
  最新证据时间是 `content_as_of`（并保留兼容字段 `source_as_of`）。两者不能
  相互冒充：新扫描不能把旧文章标新，空扫描也应明确显示“已扫描、暂无新增”。
  每条记录的抓取时间不得晚于本批覆盖时间；发布时间与披露时间只容许 5 分钟
  时钟偏差。
- 只有 `published_at` 存在且位于最近 24 小时内的记录能进入新闻栏目；只有
  `fetched_at` 的记录标为 `fetched_only`，不得冒充发布时间。
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
python3 kol_dashboard/briefing_import.py /path/to/daily-briefing.json \
  --db /path/to/kol_dashboard.db
```

应先写入同目录临时文件，完成 JSON 校验后再原子改名，再执行导入。恢复或新建
OpenClaw/Hermes 定时任务、跨主机传输和 Slack 通知属于独立运维变更；仅部署
本导入器不会自动启用这些行为。
