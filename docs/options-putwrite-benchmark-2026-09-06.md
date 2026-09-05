# Put-write 官方指数基准复算（2026-09-06）

这是一组策略指数的**研究基线**，不是个人账户可直接成交的回测，也不是对某只
股票卖 Put 的收益承诺。复算只使用 Cboe 官方日度指数 CSV，不用 Black-Scholes
合成历史权利金，不假设总能以收盘价成交。

## 结论先行

- 卖 Put 的长期收益来自承担波动率、跳跃和左尾风险，不是无风险套利。
- 四个 put-write 指数在样本内的波动与股票 Beta 普遍低于 SPX，但金融危机和
  新冠急跌期间仍出现显著亏损。
- 样本内 CAGR 最高的 PUTD，同时也是四个 put-write 指数中日度最大回撤最深的
  一个；“更高权利金 / 更高收益”不能脱离尾部风险讨论。
- WPUT 的日度最大回撤较浅，但样本内 CAGR 也最低；周度到期并没有自动产生
  免费优势。
- SPX 文件是价格指数，而现金担保的 put-write 指数含抵押现金收益，因此下表
  不应把两者 CAGR 当作严格同口径的策略胜负。这里保留 SPX 只为了观察回撤、
  波动和 Beta；正式策略研究应加入 S&P 500 总回报指数与现金 / T-bill 基线。

## 同区间结果

样本区间统一为 2007-01-03 至 2026-09-04。年化波动按 252 个交易日计算；
月末最大回撤只使用每月最后一个有效观察值。

| 指数 | 观察数 | CAGR | 日度年化波动 | 日度最大回撤 | 月末最大回撤 | 最差月份 | 正收益月份 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| PUT | 4,950 | 7.20% | 13.83% | -37.09% | -32.66% | 2008-10（-17.65%） | 73.31% |
| PUTY | 4,950 | 5.31% | 12.10% | -33.04% | -28.91% | 2008-10（-17.09%） | 77.12% |
| WPUT | 4,947 | 4.37% | 12.33% | -28.62% | -24.17% | 2008-10（-14.14%） | 65.68% |
| PUTD | 4,950 | 9.46% | 16.11% | -45.03% | -40.40% | 2008-10（-15.13%） | 70.34% |
| SPX（价格指数） | 4,950 | 9.00% | 19.67% | -56.78% | -52.56% | 2008-10（-16.94%） | 63.98% |

## 压力期结果

区间收益采用窗口开始日之后的第一个有效观察值，和窗口结束日之前的最后一个
有效观察值。

| 压力窗口 | PUT | PUTY | WPUT | PUTD | SPX（价格指数） |
| --- | ---: | ---: | ---: | ---: | ---: |
| 全球金融危机：2007-10-09 至 2009-03-09 | -34.83% | -31.02% | -23.61% | -44.67% | -56.78% |
| 新冠急跌：2020-02-19 至 2020-03-23 | -28.92% | -27.31% | -25.30% | -31.42% | -33.92% |
| 2022 加息周期：2022-01-03 至 2022-12-30 | -7.85% | -1.63% | -14.53% | -15.27% | -19.95% |

截至 2026-09-04 的滚动 CAGR 只用于描述区间敏感性，不用于推断未来收益：

| 回看区间 | PUT | PUTY | WPUT | PUTD | SPX（价格指数） |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 年 | 15.72% | 14.18% | 14.44% | 19.97% | 18.72% |
| 3 年 | 12.71% | 10.47% | 9.56% | 16.18% | 19.75% |
| 5 年 | 9.30% | 8.85% | 4.47% | 9.89% | 11.32% |
| 10 年 | 8.38% | 6.81% | 3.70% | 11.14% | 13.45% |

## 与 SPX 的月度关系

| 指数 | 月度 Beta | 月度相关系数 |
| --- | ---: | ---: |
| PUT | 0.601 | 0.864 |
| PUTY | 0.455 | 0.780 |
| WPUT | 0.528 | 0.828 |
| PUTD | 0.760 | 0.954 |
| SPX | 1.000 | 1.000 |

## 指数含义与证据边界

