# 美股期权数据采购与生产接入评估（2026-09-06）

本文只使用交易所、行情计划、清算机构、数据商和券商的一手资料，服务于
Finance Radar“期权实验室”的数据采购、PoC 和生产验收。它不是法律意见，也不代表
任何供应商已经批准本项目的使用方式。

截至本文日期，项目**尚未完成供应商签约、生产服务器连通性实测或数据授权验收**，
因此没有任何数据源可标记为 `production_ready`，也不能据此开启真实候选。官方没有
公开披露的价格不在本文中记录，最终费用以供应商针对本项目使用场景的书面报价为准。

## 1. 执行摘要

期权实验室不能用一个“期权链 API”同时解决行情真实性、历史回测、事件点时性和
账户执行问题。推荐拆成三层：

1. **事实层（market truth）**：保存有时间戳和合约身份的 OPRA NBBO、成交、合约
   定义、标的行情、公司行动和 OCC 调整证据。优先评估 Databento OPRA.PILLAR 或
   Cboe DataShop / LiveVol All Access。
2. **研究层（analytics）**：计算或取得 Greeks、波动率曲面、候选排序和逐时点回测。
   ORATS 是最快的 PoC 选择，但不能作为唯一实时价格真相；它产生的候选必须回到独立
   OPRA 事实源重新核价。
3. **账户执行层（account / execution）**：读取现金、购买力、权限、持仓并在未来承接
   人工确认后的订单。IBKR、Alpaca 或 Tradier 适合这一层，但它们都不适合作为长期
   逐合约回测主库。

首期仍保持 `research_only + abstain + candidate_count=0`。只有数据合同、授权、实际
生产服务器连通性、历史样本和 shadow-run 全部验收后，系统才可以把状态提升为
`review_ready`；它仍然不是自动下单授权。

### 1.1 不可妥协的来源规则

- 免费、sandbox、Indicative、被修改的报价或未明确时延的 feed **禁止进入候选生成、
  候选核价和交易建议**；只能用于 UI 开发或连接烟雾测试。
- 延迟数据只能用于明确标注为延迟的研究视图，不能支持“当前可操作”结论。
- 任何实时候选都必须携带 OPRA entitlement、来源、报价时间和接收时间；无法证明时
  返回 `abstain`。
- “登录后只有本人可见”不能推定已经取得内部使用、Non-Display、衍生数据、留存或
  再展示授权。必须让供应商按实际架构书面确认。
- 高权利金、正的模型期望值或价差异常都不是无风险套利。候选只表示优先复核顺序。

## 2. 目标架构与信任边界

```text
OPRA / 交易所 / OCC / 公司事件
              │
              ▼
事实层：原始时间戳、NBBO、定义、事件版本、质量标记
              │ immutable input hash
              ▼
研究层：Greeks、流动性门槛、CSP 规则、回测、压力情景
              │ independent quote validation
              ▼
私人 UI：最多少量 review-ready 候选 + 不做原因 + 证据时间
              │ explicit human approval（后续阶段）
              ▼
账户执行层：现金 / 权限 / 持仓 / 订单预览 / 券商回执
```

### 2.1 事实层

事实层只负责回答“在某个时点，市场和系统知道什么”，不产生买卖结论。它至少需要：

- OPRA consolidated NBBO 的 bid、ask、size、quote timestamp 和接收时间；
- 标的 bid / ask 与独立时间戳，不能把不同时间的标的和期权拼成同一快照；
- 逐日或逐时点合约定义，包括 root、到期、行权价、权利类型、乘数、交割物和调整状态；
- 当时已知的 earnings、dividend、split、merger 等事件版本，而不只是事后最终日期；
- OCC Information Memo 的公告、修订、撤销、生效日期和调整后 symbol / deliverable；
- 原始响应 hash、采集版本、许可范围、缺口和更正记录。

