# KOL 私人阅读的发布契约

本说明描述代码中的发布与回滚流程，不单独证明生产已发布。生产验收记录应以实际发布结果为准。

## 进程、配置与依赖

阅读页面和 API 由现有 `kol-dashboard.service` 提供，沿用 KOL 私人模式会话。唯一新增常驻单元为 `kol-reading-worker.service`，以 `kol-dashboard` 用户运行：

```text
WorkingDirectory=/opt/kol-dashboard/current
ExecStart=/usr/bin/python3 /opt/kol-dashboard/current/reading_worker.py
KOL_READING_DATA_DIR=/opt/kol-dashboard/private/reading
KOL_READING_ENABLED=1
KOL_DASHBOARD_DB=/opt/kol-dashboard/data/kol_dashboard.db
```

worker 从现有 `/etc/kol-dashboard/deepseek.env` 获取凭据；该文件必须是 `root:root 0600` 的普通文件。发布脚本只检查文件属性，不读取、复制、改写或创建凭据。Web 不加载此环境文件。原 Daily/enrichment 后台单元继续按原契约使用它。worker 禁止 core dump，日志为 `/var/log/kol-dashboard/reading.log`、`reading.err.log`，权限 `0600`。

worker 消费持久队列：自动抓取与摘要仅在谷时执行，谷时每 60 分钟检查新内容；用户在正文上手动生成或升级单篇摘要则立即进入优先队列，不受谷时限制。串行 worker 不抢断当前正在处理的另一篇，因此立即入队不等于同步返回。手动刷新来源仍等谷时，发现的文章不会继承单篇付费许可。每次 RSS 请求重新判断时段，每次 DeepSeek 分块、重试、合并重新核验当前任务的单篇许可或谷时；正在进行的请求允许结束。已完成分块及合并缓存，自动任务跨峰暂停不计失败、不增加调用计数。状态中的 `off_peak_only`、`execution_allowed`、`next_run_at` 描述自动策略，`manual_execution_policy=immediate`、`manual_execution_scope=single_summary` 描述单篇手动策略；任务仍提供 `not_before`、`deferred_reason`，只有 `on_demand=true` 能豁免谷时。没有新增 systemd timer 或 Codex 自动化；公网代理及既有采集/enrichment单元沿用原流程。

系统 `/usr/bin/python3` 需要既有 FastAPI/Uvicorn，以及 `beautifulsoup4>=4.12,<5`。Ubuntu 的兼容 `python3-bs4` 包可满足此范围。发布在停止旧服务之前执行 `import bs4, reading_service`；缺依赖直接失败，脚本不会隐式运行 pip、apt 或系统升级。

## 发布包与可选内容导入

平铺应用包增加 `reading_service.py`、`reading_worker.py`、`reading_import.py`。共享代码保留 Python namespace 路径 `research/podcast_poc`，只复制：

- 运行文件：`poc.py`、`wechat.py`、`reading_jobs.py`、`schedule_policy.py`、`inference.py`、`official_transcripts.py`、`detailed_summary.py`、`wechat_sources.json`。
- 八个界面文件：`index.html`、`wechat.html`、`app.js`、`wechat.js`、`style.css`、`wechat.css`、`reader-auth.js`、`reader-auth.css`。

不复制 tests、private、runtime、模型、虚拟环境、`configure_deepseek.py` 或本地服务控制脚本。文章、音频、队列与凭据都不进入代码 release。

如需迁移已有阅读样本，可为同一次 `kol_dashboard/deploy.sh` 提供 `KOL_READING_SEED_ARCHIVE`。该归档单独上传到 root-only 发布暂存区的 `reading-seed.tgz`，不放进 `app.tgz`、release 或 Git。格式与字段由 `reading_import.py` 的白名单检查；允许的内容仅为 `articles/*.json`、`episodes/*.json`、`media/*.wav`，不包含运行状态、凭据或缓存。原文/音频是否可迁移仍应在制作归档时确认。

全部 writer 停净、阅读队列备份完成之后，切换 `current` 和启动 worker 之前，事务内以 root 执行：

