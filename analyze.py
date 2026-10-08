"""
PIM 数据分析与图形化。

三个产出，对应三个需求：
  需求1  批量数据 + RTD 风格图形化
         - fig1_*  单台整机：PIM 功率 vs 分支，6 频点分面（仿 RTD Knock Response）
         - fig2_*  全机群热力图：序列号 x 分支，标出失败点
  需求2  天线跨测试追踪
         - fig3_*  同一根天线装在不同整机上时，对应分支的 PIM 表现对比
  需求3  FU 更换与分支关联
         - fig4_*  FU 更换事件涉及的分支，其 PIM 实测值分布
         - report 里给出 FU -> 分支 -> 实测值 的明细表

用法：
  python analyze.py              # 全部
  python analyze.py --sn XXXX    # 只画某台整机
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")                      # 无界面后端，直接存文件
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import TwoSlopeNorm

import mi_parse as mp

OUT = mp.DATA / "out"
OUT.mkdir(parents=True, exist_ok=True)

# 中文字体：Windows 自带微软雅黑
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False

# RTD 图里那 6 个频点的显示顺序（按频率升序，和 RTD 图一致）
FREQ_ORDER = [1736.0, 1776.0, 1856.0, 1896.0, 2502.5, 2562.5]

# 关注的 PIM 测量项（前缀匹配）。knock 相关是核心。
KEY_MEASURES = [
    "rx_pim_knock_impulse_power",
    "re_radiated_and_inline_continuous_knock_response",
    "re_radiated_intermittent_knock_response",
    "inline_intermittent_knock_response",
    "noise_power_level_before_testing",
    "noise_power_level_after_testing",
    "air_leakage_before_testing",
    "air_leakage_after_testing",
]


def measure_kind(name):
    """把测量项名归一到大类，便于分组。"""
    for k in KEY_MEASURES:
        if name.startswith(k):
            return k
    return "other"


# ---------------------------------------------------------------- 数据装载

def load_all(serials):
    """批量解析，返回 {sn: parsed}，并扁平化成一张大表。"""
    data, flat = {}, []
    for sn in serials:
        d = mp.parse_pim(sn)
        if not d or not d["measures"]:
            continue
        data[sn] = d
        for m in d["measures"]:
            flat.append({
                "sn": sn,
                "product": d["product"],
                "kind": measure_kind(m["measure"]),
                **{k: m[k] for k in
                   ("measure", "item", "branch", "freq_idx", "freq_mhz", "category",
                    "unit", "low_limit", "high_limit", "lowest", "highest", "avg",
                    "std", "cpk", "tested", "pct_fail")},
            })
    return data, flat


def dump_csv(flat, path):
    if not flat:
        return
    keys = list(flat[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(flat)


# ------------------------------------------------- 需求1a: 单机 RTD 风格图

def fig_pim_power_beforeafter(sn, parsed, base="noise_power_level"):
    """
    最接近 RTD Knock Response 的图，而且有物理意义：
    6 个频点横向分面，横轴=分支(0-31)，纵轴=PIM 功率 dBm。
    同时画测试前(before)和测试后(after)两条曲线 —— 两者的差就是敲击过程中的 PIM 恶化量。
    """
    ms = [m for m in parsed["measures"]
          if m["measure"].startswith(base) and m["value_kind"] == "power"]
    if not ms:
        return None
    freqs = [f for f in FREQ_ORDER if any(m["freq_mhz"] == f for m in ms)]
    if not freqs:
        return None

    fig, axes = plt.subplots(1, len(freqs), figsize=(3.0 * len(freqs), 3.9),
                             sharey=True, constrained_layout=True)
    if len(freqs) == 1:
        axes = [axes]

    for ax, f in zip(axes, freqs):
        for tag, color in (("before", "tab:blue"), ("after", "crimson")):
            sub = sorted([m for m in ms
                          if m["freq_mhz"] == f and f"_{tag}_testing" in m["measure"]],
                         key=lambda x: x["branch"])
            if not sub:
                continue
            bx = [m["branch"] for m in sub]
            av = [m["avg"] for m in sub]
            lo = [m["lowest"] if m["lowest"] is not None else m["avg"] for m in sub]
            hi = [m["highest"] if m["highest"] is not None else m["avg"] for m in sub]
            ax.fill_between(bx, lo, hi, alpha=0.18, color=color, linewidth=0)
            ax.plot(bx, av, color=color, lw=1.3, marker="o", ms=2.4,
                    label=f"测试{'前' if tag == 'before' else '后'}")
        ax.set_title(f"{f:g} MHz", fontsize=10)
        ax.set_xlabel("分支")
        ax.grid(alpha=0.25, lw=0.5)
        ax.set_xlim(-1, 32)

    axes[0].set_ylabel("PIM 功率 [dBm]")
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=2, fontsize=9, frameon=False,
               bbox_to_anchor=(0.5, -0.05))
    fig.suptitle(f"{sn}   {parsed['product']}   {base}（测试前后对比）", fontsize=11)
    p = OUT / f"fig1a_power_{base}_{sn}.png"
    fig.savefig(p, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return p


def fig_pim_degradation(sn, parsed, base="noise_power_level"):
    """PIM 恶化量热力图：after - before，分支 x 频率。正值 = 测试后变差。"""
    ms = [m for m in parsed["measures"]
          if m["measure"].startswith(base) and m["value_kind"] == "power"]
    if not ms:
        return None
    freqs = [f for f in FREQ_ORDER if any(m["freq_mhz"] == f for m in ms)]
    if not freqs:
        return None

    grid = np.full((len(freqs), 32), np.nan)
    for fi, f in enumerate(freqs):
        for b in range(32):
            bef = [m["avg"] for m in ms if m["freq_mhz"] == f and m["branch"] == b
                   and "_before_testing" in m["measure"] and m["avg"] is not None]
            aft = [m["avg"] for m in ms if m["freq_mhz"] == f and m["branch"] == b
                   and "_after_testing" in m["measure"] and m["avg"] is not None]
            if bef and aft:
                grid[fi, b] = aft[0] - bef[0]

    if np.all(np.isnan(grid)):
        return None
    vmax = max(0.5, float(np.nanmax(np.abs(grid))))
    fig, ax = plt.subplots(figsize=(12, 2.4 + 0.32 * len(freqs)), constrained_layout=True)
    im = ax.imshow(grid, aspect="auto", cmap="RdBu_r",
                   norm=TwoSlopeNorm(vcenter=0, vmin=-vmax, vmax=vmax))
    ax.set_yticks(range(len(freqs)))
    ax.set_yticklabels([f"{f:g} MHz" for f in freqs], fontsize=9)
    ax.set_xticks(range(32))
    ax.set_xticklabels(range(32), fontsize=7)
    ax.set_xlabel("分支 Branch")
    ax.set_title(f"{sn}  PIM 恶化量（测试后 − 测试前，dB）\n"
                 f"红=变差  蓝=变好  数值标在格子里", fontsize=11)
    fig.colorbar(im, ax=ax, label="ΔdBm", shrink=0.8)
    for fi in range(len(freqs)):
        for b in range(32):
            v = grid[fi, b]
            if not np.isnan(v) and abs(v) >= 0.15:
                ax.text(b, fi, f"{v:+.1f}", ha="center", va="center", fontsize=5.2,
                        color="white" if abs(v) > vmax * 0.55 else "black")
    p = OUT / f"fig1b_degradation_{sn}.png"
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return p


def fig_knock_passrate(sn, parsed):
    """敲击响应通过率 vs 分支。每个互调组合(item)一条线，失败点标红。"""
    ms = [m for m in parsed["measures"]
          if m["value_kind"] == "passrate"
          and ("knock" in m["measure"] or "re_radiated" in m["measure"]
               or "inline_" in m["measure"])]
    if not ms:
        return None
    # 按测量项分组（每组 = 一个互调组合 + 频率）
    groups = defaultdict(list)
    for m in ms:
        # 名字形如 "rx_pim_knock_impulse_power:2*C1-1*C2 1896 MHz"
        key = m["measure"].split(" ")[0]
        groups[key].append(m)

    keys = sorted(groups.keys())[:12]
    fig, ax = plt.subplots(figsize=(12, 4.4), constrained_layout=True)
    cmap = plt.get_cmap("tab20")
    for i, k in enumerate(keys):
        sub = sorted(groups[k], key=lambda x: x["branch"])
        bx = [m["branch"] for m in sub]
        av = [m["avg"] for m in sub]
        f = sub[0]["freq_mhz"]
        ax.plot(bx, av, lw=1.1, marker="o", ms=2.6, color=cmap(i % 20),
                label=f"{k.split(':')[-1]} @{f:g}MHz" if f else k)
        bad = [m for m in sub if m["pct_fail"] > 0]
        if bad:
            ax.scatter([m["branch"] for m in bad], [m["avg"] for m in bad],
                       marker="X", s=70, color="crimson", zorder=6)
    ax.set_xlabel("分支 Branch")
    ax.set_ylabel("通过率 (1.0 = 全部通过)")
    ax.set_xlim(-1, 32)
    ax.set_ylim(-0.05, 1.08)
    ax.set_xticks(range(32))
    ax.set_xticklabels(range(32), fontsize=7)
    ax.grid(alpha=0.25, lw=0.5)
    ax.legend(fontsize=7, ncol=4, loc="lower left")
    ax.set_title(f"{sn}  敲击响应通过率（红叉=该分支有失败）", fontsize=11)
    p = OUT / f"fig1c_knock_passrate_{sn}.png"
    fig.savefig(p, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return p


# --------------------------------------------- 需求1b: 全机群失败热力图

def fig_fleet_heatmap(data):
    """
    序列号 x 分支 的热力图。
    数值 = 该 (序列号, 分支) 上所有频点的最大 %Fail；0 表示全通过。
    """
    sns_ = sorted(data.keys())
    grid = np.zeros((len(sns_), 32))
    for i, sn in enumerate(sns_):
        for m in data[sn]["measures"]:
            b = m["branch"]
            if 0 <= b < 32:
                grid[i, b] = max(grid[i, b], m["pct_fail"])

    fig, ax = plt.subplots(figsize=(13, max(4, 0.22 * len(sns_))),
                           constrained_layout=True)
    im = ax.imshow(grid, aspect="auto", cmap="Reds", vmin=0, vmax=100)
    ax.set_xticks(range(0, 32))
    ax.set_xticklabels(range(0, 32), fontsize=7)
    ax.set_yticks(range(len(sns_)))
    ax.set_yticklabels(sns_, fontsize=6.5)
    ax.set_xlabel("分支 Branch")
    ax.set_title(f"PIM 失败分布热力图（{len(sns_)} 台整机 x 32 分支）\n"
                 f"颜色 = 该分支上任一频点的最大失败百分比", fontsize=11)
    fig.colorbar(im, ax=ax, label="% Fail", shrink=0.6)
    # 在有失败的格子上打叉
    for i in range(len(sns_)):
        for j in range(32):
            if grid[i, j] > 0:
                ax.text(j, i, "x", ha="center", va="center", fontsize=5.5, color="white")
    p = OUT / "fig2_fleet_heatmap.png"
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return p, grid, sns_


# ----------------------------------------- 需求2: 天线跨测试追踪对比

def build_antenna_tracking(serials_json, data):
    """
    把表格里的 天线->(轮次, 整机) 映射，和 MI 里对应整机的分支 PIM 数据连起来。

    表格里记的是"天线 A 装在整机 R 上，brN 失败"，
    那么 MI 里整机 R 的分支 N 的 PIM 数据就是这根天线在那一轮的表现。
    """
    import re
    RE_BR = re.compile(r"\bbr\s*(\d{1,2})\b", re.IGNORECASE)
    out = []
    for a in serials_json["antennas"]:
        rounds = []
        for rd in a["rounds"]:
            rsn = rd["radio_sn"]
            brs = sorted({int(x) for x in RE_BR.findall(rd["result"])})
            rec = {"round": rd["round"], "radio_sn": rsn,
                   "sheet_result": rd["result"][:90],
                   "sheet_branches": brs, "mi": None}
            if rsn in data:
                # 该整机所有分支的 PIM 失败情况
                fails = defaultdict(float)
                vals = defaultdict(list)
                for m in data[rsn]["measures"]:
                    fails[m["branch"]] = max(fails[m["branch"]], m["pct_fail"])
                    # 只取通过率类（敲击响应），功率类不能和它混在一起
                    if m["avg"] is not None and m["value_kind"] == "passrate":
                        vals[m["branch"]].append(m["avg"])
                rec["mi"] = {
                    "fail_branches": sorted(b for b, v in fails.items() if v > 0),
                    "branch_avg": {b: (sum(v) / len(v)) for b, v in vals.items() if v},
                }
            rounds.append(rec)
        if rounds:
            out.append({"antenna": a["sn"], "pim_screen": a["pim_screen"],
                        "conclusion": a["conclusion"], "rounds": rounds})
    return out


def fig_antenna_tracking(track, data, top=6):
    """挑轮次最多、且 MI 有数据的天线，画跨轮次分支 PIM 对比。"""
    cand = [t for t in track
            if sum(1 for r in t["rounds"] if r["mi"] and r["mi"]["branch_avg"]) >= 2]
    cand.sort(key=lambda t: -sum(1 for r in t["rounds"] if r["mi"]))
    made = []
    for t in cand[:top]:
        rs = [r for r in t["rounds"] if r["mi"] and r["mi"]["branch_avg"]]
        fig, ax = plt.subplots(figsize=(11, 4), constrained_layout=True)
        for r in rs:
            ba = r["mi"]["branch_avg"]
            bx = sorted(ba.keys())
            ax.plot(bx, [ba[b] for b in bx], marker="o", ms=3, lw=1.2,
                    label=f"{r['round']} @ {r['radio_sn']}")
            # 表格里标的失败分支，用竖线标出
            for b in r["sheet_branches"]:
                ax.axvline(b, color="crimson", alpha=0.18, lw=3)
            # MI 里实际失败的分支
            for b in r["mi"]["fail_branches"]:
                if b in ba:
                    ax.scatter([b], [ba[b]], marker="X", s=60, color="crimson", zorder=6)
        ax.set_xlabel("分支 Branch")
        ax.set_ylabel("敲击响应通过率 (1.0=全通过)")
        ax.set_xlim(-1, 32)
        ax.grid(alpha=0.25, lw=0.5)
        ax.legend(fontsize=8, ncol=2)
        ax.set_title(f"天线 {t['antenna']} 跨轮次对比   "
                     f"PIM筛选: {t['pim_screen'][:40] or '(空)'}   结论: {t['conclusion'][:40]}\n"
                     f"红竖线=表格标注的失败分支   红叉=MI 实测失败", fontsize=10)
        p = OUT / f"fig3_antenna_{t['antenna']}.png"
        fig.savefig(p, dpi=140, bbox_inches="tight")
        plt.close(fig)
        made.append(p)
    return made


# ------------------------------------------- 需求3: FU 更换与分支关联

def build_fu_summary(serials_json, data):
    """
    把 FU 更换事件和 MI 里对应整机/分支的 PIM 实测值连起来。

    注意：PIM 测量项有两种数值语义，不能混着平均 ——
      power    : 真实功率 dBm（约 -105）
      passrate : 通过率 0~1
    所以分开统计。
    """
    rows = []
    for ev in serials_json["fu_events"]:
        rsn = ev["radio_sn"]
        d = data.get(rsn)
        for fu in ev["fu_sns"]:
            for b in (ev["branches"] or [None]):
                rec = {
                    "date": ev["date"], "radio_sn": rsn, "stage": ev["stage"],
                    "fu_sn": fu, "branch": b,
                    "mi_has_data": d is not None,
                    "max_pct_fail": None,
                    "pwr_before_dbm": None, "pwr_after_dbm": None, "pwr_delta_db": None,
                    "knock_passrate_min": None,
                    "n_power": 0, "n_passrate": 0,
                }
                if d is not None and b is not None:
                    sub = [m for m in d["measures"] if m["branch"] == b]
                    if sub:
                        pw = [m for m in sub if m["value_kind"] == "power"]
                        pr = [m for m in sub if m["value_kind"] == "passrate"]
                        bef = [m["avg"] for m in pw
                               if "_before_testing" in m["measure"] and m["avg"] is not None]
                        aft = [m["avg"] for m in pw
                               if "_after_testing" in m["measure"] and m["avg"] is not None]
                        rec["max_pct_fail"] = max(m["pct_fail"] for m in sub)
                        rec["n_power"] = len(pw)
                        rec["n_passrate"] = len(pr)
                        if bef:
                            rec["pwr_before_dbm"] = sum(bef) / len(bef)
                        if aft:
                            rec["pwr_after_dbm"] = sum(aft) / len(aft)
                        if bef and aft:
                            rec["pwr_delta_db"] = rec["pwr_after_dbm"] - rec["pwr_before_dbm"]
                        if pr:
                            vals = [m["avg"] for m in pr if m["avg"] is not None]
                            if vals:
                                rec["knock_passrate_min"] = min(vals)
                rows.append(rec)
    return rows


def fig_fu_branches(fu_rows):
    """FU 更换涉及的分支频次 + 这些分支的 PIM 失败率。"""
    cnt = defaultdict(int)
    fail = defaultdict(list)
    for r in fu_rows:
        if r["branch"] is None:
            continue
        cnt[r["branch"]] += 1
        if r["max_pct_fail"] is not None:
            fail[r["branch"]].append(r["max_pct_fail"])
    if not cnt:
        return None
    br = list(range(32))
    c = [cnt.get(b, 0) for b in br]
    f = [(max(fail[b]) if fail.get(b) else 0) for b in br]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 6), sharex=True,
                                   constrained_layout=True)
    ax1.bar(br, c, color="tab:blue")
    ax1.set_ylabel("FU 更换次数")
    ax1.set_title("FU（滤波单元）更换涉及的分支分布，及这些分支在 MI 里的 PIM 失败率", fontsize=11)
    ax1.grid(alpha=0.25, axis="y", lw=0.5)
    for b, v in zip(br, c):
        if v:
            ax1.text(b, v + 0.06, str(v), ha="center", fontsize=7)

    ax2.bar(br, f, color=["crimson" if x > 0 else "lightgray" for x in f])
    ax2.set_ylabel("MI 最大 % Fail")
    ax2.set_xlabel("分支 Branch")
    ax2.set_xticks(br)
    ax2.set_xticklabels(br, fontsize=7)
    ax2.grid(alpha=0.25, axis="y", lw=0.5)
    p = OUT / "fig4_fu_branches.png"
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return p


# --------------------------------------------------------------- 报告

def write_report(data, flat, grid, sns_, track, fu_rows, figs):
    lines = []
    A = lines.append
    A("# PIM 测试数据分析报告\n")
    A(f"数据源：MI (Manufacturing Intelligence) 报表接口，集成 Windows 认证\n")
    A(f"- 已解析整机：**{len(data)}** 台")
    A(f"- PIM 测量条目：**{len(flat)}** 条")
    prods = sorted({d['product'] for d in data.values() if d['product']})
    A(f"- 产品号：{', '.join(prods)}")
    fm = next(iter(data.values()))["freq_map"] if data else {}
    A(f"- 测试频点：{', '.join(f'{v:g} MHz' for _, v in sorted(fm.items()))}\n")

    # --- 失败总览 ---
    A("## 一、失败分布总览（需求1）\n")
    nfail_sn = int((grid > 0).any(axis=1).sum())
    allfail = [(sns_[i], int((grid[i] > 0).sum())) for i in range(len(sns_))
                if (grid[i] > 0).sum() >= 18]
    A(f"- {len(sns_)} 台整机中，**{nfail_sn}** 台存在 PIM 失败分支")
    if allfail:
        A("")
        A(f"> ⚠️ **{len(allfail)} 台整机有 18 个以上分支同时失败**（共 32 个分支）："
          + ", ".join(f"`{s}`({n})" for s, n in allfail))
        A("> 这种全分支同时失败的形态不像单个器件的硬件缺陷，"
          "更可能是测试系统异常、连接问题或测试被中断，建议单独核查，"
          "并在统计失效率时作为异常样本剔除。")
        A("")
    bc = [(b, int((grid[:, b] > 0).sum())) for b in range(32)]
    bad = [x for x in bc if x[1] > 0]
    bad.sort(key=lambda x: -x[1])
    if bad:
        A(f"- 失败最集中的分支：" +
          ", ".join(f"**BR{b}**({n}台)" for b, n in bad[:12]))
    A("")
    A("| 整机序列号 | 失败分支 | 失败分支数 |")
    A("|---|---|---|")
    for i, sn in enumerate(sns_):
        fb = [b for b in range(32) if grid[i, b] > 0]
        if fb:
            A(f"| {sn} | {', '.join('BR'+str(b) for b in fb)} | {len(fb)} |")
    A("")

    # --- 天线追踪 ---
    A("## 二、天线跨测试追踪（需求2）\n")
    A("表格记录的是「天线装在某整机上，brN 失败」，")
    A("因此 MI 里那台整机对应分支的 PIM 数据，就是这根天线在该轮的表现。\n")
    A("| 天线 | 轮次 | 装配整机 | 表格记录 | 表格标注分支 | MI 实测失败分支 | MI 是否有数据 |")
    A("|---|---|---|---|---|---|---|")
    for t in track:
        for r in t["rounds"]:
            mi = r["mi"]
            fb = ", ".join("BR" + str(b) for b in mi["fail_branches"]) if mi else ""
            A(f"| {t['antenna']} | {r['round']} | {r['radio_sn']} | "
              f"{r['sheet_result'][:46]} | "
              f"{', '.join('BR'+str(b) for b in r['sheet_branches'])} | "
              f"{fb or '（无失败）' if mi else ''} | {'是' if mi else '否'} |")
    A("")

    # --- 表格 vs MI 一致性 ---
    A("### 表格标注 vs MI 实测 的一致性\n")
    agree = disagree = nodata = 0
    detail = []
    for t in track:
        for r in t["rounds"]:
            if not r["mi"]:
                nodata += 1
                continue
            sheet = set(r["sheet_branches"])
            mi = set(r["mi"]["fail_branches"])
            if not sheet and not mi:
                agree += 1
            elif sheet & mi:
                agree += 1
                if sheet ^ mi:
                    detail.append((t["antenna"], r["round"], r["radio_sn"],
                                   sorted(sheet), sorted(mi), "部分吻合"))
            else:
                disagree += 1
                detail.append((t["antenna"], r["round"], r["radio_sn"],
                               sorted(sheet), sorted(mi), "不吻合"))
    A(f"- 吻合：**{agree}** 轮次；不吻合：**{disagree}** 轮次；MI 无数据：{nodata} 轮次\n")
    if detail:
        A("| 天线 | 轮次 | 整机 | 表格分支 | MI 分支 | 判定 |")
        A("|---|---|---|---|---|---|")
        for a, rd, rs, s, m, j in detail[:40]:
            A(f"| {a} | {rd} | {rs} | {s} | {m} | {j} |")
        A("")

    # --- FU ---
    A("## 三、FU（滤波单元）更换与分支关联（需求3）\n")
    withbr = [r for r in fu_rows if r["branch"] is not None]
    A(f"- FU 更换记录：**{len(fu_rows)}** 条，其中 **{len(withbr)}** 条能定位到具体分支")
    uniq_fu = sorted({r['fu_sn'] for r in fu_rows})
    A(f"- 涉及 FU 序列号：**{len(uniq_fu)}** 个\n")
    A("| 日期 | 整机 | 环节 | FU 序列号 | 分支 | 该分支最大%Fail | 测前功率dBm | 测后功率dBm | 恶化ΔdB | 敲击最低通过率 |")
    A("|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(withbr, key=lambda x: (x["date"], x["radio_sn"], x["branch"] or 0)):
        def fmt(v, n=2):
            return "-" if v is None else (f"{v:.{n}f}" if isinstance(v, float) else str(v))
        A(f"| {r['date']} | {r['radio_sn']} | {r['stage']} | {r['fu_sn']} | "
          f"BR{r['branch']} | {fmt(r['max_pct_fail'],1)} | {fmt(r['pwr_before_dbm'])} | "
          f"{fmt(r['pwr_after_dbm'])} | {fmt(r['pwr_delta_db'])} | {fmt(r['knock_passrate_min'],3)} |")
    A("")

    # --- 图 ---
    A("## 四、图表\n")
    for p in figs:
        if p:
            A(f"- `{Path(p).name}`")
    A("")

    p = OUT / "report.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# --------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sn", action="append", help="只处理指定序列号（可多次）")
    ap.add_argument("--max-single", type=int, default=6,
                    help="最多画几台整机的单机图")
    args = ap.parse_args()

    serials_json = mp.load_serials_json()
    fetched = mp.fetched_serials()
    targets = args.sn if args.sn else fetched
    print(f"已抓取 {len(fetched)} 个序列号，本次处理 {len(targets)} 个")

    data, flat = load_all(targets)
    print(f"解析成功 {len(data)} 台，PIM 测量条目 {len(flat)} 条")
    if not data:
        print("没有可分析的数据，请先运行 fetch_mi.ps1")
        return 1

    dump_csv(flat, OUT / "pim_measures.csv")
    print(f"  -> {OUT / 'pim_measures.csv'}")

    figs = []

    # 需求1a：单机 RTD 风格图（优先画有失败的）
    withfail = [sn for sn, d in data.items()
                if any(m["pct_fail"] > 0 for m in d["measures"])]
    order = withfail + [sn for sn in data if sn not in withfail]
    for sn in order[: args.max_single]:
        for base in ("noise_power_level", "air_leakage"):
            p = fig_pim_power_beforeafter(sn, data[sn], base)
            if p:
                figs.append(p); print(f"  -> {p.name}")
        p = fig_pim_degradation(sn, data[sn], "noise_power_level")
        if p:
            figs.append(p); print(f"  -> {p.name}")
        p = fig_knock_passrate(sn, data[sn])
        if p:
            figs.append(p); print(f"  -> {p.name}")

    # 需求1b：全机群热力图
    p, grid, sns_ = fig_fleet_heatmap(data)
    figs.append(p); print(f"  -> {p.name}")

    # 需求2：天线追踪
    track = build_antenna_tracking(serials_json, data)
    (OUT / "antenna_tracking.json").write_text(
        json.dumps(track, ensure_ascii=False, indent=2), encoding="utf-8")
    for p in fig_antenna_tracking(track, data):
        figs.append(p); print(f"  -> {p.name}")

    # 需求3：FU 分支总结
    fu_rows = build_fu_summary(serials_json, data)
    dump_csv(fu_rows, OUT / "fu_branch_summary.csv")
    print(f"  -> {OUT / 'fu_branch_summary.csv'}")
    p = fig_fu_branches(fu_rows)
    if p:
        figs.append(p); print(f"  -> {p.name}")

    rp = write_report(data, flat, grid, sns_, track, fu_rows, figs)
    print(f"\n报告 -> {rp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


