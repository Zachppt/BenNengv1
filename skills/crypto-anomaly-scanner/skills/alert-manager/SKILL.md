---
name: alert-manager
description: >
  管理妖币监控系统的预警状态。处理 /mute、/watch、/status、/history、
  /help 指令。当用户想要静默某代币预警、加入高频监控、查看系统状态或
  历史异动记录时触发。
version: 1.0.0
parent: crypto-anomaly-scanner
---

# alert-manager

预警状态管理 Skill。不调用 LLM，纯读写共享存储。

---

## 指令列表

### /status
读取 `./shared/scan_result.json`，输出：
```
⚙️ 系统状态
上次扫描：{scan_ts}（{elapsed}分钟前）
下次扫描：{next_scan_ts}
当前 HIGH：{high_count}个
当前 MEDIUM：{medium_count}个
高频监控：{hfreq_tokens}
TWAP检测：{coldstart_done ? "✅已激活" : "🔄冷启动中"}
```

### /history TOKEN
查询 snapshots.db 过去 24 小时该代币的快照，
按时间倒序列出评分变化和阶段切换节点：
```
📋 TRIA 过去 24 小时异动记录

10:49  评分 18  🔴 逼空进行中  ← 当前
09:34  评分 14  🔴 逼空进行中
08:19  评分  9  🔴 逼空蓄力中  ← 阶段切换
07:04  评分  4  🔵 建仓中
...
```

### /mute TOKEN [时长]
将代币加入静默列表，默认 24 小时：
- 写入 `./shared/alert_state.json` 的 muted 字段
- 时长支持：1h / 6h / 24h / 7d

### /watch TOKEN
将代币加入高频监控（1 分钟快照）：
- 写入 `./shared/alert_state.json` 的 hfreq_tokens 字段
- 脚本下一轮读取后切换该代币为 1 分钟快照模式

### /help
```
📖 妖币监控系统指令

/analyze TOKEN  深度分析（约15秒）
/status         系统运行状态
/history TOKEN  过去24小时异动记录
/mute TOKEN     静默预警（默认24小时）
/watch TOKEN    加入高频监控（1分钟快照）
/help           显示本帮助
```

---

## alert_state.json 结构

```json
{
  "muted": {
    "TRIA": 1746699155,
    "MYX":  1746612555
  },
  "hfreq_tokens": ["TRIA", "COAI"],
  "updated_ts": 1746612555
}
```