```text
/usr/bin/python3 <new-release>/reading_import.py \
  --archive <root-only-stage>/reading-seed.tgz \
  --data-dir /opt/kol-dashboard/private/reading --owner kol-dashboard
```

导入只增加缺失记录，不覆盖已有文章或媒体；新目录 `0700`、文件 `0600`，归属 `kol-dashboard`。现有主站数据库和认证文件不属于阅读种子导入范围。正常发布不传 `--db` 或 `--auth`，保留原数据库和认证。

## 停写、备份与回滚

阅读目录固定在 `/opt/kol-dashboard/private/reading`，目录与 `runtime` 为 `0700`。部署会拒绝这两个位置的符号链接或非目录，阅读队列也必须是普通文件。持久队列独立于主站数据库：

```text
/opt/kol-dashboard/private/reading/runtime/reading-jobs.sqlite3
```

新 worker 纳入原有 unit enablement/activity 记录、unit 文件备份、发布前 stop、精确停止状态检查、回滚 stop 门禁、配置恢复和旧状态恢复。只有全部相关单元确认 `inactive`、`failed` 或 `not-found` 才能恢复数据库；未知状态或仍在退出的 writer 会保留失败现场及恢复材料，不冒险覆盖数据库。

已有阅读队列先在全部 writers 停净后，以 `kol-dashboard` 身份完成 `wal_checkpoint(TRUNCATE)` 和完整性检查；checkpoint 未完成则拒绝继续。root 随后用 immutable 只读连接及 SQLite backup API 创建包含已提交 WAL 内容的一致快照，将备份转为无需 WAL/SHM 文件的 DELETE journal 格式，校验并 fsync。运行中队列与回滚候选的完整性检查始终以 `kol-dashboard` 身份执行，并检查队列及 WAL/SHM 文件属主，避免 root 创建服务用户无法写入的 sidecar。immutable 只用于已停止且 checkpoint 完成的源，不用于运行中的数据库。

失败发布在停净 writers 后恢复发布前队列；首次发布前没有队列时保留新建的阅读数据，不删除文章、音频或凭据。内容导入本身不覆盖旧数据，因此回滚代码时也不删除已导入内容。旧版本没有 reading unit 时，恢复原先“未安装、未启用、未运行”的状态。

成功发布保留两类 root-only、`0600` 的发布前备份，均位于 `/opt/kol-dashboard/backups`：

- 原主站数据库：`database.before-<release-id>.sqlite3`。
- 发布前已存在的阅读队列：`reading-queue.before-<release-id>.sqlite3`。

这些是发布事务的回退点，不能替代文章与媒体的长期备份，也不能保证撤销已发送到模型供应商的付费请求。人工回滚前仍需先停止相应 writers，并将旧代码与兼容数据库一起恢复。

## 验收

既有直连健康、Nginx 代理、Daily 候选采集、五个采集 timer 和 enrichment path 验收保持。新增 worker 必须 enabled/active，且 `runtime/worker-heartbeat.json` 为 `state=running`、更新时间不超过 120 秒、PID 与 systemd MainPID 一致；随后检查阅读队列完整性，才提交发布事务。

私有页面登录边界、正文/证据引用、摘要任务实际成功与移动端体验另由真实 API 和浏览器验收；unit active 或心跳正常不代表模型生成一定成功。

本地部署契约测试不访问服务器，不读取任何凭据：

```bash
.venv-podcast/bin/python -B -m unittest discover -s kol_dashboard/tests -p test_deploy_contract.py
bash -n kol_dashboard/deploy.sh
```


## 生产验收记录（2026-10-08 23:17 CST）

用户明确要求将两个 POC 整合到 KOL 并使用远端 DeepSeek 后，已通过本脚本部署；最终 release 为 `20261008T151130Z-42886-3685dee3`。阅读资源版本 v41，主站资源维持 v40。主站、Daily、Nginx、五个原有采集 timer 和 enrichment path 的发布验收通过，新增阅读 worker 已启用并有匹配 PID 的有效心跳。代码来自 `codex/podcast-free-poc` 工作树、基于 `da8f453` 的未提交更改；本轮未推送 GitHub。源码指纹与不含正文的验收记录见 `reading-production-verification.json`。

