# Finance Radar

Finance Radar 是一个面向投资研究的 FastAPI 仪表盘，组合了 KOL 动态、
宏观风险雷达、事件—资产关系链、市场反应验证和可选的私人持仓叠加层。

> 本项目用于研究与信息整理，不构成投资建议。

## 主要能力

- 聚合并去重 KOL 新闻与观点，包含 Serenity 等重点来源；重点 KOL 约 15 分钟
  扫描一次，全量目录约 30 分钟完成一轮。
- “市场快讯”独立读取 Bloomberg Markets、WSJ Markets 和 WSJ Business 官方 RSS，
  每 5 分钟检查，支持铜、黄金、白银、铝、原油、天然气筛选；不要求消息提及某个 KOL。
- 提供私人公众号与播客阅读，来源、原文证据与模型摘要分开显示；发布和能力边界见
  [`docs/reading-production.md`](docs/reading-production.md)。
- 监控宏观风险、灰犀牛、黑天鹅和潜在市场机会。
- 在宏观页分别为美股与 A 股提供试运行的减仓 / 防御审查闸门；规则只使用
  已完成日线和结构化金融压力，连续两个不同收盘日确认，新闻与 LLM 不直接
  决定行动。方法、阈值与失效边界见 [`docs/macro-alert-v1.md`](docs/macro-alert-v1.md)。
- 用确定性规则生成可解释的事件—主题—资产关系。
- 将机制证据与 1D/3D/5D 市场统计验证分开呈现，避免把相关性写成因果。
- 严格处理时间可信度：缺失、无效或未来的发布时间默认隔离，不能进入决策。
- 通过服务端签名 Cookie 解锁私人持仓影响；公开 API 不返回持仓详情。
- 提供登录后可见的股票期权研究准备度与私人承保政策台：用“数据 → 市场 → 标的
  → 资金”闸门解释为什么当前没有现金担保卖 Put 候选，并版本化保存接货 / 排除、
  最高接货价、研究预算和集中度约束；政策不能替代券商实时现金或权限，当前仍固定
  为 `research_only`、不生成虚构合约、不连接或执行交易。仓库内的 CSP 纯函数内核
  已对快照 / 合约身份、报价与事件时效、纽约交易日、宏观状态和账户容量做失败关闭，
  但在合法行情、事件、券商数据及政策适配层完成前不接 API。
- 提供六栏每日情报、逐栏时效和跨来源事件去重；OpenClaw/Hermes 仅能通过
  受验证的离线 JSON 快照导入，公开 GET 不会触发任务或模型。
- KOL 与 Daily 将“最新发布”和浏览器本机“待读重点”分开；后台检查先提示
  新增数量，不自动重排阅读中的页面。重要性、证据与 HN 社区热度边界见
  [`docs/news-attention-v1.md`](docs/news-attention-v1.md)。
- 提供原子部署、SQLite 一致性备份和失败回滚。

## 目录结构

- `kol_dashboard/`：FastAPI 应用、前端、采集入口、部署脚本和单元测试。
- `lib/`：KOL、Serenity、宏观数据与风险雷达采集模块。
- `research/`：不访问生产数据库的可复现离线研究工具；期权实验室方案与官方
  指数基线见 [`docs/options-lab-research-v1.md`](docs/options-lab-research-v1.md)。
- `private/holdings.example.md`：完全虚构的持仓文件格式示例。
- `data/`、`logs/`、`private/`、`.cache/`：本地运行数据，默认不纳入 Git。

## 本地启动

要求 Python 3.11+。

```bash
git clone https://github.com/jinsiang2008/finance-radar.git
cd finance-radar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp private/holdings.example.md private/holdings.md
./kol_dashboard/run.sh
```

默认访问 `http://127.0.0.1:8088/`。首次运行会在 `data/` 下创建 SQLite
数据库。

## 数据采集

