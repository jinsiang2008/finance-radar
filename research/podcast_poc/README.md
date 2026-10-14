# 播客与公众号阅读：本地 POC 与 KOL 集成

阅读器已集成到线上 KOL（2026-10-14 文档更新），入口为 `/kol/podcasts` 和 `/kol/wechat`。阅读正文、摘要、任务状态与已归档音频均使用现有 KOL 私人会话鉴权。远端后台复用服务器已有 DeepSeek 环境配置，将取得的文字发送到固定官方 API，按实际 tokens 计费；不读取本地 POC 的凭据文件，也不在服务器运行 MLX。

远端自动检查公众号 RSS，并发现 14 个固定播客源每源最新最多 3 集的元数据。公众号符合增量条件的新正文自动排队摘要；播客已有文字稿可手动生成中文摘要。Dwarkesh、Lex、Diary 的最新一集及固定目录中的历史精选可自动取得官方文字稿，后台返回 `official_latest_and_curated`；精选优先，与最新节目共享每日最多 3 个自动取稿名额，未入精选的历史不自动回填。首批涵盖瑞·达利欧、马斯克、黄仁勋；Freakonomics 仅接入指定的达利欧完整访谈，没有增加全栏目 RSS 订阅。**整集音频转写尚未启用**，`automatic_audio_transcription=false`。

最初免费 POC 在 Apple Silicon Mac 上以 MLX Whisper 转写、Qwen3 4B 生成中文摘要。旧摘要和人工复核保留，页面按每篇实际处理记录显示模型。本地 POC 仍可单独运行：正文、音频、任务库与模型缓存保存在 gitignore 排除的本机目录；本地选用 DeepSeek 时才按显式本地凭据文件、进程环境 `DEEPSEEK_API_KEY` 的顺序读取配置。下文安装和 CLI 命令均针对本地 POC，不是远端部署步骤。

## 英文重点来源（2026-10-13）

用户选择的六个频道：Dwarkesh、Lex Fridman、All-In、Nikhil Kamath、The Joe Rogan Experience、The Diary Of A CEO。Nikhil 分 People by WTF 与 WTF is 两个 RSS，因此是 6 个重点频道、7 个重点 RSS。原有 4 个中文源及 Latent Space、Acquired、Invest Like the Best 保留，共 14 个 RSS。默认显示英文重点，中文深链仍能直接打开。

来源清单固定在 `poc.py` 的 `FEEDS` / `FEED_DETAILS`，直接读取节目方经 Apple 目录核验的最终 HTTPS 地址，禁止跟随重定向；每次谷时检查最多取得每源最新 3 集，保留稳定 GUID 去重和历史文字稿。发现时跳过明确标记的 Trailer 和 Nikhil 印地语配音版本；部分未标记或英印混合内容仍可能存在。JRE、DOAC 题材广泛，当前没有 AI/投资主题自动筛选。

“订阅来源与更新”分别显示检查时间、上游节目日期，以及已收录、取得文字稿、生成摘要的数量。失败保留历史内容及上次成功时间；跨高峰中断时保存已经检查的来源，不伪造未访问来源的检查时间。RSS 连通不等于逐字稿可得。自动取稿仅支持 Dwarkesh 官网、Lex 官网实际文字稿链接和 DOAC RSS 指定的官方 VTT。DOAC 之前返回 403，2026-10-13 生产复测已返回 200；仍须处理后续读取失败。其他来源显示“待接入转写”，不承诺下一谷时完成。无全文或解析失败保持元数据，不根据简介生成全文摘要。

## 历史 POC 样本与范围（2026-10-03）

- Dwarkesh / Ilya Sutskever：公开官方整理稿，全部 133 个说话人段。并非独立验证音频逐秒覆盖，时间戳沿用官方稿。
- 硅谷101：本地样本 `sv101-6cc52c3259da-sample` 保存的来源是 `https://sv101.fireside.fm/267`，这里不依据 URL 推断节目编号。公开音频第 05:00–08:00 的三分钟节选，本机真实转写；只总结这个节选，不冒充整集。实际文件为 16 kHz 单声道 WAV，Whisper Small MLX 转写得到 84 段、耗时 8.8 秒；保留分段时间戳，没有说话人识别。
- Latent Space / Bret Taylor：已取得完整访谈文字稿，初始保持待摘要。官方稿从 02:05 开始，不含开头约两分钟引介。
- 8 档固定公开 RSS：增量发现并按 GUID 去重。仅有元信息的条目不生成摘要。试验选择历史完整英文稿用于可重复验证，不冒充最新节目。

历史运行结果见 `docs/podcast-poc-results.md`；当前内容与处理状态以页面及每篇处理记录为准，历史样本状态不代表远端实时状态。