- [Cboe PUT 指数说明](https://cdn.cboe.com/resources/indices/factsheet/CboeGlobalIndices_PUT-Index.pdf)
  描述了以 T-bill 抵押、按月卖出接近平值 SPX Put 的基准。
- [Cboe PUTY 指数说明](https://cdn.cboe.com/resources/indices/factsheet/CboeGlobalIndices_PUTY-Index.pdf)
  描述了约 2% 虚值的月度 PutWrite 基准。
- [Cboe PUTD 指数说明](https://cdn.cboe.com/resources/indices/factsheet/CboeGlobalIndices_PUTD-Index.pdf)
  披露其动态选择、历史统计与回测边界；指数发布前数据是理论回测，不是账户实盘。
- [Cboe PutWrite 指数方法论](https://cdn.cboe.com/api/global/us_indices/governance/Cboe_PutWrite_Indices_Methodology.pdf)
  和 [PUTD 方法论](https://cdn.cboe.com/api/global/us_indices/governance/Cboe_Validus_Dynamic_PutWrite_Indices_Methodology.pdf)
  是理解展期、报价、抵押现金与交易成本假设的主证据。
- 期权风险溢价研究通常把收益解释为波动和跳跃风险补偿，而非稳定套利；参见
  [NBER：Option Returns and the Cross-Sectional Predictability of Implied Volatility](https://www.nber.org/papers/w10912)
  以及 [cash-secured put-write 研究摘要](https://ideas.repec.org/a/pal/assmgt/v25y2024i1d10.1057_s41260-023-00333-0.html)。

## 可复现方式

脚本：`research/options_putwrite_benchmark.py`。原始 CSV 不入库，只在运行时从
Cboe 官方地址取得；报告保留 SHA-256，避免后来文件修订却仍被当作同一输入。

官方日度文件：
[PUT](https://cdn.cboe.com/api/global/us_indices/daily_prices/PUT_History.csv)、
[PUTY](https://cdn.cboe.com/api/global/us_indices/daily_prices/PUTY_History.csv)、
[WPUT](https://cdn.cboe.com/api/global/us_indices/daily_prices/WPUT_History.csv)、
[PUTD](https://cdn.cboe.com/api/global/us_indices/daily_prices/PUTD_History.csv)、
[SPX](https://cdn.cboe.com/api/global/us_indices/daily_prices/SPX_History.csv)。

```bash
python3.11 research/options_putwrite_benchmark.py \
  --series PUT=/tmp/PUT_History.csv \
  --series PUTY=/tmp/PUTY_History.csv \
  --series WPUT=/tmp/WPUT_History.csv \
  --series PUTD=/tmp/PUTD_History.csv \
  --series SPX=/tmp/SPX_History.csv \
  --benchmark SPX \
  --start 2007-01-03 \
  --end 2026-09-04
```

| 输入 | SHA-256 |
| --- | --- |
| PUT | `de5e047788418441d6c65f3b5d55c80e2664c1406b9d017550b532d092209fdd` |
| PUTY | `b6bb0ab28d6d6b579a4e67bebc2a6bd19e04f69ccaeb1970430656db20cdefb8` |
| WPUT | `59edaa44e9a07c6b4ff941f0d524c324fdb1a8f42c0ba5ab9866d3aee7411c6c` |
| PUTD | `16c0bf9393faf8d11be28ba3bf76872e7603dc700989d5c30bb8cb1c1ab45776` |
| SPX | `863d6b0e3f716a5ada5c56e176fdade1d3a0d21fb53ba64efcb606d85e5f8b9b` |

## 还不能从这组结果推出什么

- 不能据此决定今天应卖哪只股票、哪个行权价或多少张。
- 不能覆盖单股财报跳空、停牌、并购、拆股、特殊交割物、提前指派和分红风险。
- 不能代表买卖价差、佣金、监管费、滑点、税务、保证金变化后的个人账户结果。
- 不能证明某一组 Delta / DTE / 止盈参数在样本外仍然有效。

下一阶段必须用历史 NBBO、IV / Greeks、标的价格、分红、财报、利率和公司行动做
逐合约、逐时点、买卖价两侧成交的 walk-forward 回测，才可以给“熟悉标的池”
出具策略级结论。

以上分析仅供参考，不构成投资建议。股市有风险，投资需谨慎。
