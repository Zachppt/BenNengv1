---
name: token-analyzer
description: >
  对指定代币进行深度操纵行为分析。读取本地缓存快照和当前异常标记，
  补充拉取即时价格，调用 LLM 生成完整分析报告。当用户发送
  /analyze TOKEN 或自然语言询问特定代币异动情况时触发。
version: 1.0.0
parent: crypto-anomaly-scanner
---

# token-analyzer

用户主动触发的深度分析 Skill。
脚本层已经完成数据采集和规则计算，本 Skill 只负责读缓存 + LLM 推理 + 推送。

---

## 执行步骤

### Step 1：解析代币名称
```
输入：用户消息（/analyze TRIA 或 "分析一下TRIA"）
输出：TOKEN = "TRIA"（统一大写，去除 USDT 后缀）
```

### Step 2：读取缓存数据

从 `./shared/snapshots.db` 查询：
```sql
SELECT ts, data_json FROM snapshots
WHERE token = '{TOKEN}' AND exchange = 'binance'
ORDER BY ts DESC LIMIT 8
```

从 `./shared/scan_result.json` 读取：
- 该代币当前触发的规则列表
- 当前评分和阶段判断

**如果代币不在缓存中：**
→ 告知用户"该代币未在监控列表，将进行实时分析（无历史趋势）"
→ 继续执行 Step 3

### Step 3：补充即时数据

实时拉取（不走缓存，约 0.5 秒）：
- 4 所 Best Bid/Ask（合约）
- 4 所现货价格

目的：解决缓存延迟问题，确保用户看到的是最新价格。

### Step 4：构建 LLM Prompt

参考 `references/prompt-template.md` 构建 Prompt。

**核心原则：**
- 只传入已计算好的异常标记，不传原始 API 数据
- LLM 做推理和归纳，不做数值计算
- Prompt 包含：触发规则列表、关键数值、历史趋势摘要

### Step 5：调用 Claude API

```python
model:      "claude-sonnet-4-20250514"
max_tokens: 1000
```

### Step 6：格式化并推送

使用 `alerts/telegram.py` 中的 `fmt_analysis_report()` 格式化，
包含：即时价格、现货基差、AI 分析文本、免责声明。

---

## 历史趋势摘要生成

从快照中提取关键趋势（喂给 LLM）：

```python
# 每个交易所提取以下趋势序列（时间正序）
- imbalance：失衡度趋势
- oi_usd：OI 趋势（百万元）
- funding：资金费率趋势
- max_spread：跨所价差趋势（只取 binance 快照中的汇总值）
```

格式示例：
```
binance_失衡度趋势: 0.21 → 0.28 → 0.35 → 0.44 → 0.52
bybit_OI趋势(M): 101 → 118 → 143 → 198 → 264 → 313
bybit_资金费率趋势(%): -0.0089 → -0.0310 → -0.0480
跨所价差趋势(%): 0.08 → 0.19 → 0.41 → 0.55 → 0.64
```

---

## 错误处理

| 情况 | 处理方式 |
|------|----------|
| 代币不存在于任何交易所 | 返回"未找到该合约" |
| 缓存为空 | 实时分析，注明无历史趋势 |
| LLM 调用失败 | 返回规则引擎的纯文本结果（不含 AI 分析） |
| 即时数据拉取失败 | 使用缓存中最新快照，标注数据时间 |

---

## 参考文件

- `references/prompt-template.md`：LLM Prompt 完整模板
- `references/phase-guide.md`：阶段判断参考（辅助 LLM）
- `references/pattern-guide.md`：操纵模式识别参考（辅助 LLM）
