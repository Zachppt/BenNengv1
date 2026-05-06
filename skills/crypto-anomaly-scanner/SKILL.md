---
name: crypto-anomaly-scanner
description: >
  加密衍生品妖币异动监控系统。当用户询问某个代币是否存在市场操纵、
  做市商行为、异常 OI、资金费率异常、订单簿异动、跨所价差、现货合约
  基差异常时触发。支持指令：/analyze TOKEN（深度分析）、/status（系统
  状态）、/history TOKEN（异动历史）、/mute TOKEN（静默预警）、
  /watch TOKEN（加入高频监控）。当用户提到"妖币"、"拉盘"、"逼空"、
  "做市商"、"资金费率"、"基差"、"插针"、"双杀"等关键词时自动触发。
  即使用户只说"分析一下 TRIA"或"COAI 最近有没有异动"也应触发此 Skill。
version: 1.0.0
requires:
  - python >= 3.10
  - aiohttp
  - sqlite3
---

# crypto-anomaly-scanner

妖币异动监控系统的 OpenClaw Skill 层。
本 Skill 不负责数据采集（由 VPS 上的 Python 脚本负责），
只负责：读取共享缓存 → 调用 LLM 深度分析 → 推送报告。

---

## 系统架构说明

```
VPS Python 脚本（持续运行）
  └── 每 15 分钟扫描 4 所合约 + 现货数据
      └── 写入 ./shared/scan_result.json
          └── 写入 ./shared/snapshots.db

OpenClaw Skill（本层，按需触发）
  └── 读取共享存储
      └── 调用 LLM
          └── 推送 Telegram
```

---

## 触发判断

收到用户消息后，判断是否包含以下意图：

**直接指令**
- `/analyze TOKEN` → 调用 `token-analyzer` Skill
- `/status`        → 读取 scan_result.json 返回系统状态
- `/history TOKEN` → 查询 snapshots.db 返回该代币历史异动
- `/mute TOKEN`    → 写入静默列表
- `/watch TOKEN`   → 加入高频监控列表
- `/help`          → 返回指令说明

**自然语言识别**（转换为对应指令）
- "分析一下 TRIA" → `/analyze TRIA`
- "COAI 有没有异动" → `/analyze COAI`
- "现在有什么妖币" → 读取 scan_result.json 返回当前 HIGH ALERT 列表
- "系统还在跑吗" → `/status`
- "MYX 最近的资金费率" → `/analyze MYX`

---

## 指令处理流程

### /analyze TOKEN

1. 从 `./shared/snapshots.db` 读取该代币最近 8 条快照
2. 从 `./shared/scan_result.json` 读取当前触发的规则列表
3. 实时补充拉取 Best Bid/Ask 和现货价格（4 所并发，约 0.5 秒）
4. 构建 LLM Prompt（见 `skills/token-analyzer/references/prompt-template.md`）
5. 调用 Claude API（claude-sonnet-4-20250514，max_tokens=1000）
6. 格式化完整报告并推送 Telegram

> 详细实现见 `skills/token-analyzer/SKILL.md`

### /status

读取 `./shared/scan_result.json`，返回：
- 上次扫描时间
- 下次扫描时间
- 当前 HIGH ALERT 代币数
- 当前 MEDIUM ALERT 代币数
- 冷启动状态
- 高频监控代币列表

### /history TOKEN

查询 `./shared/snapshots.db` 中该代币过去 24 小时的异动记录：
- 按时间倒序列出异动评分变化
- 标注首次触发时间
- 标注阶段切换节点

---

## 数据文件说明

### scan_result.json 结构
```json
{
  "scan_ts": 1746612555,
  "scan_id": 47,
  "duration_sec": 38,
  "total_scanned": 412,
  "high_alerts": [
    {
      "token": "TRIA",
      "score": 18,
      "level": "HIGH",
      "phase": "🔴 逼空进行中",
      "triggered": [...],
      "pushed": false
    }
  ],
  "medium_alerts": [...],
  "system": {
    "coldstart_done": true,
    "hfreq_tokens": ["TRIA"],
    "next_scan_ts": 1746613455
  }
}
```

### snapshots.db 核心字段
每条快照包含：price, imbalance, spread, bid_depth_usd, ask_depth_usd,
oi, oi_usd, funding, max_spread, total_oi（每个交易所分别存储）

---

## 注意事项

- LLM 只在 `/analyze` 时调用，不在自动扫描时调用
- 缓存数据延迟约 15~40 秒，即时价格在用户触发时实时拉取
- 如果代币不在监控列表，实时拉取后返回（无历史趋势部分）
- 推送所有消息时标注数据采集时间戳
