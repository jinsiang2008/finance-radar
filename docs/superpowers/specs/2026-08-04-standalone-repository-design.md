# Finance Radar 独立仓库设计

## 目标

将现有 KOL Dashboard、宏观风险雷达和关联决策台整理为公开、可克隆、
可测试的独立仓库 `jinsiang2008/finance-radar`，同时保留现有生产部署能力。

## 仓库边界

- `kol_dashboard/`：FastAPI 应用、前端、认证、数据层、关系引擎和测试。
- `lib/`：KOL、Serenity、宏观数据与风险雷达四个采集模块。
- `data/`、`logs/`、`private/`：仅保留说明或脱敏示例，运行数据由
  `.gitignore` 排除。
- 根目录提供 README、Python 依赖清单和测试命令。

## 路径与配置

- 本地默认数据库、日志、持仓和缓存路径使用仓库相对路径。
- 所有生产路径仍可通过现有环境变量覆盖。
- 部署脚本从仓库内 `lib/` 打包采集模块，不再依赖外部工作区的采集器或脚本目录。
- `aliyun-ops` helper 保持外部、可配置的运维依赖，不复制密钥或主机配置。

## 安全边界

禁止提交数据库、WAL/SHM、日志、真实持仓、Serenity 抓取缓存、`.env`、
口令哈希、会话密钥、SSH 密钥和临时部署载荷。公开 API 与私人 API 的隔离
继续由现有测试覆盖。

## 验证与发布

在独立仓库根目录运行完整 Python 单元测试、Python 编译、JavaScript 与
Shell 语法检查，并执行秘密扫描。验证通过后创建 `main` 初始提交，添加
`https://github.com/jinsiang2008/finance-radar.git` 为 `origin` 并推送。

本次不添加许可证，不提交生产数据库，也不覆盖当前线上数据。
