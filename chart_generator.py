# ============================================================
# alerts/chart_generator.py — K线图生成模块 v3
# 输出PNG图片推送到Telegram
# ============================================================

import io
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import matplotlib
    matplotlib.use("Agg")   # 非交互模式
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.patches import FancyBboxPatch
    import matplotlib.gridspec as gridspec
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    logger.warning("matplotlib 未安装，K线图功能不可用")


def generate_kline_chart(token: str,
                          klines: list,
                          analysis: dict,
                          entry_zone: dict = None) -> Optional[bytes]:
    """
    生成K线图（PNG字节流）
    包含：K线 + OBV + 成交量 + 标注

    返回：PNG字节流（发送给Telegram）
    """
    if not MATPLOTLIB_AVAILABLE:
        return None

    if not klines or len(klines) < 5:
        return None

    try:
        return _draw_chart(token, klines, analysis, entry_zone)
    except Exception as e:
        logger.error(f"K线图生成失败 {token}: {e}")
        return None


def _draw_chart(token: str, klines: list,
                analysis: dict, entry_zone: dict) -> bytes:

    # ── 数据准备 ──
    n       = len(klines)
    x       = list(range(n))
    opens   = [k["open"]   for k in klines]
    highs   = [k["high"]   for k in klines]
    lows    = [k["low"]    for k in klines]
    closes  = [k["close"]  for k in klines]
    volumes = [k["volume"] for k in klines]

    obv_series = analysis.get("obv_series", [])
    cmf_series = analysis.get("cmf_series", [])

    # 对齐OBV序列长度
    if len(obv_series) < n:
        from rules.kline_analyzer import calc_obv, calc_cmf
        obv_series = calc_obv(klines)
        cmf_series = calc_cmf(klines)

    # ── 图表布局 ──
    fig = plt.figure(figsize=(12, 9), facecolor="#0d1117")
    gs  = gridspec.GridSpec(
        4, 1,
        height_ratios=[4, 1.2, 1, 1],
        hspace=0.08
    )

    ax_k   = fig.add_subplot(gs[0])   # K线
    ax_vol = fig.add_subplot(gs[1])   # 成交量
    ax_obv = fig.add_subplot(gs[2])   # OBV
    ax_cmf = fig.add_subplot(gs[3])   # CMF

    for ax in [ax_k, ax_vol, ax_obv, ax_cmf]:
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#8b949e", labelsize=7)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        for spine in ax.spines.values():
            spine.set_color("#30363d")

    # ── K线绘制 ──
    for i in x:
        color = "#26a641" if closes[i] >= opens[i] else "#da3633"
        # 实体
        ax_k.bar(i, abs(closes[i] - opens[i]),
                 bottom=min(opens[i], closes[i]),
                 color=color, width=0.7, linewidth=0)
        # 影线
        ax_k.plot([i, i], [lows[i], highs[i]],
                  color=color, linewidth=0.8)

    # ── 支撑阻力标注 ──
    sr  = analysis.get("support_resistance", {})
    poc = sr.get("levels", {}).get("poc", 0) if sr else 0
    vah = sr.get("levels", {}).get("vah", 0) if sr else 0
    val = sr.get("levels", {}).get("val", 0) if sr else 0
    recent_high = sr.get("levels", {}).get("recent_high", 0) if sr else 0
    recent_low  = sr.get("levels", {}).get("recent_low", 0)  if sr else 0

    if poc > 0:
        ax_k.axhline(poc, color="#f0883e", linewidth=1.2,
                     linestyle="--", alpha=0.8, label=f"POC ${poc:.5g}")
    if vah > 0:
        ax_k.axhline(vah, color="#58a6ff", linewidth=0.8,
                     linestyle=":", alpha=0.6, label=f"VAH ${vah:.5g}")
    if val > 0:
        ax_k.axhline(val, color="#58a6ff", linewidth=0.8,
                     linestyle=":", alpha=0.6, label=f"VAL ${val:.5g}")
    if recent_high > 0:
        ax_k.axhline(recent_high, color="#ffffff", linewidth=0.6,
                     linestyle="-.", alpha=0.3, label=f"前高 ${recent_high:.5g}")
    if recent_low > 0:
        ax_k.axhline(recent_low, color="#ffffff", linewidth=0.6,
                     linestyle="-.", alpha=0.3, label=f"前低 ${recent_low:.5g}")

    # ── 入场区间标注 ──
    if entry_zone:
        el = entry_zone.get("entry_low", 0)
        eh = entry_zone.get("entry_high", 0)
        sl = entry_zone.get("stop_loss", 0)
        t1 = entry_zone.get("target_1", 0)

        if el and eh and el < eh:
            ax_k.axhspan(el, eh, alpha=0.15, color="#26a641",
                         label=f"入场区间 ${el:.5g}~${eh:.5g}")
        if sl:
            ax_k.axhline(sl, color="#da3633", linewidth=1.0,
                         linestyle="--", alpha=0.7, label=f"止损 ${sl:.5g}")
        if t1:
            ax_k.axhline(t1, color="#3fb950", linewidth=1.0,
                         linestyle="--", alpha=0.7, label=f"目标一 ${t1:.5g}")

    # ── 形态标注 ──
    patterns = analysis.get("patterns", {})
    cb = patterns.get("channel_breakout")
    db = patterns.get("double_bottom")
    dt = patterns.get("double_top")

    if cb and cb.get("confirmed"):
        ax_k.text(n - 1, highs[-1] * 1.01, "⬆ 通道突破",
                  color="#26a641", fontsize=8, ha="right")
    if db and db.get("obv_divergence"):
        ax_k.text(n - 1, lows[-1] * 0.99, "⬆ 双底",
                  color="#26a641", fontsize=8, ha="right")
    if dt and dt.get("obv_divergence"):
        ax_k.text(n - 1, highs[-1] * 1.01, "⬇ 双顶",
                  color="#da3633", fontsize=8, ha="right")

    # ── 图例 ──
    ax_k.legend(loc="upper left", fontsize=6, facecolor="#161b22",
                labelcolor="#8b949e", framealpha=0.8, ncol=3)

    # ── 成交量 ──
    vol_colors = ["#26a641" if closes[i] >= opens[i] else "#da3633"
                  for i in x]
    ax_vol.bar(x, volumes, color=vol_colors, width=0.7, linewidth=0, alpha=0.8)
    avg_vol = sum(volumes) / len(volumes) if volumes else 0
    ax_vol.axhline(avg_vol, color="#8b949e", linewidth=0.8, linestyle="--", alpha=0.5)
    ax_vol.set_ylabel("Vol", color="#8b949e", fontsize=7)
    ax_vol.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f"{x/1e6:.1f}M" if x >= 1e6 else f"{x/1e3:.0f}K")
    )

    # ── OBV ──
    if obv_series:
        obv_x = list(range(len(obv_series)))
        ax_obv.plot(obv_x, obv_series, color="#58a6ff",
                    linewidth=1.2, label="OBV")
        ax_obv.fill_between(obv_x, obv_series, alpha=0.1, color="#58a6ff")

        # OBV突破标注
        if analysis.get("obv_signals", {}).get("obv_breakout"):
            ax_obv.text(len(obv_series)-1, obv_series[-1],
                        " ↑突破", color="#26a641", fontsize=7)
        elif analysis.get("obv_signals", {}).get("positive_divergence"):
            ax_obv.text(len(obv_series)-1, obv_series[-1],
                        " ↑背离", color="#26a641", fontsize=7)

    ax_obv.set_ylabel("OBV", color="#8b949e", fontsize=7)
    ax_obv.axhline(0, color="#30363d", linewidth=0.5)

    # ── CMF ──
    if cmf_series:
        cmf_x = list(range(len(cmf_series)))
        cmf_colors = ["#26a641" if v >= 0 else "#da3633" for v in cmf_series]
        ax_cmf.bar(cmf_x, cmf_series, color=cmf_colors,
                   width=0.7, linewidth=0, alpha=0.8)
        ax_cmf.axhline(0,    color="#8b949e", linewidth=0.8)
        ax_cmf.axhline(0.1,  color="#26a641", linewidth=0.5,
                       linestyle="--", alpha=0.5)
        ax_cmf.axhline(-0.1, color="#da3633", linewidth=0.5,
                       linestyle="--", alpha=0.5)

    ax_cmf.set_ylabel("CMF", color="#8b949e", fontsize=7)
    ax_cmf.set_ylim(-0.5, 0.5)

    # ── 隐藏x轴 tick（只在最下方显示）──
    for ax in [ax_k, ax_vol, ax_obv]:
        ax.set_xticklabels([])

    # ── 标题 ──
    prob = analysis.get("probability", 0)  # 从外部传入
    trend= analysis.get("trend", {}).get("desc", "")
    ax_k.set_title(
        f"{token}/USDT  1H K线  |  {trend}",
        color="#e6edf3", fontsize=10, pad=8, loc="left"
    )

    # 时间戳
    ts_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    fig.text(0.99, 0.01, f"Generated {ts_str}",
             ha="right", va="bottom", color="#484f58", fontsize=6)

    # ── 导出 ──
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130,
                bbox_inches="tight", facecolor="#0d1117")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