- 已私有导入 12 篇文章、18 条播客记录和 1 个音频样本；随后真实抓取取得 122 条公众号记录。现有 68 篇摘要，其中 62 篇由远端 `deepseek-v4-flash` 新生成，保留旧有 6 份摘要及其中 3 份助手复核。队列 63 项全部完成；53 篇较旧的正文保持按需生成，另 1 条只有元信息，没有把整个历史库自动送入模型。
- 6 个公众号源本轮均成功读取，其中腾讯技术工程、美团技术团队标记为内容较旧。播客共 28 条：2 份既有摘要、1 份待摘要文字稿、25 条节目信息；本轮 8 个播客源中 6 个成功，未成功的源保留旧记录。
- 空闲间隔 60 分钟、高峰 240 分钟，按用户给定的北京时间工作日及节假日规则判断。服务器进程独立于本机运行；已删除旧的暂停状态 Codex 阅读 heartbeat。本机 8767 POC 保持自动处理关闭，重启后两页及新资源均 200；临时 8768 验收实例已关闭。
- 两页公网匿名验证、v41 资源、CSP、手机布局通过；私人数据和音频未登录返回 401/no-store。服务器内部认证请求通过，音频 Range 返回 206，所有已完成摘要的引用均对应实际原文段落 ID。真实本机登录浏览器验证了原文对照、证据跳转、滚动保留和跨页面退出清空。
- 验证共 870 项 KOL 测试、86 项 POC 测试、3 项源代码仓库契约，以及真实／模拟浏览器检查。第二次发布实际使用了阅读队列备份和恢复材料保留流程，队列和内容未被清空。

边界：公众号文字来自外部 RSS，未与微信原页及图片逐项核对；模型输出仍是待复核初稿。引用编号完整性不等于语义正确性，正文片段导出语义抽查被自动审批拒绝，未进行该导出。播客目前自动发现节目、手动总结已有真实文字稿；尚无服务器自动获取文字稿或整集 ASR，也不提供逐段全文翻译。

## 2026-10-13 谷时阅读与孙宇晨

- 使用官方北京时间规则：工作日（法定假期除外）09:00–12:00、14:00–18:00 为高峰；其他时间为谷时。2026 假期表显式维护，未知年份的工作日白天保守不处理。模型 token 的输入和输出谷价为峰价一半，RSS 和本地语音识别本身不消耗 DeepSeek token；实时新闻现有采集与解读频率不变。
- `justinsun` 进入 KOL 热点组，随现有 15 分钟采集批次扫描。官方 X `@justinsuntron` 和媒体搜索并行覆盖，X 失败保留告警、继续新闻。人物提及和 TRON 关联报道分别分类，不把公司消息归为本人说法。
- 已确认的邵艾伦 × 孙宇晨访谈使用原始 YouTube 节目链接；本次取得的是公开第三方转录，非官方字幕。来源 commit/checksum 与版权归属保存在 `research/podcast_poc/sources/alan-justinsun-20261010.json`，完整文字只进入私人阅读目录，不进入 Git。
- 适配器排除了来源自行标注疑似不符原音频的 00:28–00:33 转录块；其他重叠和说话人不确定性保留，`source_kind=third_party_transcript`、`coverage.scope=partial`。不补齐缺失音频、说话人或发布日期。自动摘要是待复核初稿，每条观点仍须有已收录段落 ID。
- 该单集通过受控发布的 `KOL_READING_SEED_ARCHIVE` 导入，只创建不存在的记录，不覆盖已有阅读数据。`import_public_interview.py` 仅在本地校验固定来源并制作私有临时归档，不是自动追踪该 YouTube 频道的订阅器。
- 本期实际处理 14 个分块；模型初稿的 8 个要点偏向前半程，经过明确的文字对照复核补齐后半程，并保留婚姻观点的前后条件。通过既有 `apply_review` 保存修订与原模型初稿，页面标注“摘要已对照转写复核”，没有改写转录或声称完成音频听校；其他自动稿仍待复核。
- 实际发布、测试和摘要元数据见 `offpeak-reading-production-verification.json`。上述配置不代表已自动下载或转写其他播客整集音频。

## 2026-10-13 英文重点播客订阅