```bash
./kol_dashboard/collect.sh kol
./kol_dashboard/collect.sh macro
./kol_dashboard/collect.sh decision
./kol_dashboard/collect.sh daily
python3 kol_dashboard/news_collect.py
python3 kol_dashboard/news_enrichment.py --limit 4
```

采集过程需要访问公开新闻和市场数据源。所有命令均可重复执行；数据库层会
处理去重和幂等更新。

### 权威市场快讯

界面入口 `/#news`（生产路径 `/kol/#news`），只读 API 为 `/api/news`，支持
`commodity=copper|gold|silver|aluminum|oil|gas`、`publisher=Bloomberg|WSJ`、
`limit=1..200`；不传筛选参数时返回全部来源。公开 GET 只读取缓存，不抓取网页、不调用模型。

`kol-collect-news.timer` 每 5 分钟执行独立采集器。三个固定 HTTPS RSS 并行获取，
验证 XML、发布者链接、日期和内容长度，使用 ETag/Last-Modified 条件请求；规范 URL
跨 WSJ 栏目去重。单源故障保留旧消息并标明错误；缺失、未来或无效日期不伪造为当前时间。
初次每源最多 60 条，初次及后续只发现最近 48 小时的消息，页面展示近 7 天，存储保留 14 天。
无日期消息保留明确标记，不进入自动 AI 处理。RSS 标题/简介不等于付费全文；缺简介时标为“仅标题”。

`kol-enrich-news.timer` 每 5 分钟唤醒独立 worker，实际模型处理按北京时间空闲 15 分钟、
高峰 60 分钟检查，每次最多 4 条，铜金等商品线索优先，只处理最近 24 小时且日期有效的消息。
峰谷沿用阅读模块中的用户指定规则及 2026 年节假日日历；不是对供应商未来价格的保证。
仅此后台单元加载现有 `deepseek.env`，不将凭据传给 Web 或 RSS 采集器。每条基于标题/简介
生成中文标题、概括、条件式影响和待核验点；事实卡不等待 AI。来源文字或日期更正后旧解读失效，
同一版本最多尝试 3 次，鉴权/余额/限流错误停止批次并退避，已有解读不重复付费生成。

新闻和解读保存在主 SQLite 的独立 `news_*` 表，不写入人物事件表或冒充完整的投资决策。
此版本不提供财联社自动采集、盘中行情突破检测、手机推送或付费全文读取；财联社结构化渠道仍待核验。
来源状态表示采集可用性，不能证明上游消息实时；发布时间、首次发现时间和初次导入标记
分别保留，不能把历史首次导入的差值算作持续运行延迟。

发布脚本将两个新 worker/timer 纳入停写、数据库备份和回滚。首次发布必须实际采集并验证三个
来源全部成功后才提交发布事务，默认不覆盖线上数据库或认证。新闻异常可先在来源状态查看，
再检查 `kol-collect-news.service`、`kol-enrich-news.service`，无需将采集与 AI 故障混为一谈。

宏观采集还会并发读取 SPY、沪深 300、VIX、USD/CNY 的 6 个月完成日线，
计算并保存有限的派生趋势数据；原始日线不会写入宏观快照。若任一核心输入
缺失、陈旧或带未来时间戳，仓位预警会停止升级并显示“证据不足”，不会把
缺数据解释成低风险。

## OpenClaw / Hermes Daily 快照

Daily 页可合并一个 24 小时内的结构化快照。生产者必须输出 v1 JSON；旧的
临时文本或 Slack 文稿不能直接入库，因为它们缺少可靠的发布时间、来源等级
和事件身份。完整字段与旧栏目映射见
[`docs/daily-briefing-v1.md`](docs/daily-briefing-v1.md)。

离线导入命令：

```bash
python3 kol_dashboard/briefing_import.py /path/to/daily-briefing.json \
  --db /path/to/kol_dashboard.db
```

