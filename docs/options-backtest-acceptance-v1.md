# 期权实验室稳健回测验收规范 v1

本文定义现金担保卖 Put（Cash-Secured Put，CSP）及接货后轮动策略的最低可审计
回测标准。它面向实现、测试和发布验收，不是策略收益承诺，也不构成投资建议。

核心原则：回测必须只使用决策时真实可见的数据、按当时真实存在的合约和可执行
报价成交，并通过逐事件资金账本处理公司行动、提前指派、到期和实物交割。缺少任一
关键证据时，系统必须返回 `insufficient_data`，而不是补造价格、合约或交易结果。

本文使用以下规范词：

- **必须**：不满足即阻断策略级结果或发布。
- **应该**：默认实现；偏离时必须记录理由和影响。
- **不得**：违反即视为结果失真。

## 1. 支持范围与结果等级

v1 支持两类相互隔离的研究：

1. 个股或 ETF 的美式、实物交割 CSP，以及指派后进入股票持仓的轮动状态机；
2. Cboe `PUT`、`WPUT` 的 SPX 欧洲式、现金结算基准复算或外部对照。

两类产品的行权方式、结算价和生命周期不同，不得共用一个隐式默认值。

每次回测必须返回以下结果等级之一：

| 状态 | 含义 | 是否可展示策略指标 |
| --- | --- | --- |
| `validated` | 所有 P0 门禁通过，golden tests 通过 | 可以，仍须标“历史研究” |
| `research_only` | 可运行，但存在已披露的 P1 限制 | 可以，但不得生成交易建议 |
| `insufficient_data` | PIT、报价、合约或事件证据不足 | 不可以，只展示缺口 |
| `invalid` | 未来泄漏、账本不守恒或非法状态 | 不可以，必须失败 |

## 2. Point-in-time 数据契约

### 2.1 双时间字段

任何会影响选股、选约、调仓或风控的记录都必须至少包含：

- `event_at` 或 `observed_at`：经济事件或市场观察实际发生时间；
- `available_at`：该值首次可被策略读取的时间；
- `source_id`、`source_version`、`ingested_at`；
- 如数据会修订，保留 `valid_from`、`valid_to` 和原始版本，不做静默覆盖。

每次策略决策必须固化：

```text
decision_at
data_cutoff_at
universe_hash
feature_hash
strategy_version
parameter_set_hash
source_manifest_hash
```

硬门禁：

```text
max(all_inputs.available_at) <= decision_at
data_cutoff_at <= decision_at
```

