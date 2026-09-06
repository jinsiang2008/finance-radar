# 股票期权实验室 v1：研究、产品与实现方案

## 1. 执行结论

Finance Radar 可以增加一个登录后可见的“期权实验室”，首期聚焦熟悉的美股和
ETF，核心能力是：

1. 发现**现金担保卖 Put（Cash-Secured Put, CSP）候选**；
2. 识别“当前没有合适机会”，并说明被哪条风控拦截；
3. 分开监控 put-call parity 等**可执行定价偏差**，不把高权利金包装成套利；
4. 对候选做历史、压力和接货情景分析；
5. 与宏观仓位预警联动，在风险升高时自动收紧或停止新增短 Put；
6. 只给研究与人工复核建议，首期不连接券商下单。

产品的第一原则不是“每天必须给一单”，而是“只在数据、现金、接货意愿、流动性
和组合容量同时满足时才出现候选”。任何一项关键输入缺失，系统必须返回
`abstain`，并列出需要补齐的数据。

## 2. 为什么必须把“卖 Put”和“套利”分开

### 2.1 现金担保卖 Put 是风险溢价策略

[OIC 的现金担保卖 Put 说明](https://www.optionseducation.org/strategies/all-strategies/cash-secured-put)
将其定义为价格敏感型股票获取策略：投资者收取权利金，同时承担在行权价买入
股票的义务。最大收益是权利金；如果股票跌至零，损失接近行权价减去权利金，
并不具备无风险套利结构。[SEC 的期权投资者公告](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-63)
也强调标准美股期权通常对应 100 股，卖 Put 会产生买入标的的义务。

因此页面应使用：

- “卖 Put 风险补偿候选”
- “愿意接货价”
- “接货后组合风险”
- “尾部损失情景”

而不是使用“稳健套利”“保本收益”等表述。

### 2.2 真正的定价异常是另一条扫描器

[OIC Put-Call Parity](https://www.optionseducation.org/advancedconcepts/put-call-parity)
和 [CME 的 parity 教程](https://www.cmegroup.com/education/courses/introduction-to-options/put-call-parity)
给出同一到期日、行权价下股票、Call、Put 与无风险现金流之间的关系。真实市场
需要再扣除分红、借券、融资、提前行权、买卖价差、手续费和四条腿无法同时成交
的风险。

只有同时满足以下条件，系统才允许显示“待人工复核的定价偏差”：

- 股票和所有期权腿来自可合法使用的同步 NBBO，时间差在严格上限内；
- 用买入腿的 Ask、卖出腿的 Bid 计算，不能用 Mid 制造纸面利润；
- 可见深度覆盖拟交易数量；
- 已计入分红、利率、借券费、佣金、监管费和保守滑点；
- 美式期权的提前行权边界已经进入模型；
- 净边际仍高于预设安全垫。

零售级延迟快照通常只能用于发现线索，不能证明存在可执行套利。

## 3. 首期标的范围与隐私边界

仓库中的初始列表只是一组**通用流动性研究池**，不从私人持仓反推，也不代表
用户当前持有、偏好或适合交易。真正的“熟悉池”只能在登录后，由用户显式确认的
私人配置与新鲜账户快照在运行时生成；不得写入 Git、静态 HTML、公开 API、URL、
浏览器持久存储或应用日志。

任何旧持仓资料都不能直接用于 2026-09-06 的个性化下单建议。实时上线前必须
重新同步持仓、现金、已有期权仓位、账户权限和愿意接货价。

### 3.1 v1 通用流动性研究池

| 层级 | 标的 | 初始定位 |
| --- | --- | --- |
| 核心 ETF | SPY、QQQ | 仅作流动性与指数策略研究样本 |
| 核心大盘股 | MSFT、GOOGL、META、NVDA、JPM | 仅作单股事件、流动性和集中度模型样本 |
| 半导体扩展 | AMD、AVGO、QCOM、TSM | 加强财报、行业相关性与跳空压力测试 |
| 高波动观察 | TSLA、HOOD、PLTR、MU、BABA | 默认更严阈值；组合已有高暴露时禁止继续卖 Put |

首期不支持杠杆 / 反向 ETF、VIX ETP、OTC、退市或极低流动性标的，也不支持
调整后特殊交割物合约。诸如 NVDL、TSLL、SOXL、MUU、DRAM、YINN、VXX、SPXS
一类产品不能因为权利金高就进入候选池。

### 3.2 A 股边界

A 股首期只做 ETF 期权研究，不虚构单只股票期权。上交所公开产品包括 50ETF、
300ETF、500ETF、科创 50ETF 和科创板 50ETF 等期权；深交所公开产品包括
沪深 300ETF、创业板 ETF、深证 100ETF 和中证 500ETF 等期权。产品与规则应以
[上交所最新合约公告](https://www.sse.com.cn/assortment/options/disclo/update/c/c_20260714_10825458.shtml)、
[深交所投教资料](https://www.szse.cn/www/investor/institute/rules/t20230309_599162.html)
和券商实时权限为准。上交所投教材料显示，个人投资者进行保证金卖出开仓需要
相应高级别权限；系统不能把“有账户”推断成“可以卖 Put”。

## 4. 用户看到什么

### 4.1 竞品研究转化为任务流

- [Fidelity](https://www.fidelity.com/options-trading/tools) 将 Strategy Builder、
  P/L Calculator、Evaluator、Probability Calculator、期权链和持仓管理拆开，
  说明“发现、测算、执行、管理”不应塞进一张大表。
- [Schwab thinkorswim Risk Profile](https://international.schwab.com/story/analyze-vertical-spreads-with-risk-profile-tool)
  支持按时间与波动率变化观察风险轮廓，说明损益图必须显式展示模型变量和情景，
  不能只给一个胜率或分数。
- [IBKR](https://portal.interactivebrokers.com/en/trading/products-options.php) 将期权链、
  Strategy Builder、Analytics 与 Strategy Lab 连接，说明选中标的、合约与情景应在
  工作流中保持一致。
- [OIC P/L Simulator](https://www.optionseducation.org/oic-profit-and-loss-simulator)
  明确行情延迟，说明数据时间与模型边界必须永久可见。

Finance Radar 不复制高密度券商终端，而采用：

```text
发现候选 → 判断是否愿意接货 → 测算尾部风险 → 保存模拟方案 → 持仓监控与复盘
```

桌面端采用约 58% / 42% 主从布局，右侧情景与风险台保持上下文；移动端改为候选
列表到单合约详情的下钻流程，不把桌面期权链压缩成横向宽表。页面标题延续 Daily
的编辑式宋体和细分隔线，正文不低于 16px；数值用等宽数字，收益不使用绿色大字，
琥珀色只表示谨慎，暗红色只表示尾部风险。

### 4.2 当前安全切片：研究准备度

登录后的一级标签为“期权研究”，公开页面只出现锁定态；解锁后首先回答“为什么
现在不能出候选，还缺什么”，而不是展示伪造合约：

```text
研究模式：暂无可执行候选                    证据截至 / 规则版本
数据 → 市场 → 标的 → 资金                    承保闸门轨迹
────────────────────────────────────────────────────────────
账户与数据准备度（58%）             官方 PutWrite 风险基线（42%）
实时链 / 现金 / 权限 / 持仓 / 事件   CAGR / 最大回撤 / 压力期 / Beta
────────────────────────────────────────────────────────────
0 个候选是风控结果，不是系统故障 · 人工复核 · 不会执行交易
```

服务端固定返回 `research_only + abstain + candidate_count=0`，直至实时链、现金、
权限、事件日历和显式接货政策全部可用。缺数据是有效风控结论，返回 HTTP 200；未
登录返回 401 且 `Cache-Control: no-store`，登出或会话失效必须立即清除私人 DOM。

### 4.3 完整候选阶段的信息架构

完成关键数据接入后，期权实验室再扩展为：

```text
期权实验室
├── 今日状态：可扫描 / 观察 / 暂停新增 / 数据不足
├── 账户容量：可用现金、已占用担保、接货后集中度
├── CSP 候选：最多 3 个，按可解释质量排序
├── 不做清单：被排除的熟悉标的与具体原因
├── 定价偏差：独立标签，默认只显示“无可执行机会”
├── 压力情景：-5% / -10% / -20% / 跳空与指派
└── 历史研究：样本外结果、参数稳定性、失败案例
```

“不做清单”与候选同等重要。例如：

- `TSLA：接货后单一标的将超过 20%，禁止新增短 Put`
- `NVDA：到期前覆盖财报，v1 不开仓`
- `QCOM：买卖价差或成交量不达标`
- `SPY：宏观状态为 reduce_candidate，暂停新增短 Put`

### 4.4 候选卡必须显示的字段

| 分组 | 字段 |
| --- | --- |
| 合约 | 标的、到期日、DTE、行权价、Delta、合约乘数、标准 / 调整后合约 |
| 报价 | Bid / Ask、报价时间、数据延迟、建议限价区间、价差比例、可见尺寸 |
| 现金 | 毛担保现金 `K × multiplier`、收到权利金、盈亏平衡、资金占用比例 |
| 收益 | 最大收益、简单年化权利金率；必须同时展示非年化绝对值 |
| 风险 | 股票跌零损失、-5% / -10% / -20% 情景、接货后市值和组合集中度 |
| 证据 | IV、RV、IV-RV gap、OI、成交量、财报 / 分红 / 宏观事件 |
| 决策 | 入选原因、反对理由、撤销条件、数据源、规则版本、人工复核状态 |

核心计算统一、可审计：

```text
gross_cash_reserved = strike × contract_multiplier × contracts
premium_received = option_credit × contract_multiplier × contracts
breakeven = strike - option_credit
max_profit = premium_received
stock_zero_loss = (strike - option_credit) × contract_multiplier × contracts
breakeven_cushion = 1 - breakeven / spot
simple_annualized_premium_yield = option_credit / strike × 365 / DTE
spread_ratio = (ask - bid) / midpoint
```

“简单年化”不是可实现 CAGR，不能用于跨越不同尾部风险的合约做单一排名。

## 5. 决策框架

### 5.1 先过硬门槛，再排序

以下任一条件不满足，候选直接被拒绝，不能靠评分补回来：

1. **数据**：报价新鲜、Bid 大于零、Bid 不高于 Ask、时间语义明确；
2. **账户**：实时现金、购买力、权限和已有期权仓位可用；
3. **全额担保**：按最保守口径预留 `K × multiplier`；
4. **接货意愿**：行权价不高于用户显式设定的愿意买入价；
5. **组合容量**：接货后单一标的不超过 20%，行业和高相关因子不过度集中；
6. **事件**：v1 到期前不能覆盖财报、重大监管裁决、明确并购表决等二元事件；
7. **流动性**：OI、成交量、价差、报价尺寸均达到按标的分层的阈值；
8. **宏观**：宏观页出现 `reduce_candidate` 或 `exit_candidate` 时停止新增短 Put；
9. **产品**：非杠杆 / 反向 / 波动率 ETP，且为标准交割合约；
10. **执行**：只允许人工 Limit Order，不显示 Market Order 建议。

[OIC 的指派说明](https://www.optionseducation.org/referencelibrary/faq/options-assignment)
提醒美式期权在到期前任何交易日都可能被指派；深度实值、临近到期和分红附近
风险更高。页面必须把“提前接货”视为正常路径，而不是异常事故。

### 5.2 宏观页联动

| 宏观状态 | 期权实验室行为 |
| --- | --- |
| `observe` | 正常扫描，但仍受账户、事件和流动性门槛约束 |
| `prepare_reduce` | 风险预算减半；首期只保留 SPY / QQQ 候选，并提高现金缓冲 |
| `reduce_candidate` | 停止新增短 Put；只提示已有仓位的平仓 / 展期人工复核 |
| `exit_candidate` | 全部新增候选关闭；展示尾部情景、指派和流动性风险 |
| `abstain` | 数据不足，停止个性化候选；不能把未知解释成低风险 |

### 5.3 排序不是下单指令

硬门槛通过后，可以用透明评分减少人工阅读量：

- 25%：愿意买入价与基本面 / 估值匹配；
- 20%：流动性与可执行性；
- 20%：IV 相对同期 RV 的风险补偿；
- 15%：到期结构与时间价值；
- 10%：组合分散贡献；
- 10%：趋势和宏观环境。

排名只回答“先复核哪一个”，不能替代适当性、仓位和交易决定。分数旁必须列出
每个分项、数据时间、规则版本与反对理由。

## 6. 数据方案

### 6.1 实时 / 准实时扫描

- [Tradier Option Chains](https://docs.tradier.com/reference/brokerage-api-markets-get-options-chains)
  可以按标的与到期日提供期权链，并可返回 ORATS Greeks / IV；其
  [市场数据说明](https://docs.tradier.com/docs/market-data) 应用于核验账户对应的
  实时权限、延迟语义与使用限制。
- Cboe 的公开延迟 JSON 可用于开发烟雾测试，但在明确生产许可、稳定性和再展示
  权利之前，不能作为生产 SLA 或数据授权依据。
- 所有快照必须保存 `provider_timestamp`、`fetched_at`、延迟类型、NBBO / 单市场
  语义和原始输入哈希。前端不能用页面刷新时间冒充报价时间。

### 6.2 历史回测

[Cboe DataShop Option EOD Summary](https://datashop.cboe.com/option-eod-summary)
提供自 2012 年起的历史期权 EOD 数据，并列出 15:45 和收盘 NBBO、OHLC、成交量、
OI，以及可选的 IV / Greeks。正式逐合约回测需要授权数据，还必须补充：

- 复权标的 OHLCV；
- 点时财报日、分红除息日与公司行动；
- 无风险利率、借券限制和特殊交割物；
- 当时真实存在的标的池，避免幸存者偏差；
- 佣金、监管费与按价差 / 流动性分层的滑点。

不能用今天的期权链反推过去，也不能用 Black-Scholes 生成权利金后称为“真实
历史回测”。

## 7. 回测设计

### 7.1 交易时点与成交假设

- 在交易日 `t` 收盘后计算趋势、宏观和事件过滤；最早在 `t+1` 的 15:45 快照
  选合约并模拟卖出；
- 卖出按 Bid，买回按 Ask，再加手续费和滑点；Mid 只做敏感性对照；
- 零 Bid、交叉报价、陈旧报价、过宽价差、非标准交割合约全部跳过；
- 没有满足条件的合约就是 0 笔交易，不向最近行权价强行降级；
- 现金从信号日开始占用，指派、平仓、展期和分红现金流逐日记账。

### 7.2 参数网格

| 维度 | 待测范围 |
| --- | --- |
| Delta | 0.10、0.15、0.20、0.25、0.30、接近平值 |
| 初始 DTE | 14–21、22–35、36–50、51–65 |
| 退出 | 持有到期；25% / 50% / 75% 权利金止盈 |
| 展期复核 | 剩余 21 / 14 / 7 DTE，按净 Debit / Credit 分开报告 |
| 财报 | 排除 vs 纳入；v1 生产只采用排除组 |
| 环境过滤 | 无过滤、趋势过滤、宏观预警过滤、二者结合 |
| 风险补偿 | IV-RV gap / ratio 分位数；不能使用未来 IV Rank |
| 指派后 | 持股、下一交易日卖出、Wheel，三种政策分别回测 |

### 7.3 样本外验证

采用滚动 5 年训练、下一年完全样本外测试，每年只用当时已知数据更新。报告必须：

- 单列 2018、2020、2022 等压力期；
- 与现金 / T-bill、标的买入持有、在行权价挂限价买股、PUT / PUTY 基准比较；
- 报告净值 CAGR、最大回撤及持续时间、Expected Shortfall 95 / 99、最差周月、
  偏度、指派率、指派后损益、现金利用率、跳过率、价差和费用；
- 用 block bootstrap 给出置信区间，并对大量参数搜索做多重检验修正；
- 展示相邻参数能否维持结论，拒绝只挑一个最优 Sharpe。

策略只有在扣费、样本外、多个市场状态与相邻参数下都稳定，才可以从“研究”升级
为“纸面候选”。仍不能因此自动下单。

### 7.4 已完成的官方指数基线

复算结果见 [Put-write 官方指数基准复算](options-putwrite-benchmark-2026-09-06.md)。
2007-01-03 至 2026-09-04 的同区间结果显示：

- PUT / PUTY / WPUT / PUTD 日度最大回撤分别为 -37.09%、-33.04%、-28.62%、
  -45.03%；
- 新冠急跌窗口四者分别约为 -28.92%、-27.31%、-25.30%、-31.42%；
- 月度 Beta 约 0.455–0.760，但 Beta 较低没有消除尾部亏损；
- PUTD 的样本内 CAGR 最高，put-write 组中回撤也最深。

这一步只证明回测工具口径与已知指数特征一致，不能替代熟悉标的的逐合约回测。

## 8. 技术架构

### 8.1 模块职责

| 模块 | 职责 |
| --- | --- |
| `options_domain.py` | 合约、报价、Greeks、账户策略与拒绝原因的严格模型 |
| `options_market_data.py` | 数据提供方适配、时间语义、重试、限流与快照哈希 |
| `options_strategy.py` | CSP 硬门槛、情景损益、评分与 parity 偏差检测 |
| `options_collect.py` | 熟悉池定时扫描；失败时保留 last-good，但不刷新证据时间 |
| `options_backtest.py` | 点时链、Bid/Ask 成交、指派、费用与 walk-forward 引擎 |
| `options_service.py` | 私有 API、安全投影、缓存和前端 view model |

### 8.2 数据表

- `option_underlyings`：标的层级、产品类型、愿意买入价、启停状态；
- `option_chain_snapshots`：提供方、标的、到期日、报价时间、抓取时间、原始哈希；
- `option_contract_quotes`：合约键、Bid / Ask、Greeks、IV、OI、成交量、尺寸；
- `option_scan_snapshots`：规则版本、宏观状态、候选与逐条拒绝原因；
- `option_backtest_runs`：数据版本、参数、样本区间、样本外结果与成本假设；
- `option_policy`：私有账户约束；不得进入公开 API 或日志。

账户现金和持仓沿用现有 portfolio snapshot，但必须增加“数据截至日门禁”。旧持仓
不能静默参与今天的张数计算。

### 8.3 私有 API 草案

```text
GET /api/private/options/overview
GET /api/private/options/candidates?strategy=cash_secured_put
GET /api/private/options/contracts/{opaque_id}
GET /api/private/options/backtests
POST /api/private/options/scans       # 可选、限流、只生成研究快照
```

所有响应固定包含：

```json
{
  "human_review_required": true,
  "automatic_execution": false,
  "trade_execution_available": false
}
```

前端无论如何都不能绕过后端硬门槛。公开 `/kol/` 页面不返回账户现金、持仓张数、
愿意买入价或个性化候选。中国境内面向公众提供个性化证券投资建议还涉及持牌与
合规边界，应保持登录后私人研究用途，并在扩大用户范围前取得专业法律意见；
参见 [证监会关于证券投资咨询业务的公开答复](https://www.csrc.gov.cn/guangdong/c105590/c1295910/content.shtml)。

## 9. 实施顺序与验收

### Phase 0：研究基线（本次）

- 完成官方 put-write 指数复算；
- 固化输入哈希、压力窗口、回撤与 Beta 算法；
- 明确数据、合规、风险和“拒绝给建议”的产品边界。

### Phase 1A：只读研究准备度（当前实现切片）

- 增加登录后可见的“期权研究”页与私有只读 API；
- 用“数据 → 市场 → 标的 → 资金”承保闸门解释缺失输入；
- 展示版本化的 Cboe PutWrite 官方指数风险基线，不把它冒充个人账户回测；
- 固定 `abstain` 和 0 个候选，不连接行情或下单，不生成虚构 Strike、Delta、
  权利金、张数或年化收益；
- 登出、401 或 403 时立即清空私人视图，不保留 last-good 私人数据。

验收：未登录绝不请求私有 API；认证响应 `no-store`；缺失或陈旧信息仍返回可解释
的有效风控状态；桌面与移动端都能在 10 秒内看懂“为何不能出候选、还缺什么”。

### Phase 1B：只读候选 MVP

- 接入合法的实时 / 延迟链与报价时效门禁；
- 同步真实持仓、现金、购买力、权限、已有期权和愿意买入价；
- 实现熟悉池、CSP 硬门槛、宏观联动和最多 3 个候选；
- 保存决策快照和拒绝原因；不连接下单。

验收：数据陈旧、现金不足、接货后集中、财报覆盖、宏观减仓、流动性不足等每种
场景均有自动化测试；页面永远能解释“为什么有 / 没有候选”。

### Phase 2：逐合约历史回测

- 取得授权的历史 NBBO / Greeks 数据；
- 完成点时公司行动、财报、分红、指派、费用和 walk-forward；
- 发布完整样本内 / 样本外及失败案例，不只发布最优参数。

验收：任一回测都可由数据版本、代码 commit、参数 JSON 和输出哈希复现。

### Phase 3：纸面跟踪与预警

- 候选先进入 paper ledger，记录实际可见 Bid / Ask 和随后路径；
- 比较模型成交、可成交区间和真实人工成交偏差；
- 只在候选发生实质变化、需要人工复核或数据失败时提醒，避免噪音。

只有在用户另行明确授权、券商权限与合规完成、长期纸面验证通过后，才讨论交易
执行；当前方案不包含自动下单。

## 10. 开始个性化候选前必须补齐

1. Robinhood / Schwab 等账户的最新持仓、现金、购买力和现有期权；
2. 账户可用的期权等级、是否允许 CSP、费用与合约乘数规则；
3. 每只由用户显式确认的熟悉标的的“愿意接货价”和不愿持有清单；
4. 组合级最大现金占用、每周新增上限、单一标的 / 行业上限；
5. 指派后的默认策略：持有、卖股，还是在明确条件下进入 Wheel；
6. 实时与历史期权数据授权。

在这些输入补齐前，系统可以做研究和市场扫描，但不能诚实地回答“今天应该卖
哪只、哪个行权价、几张”。

阅读期权交易前应先阅读最新版
[OCC《Characteristics and Risks of Standardized Options》](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document)。

以上分析仅供参考，不构成投资建议。股市有风险，投资需谨慎。