用户选择 Nikhil Kamath、Dwarkesh Patel、PowerfulJRE、Lex Fridman、The Diary Of A CEO、All-In 为英文重点。Nikhil 的 People by WTF 与 WTF is 分成两个固定 RSS、同一个频道组；保留原有中文和英文补充源，共 14 个 RSS、6 个重点频道。

- 固定源使用经节目方及 Apple 目录核验的最终 URL；保留 Dwarkesh 旧 GUID 源避免重复。读取仍只接受配置 URL、不跟随重定向，40,000,000 字节上限能容纳 JRE / DOAC 的大 RSS。
- 每次读取按可解析且带时区的发布日期排序（兼容 JRE / DOAC 的 RFC 5322 `-0000` UTC 标记）、GUID 去重，最多每源新增/检查最新 3 集；排除明确 Trailer 标记及 Nikhil 印地语标题/配音重复。英印混合节目仍可能出现；没有自动主题筛选，也没有历史全集回填。
- 私人阅读页面默认英文重点，可切中文、英文补充、全部或单一来源；已有中文单集链接保持可用。折叠来源面板分别显示最近检查、源内最新节目、已收录/文字稿/摘要数量，不把 HTTP 成功代表为全文可用。
- 来源失败保留旧节目、摘要及上次成功时间；空 RSS 明示暂无节目。每完成一个源立即保存健康状态，跨高峰暂停不抹掉已完成检查，也不更新未访问来源的时间。旧记录只在响应中补来源标签，不重写原稿或复核。
- 谷时每 60 分钟的调度和已有文字稿摘要方式保持；本次发现不发送模型请求。Dwarkesh / Lex 部分官方稿可访问，但服务器自动全文抓取仍未实现；DOAC 官方 VTT 本次公开读取返回 403，不能当作可用全文。

远端固定源连通性证据见 `english-podcast-source-probe-20261013.json`：7/7 返回 200，其中 All-In 完整读取耗时112秒。socket timeout 是单次等待限制，不是总请求时长保证；单次可用不代表持续可用或 YouTube 与 RSS 发布完全同步。发布后实际采集、服务、代码指纹与匿名鉴权验证单独记录在 `english-podcast-production-verification.json`。

最终生产验收：2026-10-13 23:36 CST，应用提交 `bc8f0471fb090d73466c18161fa3bf76730bcf08`、release `20261013T153016Z-30479-1624964b`；54 个运行文件指纹匹配，阅读资源 v43。首次发现新增18集元数据并逐字节保留原35条，最终库内53集；7/7重点RSS成功、日期均可显示，全部14源中原“晚点聊”因302仍失败且保留旧数据。982主站、122阅读、3仓库测试及公网浏览器验收通过；私人接口保持401/no-store。

第二次发布在Daily上游RSS间歇故障时被既有门禁拒绝，回滚曾报告未完成。之后逐项核验10个unit状态、20个配置文件、两个数据库完整性/权限、Nginx和HTTP健康均恢复；正式Daily重试成功，再执行正常发布，全部门禁通过。该次恢复材料仍保留在生产 `.staging/20261013T151552Z-28133-a7139fa5`，未删除或绕过检查。上游间歇性不因本次成功而消失，后续可单独评估有界RSS重试；本次未扩大Daily代码范围。

## 官方文字稿自动处理（2026-10-14 代码契约）

- 同一 worker 锁内，RSS 更新完成后处理 Dwarkesh、Lex、Diary 各自最新且不晚于当前时间的已收录节目；每个北京时间自然日最多给 3 个不同节目安排取稿，预约额度持久化，重启不重置。不会逐小时回填每源历史 3 集。已有文字稿和复核稿不覆盖。
- Dwarkesh 从显式 Transcript 区域提取；Lex 跟随节目页内真实同域文字稿链接；Diary 只接受 RSS 明确声明的官方 VTT。URL 限定 HTTPS、精确主机和路径，无凭据/端口/重定向；单页 4 MB 上限，每次网络请求前重新检查谷时。
- 官方稿原子合并到原节目，保留 GUID、音频、发布日期等 RSS 身份，并记录独立文字稿 URL、SHA256、adapter 版本。章节级时间码明确注明精度；字幕保留实际起止时间。官方稿/字幕并不等于已逐字听校。全文只进私人阅读目录。
- 成功保存正文后使用既有版本去重队列生成中文要点和段落证据；保存与排队间中断可恢复。取稿失败保持元数据、最早 24 小时后免费重试；不会因此循环重试付费摘要。字幕地址更新可重新尝试。
- 页面区分等待取稿、谷时等待、正在读取、来源暂不可读、历史未自动处理、待接入转写。All-In、JRE、Nikhil 及其他未支持源仍只自动发现新集，尚无服务器整集 ASR；不能承诺到谷时就生成摘要。
- 本节是实现契约；真实发布与端到端结果见 `podcast-transcript-production-verification.json`。

