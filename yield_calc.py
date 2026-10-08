"""良率（Yield）与 FPY 计算。

几个容易混淆的概念，先说清楚：

**良率 Yield**：合格的比例。这里有两个层级，数字不一样，别混用：
  - 测试点良率：以"测试次数"为单位。测了 100 次、失败 5 次 -> 95%。
  - 机器良率：以"整机台数"为单位。一台机器只要有一个测试点失败，整台算不良。
    产线上说"良率"通常指这个，它一定低于或等于测试点良率。

**FPY（First Pass Yield，首次通过率）**：只算每台机器**第一次**测试的结果。
  复测修好的不算。这个数字才反映真实工艺水平。
  取数时用 TestCount="First"，算法和普通良率一样。

**为什么不能对 %Fail 求简单平均**：每个测试点的测试次数（# Tested）不同，
  测了 90 次的点和测了 10 次的点权重不该一样。必须先换算成失败次数再汇总。

本模块只做计算，不碰网络。输入统一是 MiClient.test_points() 返回的 23 列行数组。
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable, Sequence

# TestPointSummary 的 23 列，顺序固定（实测确认）
C_CODE, C_NAME, C_GRP_CODE, C_GRP_NAME = 0, 1, 2, 3
C_CRITERIA, C_AREA, C_LINE, C_CTRL_CAT, C_CATEGORY, C_UNIT = 4, 5, 6, 7, 8, 9
C_TESTED, C_PCT_FAIL = 10, 11
C_LOW, C_HIGH, C_LOWEST, C_HIGHEST, C_AVG, C_STD = 12, 13, 14, 15, 16, 17
C_CPK, C_CPL, C_CPU, C_ODPMO, C_EDPMO = 18, 19, 20, 21, 22

# PIM 测试点编码 5.2.<测量项>.<分支>.<频率索引>；DVT: 前缀是限值定义行，没有实测值
RE_TPC = re.compile(r"^(DVT:)?5\.2\.(\d+)\.(\d+)\.(\d+)$")
# 从测量项名称里取频率，如 "... 2502.5 MHz"
RE_MHZ = re.compile(r"([\d.]+)\s*MH", re.IGNORECASE)

# 出图用的频率顺序（与 RTD 报表一致）
FREQ_ORDER = [1736.0, 1776.0, 1856.0, 1896.0, 2502.5, 2562.5]


def num(x: Any) -> float | None:
    """安全转 float。空串、null、[NULL] 都算没有值。"""
    if x is None:
        return None
    s = str(x).strip()
    if not s or s.lower() in ("null", "[null]", "nan", "[blank]"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def is_limit_def(code: str) -> bool:
    """DVT: 前缀的行是限值定义，没有实测值，统计时必须排除。"""
    return str(code).startswith("DVT:")


def classify_value(avg: float | None) -> str:
    """判断这个数值是功率还是通过率。

    两类数值绝不能混在一起平均——功率约 -104 dBm、通过率 0~1，
    混起来平均会得到 -52 这种物理上无意义的数。
    """
    if avg is None:
        return "unknown"
    if avg <= -10:
        return "power"          # dBm
    if 0.0 <= avg <= 1.0:
        return "passrate"       # 通过比例
    return "other"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def parse_rows(rows: Iterable[Sequence[Any]],
               freq_map: dict[int, float] | None = None,
               sn: str | None = None) -> list[dict]:
    """把 23 列的行数组解析成结构化记录。

    会自动跳过 DVT: 限值定义行。非 5.2.x 编码（比如 1.01.0.1）也能处理，
    只是 item/branch/freq_idx 为 None。

    频率解析的顺序很重要：**先从测量项名称取，取不到才用编码**。
    因为 knock 类测量项的编码第 5 段恒为 0，真实频率写在名称里；
    如果只看编码，六个频点会全被归成同一个。
    """
    out: list[dict] = []
    for r in rows:
        code = str(r[C_CODE])
        if is_limit_def(code):
            continue

        m = RE_TPC.match(code)
        if m:
            item, branch, fidx = int(m.group(2)), int(m.group(3)), int(m.group(4))
        else:
            item = branch = fidx = None

        name = str(r[C_NAME] or "")
        # 名字里去掉组名前缀，短一些
        measure = name.replace("RX PIM Power", "").strip().rstrip(":").strip()

        mm = RE_MHZ.search(measure)
        if mm:
            freq = float(mm.group(1))
        elif freq_map and fidx is not None:
            freq = freq_map.get(fidx)
        else:
            freq = None

        avg = num(r[C_AVG])
        tested = num(r[C_TESTED]) or 0.0
        pct_fail = num(r[C_PCT_FAIL]) or 0.0

        rec = {
            "sn": sn,
            "code": code,
            "item": item,
            "branch": branch,
            "freq_idx": fidx,
            "freq_mhz": freq,
            "measure": measure,
            "name": name,
            "group": str(r[C_GRP_NAME] or ""),
            "group_code": str(r[C_GRP_CODE] or ""),
            "category": str(r[C_CATEGORY] or ""),
            "criteria": str(r[C_CRITERIA] or ""),
            "area": str(r[C_AREA] or ""),
            "unit": str(r[C_UNIT] or ""),
            "value_kind": classify_value(avg),
            "low_limit": num(r[C_LOW]),
            "high_limit": num(r[C_HIGH]),
            "lowest": num(r[C_LOWEST]),
            "highest": num(r[C_HIGHEST]),
            "avg": avg,
            "std": num(r[C_STD]),
            "cpk": num(r[C_CPK]),
            "tested": tested,
            "pct_fail": pct_fail,
            # 失败次数：由测试次数和失败率反算，四舍五入到整数
            "n_failed": round(tested * pct_fail / 100.0),
        }
        out.append(rec)
    return out


def build_freq_map(rows: Iterable[Sequence[Any]]) -> dict[int, float]:
    """从 DVT: 限值行推断 频率索引 -> 频率(MHz)。

    那些行的名称里写了频率，是唯一能建立这个映射的地方。
    实测结果：0->1896, 1->1856, 2->1736, 3->1776, 4->2502.5, 5->2562.5
    """
    fmap: dict[int, float] = {}
    for r in rows:
        code = str(r[C_CODE])
        if not is_limit_def(code):
            continue
        m = RE_TPC.match(code)
        if not m:
            continue
        fidx = int(m.group(4))
        mm = RE_MHZ.search(str(r[C_NAME] or ""))
        if mm and fidx not in fmap:
            fmap[fidx] = float(mm.group(1))
    return fmap


def pim_only(rows: Iterable[Sequence[Any]]) -> list[list[Any]]:
    """只留下 PIM 相关的测试点（按测试点组名过滤）。"""
    return [list(r) for r in rows if "PIM" in str(r[C_GRP_NAME] or "").upper()]


# ---------------------------------------------------------------------------
# 良率
# ---------------------------------------------------------------------------

def yield_summary(rows: Iterable[Sequence[Any]]) -> dict:
    """测试点次数级的失败统计。

    ⚠️ 这里算出来的**不是良率**。本项目的良率只有一种口径：场次级 FPY
    （见 test_history.run_yield(first_only=True)）。
    测试点次数级的比率分母是"测试点被执行的次数"（量级十万），
    算出来总是 99.x%，拿它当良率会严重高估。所以字段叫 fail_rate_pct。

    返回:
      total_tested         总测试次数（所有测试点的 # Tested 之和）
      total_failed         总失败次数（按 # Tested x % Fail 反算）
      fail_rate_pct        失败率% = 失败/总数 x 100；没数据时为 None
      n_test_points        测试点个数（已排除 DVT 限值行）
      n_failed_points      有失败的测试点个数
      point_fail_rate_pct  有失败的点 / 总点数 x 100
    """
    total_tested = 0.0
    total_failed = 0.0
    n_points = 0
    n_failed_points = 0

    for r in rows:
        if is_limit_def(str(r[C_CODE])):
            continue
        n_points += 1
        tested = num(r[C_TESTED]) or 0.0
        pf = num(r[C_PCT_FAIL]) or 0.0
        total_tested += tested
        total_failed += tested * pf / 100.0
        if pf > 0:
            n_failed_points += 1

    total_failed = round(total_failed)
    return {
        "total_tested": int(total_tested),
        "total_failed": int(total_failed),
        "fail_rate_pct": (100.0 * total_failed / total_tested
                          if total_tested else None),
        "n_test_points": n_points,
        "n_failed_points": n_failed_points,
        "point_fail_rate_pct": (100.0 * n_failed_points / n_points
                                if n_points else None),
    }


# 支持的分组维度 -> 从解析记录里取哪个字段
GROUP_KEYS = {
    "branch": "branch",
    "freq_mhz": "freq_mhz",
    "category": "category",
    "group": "group",
    "measure": "measure",
    "value_kind": "value_kind",
    "item": "item",
    "sn": "sn",
    "area": "area",
}


def _avg_of_kind(items: Iterable[dict], kind: str) -> float | None:
    """只对某一类数值求平均（功率或通过率）。

    为什么需要：按分支之类的维度分组时，一组里会同时有功率(dBm)和通过率(0~1)，
    这两类**绝不能混在一起平均**（会算出 -51.75 这种无意义的数）。
    所以 avg_value 在混杂时返回 None，同时用这个函数分别给出两类的平均，
    免得界面上显示一整列 None 让人困惑。
    """
    vals = [i["avg"] for i in items
            if i["avg"] is not None and i["value_kind"] == kind]
    return (sum(vals) / len(vals)) if vals else None


def yield_by(rows: Iterable[Sequence[Any]], key: str,
             freq_map: dict[int, float] | None = None,
             sn: str | None = None) -> list[dict]:
    """按某个维度分组统计失败率，结果按失败率降序（最差的在最前面）。

    ⚠️ 这里给的是**失败率**，不是良率。分支、频率、测量项都不是交付单位，
    "BR28 良率 99.44%" 这种说法不成立——一台机器 32 个分支坏一个，整机就是
    不良。分组数据只用来定位问题（哪个分支/频率失败最多）。

    key 可选：branch / freq_mhz / category / group / measure / value_kind / item / area

    每组返回:
      key, total_tested, total_failed, fail_rate_pct,
      n_test_points, n_failed_points,
      avg_value  该组数值的平均（**只在组内数值类型一致时才有意义**，
                 所以按 value_kind 分组时最可靠）
    """
    if key not in GROUP_KEYS:
        raise ValueError(f"不支持按 {key!r} 分组，可用：{sorted(GROUP_KEYS)}")
    field = GROUP_KEYS[key]

    recs = parse_rows(rows, freq_map=freq_map, sn=sn)
    buckets: dict[Any, list[dict]] = defaultdict(list)
    for rec in recs:
        buckets[rec[field]].append(rec)

    out = []
    for k, items in buckets.items():
        if k is None:
            continue
        tested = sum(i["tested"] for i in items)
        failed = sum(i["n_failed"] for i in items)
        vals = [i["avg"] for i in items if i["avg"] is not None]
        # 只有组内数值类型单一时平均才有物理意义
        kinds = {i["value_kind"] for i in items if i["avg"] is not None}
        avg_value = (sum(vals) / len(vals)) if vals and len(kinds) == 1 else None
        out.append({
            "key": k,
            "total_tested": int(tested),
            "total_failed": int(failed),
            "fail_rate_pct": (100.0 * failed / tested) if tested else None,
            "n_test_points": len(items),
            "n_failed_points": sum(1 for i in items if i["pct_fail"] > 0),
            "avg_value": avg_value,
            # 混杂时 avg_value 是 None，但两类各自的平均仍然有意义，分开给出
            "avg_power": _avg_of_kind(items, "power"),
            "avg_passrate": _avg_of_kind(items, "passrate"),
            "value_kinds": sorted(kinds),
        })

    # 失败率降序，最差的排前面；没有数据（None）的放最后
    out.sort(key=lambda g: (g["fail_rate_pct"] is None,
                            -(g["fail_rate_pct"] or 0.0)))
    return out


# ---------------------------------------------------------------------------
# 机器级别良率已移除
#
# 原来这里有 unit_yield()：按整机台数算良率（一台有一个测试点失败就算不良）。
# 已删除，因为本项目的良率口径统一为**场次级 FPY**：
#     test_history.run_yield(runs, first_only=True)
# 它每台只取 TestCount=1 的那一场，分母天然就是台数，而且数据来自带时间戳的
# 测试历史报表，任意日期都能查；unit_yield 要逐台拉 TestPointSummary，
# 79 台要 5~8 分钟。留着两个良率函数只会让调用方选错口径。
# ---------------------------------------------------------------------------


def failed_branches(rows: Iterable[Sequence[Any]]) -> dict[int, float]:
    """PIM 失败分支 -> 该分支上任一频点的最大失败率。用于热力图和快速定位。

    只对**单台查询**的数据有效（批量查询的 %Fail 是坏的）。
    批量数据请用 failed_branches_from_counts()。
    """
    out: dict[int, float] = {}
    for rec in parse_rows(pim_only(rows)):
        b, pf = rec["branch"], rec["pct_fail"]
        if b is None or pf <= 0:
            continue
        out[b] = max(out.get(b, 0.0), pf)
    return out


# ---------------------------------------------------------------------------
# 批量查询专用：两次请求相除
#
# 为什么需要这一套：批量查询（序列号多于一个）时 MI 返回的 %Fail 列恒为 0。
# 实测同一个测试点，单台查 %Fail=16.67，3 台批量查就变成 0。
# 所以批量数据算良率必须靠两次请求：
#     [All]  模式的 #Tested = 总测试次数
#     Failed 模式的 #Tested = 失败次数
# 下面所有函数都**完全不读 %Fail 列**。
# ---------------------------------------------------------------------------

def fail_counts(failed_rows: Iterable[Sequence[Any]]) -> dict[str, int]:
    """从 Failed 模式的结果里取出 {测试点编码: 失败次数}。

    Failed 模式下 #Tested 的语义就是失败次数——这一点用逐台查询交叉验证过，
    53 个有失败的测试点全部精确一致。
    """
    out: dict[str, int] = {}
    for r in failed_rows:
        code = str(r[C_CODE])
        if is_limit_def(code):
            continue
        out[code] = out.get(code, 0) + int(num(r[C_TESTED]) or 0)
    return out


def yield_from_pair(all_rows: Iterable[Sequence[Any]],
                    failed_rows: Iterable[Sequence[Any]]) -> dict:
    """用两次批量请求的结果算测试点次数级失败率。结构和 yield_summary 一致。

    ⚠️ 同样**不是良率**，良率只有场次级 FPY 一种口径。

    all_rows    来自 only_failed=False，提供总测试次数
    failed_rows 来自 only_failed=True，提供失败次数
    """
    fails = fail_counts(failed_rows)

    total_tested = 0.0
    n_points = 0
    codes_seen: set[str] = set()
    for r in all_rows:
        code = str(r[C_CODE])
        if is_limit_def(code):
            continue
        n_points += 1
        codes_seen.add(code)
        total_tested += num(r[C_TESTED]) or 0.0

    # 失败的测试点可能不在 [All] 的返回里（两次请求的测试点集合不完全重合），
    # 那些失败次数也必须计入，否则会低估失败
    total_failed = sum(fails.values())
    n_failed_points = len(fails)

    return {
        "total_tested": int(total_tested),
        "total_failed": int(total_failed),
        "fail_rate_pct": (100.0 * total_failed / total_tested
                          if total_tested else None),
        "n_test_points": n_points,
        "n_failed_points": n_failed_points,
        "point_fail_rate_pct": (100.0 * n_failed_points / n_points
                                if n_points else None),
    }


def yield_by_pair(all_rows: Iterable[Sequence[Any]],
                  failed_rows: Iterable[Sequence[Any]],
                  key: str,
                  freq_map: dict[int, float] | None = None) -> list[dict]:
    """批量数据按维度分组统计失败率，结果按失败率降序（最差的在前）。

    ⚠️ 给的是失败率不是良率，理由见 yield_by 的说明。

    key 可选：branch / freq_mhz / category / group / measure / value_kind / item / area
    """
    if key not in GROUP_KEYS:
        raise ValueError(f"不支持按 {key!r} 分组，可用：{sorted(GROUP_KEYS)}")
    field = GROUP_KEYS[key]

    # 先解析两边，这样才能按同一个维度对齐
    all_recs = parse_rows(all_rows, freq_map=freq_map)
    fail_recs = parse_rows(failed_rows, freq_map=freq_map)

    tested: dict[Any, float] = defaultdict(float)
    npoints: dict[Any, int] = defaultdict(int)
    vals: dict[Any, list[float]] = defaultdict(list)
    kinds: dict[Any, set[str]] = defaultdict(set)
    items_by_key: dict[Any, list[dict]] = defaultdict(list)
    for rec in all_recs:
        k = rec[field]
        tested[k] += rec["tested"]
        npoints[k] += 1
        items_by_key[k].append(rec)
        if rec["avg"] is not None:
            vals[k].append(rec["avg"])
            kinds[k].add(rec["value_kind"])

    failed: dict[Any, float] = defaultdict(float)
    fpoints: dict[Any, set[str]] = defaultdict(set)
    for rec in fail_recs:
        k = rec[field]
        failed[k] += rec["tested"]          # Failed 模式下 tested = 失败次数
        fpoints[k].add(rec["code"])

    out = []
    for k in set(tested) | set(failed):
        if k is None:
            continue
        t, f = tested.get(k, 0.0), failed.get(k, 0.0)
        vl, kd = vals.get(k, []), kinds.get(k, set())
        its = items_by_key.get(k, [])
        out.append({
            "key": k,
            "total_tested": int(t),
            "total_failed": int(f),
            "fail_rate_pct": (100.0 * f / t) if t else None,
            "n_test_points": npoints.get(k, 0),
            "n_failed_points": len(fpoints.get(k, ())),
            # 只有组内数值类型单一时平均才有物理意义
            "avg_value": (sum(vl) / len(vl)) if vl and len(kd) == 1 else None,
            # 混杂时上面是 None，但两类各自的平均仍有意义
            "avg_power": _avg_of_kind(its, "power"),
            "avg_passrate": _avg_of_kind(its, "passrate"),
            "value_kinds": sorted(kd),
        })

    out.sort(key=lambda g: (g["fail_rate_pct"] is None,
                            -(g["fail_rate_pct"] or 0.0)))
    return out


def failed_branches_from_counts(failed_rows: Iterable[Sequence[Any]]) -> dict[int, int]:
    """PIM 失败分支 -> 失败次数。批量数据用这个（不依赖 %Fail 列）。"""
    out: dict[int, int] = {}
    for rec in parse_rows(pim_only(failed_rows)):
        b = rec["branch"]
        if b is None:
            continue
        out[b] = out.get(b, 0) + int(rec["tested"])
    return out
