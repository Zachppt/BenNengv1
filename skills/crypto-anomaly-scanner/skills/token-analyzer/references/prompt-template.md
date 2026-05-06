# prompt-template.md
# token-analyzer 的 LLM Prompt 模板

## 使用说明

将以下模板中的占位符替换为实际数据后，作为 user message 发送给 Claude API。
不要传入原始 API 数据，只传入已计算好的异常标记和趋势摘要。

---

## System Prompt

```
你是一个加密衍生品市场操纵行为分析专家，专注于识别做市商操纵手法。

分析原则：
1. 不重新计算数值，直接基于提供的数据进行推理
2. 结论必须有具体数据支撑，不能泛泛而谈
3. 对不确定的部分如实说明，不猜测
4. 输出使用中文，总字数控制在 300 字以内
5. 格式严格按照要求输出，不添加多余内容
```

---

## User Prompt 模板

```
以下是 {TOKEN}/USDT 的异常检测报告，请进行分析。

════════════════════════════════
异动评分：{SCORE}
当前阶段（规则判断）：{PHASE}
════════════════════════════════

【触发规则】
{TRIGGERED_RULES}

【关键数值】
跨所合约价差：{FUTURES_SPREAD}%（异常方：{OUTLIER_EXCHANGE}）
现货-合约基差：{MAX_BASIS}%（{BASIS_DIRECTION}，{BASIS_EXCHANGE}）
全平台资金费率均值：{FUNDING_MEAN}%
各所资金费率：{FUNDING_DETAIL}
OI 集中度：{OI_SHARES}
订单簿失衡度：{IMBALANCE_DETAIL}
Taker 买卖比：{TAKER_RATIO}（当前）

【历史趋势（过去 2 小时）】
{HISTORY_SUMMARY}

════════════════════════════════
请按以下格式输出，不添加其他内容：

**阶段判断**
[一句话，从以下选择：建仓中 / 逼空蓄力中 / 逼空进行中 / 逼空收尾 / 出货进行中 / 多空双杀 / 流动性猎杀 / 信号混合]

**主操纵场所**
[交易所名称 + 一句话说明为什么]

**操纵模式**
[从以下选择，可多选：MYX型逼空 / COAI型拉高出货 / TWAP建仓 / 洗盘刷量 / 跨所标记价格操纵 / 多空双杀 / 定点清算 / 流动性猎杀 / 现货合约背离 / 无法判断]

**风险评级**
操纵风险：[低 / 中 / 高 / 极高]
做多风险：[低 / 中 / 高 / 极高]
做空风险：[低 / 中 / 高 / 极高]

**核心结论**
[一句话，不超过 40 字，说明当前最重要的风险或机会]
```

---

## 占位符填充说明

| 占位符 | 来源 | 格式示例 |
|--------|------|----------|
| `{TOKEN}` | 用户输入 | `TRIA` |
| `{SCORE}` | scan_result.json | `18` |
| `{PHASE}` | scan_result.json | `🔴 逼空进行中` |
| `{TRIGGERED_RULES}` | scan_result.json triggered 列表 | 每条一行，格式：`- [L1] 跨所价差5.95%...` |
| `{FUTURES_SPREAD}` | agg.max_futures_spread × 100 | `5.95` |
| `{OUTLIER_EXCHANGE}` | agg.futures_outlier | `bybit` |
| `{MAX_BASIS}` | agg.max_basis × 100 | `7.00` |
| `{BASIS_DIRECTION}` | basis[max_basis_ex] > 0 ? "合约溢价" : "合约折价" | `合约溢价` |
| `{BASIS_EXCHANGE}` | agg.max_basis_ex | `bybit` |
| `{FUNDING_MEAN}` | agg.funding_mean × 100 | `-0.0033` |
| `{FUNDING_DETAIL}` | agg.fundings 格式化 | `binance:+0.0121% okx:+0.0118% bybit:-0.0480%` |
| `{OI_SHARES}` | agg.oi_shares 格式化 | `binance:44.9% bybit:42.2%` |
| `{IMBALANCE_DETAIL}` | agg.imbalances 格式化 | `binance:0.52 bybit:0.79` |
| `{TAKER_RATIO}` | taker_ratio.current | `1.2536` |
| `{HISTORY_SUMMARY}` | build_history_summary() 输出 | 多行趋势序列 |

---

## 填充后示例

```
以下是 TRIA/USDT 的异常检测报告，请进行分析。

════════════════════════════════
异动评分：18
当前阶段（规则判断）：🔴 逼空进行中
════════════════════════════════

【触发规则】
- [L1] 跨所合约价差5.95%，异常方:bybit
- [L1] bybit 基差合约溢价7.00%，合约严重脱离现货
- [L1] bybit OI 4h暴增31.4%
- [L1] bybit 失衡度0.79 极度异常
- [L1] bybit 卖方深度4h下降37.3%
- [L2] bybit 资金费率连续3期为负
- [L2] binance 失衡度0.52 明显异常
- [L2] 大户多头(1.35)vs 散户空头(0.89)
- [L1] 跨所价差>0.3%持续90分钟

【关键数值】
跨所合约价差：5.95%（异常方：bybit）
现货-合约基差：7.00%（合约溢价，bybit）
全平台资金费率均值：-0.0033%
各所资金费率：binance:+0.0121% okx:+0.0118% bybit:-0.0480% bitget:+0.0109%
OI 集中度：binance:44.9% okx:8.3% bybit:42.2% bitget:4.6%
订单簿失衡度：binance:0.52 okx:0.02 bybit:0.79 bitget:-0.06
Taker 买卖比：1.2536（当前）

【历史趋势（过去 2 小时）】
binance_失衡度趋势: 0.21 → 0.28 → 0.35 → 0.44 → 0.52
bybit_OI趋势(M): 101 → 118 → 143 → 198 → 264 → 313
bybit_资金费率趋势(%): -0.0089 → -0.0310 → -0.0480
跨所价差趋势(%): 0.08 → 0.19 → 0.41 → 0.55 → 0.64
```