### 发布中发现的既有 Daily RSS 时限问题

2026-10-14 首次发布因既有 Daily 根 RSS 超时被正常门禁拦截，并完成自动回滚。生产只读探测确认两个约 190–203 KB 的根 RSS 有 HTTP 200、连接快而正文约 6 秒的情况，也有偶发更慢传输。`briefing_collect.py` 为这两个根请求引入独立 12 秒预算；HN 和普通条目仍为 4 秒，全请求共享默认 40 秒整轮上限。只对根 RSS 的墙钟超时最多重试一次；不重复整个采集、不重复模型调用，不对解析、大小或 HTTP 错误重试。必须取得并解析有效根 RSS 的发布门禁保持。

### 实际生产发布与验证（2026-10-14）

应用提交 `549af10efa8c9e9c7a7103dbe90e36cdb87e8e26` 已通过正常发布流程，release 为 `20261013T163030Z-42229-fa4309b5`。55 个运行文件指纹一致，阅读资源 v44；4 份既有文字稿文件逐字节保持，主站、阅读 worker、Nginx 和相关定时器健康。五条 Bloomberg/WSJ 源、跨页新闻上下文、Daily 根 RSS 与 API、公网健康均通过发布门禁。

1011 项主站、138 项阅读、3 项仓库契约测试通过。真实 Chrome 验证了新取稿状态、峰谷切换时保留阅读位置、重试时间说明、安全出处链接和手机布局；公网 v44、匿名正文隐藏、私有接口 401/no-store 通过。第一次公网主站检查等待人物筛选项超时，隔离上下文复查首次加载成功，未确定瞬时网络或服务端延迟原因。

上线后已通过既有去重队列启动一次真实刷新。最新正文获取、摘要状态、模型用量和证据引用计数以 `podcast-transcript-production-verification.json` 中带时间戳的结果为准。每小时检查不是摘要完成时限：RSS 串行读取、上游故障与长节目分块都会延后处理；每日 3 集是不同节目的取稿预约额度，失败也占用额度，不能解释为每日保证完成 3 份摘要。

首轮端到端验收于 2026-10-14 00:49 CST 完成：Dwarkesh 的 Si Sheppard 一期、Lex 的 Andrew Scull 一期、Diary 的 Eric Weinstein 一期都成功取得官方稿并生成中文初稿，分别处理 59/431/1521 个原文段落、11/16/9 个分块，各有 8 条中文要点，9/19/24 个引用均对应已存段落。队列无运行或等待任务，自动摘要仍待人工语义复核，未独立核验音频。Dwarkesh 本轮 RSS 曾返回 502，但保留的最新节目身份及独立官方正文读取成功；不能把一次端到端通过解释为所有上游持续可用。

## 详细阅读与影响分析（2026-10-14 实现契约）

长访谈的生成目标为约 1800–3000 字中文详解，分块时保留论据、案例、数字、条件、反例及观点差异；短文章不按固定条数填充。`detailed-v1` 保留 `overview` 和有原始段落引用的 `points`，新增独立 `impacts`，包括投资、工作或行业的影响机制、不确定性和可验证观察项。界面固定标注 AI 推断，引用只表明推断的原文起点，不能冒充嘉宾原话或已证实结论。未查询用户持仓、实时行情或企业最新经营数据，不生成个性化买卖指令。