**历史本地模型质量验收未达到无人值守要求。** 当时实测自动稿有语义误读、过度概括和 ASR 专名错误，两个展示样本由助手对照文字稿复核。页面明确区分复核稿与折叠保留的 `model_draft_summary`；人工复核不是模型自动完成，不能据此推断远端 DeepSeek 或自动流程的实际质量。

## 安装（Python 3.11 / Apple Silicon）

从仓库根目录执行。使用明确的 Python 3.11 路径，避免 macOS 系统 Python 3.9。

```sh
python3.11 -m venv .venv-podcast
.venv-podcast/bin/python -m pip install -r research/podcast_poc/requirements.lock
.venv-podcast/bin/python research/podcast_poc/setup_models.py
```

ffmpeg 需可执行，当前 POC 使用 `/opt/homebrew/bin/ffmpeg`。两套模型权重合计约 2.76GB；Python 依赖另占空间。固定模型 revision 记录在 `poc.py` 和 `setup_models.py`。

## 可重复执行的流程

```sh
# 查看最新节目。不会下载音频或自动转写。
.venv-podcast/bin/python research/podcast_poc/poc.py discover --limit 1

# 免费官网稿路径；不是从节目简介生成内容摘要。
.venv-podcast/bin/python research/podcast_poc/poc.py fetch-transcript \
  https://www.dwarkesh.com/p/ilya-sutskever-2 \
  --id dwarkesh-ilya-2 --show 'Dwarkesh Podcast'
.venv-podcast/bin/python research/podcast_poc/poc.py summarize dwarkesh-ilya-2

# 中文音频路径。先发现足够条目，从 private/podcast-poc/episodes 中选择实际 ID。
.venv-podcast/bin/python research/podcast_poc/poc.py discover --feeds sv101 --limit 8
.venv-podcast/bin/python research/podcast_poc/poc.py sample-audio sv101-6cc52c3259da --offset 300 --seconds 180
.venv-podcast/bin/python research/podcast_poc/poc.py transcribe sv101-6cc52c3259da-sample
.venv-podcast/bin/python research/podcast_poc/poc.py summarize sv101-6cc52c3259da-sample

# 查看真实结果，仅监听本机。终端关闭后需重新启动。
.venv-podcast/bin/python research/podcast_poc/poc.py serve --port 8767
```

浏览器访问 http://127.0.0.1:8767。选择节目 → 展开观点出处 → 搜索原文 → 在中文节选中点击时间戳回听。播放器显示节选相对时间，按钮显示原节目绝对时间。没有可用音频链接的官方稿显示原站入口。

`import-transcript /absolute/path.json` 支持导入显式提供的带时间戳稿件，字段见 `poc.import_transcript()`。获取器当前专门适配公开 Substack 的 Transcript/说话人时间码布局，遇到付费墙、验证页或布局变化会报错，不能保证任意网站可用。

## 公众号阅读（外部免费 RSS）