同一交易日不等于同一可用时点。Cboe EOD 产品的一行可以同时包含 15:45 ET 的
NBBO/Greeks，以及产品收盘后的 OHLC、VWAP、成交量和 OI；字段必须分别带时点，
不得整行视为 15:45 已知。参见
[Cboe Option EOD Summary](https://datashop.cboe.com/option-eod-summary)。

### 2.2 PIT 验收用例

- Given 决策时点为 15:45 ET，When 当日 EOD volume/OI 的 `available_at` 晚于
  决策时点，Then 这些字段不得参与当次筛选。
- Given 已完成一轮回测，When 任意修改 `decision_at` 之后发布的数据，Then 当次
  universe、feature、series selection 和 orders 必须逐字节不变。
- Given 数据只有日期而没有可证明的 `available_at`，Then 该字段不得作为入场特征；
  系统返回可定位的数据缺口。
- Given 财报或股息日历没有覆盖某标的，Then 状态是 `unknown`，不得折叠成
  `known_none`。

### 2.3 事件覆盖

财报、股息、停牌和公司行动数据必须提供：

```text
coverage_state = complete | known_none | unknown
announced_at
effective_at
last_verified_at
source_id
```

入场筛选只可使用 `announced_at <= decision_at` 的事件。之后实际发生的拆股、并购、
停牌或退市仍必须进入持仓生命周期和 P&L；“当时不知道”只影响决策，不允许回测
跳过后来实际发生的现金流。

## 3. 历史合约池与 series lifecycle

历史合约池必须由当时生效的 product/series 快照构造，不能用今天的期权链或当前
股票列表回填。OCC 的产品/series 文件包含 exercise style、settlement method、
activation/inactivation date；Cboe 当前目录本身说明其仅覆盖 Cboe、按上一营业日
信息更新，并可能包含非标准合约。参见
[OCC Product/Series Data](https://www.theocc.com/market-data/market-data-reports/other-market-data-info/data-sales)、
[Cboe Reference Data](https://www.cboe.com/markets/us/options/market-statistics/reference-data/)
和 [Cboe Symbol Directory](https://www.cboe.com/markets/us/options/symbol-directory/equity-index-options)。

### 3.1 最小合约模型

```text
OptionSeriesVersion
  series_id                 # 稳定、不可复用的内部主键
  osi_symbol
  option_root
  underlying_perm_id
  put_call
  strike
  expiration_date
  last_trade_at
  expiry_at
  exercise_style            # american | european
  settlement_type           # physical | cash
  multiplier
  deliverable_id
  activation_at
  inactivation_at
  valid_from
  valid_to
  source_id
  source_version
```

候选 series 必须同时满足：

```text
activation_at <= decision_at < inactivation_at
valid_from <= decision_at < valid_to
last_trade_at > decision_at
```

如果 `inactivation_at` 或 `valid_to` 为空，含义必须是明确的 open interval，不得用
“当前仍存在”反推历史。

### 3.2 存续偏差门禁

- 已退市、被收购、停牌、改名和已经消失的标的或合约必须保留在历史路径中。
- 缺少报价时不得删除该持仓、沿用最后价格至到期，或假设按内在价值成交。
- 当前熟悉股票列表做历史回测时，结果必须标记
  `current_watchlist_conditional=true` 和 `survivorship_bias=true`；它只能回答这些
  当前标的过去如何，不能证明历史可发现的策略 alpha。
- 无偏 universe 研究必须保存标的 `eligible_from`、`eligible_to` 及其当时可见的
  准入理由。

## 4. NBBO 成交、size 与 no-fill

OPRA 根据参与市场报价形成最高 bid 和最低 offer。参见
[OPRA BBO Specification](https://cdn.opraplan.com/documents/OPRA_Pillar_Output_Specification.pdf)。

### 4.1 最小行情模型

```text
MarketSnapshot
  series_id
  quote_at
  available_at
  bid
  ask
  bid_size
  ask_size
  underlying_bid
  underlying_ask
  underlying_quote_at
  trade_price?              # 单独时点与 condition code
  trade_size?
  volume?                   # 单独 available_at
  open_interest?            # 单独 available_at
  vendor_iv?                # 单独 snapshot/model 元数据
  vendor_greeks?
  source_id
  source_version
```

报价必须通过：

```text
bid > 0
ask > 0
ask >= bid
relevant_side_size > 0
quote_at <= decision_at
decision_at - quote_at <= max_quote_age
abs(option_quote_at - underlying_quote_at) <= max_snapshot_skew
```

`ask < bid` 的 crossed quote 必须拒绝；locked quote 只有在 size 和时效均有效时才可用。

### 4.2 默认可执行成交模型

| 行为 | 基准成交价 | size 门禁 |
| --- | --- | --- |
| Sell-to-open Put | NBBO bid | `qty <= bid_size` |
| Buy-to-close Put | NBBO ask | `qty <= ask_size` |
| 研究净值标记 | `(bid + ask) / 2` | 不代表可成交 |
| 立即清算净值 | 短期权负债按 ask | 使用 ask size 并披露超量 |

订单超过 top-of-book size 时，超出部分必须 partial fill 或 `no_fill`；没有 size 的报价
不得在可审计基线中成交。无有效报价必须产生 `no_fill(reason=...)`，不得回退到理论价、
昨收、零价或内在价值。

使用 mid、bid 与 ask 的不同结果必须分开展示。主结果使用买卖价可执行侧；mid 仅可
命名为 `optimistic_mid_sensitivity`。原始研究显示期权点差会明显侵蚀策略利润，并以
midpoint 加减 25%/50% spread 做交易成本敏感性；50% 即买 ask、卖 bid。参见
[Aretz, Lin & Poon, Review of Finance](https://academic.oup.com/rof/article/27/1/289/6510952)。

### 4.3 禁止伪造成交

- 当日 volume 大于零不能证明订单发出后成交。
- 被动挂单在 mid 或 ask 成交，必须有下单后的逐笔交易、报价和明确队列/触价模型；
  只有日线或单个快照时不得假设成交。
- 同日 OHLC 的 high/low 不得用于证明限价单成交，除非有符合时序的逐笔证据。
- 只有 EOD 数据时，不得模拟 9:30 开盘、盘中止盈或 Cboe 11:30–12:00 VWAP 窗口。

## 5. 费用、滑点与利息版本

费用配置必须按有效期版本化：

```text
FeeScheduleVersion
  fee_schedule_id
  effective_from
  effective_to
  venue
  product_class
  customer_capacity
  maker_taker
  commission_per_contract
  exchange_fee_per_contract
  regulatory_fee_per_contract
  exercise_fee
  assignment_fee
  minimum_order_fee
  source_id
```

交易所费用依 venue、产品和订单角色变化，不能把当前单一费率回填历史。参见
[Cboe Options Fee Schedules](https://www.cboe.com/us/options/membership/fee_schedule/)。

验收要求：

- 每笔成交记录适用的 `fee_schedule_id` 和逐项费用。
- 主报告展示从 gross 到 net 的 spread、额外滑点、佣金、监管费和行权/指派费瀑布。
- 零费用结果只能命名 `gross`，不得命名 `net`。
- 现金利息必须使用决策时已公布的利率和明确 day-count；不同抵押现金是否计息必须
  由策略规则显式决定。

## 6. ET 交易日、DST 与产品时间

所有市场规则使用 IANA `America/New_York`，数据库同时保存 UTC。业务日期必须先按
ET 交易日确定，不能用 UTC 零点切日。

Cboe 官方页面列出 ET 交易时间、完整休市和 13:00 提前收盘；不同产品的正常收盘
也可能为 16:00 或 16:15。参见
[Cboe Hours & Holidays](https://www.cboe.com/about/hours/us-options)。

最小日历模型：

```text
TradingSession
  exchange
  product_class
  session_date_et
  open_at
  regular_close_at
  last_trade_at
  early_close
  holiday_name?
  calendar_version
```

必须分别保存并使用：

```text
last_trade_at
expiry_at
exercise_cutoff_at
settlement_at
```

golden tests 至少覆盖：

- 美东夏令时开始和结束前后的 UTC 转换；
- Good Friday 完全休市；
- 周五休市时 roll 到前一营业日；
- 感恩节后和圣诞前后的 13:00 提前收盘；
- 股票期权 16:00 与部分 broad-based ETF/index 16:15 的差异；
- AM-settled 合约最后交易日与 settlement date 不同。

## 7. 到期、Ex-by-Ex、pin risk 与提前指派

### 7.1 产品状态机

```text
listed -> open_position -> {closed | early_assigned | expired}
expired -> {exercised_assigned | expired_worthless | contrary_exercise}
assigned -> unsettled_t1 -> stock_position
stock_position -> {held | covered_call_open | sold}
```

所有状态转换必须记录 `event_at`、`available_at`、合约数量、现金、deliverable 和费用。

### 7.2 标准个股/ETF 期权

标准个股期权通常是美式、实物交割并对应 100 股，但公司行动可改变 deliverable；
行权产生的股票交割当前为 T+1。参见
[OCC Equity Options Specifications](https://www.theocc.com/clearance-and-settlement/clearing/equity-options-product-specifications)
和 [OCC June 2024 T+1 supplement](https://www.theocc.com/getmedia/31a1f0fc-f7e7-477c-9c03-f84d1dd05cf6/june_2024_supplement.pdf)。

到期主情景采用 OCC Exercise-by-Exception：标准期权 ITM 至少 $0.01 时默认行权，
但持有人/清算会员可以 contrary instruction。FINRA 规则规定最终行权决定通常可到
到期营业日 17:30 ET，晚于正常收盘。参见
[FINRA Rule 2360](https://www.finra.org/rules-guidance/rulebooks/finra-rules/2360)
和 [OCC Exercise FAQ](https://www.optionseducation.org/referencelibrary/faq/options-exercise)。

到期验收：

- 主路径按 Ex-by-Ex 处理；不得把它描述为绝对自动、不可更改。
- 在可配置的 `pin_risk_band` 内，必须同时计算 `exercise` 和 `no_exercise` 两条边界。
- 不得仅依据 16:00 股票收盘价断言不会被指派。
- 指派必须从 reserved cash 支付真实 deliverable obligation，生成股票持仓和
  `unsettled_t1`，而非只记录 `max(K-S,0)`。

### 7.3 提前指派双情景

美式短 Put 在任一营业日都可能被指派；OCC 向清算会员分配、券商再以获准方法分配，
无法仅由普通行情确定某账户是否被指派。风险随深度 ITM、临近到期和时间价值下降而
增加，短 Put 在除息后也可能增加，停牌不解除 writer 的义务。参见
[OCC Assignment FAQ](https://www.optionseducation.org/referencelibrary/faq/options-assignment)。

在没有经过样本外验证的实际 exercise/assignment 数据前，必须至少同时输出：

1. `no_early_assignment`：仅到期按 Ex-by-Ex，明确标为乐观下界；
2. `all_economically_eligible_assigned`：当 Put ITM 且时间价值低于经版本化的经济
   触发阈值时，符合条件的全部指派，作为保守边界。

不得把自造的单点概率包装成“真实指派概率”。如以后引入概率模型，必须披露训练样本、
校准误差、样本外表现和随机种子，并继续保留上述边界情景。

轮动逻辑只能在指派通知可用、股票状态创建后的下一个可交易时点寻找 covered call；
不得在期权到期收盘价上瞬时完成接货和下一腿卖出。

## 8. OCC 公司行动与调整合约

OCC 文件显示，拆股可能改变 strike、multiplier、合约数量和 deliverable；反向拆股
可能保持 strike/multiplier 而只改变交割股数；现金并购可能把 deliverable 改成现金
却不改变 symbol。数字后缀通常表示非标准合约，但没有后缀也不能绝对证明是标准合约。
参见 [OCC Contract Adjustments and OSI](https://infomemo.theocc.com/infomemos?number=26853)
和 [OCC Information Memos](https://infomemo.theocc.com/infomemo/search-memo)。

公司行动模型必须保存：

```text
ContractAdjustment
  adjustment_id
  occ_memo_id
  announced_at
  effective_at
  old_series_id
  new_series_id?
  old_deliverable_id
  new_deliverable_id
  strike_before/after
  multiplier_before/after
  contracts_before/after
  cash_component
  source_hash
```

v1 若尚无完整 deliverable engine，必须排除所有不能确认是标准 100 股交割的合约，
返回 `unsupported_contract`。不得只做 underlying price/strike 复权后继续回测。

## 9. 现金担保账本与守恒

每张标准 CSP 至少冻结 `strike * 100`；调整合约按真实 deliverable obligation 冻结。
权利金、可用现金、担保现金、短期权负债、股票和 T+1 未结算项必须分账。

最小账本：

```text
PortfolioLedger
  event_at
  available_cash
  reserved_cash
  premium_receivable
  premium_payable
  option_market_value
  stock_market_value
  unsettled_cash
  unsettled_stock
  accrued_interest
  accrued_fees
  nav_mid
  nav_liquidation
```

每个事件后必须满足：

```text
available_cash >= 0
reserved_cash >= maximum_delivery_obligation_of_open_short_puts
nav_mid = cash_and_receivables + stock_market_value
          - short_option_mid_liability - accrued_fees
nav_liquidation = cash_and_receivables + executable_stock_value
                  - short_option_ask_liability - accrued_fees
sum(ledger_debits) == sum(ledger_credits)
```

权利金不得同时计入可复投现金和策略收益。策略回报分母必须是账户 NAV/担保资本，
不得使用收到的权利金或券商 margin 作为回报分母。

必须有以下 property tests：

- 开仓瞬间在忽略费用且 mid 等于成交价时，NAV 不凭空增加；
- 同一订单拆成多笔成交与合并成交，在相同费用规则下总账一致；
- 指派前后的 option liability 消失、股票和现金义务出现，净经济价值连续；
- 到期 worthless 不留下 reserved cash 或幽灵持仓；
- 任意事件序列结束后借贷守恒，重复消费同一 event id 不改变账本。

## 10. IV、Greeks 与符号约定

Greeks 是带模型和时点的估计值，不是无时点真值。供应商 Greeks 只能用于其明确的
snapshot；自算 IV/Greeks 的 option quote、spot、利率、股息和日历输入都必须满足
`available_at <= decision_at`，并记录 `model_name`、`model_version`、`input_hash`。

统一采用 long-option convention：

- long call delta 在 `[0, 1]`；
- long put delta 在 `[-1, 0]`；
- `signed_contract_qty`：long 为正，short 为负；
- `position_delta_shares = option_delta * signed_contract_qty * multiplier`。

因此一张 `delta=-0.25`、`signed_contract_qty=-1`、`multiplier=100` 的 short put，
组合 delta 必须是 `+25`。OIC 也明确说明 long put delta 为负、short put position
delta 为正。参见 [OIC Delta](https://prd-web.optionseducation.org/advancedconcepts/delta)。

验收要求：

- 供应商仅提供绝对 delta 时，必须携带 `delta_convention=absolute` 后再标准化；
  缺少 convention 时不得按 delta 选约。
- 非有限值、越界 delta、负 IV 或缺模型元数据必须拒绝。
- 决策后改变 rate、dividend 或 quote，历史 Greeks 和选约不得变化。
- short put 的 position gamma、vega 应为负；theta 的定义和年化/day-count 必须显式，
  不以“通常为正”替代计算。

## 11. Nested walk-forward 与反过拟合

策略参数包括但不限于 underlying universe、DTE、delta、事件窗口、最小流动性、
止盈、止损、roll、提前平仓、仓位上限和宏观门禁。所有尝试均必须进入 trial registry，
不能只登记最终获胜版本。

### 11.1 外层和内层

```text
outer_train -> inner_train/validation parameter selection -> locked outer_test
```

- 外层 test 是最终 OOS；生成结果后不得再调参。
- 内层只使用外层 test 之前可用的数据选择参数。
- 任一训练样本的标签/持仓区间与 test 起点重叠时必须 purge。
- train/validation/test 之间的 embargo 至少覆盖最大持有期；若采用更短 embargo，
  必须证明不存在重叠信息。
- 每折保存窗口、purged rows、embargo、候选参数数、获胜参数、随机种子和数据 hash。
- 最终锁定 OOS 只能运行一次；后续改动产生新的 strategy version 和新的未来 OOS。

普通单次 holdout 对投资回测可能不稳健；应该补充 CSCV/PBO 或同类过拟合评估。
大量试验还必须披露多重检验问题。参见
[Bailey et al., The Probability of Backtest Overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)
和 [Harvey, Liu & Zhu, NBER w20592](https://www.nber.org/papers/w20592)。

任何清洗规则也必须能在组合形成时执行。使用事后才知道的错误/噪声标志删除观察值
会产生 look-ahead bias。参见
[Duarte et al., Too Good to Be True](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4590083)。

### 11.2 trial registry 最小字段

```text
trial_id
created_at
research_question
strategy_version
data_manifest_hash
universe_definition
parameter_grid
selection_metric
fold_spec
purge_spec
embargo_spec
result_status
parent_trial_id?
```

## 12. PUT / WPUT 基准

官方 ticker 是 `PUT` 和 `WPUT`，不得写作 `PUTW`。

### 12.1 PUT

`PUT` 是按月卖出 ATM SPX Put 并持有 1/3 月 T-bill 的基准。第三个周五滚动，若
遇交易所假期则取前一营业日；到期用 SPX Special Opening Quotation（SOQ）结算；
新 Put 使用 11:30–12:00 ET 符合方法论 trade-condition 的 OPRA 成交 VWAP，窗口
无成交时回退至窗口结束前最后 bid。参见
[Cboe S&P 500 PutWrite Indices Methodology](https://cdn.cboe.com/api/global/us_indices/governance/Cboe_SP_500_PutWrite_Indices_Methodology.pdf)。

### 12.2 WPUT

`WPUT` 是每周 ATM SPX Put 加 4-week T-bill。AM-settled 合约按 SOQ；PM-settled
到期 Put 按 16:00 ET 前最后 ask 回补；新仓按方法论规定使用开盘后的首个 bid 或
收盘前最后 bid。参见
[Cboe PutWrite Indices Methodology](https://cdn.cboe.com/api/global/us_indices/governance/Cboe_PutWrite_Indices_Methodology.pdf)。

SPX 是欧洲式、现金结算；PUT/WPUT 不包含个股美式提前指派、股票接货和单一公司
跳空风险，故只能作为市场环境基准，不能作为个股 CSP 的同风险替代品。

### 12.3 基准验收

- golden fixtures 的 roll date、series、strike、AM/PM 分支、SOQ payoff、VWAP
  trade-condition 过滤和无成交 fallback 必须逐项匹配方法论。
- 使用相同输入和精度时，独立 oracle 与生产实现的 ledger/index level 必须精确匹配；
  舍入只允许在展示层发生。
- 只有 EOD 快照时不得宣称“复刻 PUT/WPUT”；应使用官方历史指数作外部对照。
- 同期报告至少包含策略 gross、策略 net、标的 total return、T-bill、PUT 和 WPUT，
  全部 rebased 到同一日期。
- `PUT` 的 base date 为 1988、launch date 为 2007；`WPUT` 的 base date 为 2006、
  launch date 为 2015。launch 前官方值必须标记 `backtested_theoretical=true`。
  Cboe 也明确说明指数发布前数据是理论回测且不代表实际费用和税务。参见
  [Cboe PUT Factsheet](https://cdn.cboe.com/resources/indices/factsheet/CboeGlobalIndices_PUT-Index.pdf)。

## 13. 风险指标与压力矩阵

### 13.1 必备指标

主报告必须至少包含：

- TWR、CAGR、日度年化波动、Sharpe、Sortino、Calmar；
- 最大回撤深度、开始/谷底/恢复日期、回撤持续期和水下曲线；
- worst day/week/month、历史 VaR 与 Expected Shortfall；
- 相对标的 total-return 的 beta、相关性和 downside capture；
- 指派率、提前指派双情景差、接货后股票持有天数、roll 次数和换手；
- no-fill/partial-fill 比例、spread cost、额外滑点和全部费用；
- 最大单一标的/行业担保占比、总现金占用率；
- portfolio delta/gamma/vega/theta；
- `nav_mid` 与 `nav_liquidation` 两套路径。

Sharpe/Sortino 的无风险利率必须与收益同频并使用当时可见的 T-bill 数据；年化因子、
day-count 和缺失日处理必须写入报告元数据。

### 13.2 压力矩阵

每个情景必须对期权做全量重估或使用明确的到期 payoff，不得只靠 delta 线性近似：

| 维度 | 最小情景 |
| --- | --- |
| 标的价格 | `-10%`, `-20%`, `-30%`, `-50%`, `to_zero` |
| 隐含波动率 | `+10`, `+20`, `+40` vol points |
| 流动性 | spread `2x`, `4x`；bid size `0`；整日 `no_quote` |
| 相关性 | 相关性趋近 1；行业同步下跌 |
| 个体跳空 | 单一标的极端 idiosyncratic gap |
| 指派 | 所有经济上符合条件的 Put 同时提前指派 |
| 市场状态 | underlying/option halt、无法平仓、隔夜 gap |
| 公司行动 | 普通/特殊股息、拆股、反向拆股、现金并购、退市 |
| 联合情景 | spot 下跌 + IV 跳升 + spread 扩大 + size 归零 |

还必须做 reverse stress：求使 NAV 回撤达到 20%、30%、50% 的最小联合冲击，并
报告是哪一条资金/集中度门禁首先失效。

OCC 的 STANS 风险方法采用 99% Expected Shortfall，并额外考虑极端市场运动、
相关性失效和单一风险因子的极端不利冲击；这里将其作为压力设计原则，而非声称复制
OCC 专有模型。参见
[OCC Margin Methodology](https://www.theocc.com/risk-management/margin-methodology)。

## 14. 最小可发布 schema

除前述 series、snapshot、fees、calendar、adjustment 和 ledger 外，每次回测至少输出：

```text
BacktestRun
  run_id
  created_at
  strategy_version
  parameter_set_hash
  data_manifest_hash
  start_at
  end_at
  decision_schedule
  execution_model
  assignment_model
  fee_schedule_ids[]
  calendar_versions[]
  benchmark_versions[]
  result_status
  insufficiency_reasons[]
  warnings[]

DecisionTrace
  decision_id
  decision_at
  max_input_available_at
  universe_hash
  eligible_series_ids[]
  rejected_series[{series_id, reason_codes[]}]
  selected_series_ids[]
  feature_values_with_provenance[]

OrderEvent
  order_id
  decision_id
  series_id
  side
  intent
  qty_requested
  quote_at
  bid/ask/bid_size/ask_size
  qty_filled
  fill_price
  fill_model
  no_fill_reason?
  fee_schedule_id
  total_fees
```

所有输出需可由 `run_id` 追溯到不可变输入 manifest。原始数据不能静默修订；重跑新
数据必须产生新 `run_id` 和新 hash。

## 15. Golden tests 与发布门禁

### 15.1 必须通过的 golden tests

1. **PIT mutation test**：修改未来数据不改变历史决策。
2. **mixed-timestamp row test**：15:45 决策不能读取同日 EOD-only 字段。
3. **series activation test**：尚未 activation 或已经 inactivation 的 series 不入池。
4. **delisted survivor test**：退市标的不从持仓或损失路径消失。
5. **NBBO side test**：STO=bid、BTC=ask，mid 仅用于研究标记。
6. **size test**：请求量超过相关 side size 时 partial/no-fill。
7. **stale/crossed/no-quote test**：均产生确定的 rejection/no-fill reason。
8. **ET calendar test**：DST、休市、早收盘和产品 close 均正确。
9. **Ex-by-Ex test**：ITM $0.01 主路径和 contrary/pin 双路径均可重放。
10. **early assignment test**：同一路径输出乐观与保守两条结果。
11. **T+1 test**：指派后先 unsettled，再于下一营业日交割。
12. **OCC adjustment test**：拆股、反向拆股和现金并购不靠价格复权蒙混通过。
13. **ledger conservation test**：每个事件借贷守恒，重复 event id 幂等。
14. **Greek sign test**：一张 -0.25 delta short put 的组合 delta 为 +25 shares。
15. **walk-forward isolation test**：外层 test 数据和结果不能影响内层选择。
16. **purge/embargo test**：跨 test 起点的持有期样本被 purge。
17. **PUT fixture test**：月度 roll、SOQ、VWAP 和 last-bid fallback 匹配官方方法。
18. **WPUT fixture test**：AM/PM settlement、first-bid/last-bid/last-ask 分支匹配。
19. **stress monotonicity test**：其他输入相同，short put 在更深下跌和更高 IV 下的
    liquidation NAV 不得改善。
20. **reproducibility test**：相同代码、manifest、参数和 seed 产生相同账本与指标。

### 15.2 发布阻断条件

任一以下条件存在时，结果必须是 `invalid` 或 `insufficient_data`：

- `max_input_available_at > decision_at`；
- 历史 universe 来源于当前目录且未披露存续偏差；
- 合约 style、settlement、multiplier 或 deliverable 未知；
- 公司行动未解析却继续交易调整合约；
- 无有效 NBBO/size 却生成成交；
- 费用或日历没有版本；
- 账本不守恒、现金为负或担保不足；
- 提前指派完全忽略且没有边界情景；
- Greeks 符号或 snapshot 时点未知；
- 外层 OOS 被用于调参；
- 任何 golden test 失败。

## 16. 明确禁止事项

以下实现无论回测收益多好都不得通过验收：

- 用当前期权链、当前成分股或当前“熟悉列表”伪造历史可交易 universe；
- 使用决策后才发布的 OI、成交量、财报、分红、IV、Greeks 或质量标记；
- 默认按 mid、收盘价、日内 high/low 或理论价成交；
- 无报价时按昨收、内在价值、Black-Scholes 或零价补成交；
- 忽略 NBBO size，假设任意张数全部成交；
- 把缺失事件、缺失报价或缺失公司行动当成“没有风险”；
- 仅调整 underlying price 和 strike 就声称支持 OCC 调整合约；
- 把 Ex-by-Ex 当成绝对自动行权，或忽略收盘后的 pin risk；
- 把美式提前指派设为永不发生，却不披露为乐观下界；
- 指派后不创建真实股票、现金和 T+1 交割状态；
- 用权利金或保证金而非账户 NAV 作为收益分母；
- 权利金在现金和收益中重复计算；
- 把 short put delta 记成负值，或混用绝对 delta 与带符号 delta；
- 看到 OOS 后继续优化同一 OOS 并仍称其为样本外；
- 删除停牌、退市、并购或亏损路径来“清洗异常值”；
- 将 Cboe PUT/WPUT 的 SPX 欧洲式结果直接宣称为个股美式 CSP 实盘结果；
- 将高权利金、历史正收益或 put-write 风险溢价描述为“无风险套利”。

## 17. 主要证据来源

- [OCC Characteristics and Risks of Standardized Options](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document)
- [OCC Equity Options Product Specifications](https://www.theocc.com/clearance-and-settlement/clearing/equity-options-product-specifications)
- [OCC Options Assignment FAQ](https://www.optionseducation.org/referencelibrary/faq/options-assignment)
- [OCC Options Exercise FAQ](https://www.optionseducation.org/referencelibrary/faq/options-exercise)
- [FINRA Rule 2360](https://www.finra.org/rules-guidance/rulebooks/finra-rules/2360)
- [OPRA Pillar Output / BBO Specification](https://cdn.opraplan.com/documents/OPRA_Pillar_Output_Specification.pdf)
- [Cboe Option EOD Summary](https://datashop.cboe.com/option-eod-summary)
- [Cboe U.S. Options Reference Data](https://www.cboe.com/markets/us/options/market-statistics/reference-data/)
- [Cboe Hours & Holidays](https://www.cboe.com/about/hours/us-options)
- [Cboe S&P 500 PutWrite Indices Methodology](https://cdn.cboe.com/api/global/us_indices/governance/Cboe_SP_500_PutWrite_Indices_Methodology.pdf)
- [Cboe PutWrite Indices Methodology](https://cdn.cboe.com/api/global/us_indices/governance/Cboe_PutWrite_Indices_Methodology.pdf)
- [OCC Margin Methodology](https://www.theocc.com/risk-management/margin-methodology)
- [Duarte et al., Too Good to Be True: Look-Ahead Bias in Empirical Options Research](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4590083)
- [Bailey et al., The Probability of Backtest Overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)
- [Harvey, Liu & Zhu, ...and the Cross-Section of Expected Returns](https://www.nber.org/papers/w20592)
