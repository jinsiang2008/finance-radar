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

worker 消费持久队列并在内部判断是否到期：线上仅谷时抓取与摘要，谷时每 60 分钟检查新内容，高峰仅排队；手动摘要同样等待谷时。每次 RSS 请求和每个 DeepSeek 分块、重试、合并前重新判断时段，正在进行的请求允许结束。已完成分块及合并缓存，暂停不计失败，不增加付费调用计数。状态提供 `off_peak_only`、`execution_allowed`、`next_run_at` 以及任务的 `not_before`、`deferred_reason`；不新增 systemd timer 或 Codex 自动化。机器上的运行状态与代码中的默认值应分别核验。公网代理、五个既有采集 timer 和 enrichment 唤醒 path 沿用现有流程。

系统 `/usr/bin/python3` 需要既有 FastAPI/Uvicorn，以及 `beautifulsoup4>=4.12,<5`。Ubuntu 的兼容 `python3-bs4` 包可满足此范围。发布在停止旧服务之前执行 `import bs4, reading_service`；缺依赖直接失败，脚本不会隐式运行 pip、apt 或系统升级。

## 发布包与可选内容导入

平铺应用包增加 `reading_service.py`、`reading_worker.py`、`reading_import.py`。共享代码保留 Python namespace 路径 `research/podcast_poc`，只复制：

- 运行文件：`poc.py`、`wechat.py`、`reading_jobs.py`、`schedule_policy.py`、`inference.py`、`wechat_sources.json`。
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
- 完成后的实际发布、测试和摘要元数据见 `offpeak-reading-production-verification.json`。上述配置不代表已自动下载或转写其他播客整集音频。