本地打开 `http://127.0.0.1:8767/wechat`，线上使用 `/kol/wechat`，顶部均可切换播客。初始 POC 的六家来源为财经（猫笔刀、集思录）、AI（机器之心、量子位）、技术（腾讯技术工程、美团技术团队）；当前订阅以 `wechat_sources.json` 为准。外部源来自 [Wechat2RSS 免费目录](https://wechat2rss.xlab.app/list/all)，并非按实时阅读量排序。

```sh
# 每个来源取发布时间最新的两篇，原始 RSS 和正文只保存在本机。
.venv-podcast/bin/python research/podcast_poc/poc.py wechat-fetch --per-source 2

# 每源最多一篇、总量六篇；只选正文至少500字且尚未完成摘要的文章。
.venv-podcast/bin/python research/podcast_poc/poc.py wechat-summarize --max-articles 6

# 也可以显式选择文章；文章 ID 来自 /api/wechat 或本地 articles 目录。
.venv-podcast/bin/python research/podcast_poc/poc.py summarize wx-ARTICLE_ID
```

页面“刷新列表”只读取对应后台已经保存的处理结果，不会立即抓取外部源。“安排摘要”提交持久化任务，有正文才能生成；同稿重复点击复用任务，失败可手动重试。下方分时调度负责周期抓取。以上本地批量 CLI 每源选一篇；直接 CLI 写入与后台共用进程锁，后台正处理时会明确拒绝并发写入。

`content:encoded` 有至少200个非空白字符且不是验证页时，标记“外部 RSS 正文”；首次取得的仅 description、太短或验证页内容保留为元信息，不生成全文摘要。该字符门槛只是 POC 可读性判断，**不是完整性检测**。摘要引用 p0001 等原文段落；HTML 去除活动内容，表格单元格保留分隔。未做图片 OCR，也未对照微信原页逐段验证。2026-10-03 的初始选样中，长赢指数投资因当时可得文章约40天未更新、偏图片而未入选；腾讯活动通知因不足500字被当时的批量 CLI 跳过，选择下一篇技术文。这些记录不代表来源当前更新情况。

已按微信原始 URL 的稳定字段去重，新取得的有效正文发生变化时旧摘要与复核失效。抓取失败保留已有文章；同篇来源暂时不再提供正文时，保留上次正文、对应摘要及其实际取得时间，另行记录本次检查时间并提示正在显示旧正文。来源日期与检查日期分别显示；14天提示阈值不等于真实停更或 SLA。第三方源并不保证所有群发、历史、时效或长期可用，范围参见 [提供商 FAQ](https://wechat2rss.xlab.app/deploy/qa)。历史验收和质量问题见 `docs/wechat-poc-results.md`。

## 公众号阅读界面（2026-10-08）

- 默认“要点速读”：文章标题、日期及复核状态后直接展示核心摘要和要点。模型信息、来源检查、完整性说明及原草稿放入折叠区域。
- “查看出处”展开要点引用的原文段落，可继续定位正文、查看前后文，再返回刚才的要点。长文引用无需逐页加载。
- “阅读原文”显示已取得的 RSS 文字，“微信原文”另开来源链接。前者不等于已验证完整的微信页面；未解析图片或核实文章主张。
- 大屏“对照阅读”暂时收起文章列表，让摘要与原文并排；手机通过模式切换阅读，并可返回清单。
- `?article=<id>&view=summary|original|compare` 保留文章与阅读模式。未生成摘要时同时提供正文入口和真实“安排摘要”按钮；排队、处理中、失败和完成由后端返回。后台轮询不重建未变化的正文，完成后保留阅读模式、证据展开与阅读位置。
- “文字已复核”显示保存记录中的复核日期，仅代表助手对照来源文字进行过修订；“模型初稿”仍需核对。复核不等于事实验证，也不属于自动处理流程。
- 播客与公众号共用同一组导航样式和触控尺寸。

## 完整度、复跑和边界

长稿按字符预算切块，DeepSeek 路径还计入最终 JSON 序列化开销，并对多块摘要分层合并；超长单段也逐字覆盖，不截取首中尾。每块和每层合并均保存结果，跨高峰暂停后复用已完成请求，汇总只使用实际存在的来源段落 ID；记录已处理块数及段数。结构校验不等于语义无误，数字、人名、否定及嘉宾主张仍需抽查。地方口音和专业术语可能识别错误，ASR 原始文字不会被模型静默改写。

输入、模型和 prompt 版本决定摘要缓存。同一内容重复执行复用结果；改变稿件则清除旧完成态。中途失败保留成功块，重试不重复推理这些块。页面中的 `partial` 指存在明确缺失或排除区域的文字稿；`full` 指所取得文字稿的全部段落，`sample` 明确指节选，`metadata_only` 不包含正文。

显式复核可使用 `apply-review <episode-id> <review.json>`。review JSON 包含匹配当前稿件的 `summary_signature`、修订后的 `summary` 及字符串数组 `issues`。版本不匹配会拒绝；原模型草稿保留，网页显示“文字已复核”及保存的复核日期。自动执行 `summarize` 不会产生复核标记。

CLI 仅供本机可信用户；本地 HTTP 只监听回环地址，写操作核对 Host、Origin、进程 token、JSON 内容和固定文章 ID。线上通过 KOL 私人会话授权读取数据和提交任务，写操作检查同源、动作头及有限 JSON 参数；worker 只请求配置中的 HTTPS RSS 及三个支持来源的固定官方文字稿/字幕地址，不接受任意抓取 URL，也不跟随重定向。线上数据保存在 `KOL_READING_DATA_DIR` 指定的持久化目录，凭据由服务器环境单独提供。此版尚无整集中文音频质量验证、自动说话人身份识别、跨节目日报去重或总金额硬预算。

## 线上分时检查与后台任务

- 远端由 `kol_dashboard/reading_worker.py` 的 systemd 后台运行，使用已有 DeepSeek 配置，不依赖本机开机或 Codex 运行。实际启用、worker 存活、最后检查及下一次检查以已登录的 `/kol/api/private/reading/status` 为准。
- 线上自动处理仅谷时运行：谷时每 60 分钟检查公众号 RSS，并刷新 14 个播客 RSS 每源最多最新 3 集的元数据。用户手动生成/升级单篇摘要可立即进入优先队列，不等谷时；自动抓取与总结在高峰排队。串行 worker 可能仍在处理前一篇，不承诺点击后同步完成。
- 北京时间周一至周五，排除国务院公告假期，`[09:00,12:00)`、`[14:00,18:00)` 为高峰；周末（含补班）全天空闲。已内置 2026 假期表；未知年份显示待更新，工作日白天保守按高峰处理。来源链接保存在 `schedule_policy.py`。
- 每源读取最多 20 篇，以初次启用的文章和发布时间为基线；补齐当时已有的待摘要文章，处理基线之后新文章或已处理文章的新正文。首次导入的更老档案仅供阅读和手动生成，不整库自动付费。RSS 自身可能延迟、缺篇或正文不完整。
- SQLite 保存任务，跨进程锁串行执行抓取/推理；进程中断后恢复，反复中断有上限；普通生成失败不自动循环付费。明确手动选择的单篇摘要/升级以持久 `on_demand=true` 立即处理，自动任务仍有 `not_before` 和 `deferred_reason`。手动刷新来源不授予整批文章高峰调用权限；旧 `manual` 标记也不等于即时许可。每次模型分块、重试、合并前重新核验本任务权限或谷时，已发出的请求允许结束；自动跨峰暂停不算失败，已完成分块和合并不重复付费。
- 生产新任务生成分主题中文详解、段落证据及单独标注的 AI 影响分析，不是逐段全文翻译。长访谈保留论证与案例，短文章不填充；投资/工作/行业影响给出机制、条件和观察项，未接入用户持仓或实时行情。播客仅有元信息时不生成摘要；已有官方稿、提供的文字稿、注明来源和覆盖缺失的第三方转录稿或明确标注的 ASR 节选可手动排队。Dwarkesh、Lex、Diary 最新一集与固定历史精选在谷时自动取稿，失败最早 24 小时后重试；同一输入与解读版本复用已有结果，普通摘要失败须手动重试。旧短稿不会整库自动重跑，可通过“生成详细解读”显式升级；新稿完成前保留原摘要及复核，新稿成功后旧版本仍可查看。自动音频转写尚未接入，DeepSeek 在当前流程中只接收文字。RSS 和本地 ASR 不产生 DeepSeek token 费用；节省来自文字摘要及摘要内的中译，当前官方输入/输出谷价均为峰价一半，不代表整套系统成本减半。自动稿始终待复核。实时新闻采集和新闻摘要维持原有频率，不受阅读队列限制。

## 本地独立运行与可选自动化

以下为保留的本地 POC 工具，不控制线上 systemd worker，也不表示本地定时任务已启用。

- 本地 `reader_control.py tick` 必要时启动回环服务、检查是否到期再提交任务，脚本本身没有 cron/定时器。若另行启用 Codex 自动化唤醒，本机和 Codex 需运行；休眠期间不抓取，恢复后仅检查一轮，不补跑所有错过的时段。实际启用情况需同时检查 `/api/wechat` 的 `automation.enabled` 和相应 Codex 自动化状态。
- 本地 `private/podcast-poc/runtime/automation.json` 仅保存 provider、频率、启用状态与自动化 ID，禁止放凭据。禁用本地自动化时同步把 `enabled` 设为 false；已开始的单个请求可完成，手动任务仍可运行。远端使用自己的 `production-config.json` 和环境配置，不读取此文件。

```sh
# 后台启动并检查；不返回 token 或密钥
.venv-podcast/bin/python research/podcast_poc/reader_control.py start
.venv-podcast/bin/python research/podcast_poc/reader_control.py status
.venv-podcast/bin/python research/podcast_poc/reader_control.py tick
# 显式立即抓取并为符合增量条件的文章排队
.venv-podcast/bin/python research/podcast_poc/reader_control.py check-now
```

仅需更新**本地 POC** 的 DeepSeek 密钥时，在本机交互终端执行下列命令。输入隐藏，不在聊天粘贴密钥。脚本只读验证官方账户接口，验证成功后保存到忽略目录 `private/podcast-poc/runtime/deepseek-credentials.json`（权限 0600）；不传给前端，不上传 Git，不发起生成。认证或网络失败不覆盖原配置。如需本地定时运行，验证后再启用已有本地自动化，不创建第二份定时任务。该命令不修改远端 DeepSeek 配置。

```sh
.venv-podcast/bin/python research/podcast_poc/configure_deepseek.py
```

## 验证

```sh
.venv-podcast/bin/python -m unittest discover -s research/podcast_poc -p 'test_*.py' -v
node --check research/podcast_poc/web/app.js
node --check research/podcast_poc/web/wechat.js
```

模型与实现依据：

- https://github.com/ml-explore/mlx-lm
- https://github.com/ml-explore/mlx-examples/tree/main/whisper
- https://huggingface.co/mlx-community/Qwen3-4B-Instruct-2507-4bit
- https://huggingface.co/mlx-community/whisper-small-mlx