生产 worker 对尚无摘要的新任务使用详细格式，普通刷新仍复用旧完成稿；旧稿须显式提交 `{"id":"已有内容ID","upgrade":true}` 升级，同一版本重复请求复用任务。升级意图保存在 SQLite；2026-10-14 后续按用户要求改为显式单篇立即处理，自动任务和来源刷新继续等待谷时，详见下节。

升级期间继续显示原摘要和原复核记录，进度独立保存。只有新稿完整生成并通过引用与格式校验后才切换，旧摘要、复核和可确认对应的原文进入私人历史记录；新稿重新标为待复核。无法确认旧证据版本时仍保留旧摘要，但不把当前正文误当历史证据。失败或谷时暂停不丢原稿，已完成分块和合并继续复用缓存。

原文与历史正文只存私人阅读目录；Git 仅保存代码、说明和不含私有正文的验收元数据。

### 发布与峰时排队验收（2026-10-14 11:32 CST）

应用提交 `48e12f16b507b9101ac24979c93c1c3cf9827c42` 已推送 main，并通过既有原子流程发布为 `20261014T032459Z-23043-20870da5`。56 个运行文件与本地指纹一致，阅读资源 v45；主站、阅读 worker、Nginx 及既有采集/新闻富化单元正常。发布内 Bloomberg/WSJ 五个源、跨页新闻、Daily 和公网健康门禁均通过。

1012 项 KOL 测试、158 项阅读全套测试通过；最后增加本地旧版请求保护后，64 项相关测试通过（详细契约共 20 项）。干净暂存导出的 3 项仓库契约及编译检查通过。真实 Chrome fixture 验证详细内容、三类影响、旧稿保护、历史证据隔离、谷时等待与手机布局；公网两个阅读页和六个资源均 200，三私有 API 均 401/no-store，1440/390/320 px 无横向溢出。

仅选择 Diary 的 Eric Weinstein、Dwarkesh 的 Ilya、腾讯技术工程的 AI 接入文章和一条 267 字的财经短讯升级。11:31 入队时仍为高峰，四个任务均 queued、attempts=0，重复请求返回同一任务，原摘要及复核字段完整保留；预计最早从当日 12:00 CST 谷时开始处理，不能解释为 12:00 已完成。其他旧稿不整库重跑。

本次验收阶段是 **已部署、峰时排队已核验**。真实详细稿尚未生成，篇幅、模型用量、语义支撑及生成后历史保留仍待谷时结果验证；测试夹具通过不能替代真实模型质量验收。不含正文的证据见 `detailed-reading-production-verification.json`。

## 手动单篇即时处理（2026-10-14 后续修订）

用户反馈谷时等待影响阅读，明确要求手动触发立即调用、自动处理继续谷时。SQLite 新增 `on_demand INTEGER NOT NULL DEFAULT 0`；只有认证后的单篇摘要或升级入口授予该权限，客户端不能自带执行权限。旧 `manual` 曾被手动来源刷新传播给批量任务，因此仅保留其优先级用途，不批量给历史记录授予豁免。显式点击可提升同一输入版本的既有排队任务，重复点击继续去重。

worker 通过仅覆盖当前任务生命周期的上下文，在每次实际模型请求前重新读取 SQLite 的单篇权限；任务退出或异常后清除上下文，下一篇自动任务不会继承。运行中提升、跨峰暂停、重启恢复及缓存均按同一持久身份处理。来源抓取的谷时门禁保持，手动刷新发现的文章也只入自动队列。

界面将自动等待改为短说明和可点的“立即生成”，已有摘要和正文继续可读；已手动排队/运行时禁重复提交并说明前方任务可能影响开始时间。没有增加确认弹窗，也不在打开页面时自动发起付费请求。

首轮真实详细稿暴露的两处限制同步修复：短字幕主题确实引用32段时原30段上限误拒，现有限放宽至64并继续校验每个来源ID；长文合并的5000输出token上限导致provider返回截断，现中间9000/最终12000。完整分块的输入和缓存身份保持，可复用已有7块，不整篇重算；最终提示变化按提示hash单独缓存。短文最终校验限制1–3主题、最多2项影响、概述/要点最多600字符，并去除元信息扩写及把标题日/事件日差异误写为矛盾的提示。

本节是当前实现契约，真实发布、请求执行与详细稿结果另行记录。