OPRA 官方说明，OPRA 发布美国上市期权交易所的综合成交和报价；`current` 指紧邻的
15 分钟窗口。任何向组织外部提供当前或延迟 OPRA 数据的主体都可能成为 Vendor，
即使只是下单前的一次 snapshot，也不能把“请求量很少”视为豁免。参见
[OPRA 官方概览](https://www.opraplan.com/) 和
[OPRA Access Quiz](https://www.opraplan.com/access-quiz)。

### 2.2 研究层

研究层只能读取通过事实层质量检查的数据。Greeks 必须标明是供应商模型值还是本地
计算值，以及模型版本、利率、分红、标的价格和计算时间。回测必须按当时可得数据
重放，并用可成交一侧报价和保守滑点，不得用事后最终事件日期、当前 symbol mapping、
mid price 或理论价值制造收益。

ORATS 可加速期权链清洗、Greeks、扫描和回测 PoC；Databento / Cboe 可提供更适合
审计的原始或分钟 NBBO。研究层应允许两个来源并存和逐字段比对，不把供应商的
综合分数直接映射成产品分数。

### 2.3 账户执行层

账户层必须与行情和研究层解耦。它只在运行时读取私人现金、购买力、期权权限、持仓、
已有同标的短 Put 潜在指派和券商订单预览，不把账户数据写入 Git、静态页面、URL、
浏览器持久存储或普通日志。

IBKR、Alpaca 或 Tradier 的行情可作为下单前第二来源，但券商历史接口不能替代事实层
回测库。首期没有订单创建、修改、取消或 exercise 路径。

## 3. 供应商事实矩阵

表中“可用”只表示官方文档存在相应能力，不表示本项目已经签约、获准或完成服务器
验收。

| 来源 | 实时 NBBO 与 Greeks | 历史 bid / ask 与回测 | 合约、公司行动与事件 | 点时性和缺口 | 适合的角色 |
| --- | --- | --- | --- | --- | --- |
| **Cboe DataShop / LiveVol** | All Access API 可返回同步期权链、OPRA NBBO、标的报价、IV / Greeks，并提供自定义扫描；live / delayed 的 SIP 字段需要相应授权。[All Access API](https://datashop.cboe.com/cboe-all-access-api)、[同步期权链](https://api.livevol.com/v1/docs/Help/Api/GET-allaccess-market-option-and-underlying-quotes_root_option_type_date_min_expiry_max_expiry_min_strike_max_strike_symbol) | Option Quote Intervals 提供 1 分钟或自定义分钟 NBBO、size、OHLCV，并可选 Greeks / OI；当前产品页注明历史从 2012 年起，历史 tick 需联系销售。All Access 历史链 EOD 从 2011 年起。[Option Quote Intervals](https://datashop.cboe.com/option-quote-intervals) | 有按日期的 [options reference](https://api.livevol.com/v1/docs/Help/Api?apiId=GET-allaccess-reference-options_date_symbol)、earnings API、历史/实时 trades-with-Greeks；未核验到完整 OCC 调整后 deliverable API。[API 总表](https://api.livevol.com/v1/docs/Help?area=HelpPage) | 报价和合约列表可按日期回看；earnings 是否保存每次预测的 `known_at / revised_at` 未公开说明。 | 能力最完整的一站式候选；采购前必须确认 API 配额、tick 范围、Non-Display、derived data 和生产再展示。 |
| **Databento OPRA.PILLAR** | CMBP-1 是每次 OPRA consolidated BBO 更新，TCBBO 是成交前 NBBO；没有现成 Greeks。[OPRA 数据集](https://databento.com/docs/knowledge-base/datasets/opra-pillar) | CBBO-1m 可回到 2013-04-01；完整 CMBP-1 / TCBBO / CBBO-1s 从 2023 年附近开始。官方页面分别出现 2023-02-28 与 2023-03-28 的边界，实施时必须用 `metadata.get_dataset_range` 查 entitlement 下的实际范围，不能硬编码。[历史扩展说明](https://databento.com/blog/opra-improvements-coming-soon) | 有 PIT instrument definitions、statistics / OI、历史原始 symbol；另有 PIT corporate-actions。没有核验到 earnings calendar。[PIT symbology](https://databento.com/docs/standards-and-conventions/symbology)、[Corporate actions](https://databento.com/docs/schemas-and-data-formats/corporate-actions) | 历史 symbol 不按今天名称回写，最适合防 survivorship bias。2023 年前最高 NBBO 粒度是分钟；实时 CMBP-1 的 slow-reader 策略会跳数据，必须检测 sequence gap 并回补。 | 最适合做可审计事实层；项目需自算 Greeks、存储数据并另外采购 earnings / dividend 版本。 |
| **ORATS** | Live API 提供 bid / ask、Greeks、theos 和 IV，官方描述计算延迟低于 10 秒；Scanner 可返回候选与风险指标。[Live API](https://orats.com/docs/live-data-api)、[Scanner API](https://orats.com/docs/option-scanner-api) | EOD 链从 2007 年；1 分钟链从 2020-08；期权逐笔从 2022-09-09，并给成交当时 NBBO 和从一分钟快照拼接的 Greeks。[Historical Intraday](https://orats.com/docs/historical-intraday-api)、[Time & Sales](https://orats.com/docs/time-and-sales-api) | 有 earnings history、split history 和股息产品；Scanner 给 next / last earnings 与 next ex-dividend。[Historical API](https://orats.com/docs/historical-data-api) | 历史报价适合时点重放，但 earnings API 未证明保留每次预测修订。Live `stockPrice` 由 put-call parity 推导，官方说明 15 分钟后才精确，不能单独用于 moneyness、担保或风险计算。ORATS 还会过滤非标准合约，并在公司行动前退出回测，不能代表真实持有调整后合约。 | 最快的研究层 PoC；候选必须由独立 OPRA 事实源重新核价，不能单源发布。 |
| **Alpaca** | 付费 entitlement 后可用 OPRA feed；snapshot 包含 latest trade、latest quote 和 Greeks。免费 Indicative 的 quote 被修改、trade 延迟 15 分钟，禁止进入候选。[Snapshots](https://docs.alpaca.markets/us/reference/optionsnapshots)、[Market Data 说明](https://docs.alpaca.markets/us/docs/about-market-data-api) | 官方只确认 options 历史从 2024-02 起，当前公开端点以 bars / trades 为主；未核验到可满足逐合约回测的历史 option bid / ask 序列。[Historical Options](https://docs.alpaca.markets/us/docs/historical-option-data) | 合约 API 支持 active / inactive、style 和 deliverables；corporate actions 类型较全，但官方明确声明创建和处理可能延迟且无时效保证。未核验到 earnings calendar。[Contracts](https://docs.alpaca.markets/us/v1.1/reference/get-options-contracts)、[Corporate actions](https://docs.alpaca.markets/us/reference/corporateactions-1) | 历史短，事件修订点时性不足，不适合作为长期回测主源。 | 适合作为私人 REST 报价校验和未来 broker adapter；不能使用 Indicative fallback。 |
| **Tradier** | Brokerage production 提供全交易所 consolidated L1 options 数据；sandbox 延迟。Greeks 来自 ORATS，但官方注明小时级更新。[Market Data](https://docs.tradier.com/docs/market-data) | 期权历史是 daily OHLCV，且 expired options 不可用；Time & Sales 的窗口短，不能构成长周期逐合约回测。[Historical Data](https://docs.tradier.com/docs/historical-data) | 有当前 lookup、expiration、strike 和 chain；未核验到 PIT 合约主档、公司行动或 earnings API。[Option Chain](https://docs.tradier.com/reference/brokerage-api-markets-get-options-chains) | 过期合约不可取是决定性缺口；小时级 Greeks 不适合时效敏感扫描。 | 已有 Tradier 账户时可做 L1 / 下单或第二报价源；默认 API 仅供个人使用，对外应用需 Partner。[Tradier FAQ](https://docs.tradier.com/docs/faq) |
| **Interactive Brokers** | 订阅 OPRA 后可取 top-of-book bid / ask 和实时 model Greeks。[Market Data subscriptions](https://ibkrcampus.com/campus/ibkr-api-page/market-data-subscriptions/)、[Greeks 字段](https://ibkrcampus.com/docs/tws-api/protobuf/tick-option-computation) | active contract 可取历史 bars / ticks / BID / ASK，但过期期权历史不可用；IBKR 官方也明确历史 Greeks 不提供。[历史期权说明](https://ibkrcampus.com/campus/ibkr-quant-news/historical-options-futures-data-using-tws-api/)、[历史 Greeks 限制](https://ibkrcampus.com/campus/trading-lessons/requesting-market-data/) | secdef 可取当前链；额外订阅 Wall Street Horizon 后可取 earnings、dividend、split 等事件，revision history / PIT 未明确。[WSH 字段](https://ibkrcampus.com/docs/web-api/api-reference/trading/trading-market-data/get-md-snapshot) | 默认 market-data lines、单 brokerage session 和 snapshot rate limit 限制全市场链扫描；过期合约缺失阻断长期回测。 | 适合最终 account / execution adapter，不适合全市场事实仓库。协议还要求关键用途保留独立备用数据源。[IBKR 数据协议](https://www.interactivebrokers.com/Universal/servlet/Registration_v2.formSampleView?file=IBISAgreement.html) |

### 3.1 合约调整的独立证据

单看 OSI symbol 不能还原所有 adjusted contract 的交割物、现金替代或加速到期。
[OCC Information Memos](https://infomemo.theocc.com/infomemo/search-memo) 是合约调整、
修订和结算说明的一手来源。生产方案必须把 OCC memo 作为事实层依赖，或让数据商
书面确认其 contract master 已完整映射这些字段和更正。

## 4. 硬数据合同

供应商 adapter 只能输出一个规范化 envelope。字段缺失不能用零、上次值或另一来源
静默填充；必须显式 `null` 并附 `missing_reason`。

```json
{
  "schema_version": "options-market-snapshot/v1",
  "provider": "vendor-id",
  "feed": "licensed-opra-live",
  "license_scope": "unverified",
  "request_id": "opaque-id",
  "received_at": "RFC3339 UTC",
  "source_status": "live|delayed|indicative|historical|unknown",
  "contract": {
    "osi_symbol": "exact OSI symbol",
    "root": "root",
    "underlying": "underlying",
    "expiry": "YYYY-MM-DD",
    "expiry_session": "am|pm|unknown",
    "right": "put|call",
    "strike": "decimal string",
    "multiplier": "decimal string or null",
    "deliverable": "normalized object or null",
    "adjusted": "true|false|unknown",
    "definition_asof": "RFC3339 UTC",
    "definition_hash": "sha256"
  },
  "option_quote": {
    "bid": "decimal string",
    "ask": "decimal string",
    "bid_size": "integer or null",
    "ask_size": "integer or null",
    "quote_at": "RFC3339 UTC",
    "sequence": "provider sequence or null"
  },
  "underlying_quote": {
    "bid": "decimal string",
    "ask": "decimal string",
    "quote_at": "RFC3339 UTC",
    "source": "provider/feed"
  },
  "greeks": {
    "delta": "decimal string or null",
    "gamma": "decimal string or null",
    "theta": "decimal string or null",
    "vega": "decimal string or null",
    "rho": "decimal string or null",
    "iv": "decimal string or null",
    "asof": "RFC3339 UTC or null",
    "method": "vendor-model|local-model|unknown",
    "model_version": "version or null"
  },
  "events": [{
    "type": "earnings|dividend|split|merger|other",
    "event_at": "date/time",
    "known_at": "RFC3339 UTC",
    "revised_at": "RFC3339 UTC or null",
    "confirmed": "true|false|unknown",
    "source": "provider",
    "source_id": "stable id"
  }],
  "quality": {
    "entitlement_verified": false,
    "freshness_verified": false,
    "cross_source_verified": false,
    "contract_verified": false,
    "event_coverage_verified": false,
    "errors": []
  },
  "raw_payload_sha256": "sha256"
}
```

### 4.1 数值与时间规则

- 金额、行权价、权利金、IV 和 Greeks 在边界层使用十进制字符串或 Decimal，不能先
  转 binary float 再做担保或收益计算。
- 所有原始时间保留 UTC 和来源时区语义；DTE、交易日和到期 session 统一由
  exchange calendar 计算，不能用本地日期相减。
- `quote_at` 是来源事件时间，`received_at` 是本系统接收时间；两者都必须存在才能
  评估陈旧度和传输延迟。
- option quote、underlying quote、Greeks 和 event calendar 不能共用一个虚构
  `as_of`；分别保存各自时间。
- 任何 symbol mapping 都必须以 `definition_asof` 为准；不得用今天的 ticker 回写历史。
- 供应商更正不得覆盖旧行；以 append-only 版本和 supersedes 关系保存。

### 4.2 进入候选前的 fail-closed 门槛

以下任一条件成立时必须返回 `abstain`，并给出机器可读拒绝码：

- `source_status != live`，或不能证明 OPRA live entitlement；
- feed 是 `indicative`、sandbox、modified、unknown 或无文档 fallback；
- bid 不大于零、ask 小于 bid、报价 crossed / malformed、size 缺失且规则要求 size；
- 数据年龄超过经采购验收批准的 feed-specific 阈值；
- 标的和期权时间戳不同步，或 Greeks 输入快照无法绑定；
- 两个独立来源超出经验证的容差；
- 合约 multiplier / deliverable / adjusted 状态未知；
- earnings / dividend / corporate-action 覆盖不到到期日，或事件只有事后最终值而没有
  `known_at`；
- 数据 sequence gap 未完成回补，或服务器时钟异常；
- 账户现金、购买力、权限、持仓、同标的潜在指派任一项不新鲜或不完整。

## 5. 授权与书面确认清单

OPRA 的 [Non-Display Use Declaration](https://cdn.opraplan.com/documents/OPRA_Non_Display_Declaration.pdf)
明确把 investment analysis、risk management、order verification、自动订单或算法
交易等列入 Non-Display Use。期权实验室即使不自动下单，服务端扫描和排名也不能
自行认定为普通个人 display。

在签约前，应把数据流图、服务器位置、用户数、页面截图、存储周期、备份和未来执行
计划发送给供应商，并取得下列问题的书面回答：

1. 单一自然人通过私人生产服务器运行自动扫描，属于 personal display、personal
   non-display，还是 commercial non-display；需要哪些 OPRA 表格和报送？
2. 登录后页面若增加第二位用户、家庭成员、公司员工或顾问，授权如何变化？
3. 页面只展示候选、分数、概率、收益率或警报，不显示 raw quote，是否仍属于 derived
   data distribution；需要何种批准？
4. 能否在公网域名后使用强认证提供私人访问；“可从互联网访问”是否改变 internal /
   external 分类？
5. 原始实时、延迟、T+1、历史和衍生数据分别允许留存多久；退订后可否继续内部回测？
6. 浏览器缓存、服务器缓存、日志、指标、错误追踪、数据库备份和灾备副本是否计入
   device、site、user 或再分发？
7. 开发、测试、预发布、生产和双活服务器是否需分别授权；生产位于中国境内是否有
   地域、数据驻留或跨境限制？
8. 可否把数据用于模型训练、参数拟合、回测结果保存和后续再计算；原始数据是否必须
   删除或隔离？
9. earnings / dividend 是否提供完整 `known_at / revised_at / confirmed` 历史；历史
   API 返回的是当时记录还是今天修订后的最终主档？
10. adjusted options 是否覆盖 multiplier、deliverable、cash-in-lieu、symbol change、
    accelerated expiration 和后来修订；缺失时的 SLA 是什么？
11. 实时 feed 的官方延迟定义、计划维护、限流、并发、断线续传、历史回补、坏数据更正
    和支持 SLA 是什么？
12. 是否允许把原始或衍生字段发送给外部 LLM、监控平台、日志 SaaS 或其他 subprocessors；
    默认方案是不发送。

供应商自己的公开边界也需要纳入合同：

- Cboe 购买流程区分 Individual / Firm、Internal / External、Live / Delayed，并单列
  OPRA Non-Display 与 redistribution；参见 [Cboe SIP Fees](https://datashop.cboe.com/sip-fees)。
- Databento 明确区分 personal、commercial、display、non-display 和 distribution；
  参见 [Live licensing](https://databento.com/docs/api-reference-live/basics/metered-pricing)。
- ORATS 标准条款默认指定 Service User、内部使用且不得转售或转移；参见
  [ORATS Terms](https://orats.com/terms-conditions)。
- Alpaca 客户协议禁止未经书面许可复制、分发、销售或商业利用行情；参见
  [Alpaca Customer Agreement](https://files.alpaca.markets/disclosures/library/AcctAppMarginAndCustAgmt.pdf)。
- Tradier 官方 FAQ 说明 API 默认供个人使用，对外应用需成为 Partner；参见
  [Tradier FAQ](https://docs.tradier.com/docs/faq)。
- IBKR 数据协议禁止未经书面同意再分发，并要求关键系统有独立备用数据源；参见
  [IBKR Market Data Agreement](https://www.interactivebrokers.com/Universal/servlet/Registration_v2.formSampleView?file=IBISAgreement.html)。

## 6. 生产服务器可达性必须实测

本地电脑、浏览器或供应商在线 console 成功，不能证明
`https://zlstreet.xyz/kol/` 所在生产服务器能稳定接入。正式选择供应商前，必须在实际
ECS 上使用将来相同的 DNS、出站网络、TLS、代理、运行用户和 secret 注入方式完成
测试。未提供正式测试 entitlement 前，不能以匿名请求、网页抓取或免费 key 代替。

每个候选供应商至少记录以下实测证据：

- 测试时间、服务器 host 标识、出口 IP、目标 hostname、解析到的 IP 和 DNS 耗时；
- TLS 协议、SNI、证书链、证书到期时间，以及 HTTP / WebSocket 握手结果；
- 鉴权成功与失败的状态码，确认日志不会打印 token 或完整行情 payload；
- REST 的 connect、TTFB、total latency 分布，以及 WebSocket 在真实美股时段的持续
  连接、heartbeat、断线重连和 subscription 恢复；
- 来源 event time 与服务器 receive time 的差值，以及服务器 NTP / chrony 状态；
- 429 / quota headers、并发和 symbol 上限、慢消费者行为、sequence gap 检测；
- 主动断网、DNS 失败、TLS 失败、401 / 403 / 429 / 5xx、空链、半条链和 malformed
  payload 注入时是否 fail closed；
- 断线后的 replay / backfill 是否完整，回补数据是否与实时 schema、symbol mapping
  和 timestamp 语义一致；
- 生产所在地域是否被允许，是否存在跨境链路、运营商或供应商区域性阻断；
- 实测原始证据只写入私有运维记录，仓库仅保存脱敏结论、日期和通过 / 失败状态。

延迟、数据年龄、重连时间和可用率阈值必须根据供应商合同与实测分布批准后配置；在
此之前保持 `unverified`，不能虚构一个看似精确的 SLA。

## 7. 采购与技术验收清单

### 7.1 授权验收

- [ ] 供应商已收到完整架构图和实际 use case，而不是泛称“个人研究”。
- [ ] OPRA、供应商、第三方事件源分别给出适用协议和书面批准。
- [ ] display / non-display、personal / commercial、internal / external、raw / derived
      的分类与当前产品一致。
- [ ] 用户数、设备数、开发 / 生产 / 灾备环境和报送责任已确认。
- [ ] 留存、备份、退订、删除、审计、subprocessor 和再展示条款已确认。
- [ ] 授权编号、有效期和允许用途仅保存在私有 secret / compliance store，不写入前端。

### 7.2 数据覆盖验收

- [ ] 以熟悉池的小样本覆盖普通月度、周度、LEAPS、低流动性、零 bid、宽价差、AM / PM
      settlement 和交易所休市边界。
- [ ] 取得 active、expired、delisted underlying 和 symbol-change 前后的真实历史合约。
- [ ] 核验 NBBO、size、trade、OI、标的 quote、Greeks、definition 的来源和时间语义。
- [ ] 用 OCC memo 验证 split、merger、cash distribution、cash-in-lieu、加速到期和调整后
      deliverable；无法还原的合约进入永久排除集。
- [ ] earnings 和 dividend 至少包含当时已知时间、后续修订与 confirmed 状态；只返回
      最终日期的产品不能通过事件回测验收。
- [ ] 历史起始、字段覆盖和更正情况通过 metadata / sample 实测，不只引用营销页。

### 7.3 质量与可靠性验收

- [ ] 两个独立来源在相同期权、相同时间窗口做 bid / ask / size / trade / definition
      对账，并保存差异分布。
- [ ] provider adapter 通过 contract tests、录制样本重放、时区 / DST、分页、限流、重连、
      缺字段、精度和 schema drift 测试。
- [ ] 服务器连通性检查在盘前、盘中、盘后和周末维护窗口分别执行。
- [ ] 数据 gap 可检测、可回补、可审计；无法确认完整时不使用上次快照继续出候选。
- [ ] 原始响应加 hash，标准化记录 append-only；更正不静默覆盖旧版本。
- [ ] 任何 provider outage、license expiry、clock drift 或 cross-source mismatch 都会把
      候选计数归零并显示可解释原因。

### 7.4 回测验收

- [ ] universe 按历史 definition 构造，包含后来退市和改名标的，避免 survivorship bias。
- [ ] 每次信号只使用 `known_at <= decision_at` 的事件和指标，避免 look-ahead。
- [ ] 进入用 sell-side Bid、退出用 buy-side Ask，并计入保守滑点、费用、早指派、分红、
      停牌、缺报价和无法平仓情景。
- [ ] CSP 按 `strike × multiplier` 全额预留现金，并计入已有同标的短 Put 潜在指派。
- [ ] adjusted / non-standard 合约如不能完整模拟则排除，并公开排除数量和影响。
- [ ] 参数选择与样本外评估分离，进行 walk-forward、压力期、敏感性和失败案例分析。
- [ ] 结果可由原始 input hash、规则版本和代码提交复现；供应商内置回测不能成为唯一
      验证结果。

## 8. 推荐 PoC 次序

### 阶段 0：先锁定授权，不接生产候选

向 Cboe、Databento 和 ORATS 发送同一份使用场景问卷，取得第 5 节问题的书面回复。
同时选定一个未来账户 adapter 候选（IBKR 或 Alpaca；已有 Tradier 账户时可作为备选）。
在授权与留存边界明确前，前端继续显示 `abstain`。

### 阶段 1：历史小样本 bake-off

用 SPY、QQQ 和少量高流动性单股的若干非连续交易日比较：

- Databento / Cboe 的 NBBO、definition、OI 和数据完整性；
- ORATS 的 bid / ask、Greeks、理论值和同一时点输入；
- earnings / dividend 的当时已知版本；
- 一般合约、事件前后和调整合约的处理差异。

只做数据对账和既有规则重放，不搜索“最佳参数”。如果事件版本或合约交割物无法
重建，就先解决数据问题，不进入完整回测。

### 阶段 2：生产 ECS 连通性和 shadow ingest

在实际 ECS 上完成第 6 节全部测试，以 append-only 方式影子采集，不写候选 API。
至少跨越正常交易日、周末维护和一次真实断线 / 重连演练。数据 gap、quota、时钟或
授权状态没有可观测性时，不进入下一阶段。

### 阶段 3：双源候选 shadow-run

推荐优先组合：

1. ORATS 生成研究候选；
2. Databento OPRA 或 Cboe All Access 重新获取同一 OSI 合约的独立 NBBO；
3. 本地重新计算关键现金、收益和风险字段；
4. 不一致、陈旧或事件缺口时统一 `abstain`；
5. 只在内部日志生成候选，不展示、不通知、不下单，先统计误报和缺失原因。

如果最终只采购一个研究数据商，则优先选择 Databento 或 Cboe 作为事实层，并本地
计算 Greeks；不能让单一供应商同时生成、核价和证明自己的候选。

### 阶段 4：私人人工复核 UI

shadow-run 达到预先批准的质量门槛后，才在强认证页面展示最多少量候选，同时展示
数据时间、两源状态、反对理由、事件覆盖、调整状态和撤销条件。仍不提供 Market Order，
不自动连接券商，不把结果公开或分享给未授权用户。

### 阶段 5：账户只读与订单预览

最后再接账户现金、购买力、持仓、权限和券商 order preview。交易写入需要另行设计、
审计和明确授权；数据采购完成不等于允许自动交易。

## 9. 推荐决策

- **最快获得高质量研究体验**：ORATS 作为研究层 PoC，Databento OPRA 或 Cboe
  作为独立事实层；未完成第二来源核价的 ORATS 结果不进入 UI 候选。
- **最重视点时审计和长期自建**：Databento OPRA.PILLAR + PIT corporate actions +
  本地 Greeks / 曲面，并另购具有事件修订历史的 earnings / dividend feed。
- **希望由一家专业供应商覆盖最多能力**：评估 Cboe All Access + DataShop，但先让
  Cboe 书面确认 API 配额、历史 tick 范围、Non-Display、衍生展示和留存。
- **账户与未来执行**：优先在 IBKR 与 Alpaca 中按用户真实账户资格、地区可用性、权限、
  session 运维和订单预览实测选择；Tradier 可作为已有账户情况下的备选。

在供应商授权、真实 ECS 网络、点时事件和独立报价四项中，任一项未通过，产品应把
“0 个候选”视为正确的安全结果。

以上分析仅供参考，不构成投资建议。股市有风险，投资需谨慎。
