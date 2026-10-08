"""出图。所有函数都接收数据、返回 matplotlib 的 figure 对象，
不读文件也不存文件——这样既能在网页界面里直接显示，也能自己存成图片。

用 Agg 后端（不需要图形界面的绘图后端），因为要在没有桌面的环境里跑。
中文字体用微软雅黑，否则中文会显示成方框。
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap

import yield_calc as yc

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 110

# 良率用的配色：越红越差
CMAP_FAIL = LinearSegmentedColormap.from_list("fail", ["#fdf3f0", "#e8503a", "#7a0d00"])
COLOR_BEFORE = "#2f6fb5"
COLOR_AFTER = "#d1493a"


def _no_data(msg: str = "没有数据"):
    """统一的"空数据"占位图，免得界面上出现空白或报错。"""
    fig, ax = plt.subplots(figsize=(7, 2.2))
    ax.text(0.5, 0.5, msg, ha="center", va="center", fontsize=13, color="#888")
    ax.axis("off")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 良率
# ---------------------------------------------------------------------------

def fig_fail_rate_bar(groups: Sequence[dict], *, title: str = "失败率",
                      xlabel: str = "", top: int = 32,
                      key_fmt=str, sort_by_key: bool = False):
    """失败率柱状图。groups 是 yield_by / yield_by_pair 的返回值。

    ⚠️ 画的是**失败率**不是良率：柱子越高越差。分支、频率这类维度不是
    交付单位，不能算良率（良率只有场次级 FPY 一种口径）。

    只画有测试数据的组。默认按失败率从高到低（最差的在左边），
    sort_by_key=True 则按分组键排序（看分支分布时更直观）。
    """
    gs = [g for g in groups if g.get("fail_rate_pct") is not None]
    if not gs:
        return _no_data("没有失败率数据")
    gs = gs[:top] if not sort_by_key else sorted(gs, key=lambda g: g["key"])[:top]

    labels = [key_fmt(g["key"]) for g in gs]
    vals = [g["fail_rate_pct"] for g in gs]
    fails = [g["total_failed"] for g in gs]

    fig, ax = plt.subplots(figsize=(max(7, len(gs) * 0.42), 4.2))
    # 颜色按失败次数深浅，让"哪个最严重"一眼看出
    mx = max(fails) if any(fails) else 1
    colors = [CMAP_FAIL(0.15 + 0.85 * f / mx) for f in fails]
    bars = ax.bar(range(len(gs)), vals, color=colors, edgecolor="white", linewidth=0.6)

    # 失败率通常都在 0~1% 之间，纵轴顶格留一点余量就够；
    # 全是 0 时给一个固定小范围，否则 set_ylim(0, 0) 会报错
    hi = max(vals)
    ax.set_ylim(0, hi * 1.25 if hi > 0 else 1.0)
    ax.set_xticks(range(len(gs)))
    ax.set_xticklabels(labels, rotation=45 if max(len(l) for l in labels) > 4 else 0,
                       ha="right" if max(len(l) for l in labels) > 4 else "center",
                       fontsize=8)
    ax.set_ylabel("失败率 %")
    if xlabel:
        ax.set_xlabel(xlabel)
    ax.set_title(title)
    ax.grid(axis="y", alpha=0.25, linestyle=":")
    ax.set_axisbelow(True)

    # 在柱子上标失败次数
    for b, f in zip(bars, fails):
        if f:
            ax.annotate(str(f), (b.get_x() + b.get_width() / 2, b.get_height()),
                        textcoords="offset points", xytext=(0, 2),
                        ha="center", fontsize=7, color="#7a0d00")
    fig.tight_layout()
    return fig


def fig_fail_heatmap(fail_by_branch_freq: dict[tuple[int, float], float], *,
                     title: str = "失败分布（分支 x 频率）",
                     freqs: Sequence[float] | None = None,
                     value_label: str = "失败次数"):
    """分支 x 频率的失败热力图。

    fail_by_branch_freq: {(分支, 频率MHz): 数值}
    """
    if not fail_by_branch_freq:
        return _no_data("这个范围内没有失败记录")

    branches = sorted({b for b, _ in fail_by_branch_freq})
    fs = list(freqs) if freqs else sorted({f for _, f in fail_by_branch_freq})
    grid = np.zeros((len(fs), len(branches)))
    for (b, f), v in fail_by_branch_freq.items():
        if f in fs and b in branches:
            grid[fs.index(f), branches.index(b)] = v

    fig, ax = plt.subplots(figsize=(max(7, len(branches) * 0.38), 2.0 + len(fs) * 0.42))
    im = ax.imshow(grid, aspect="auto", cmap=CMAP_FAIL, vmin=0)
    ax.set_xticks(range(len(branches)))
    ax.set_xticklabels([f"{b}" for b in branches], fontsize=8)
    ax.set_yticks(range(len(fs)))
    ax.set_yticklabels([f"{f:g}" for f in fs], fontsize=9)
    ax.set_xlabel("分支 Branch")
    ax.set_ylabel("频率 MHz")
    ax.set_title(title)
    fig.colorbar(im, ax=ax, label=value_label, fraction=0.025, pad=0.012)

    # 数值直接标在格子里，热力图光看颜色读不出具体数
    vmax = grid.max()
    for i in range(len(fs)):
        for j in range(len(branches)):
            v = grid[i, j]
            if v > 0:
                ax.text(j, i, f"{v:g}", ha="center", va="center", fontsize=6.5,
                        color="white" if v > vmax * 0.55 else "#5a0a00")
    fig.tight_layout()
    return fig


def fig_fleet_heatmap(fails_per_sn: dict[str, dict[int, float]], *,
                      title: str = "机群失败分布（整机 x 分支）",
                      n_branches: int = 32, abnormal_threshold: int = 18):
    """每台整机 x 每个分支的失败热力图。一眼看出"哪些机器坏、坏在哪"。

    失败分支数 >= abnormal_threshold 的机器会被标成异常（多半是测试系统问题，
    不是真的 32 个分支同时坏），序列号前面加 ⚠。
    """
    if not fails_per_sn:
        return _no_data("没有整机数据")

    sns = sorted(fails_per_sn)
    grid = np.zeros((len(sns), n_branches))
    for i, sn in enumerate(sns):
        for b, v in fails_per_sn[sn].items():
            if 0 <= b < n_branches:
                grid[i, b] = v

    labels = []
    for i, sn in enumerate(sns):
        n = int((grid[i] > 0).sum())
        # 用 ASCII 的 [!] 而不是 ⚠ —— 微软雅黑没有警告符号那个字，会显示成方框
        labels.append(f"[!] {sn} ({n})" if n >= abnormal_threshold else sn)

    fig, ax = plt.subplots(figsize=(11, max(3, len(sns) * 0.19)))
    im = ax.imshow(grid, aspect="auto", cmap=CMAP_FAIL, vmin=0)
    ax.set_xticks(range(n_branches))
    ax.set_xticklabels(range(n_branches), fontsize=7)
    ax.set_yticks(range(len(sns)))
    ax.set_yticklabels(labels, fontsize=6.5)
    ax.set_xlabel("分支 Branch")
    ax.set_title(title)
    fig.colorbar(im, ax=ax, label="失败次数", fraction=0.015, pad=0.01)
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# PIM 明细（单台）
# ---------------------------------------------------------------------------

def fig_pim_power(recs: Sequence[dict], *, sn: str = "",
                  measure_prefix: str = "noise_power_level",
                  freqs: Sequence[float] | None = None):
    """PIM 功率的测试前/后对比，6 个频点分面。最接近 RTD 报表那张图。

    recs 是 yield_calc.parse_rows() 的输出。
    只画 value_kind == 'power' 的记录（功率类），单位 dBm。
    """
    power = [r for r in recs
             if r["value_kind"] == "power" and measure_prefix in r["measure"]]
    if not power:
        return _no_data(f"没有 {measure_prefix} 的功率数据")

    fs = list(freqs) if freqs else yc.FREQ_ORDER
    fs = [f for f in fs if any(r["freq_mhz"] == f for r in power)]
    if not fs:
        return _no_data("数据里没有可识别的频率")

    fig, axes = plt.subplots(1, len(fs), figsize=(3.0 * len(fs), 3.6),
                             sharey=True, squeeze=False)
    axes = axes[0]

    for ax, f in zip(axes, fs):
        at = [r for r in power if r["freq_mhz"] == f]
        for tag, color, label in (("before", COLOR_BEFORE, "测试前"),
                                  ("after", COLOR_AFTER, "测试后")):
            sub = sorted([r for r in at if tag in r["measure"]],
                         key=lambda r: r["branch"] if r["branch"] is not None else -1)
            if not sub:
                continue
            xs = [r["branch"] for r in sub]
            ys = [r["avg"] for r in sub]
            ax.plot(xs, ys, color=color, lw=1.1, marker="o", ms=2.4, label=label)
            # 阴影表示该分支上的最低~最高范围
            lo = [r["lowest"] if r["lowest"] is not None else r["avg"] for r in sub]
            hi = [r["highest"] if r["highest"] is not None else r["avg"] for r in sub]
            ax.fill_between(xs, lo, hi, color=color, alpha=0.14, linewidth=0)
            # 失败的分支标红叉
            bad = [(r["branch"], r["avg"]) for r in sub if r["pct_fail"] > 0]
            if bad:
                ax.scatter([b for b, _ in bad], [v for _, v in bad],
                           marker="x", s=30, color="#b00", zorder=5, linewidths=1.2)
        # 限值线
        lims = [r["high_limit"] for r in at if r["high_limit"] is not None]
        if lims:
            ax.axhline(max(lims), color="#666", ls="--", lw=0.8)
        ax.set_title(f"{f:g} MHz", fontsize=10)
        ax.set_xlabel("分支")
        ax.grid(alpha=0.22, linestyle=":")
        ax.set_axisbelow(True)

    axes[0].set_ylabel("PIM 功率 dBm")
    axes[0].legend(fontsize=8, loc="best")
    fig.suptitle(f"{sn}  {measure_prefix}  测试前后对比（红叉 = 该分支有失败）",
                 fontsize=11)
    fig.tight_layout()
    return fig


def fig_knock_passrate(recs: Sequence[dict], *, sn: str = ""):
    """敲击响应通过率 vs 分支。每个互调组合一条线。

    这类测量项的值是 0~1 的通过率，和功率(dBm)是两种完全不同的量，
    所以必须单独画，不能和功率图混在一起。
    """
    pr = [r for r in recs if r["value_kind"] == "passrate"]
    if not pr:
        return _no_data("没有敲击通过率数据")

    # 按测量项名称分组，每组一条线
    groups: dict[str, list[dict]] = {}
    for r in pr:
        groups.setdefault(r["measure"], []).append(r)

    fig, ax = plt.subplots(figsize=(11, 4.2))
    cmap = plt.get_cmap("tab20")
    for i, (name, items) in enumerate(sorted(groups.items())):
        sub = sorted([r for r in items if r["branch"] is not None],
                     key=lambda r: r["branch"])
        if not sub:
            continue
        ax.plot([r["branch"] for r in sub], [r["avg"] for r in sub],
                lw=1.0, marker="o", ms=2.6, color=cmap(i % 20),
                label=name[:38], alpha=0.85)
        bad = [(r["branch"], r["avg"]) for r in sub if r["pct_fail"] > 0]
        if bad:
            ax.scatter([b for b, _ in bad], [v for _, v in bad], marker="x",
                       s=34, color="#b00", zorder=5, linewidths=1.3)

    ax.set_xlabel("分支 Branch")
    ax.set_ylabel("通过率 (0~1)")
    ax.set_ylim(-0.04, 1.06)
    ax.axhline(1.0, color="#888", ls="--", lw=0.7)
    ax.grid(alpha=0.22, linestyle=":")
    ax.set_axisbelow(True)
    ax.set_title(f"{sn}  敲击响应通过率（红叉 = 失败；掉到 1.0 以下就是间歇性问题）")
    if len(groups) <= 14:
        ax.legend(fontsize=6.5, ncol=2, loc="lower left")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 下层件追踪
# ---------------------------------------------------------------------------

def fig_part_timeline(spans: Sequence[dict], *, part_sn: str = "",
                      fails_by_parent: dict[str, dict[int, float]] | None = None):
    """某个下层件（天线/滤波单元）的装配时间线，以及在每台机器上的失败分支。

    左边是甘特图式的装配时间段，右边是每段对应的失败分支。
    这张图用来回答"故障跟着这个件走，还是跟着整机走"。
    """
    if not spans:
        return _no_data(f"没有 {part_sn} 的装配记录")

    sp = sorted(spans, key=lambda s: s["installed_at"] or "")
    n = len(sp)
    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(13, 1.1 + n * 0.62),
        gridspec_kw={"width_ratios": [1.15, 1.85]})

    # --- 左：装配时间段 ---
    for i, s in enumerate(sp):
        ins = s["installed_at"] or "?"
        rem = s["removed_at"] or "仍在机器上"
        ax1.barh(i, 1, left=0, height=0.5, color="#2f6fb5", alpha=0.75)
        ax1.text(0.02, i, f"{s['parent_sn']}", va="center", fontsize=8.5,
                 color="white", fontweight="bold")
        ax1.text(1.03, i, f"{ins[:16]}  →  {rem[:16]}", va="center", fontsize=7.4,
                 color="#333")
    ax1.set_ylim(-0.6, n - 0.4)
    ax1.set_xlim(0, 3.1)
    ax1.set_yticks(range(n))
    ax1.set_yticklabels([f"第{i+1}次" for i in range(n)], fontsize=8)
    ax1.set_xticks([])
    ax1.invert_yaxis()
    ax1.set_title("装配历史（系统记录）", fontsize=10)
    for side in ("top", "right", "bottom"):
        ax1.spines[side].set_visible(False)

    # --- 右：每段的失败分支 ---
    fails_by_parent = fails_by_parent or {}
    all_br = sorted({b for s in sp
                     for b in fails_by_parent.get(s["parent_sn"], {})})
    if all_br:
        grid = np.zeros((n, len(all_br)))
        for i, s in enumerate(sp):
            for b, v in fails_by_parent.get(s["parent_sn"], {}).items():
                if b in all_br:
                    grid[i, all_br.index(b)] = v
        im = ax2.imshow(grid, aspect="auto", cmap=CMAP_FAIL, vmin=0)
        ax2.set_xticks(range(len(all_br)))
        ax2.set_xticklabels([f"BR{b}" for b in all_br], fontsize=7.5, rotation=45)
        for i in range(n):
            for j in range(len(all_br)):
                if grid[i, j] > 0:
                    ax2.text(j, i, f"{grid[i,j]:g}", ha="center", va="center",
                             fontsize=7, color="white" if grid[i, j] > grid.max()*0.55
                             else "#5a0a00")
        fig.colorbar(im, ax=ax2, label="失败次数", fraction=0.02, pad=0.012)
    else:
        ax2.text(0.5, 0.5, "这几台整机都没有 PIM 失败记录",
                 ha="center", va="center", fontsize=11, color="#888")
        ax2.set_xticks([])
    ax2.set_yticks(range(n))
    ax2.set_yticklabels([s["parent_sn"] for s in sp], fontsize=8)
    ax2.set_title("各次装配时该整机的失败分支", fontsize=10)

    fig.suptitle(f"{part_sn}  跨整机追踪"
                 f"（同一分支反复出现 → 跟着这个件走；每次都不同 → 跟着整机走）",
                 fontsize=11)
    fig.tight_layout()
    return fig
