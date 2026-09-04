# Finance Radar

Finance Radar 是一个面向投资研究的 FastAPI 仪表盘，组合了 KOL 动态、
宏观风险雷达、事件—资产关系链、市场反应验证和可选的私人持仓叠加层。

> 本项目用于研究与信息整理，不构成投资建议。

## 主要能力

- 聚合并去重 KOL 新闻与观点，包含 Serenity 等重点来源。
- 监控宏观风险、灰犀牛、黑天鹅和潜在市场机会。
- 用确定性规则生成可解释的事件—主题—资产关系。
- 将机制证据与 1D/3D/5D 市场统计验证分开呈现，避免把相关性写成因果。
- 严格处理时间可信度：缺失、无效或未来的发布时间默认隔离，不能进入决策。
- 通过服务端签名 Cookie 解锁私人持仓影响；公开 API 不返回持仓详情。
- 提供六栏每日情报、逐栏时效和跨来源事件去重；OpenClaw/Hermes 仅能通过
  受验证的离线 JSON 快照导入，公开 GET 不会触发任务或模型。
- 提供原子部署、SQLite 一致性备份和失败回滚。

## 目录结构

- `kol_dashboard/`：FastAPI 应用、前端、采集入口、部署脚本和单元测试。
- `lib/`：KOL、Serenity、宏观数据与风险雷达采集模块。
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
```

采集过程需要访问公开新闻和市场数据源。所有命令均可重复执行；数据库层会
处理去重和幂等更新。

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
python3 -m compileall -q kol_dashboard lib
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