导入器会限制文件和字段大小、拒绝私网或携带敏感参数的 URL、复核来源等级、
规范化链接并跨栏目去重。同一天重复导入只能向更新的 `generated_at` 与
`source_as_of` 前进。只有带时区的 `published_at` 会被视为已核验发布时间；只有
`fetched_at` 的线索会保留在快照中但不会进入新闻正文。投资披露可另外携带
`disclosed_at` 与 `effective_at`，页面会把披露时间和持仓截至时间分开显示。
API 还会分开返回内容证据时间与批次采集覆盖时间：空批次代表“扫描完成但暂无
新增”，不会被误报成任务未运行；重新抓取也不会把旧文章的证据时间刷新。
该命令不会运行 OpenClaw/Hermes、访问网络或调用 LLM，也没有对应的公网写 API。

生产部署会安装并启用应用自管的 `kol-collect-daily.timer`：每小时第 5 分钟触发，
附加不超过 90 秒的随机延迟，并在错过执行后自动补跑。它调用 `collect.sh daily`
采集 Hacker News Top/Best、AI Digest 与 AI Brief，校验后原子导入同一份 Daily
快照；DeepSeek 已配置时同时生成有证据边界的中文阅读增强。

## 私人模式

未配置认证变量时，私人 API 保持禁用。生成本地测试配置：

```bash
export KOL_DASHBOARD_PASSCODE_HASH="$(
  PYTHONPATH=kol_dashboard python3 -c \
    'import auth, getpass; print(auth.hash_passcode(getpass.getpass("私人模式口令: ")))'
)"
export KOL_DASHBOARD_SESSION_SECRET="$(
  python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
)"
export KOL_DASHBOARD_COOKIE_SECURE=false
```

`KOL_DASHBOARD_COOKIE_SECURE=false` 仅适用于本机 HTTP 开发。生产环境必须
使用 HTTPS 和安全 Cookie。真实持仓文件应放在 `private/holdings.md`，不得
提交到版本库。

常用配置：

- `KOL_DASHBOARD_DB`：SQLite 路径。
- `KOL_DASHBOARD_HOST` / `KOL_DASHBOARD_PORT`：监听地址与端口。
- `KOL_DASHBOARD_HOLDINGS_FILE`：私人持仓 Markdown 路径。
- `KOL_DASHBOARD_PASSCODE_HASH`：PBKDF2 口令校验值。
- `KOL_DASHBOARD_SESSION_SECRET`：至少 32 字节的会话签名密钥。
- `KOL_DASHBOARD_COOKIE_PATH`：生产部署默认 `/kol`。
- `KOL_DASHBOARD_COOKIE_SECURE`：生产环境应为 `true`。

## 测试

```bash
python3 -m unittest discover -s kol_dashboard/tests -v
python3 -m unittest tests.test_repository_contract -v
python3 -m compileall -q kol_dashboard lib research
node --check kol_dashboard/static/app.js
bash -n kol_dashboard/collect.sh
bash -n kol_dashboard/run.sh
bash -n kol_dashboard/deploy.sh
```

## 部署

`kol_dashboard/deploy.sh` 是针对 `zlstreet.xyz/kol/` 的站点部署脚本，依赖
可执行的 `VPS_HELPER`。默认部署只切换代码并保留远端数据库和认证配置：

```bash
VPS_HELPER=/path/to/vps.sh ./kol_dashboard/deploy.sh
```

- `--auth`：显式轮换私人模式口令和会话。
- `--db`：显式上传并覆盖远端数据库；此选项会先创建一致性备份。

成功发布时，脚本还会把升级前数据库以 root-only（目录 `0700`、文件
`0600`）方式保留在 `/opt/kol-dashboard/backups/`。如果回退到不兼容新
schema 的旧版本，必须同步恢复对应数据库，不能只切换 `current` 软链接。

不要把口令、会话密钥、数据库或真实持仓写入脚本或提交到 Git。

## 许可证

当前仓库未附加开源许可证。未经版权所有者明确许可，不授予复制、修改或再
分发权利。
