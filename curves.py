"""多台同屏分支曲线 —— 实时从 MI 取数。

为什么是"横轴分支"而不是 RTD 那种"横轴迭代"
--------------------------------------------
RTD（Realtime Test Data）的 Knock Response 图横轴是 200 个 knock 采样点。
MI 给不了这个维度：实测确认 MI 的 knock 类测量项只返回**通过率标量**
（0~1），迭代过程在入库前就被聚合掉了。

MI 能给的最细粒度是每台机器每个 (测量类型, 分支, 频率) 一个统计值。
所以这里的等价形态是**横轴 32 个分支、一条线一台机器**，既保留了 RTD 的
视觉语言（多条细线叠加 + 纵轴贴着数据），又天然适合"一页看很多台"。

★ 台数上限是硬约束，不是偷懒
----------------------------
MI 没有"批量拿明细"的接口：批量查回来的是按测试点聚合的统计，**分不出
哪一行属于哪台机器**，画不了"一条线一台"。所以 N 台就得发 N 个请求，
而单台实测 28~70 秒。8 路并行下：

    8 台   约 1 分钟      <- 默认
    16 台  约 2 分钟      <- 硬上限
    79 台  30~90 分钟     <- 不可用

这一块原来读 `data/out/pim_measures.csv`（analyze.py 事先跑出来的 20 MB
文件）。那样确实能一次画 79 台，但实测那份文件是 9 月 29 日生成的、之后
再没更新，所以图上一直画着那天的数据。现在改成实时查，代价就是台数受限。

内存设计：列式
--------------
返回结构里横轴 `branches` 只有一份，每台只给一个纯数值数组 `y`。
对比行式（每个点一个 `{"sn":..,"branch":..,"avg":..}` 对象）：

    8 台 x 32 分支 = 256 点
    行式   每点一个对象，字节数约 4 倍
    列式   Plotly 正好要一条曲线一个数组，不用再转换

缺失的分支必须用 `None` 占位，**不能跳过** —— 否则曲线左移、把值画到
错误的分支上。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Sequence

import yield_calc as yc

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

# 默认查几台。单台 28~70 秒、8 路并行，所以 8 台约 1 分钟。
DEFAULT_MAX_SERIALS = 8
# 硬上限：参数再大也不超过这个，免得一次请求把服务占住几十分钟。
MAX_SERIALS_HARD_LIMIT = 16
# 并行度。和 mi_client 其它批量操作保持一致（实测 8 路最稳）。
DEFAULT_WORKERS = 8


def measure_kind(name: str) -> str:
    """把测量项名归一到大类，便于分组。"""
    for k in KEY_MEASURES:
        if name.startswith(k):
            return k
    return "other"


def classify(avg: float | None) -> str:
    """靠数值范围判断是功率还是通过率。

    绝对不能读 `Unit` 列 —— 实测那一列是反的（真功率的单位列为空，
    通过率反而标着 Dbm）。两类混在一起平均会算出 −51.89 这种无意义值。
    """
    return yc.classify_value(avg)


def _spread(items: Sequence[str], k: int) -> list[str]:
    """等距抽 k 个，而不是取前 k 个。

    取前 k 个只会留下序列号相邻的一批（同一天同一条线生产的），
    等距抽样才能让样本覆盖整个机队。
    """
    n = len(items)
    if k >= n:
        return list(items)
    step = n / k
    return [items[min(n - 1, int(i * step))] for i in range(k)]


def _padded(lo: float | None, hi: float | None) -> list[float] | None:
    """纵轴贴着数据给范围，上下各留 8% 边距。

    这是 RTD 那张图好看的关键之一：纵轴从 −103.6 到 −101.2，
    而不是从 0 开始 —— 否则 2 dB 的波动会被压成一条平线。
    """
    if lo is None or hi is None:
        return None
    span = hi - lo
    if span <= 0:
        span = max(abs(hi) * 0.01, 0.1)
    pad = span * 0.08
    return [lo - pad, hi + pad]


class CurveIndex:
    """把逐测试点的统计值按 (测量类型, 频率) 组织成可直接画的曲线。

    这个类只管"组织数据"，不管数据从哪来 —— 所以换数据源不影响它。
    """

    def __init__(self):
        # (kind, freq_mhz) -> {序列号: {分支: 均值}}
        self._idx: dict[tuple[str, float], dict[str, dict[int, float]]] = {}
        # (kind, freq_mhz) -> 这组数据的数值语义（power / passrate）
        self._vk: dict[tuple[str, float], str] = {}
        self._samples: dict[tuple[str, float], list[float]] = {}
        self._serials: list[str] = []
        # 取数过程的元信息，给前端提示用
        self.elapsed_s: float = 0.0
        self.truncated: bool = False
        self.n_requested: int = 0
        self.failed_serials: list[str] = []

    # ---------- 装载 ----------

    def add_rows(self, sn: str, rows: Sequence[Sequence[Any]]) -> None:
        """喂进一台机器的 TestPointSummary 行数组（23 列）。

        解析用 yield_calc.parse_rows，和良率统计走同一套逻辑 ——
        尤其是频率解析那条不变量：**先从测量项名称取，取不到才用编码**。
        knock 类测量项的编码第 5 段恒为 0，真实频率写在名称里，
        只看编码会把六个频点全归成同一个。
        """
        freq_map = yc.build_freq_map(rows)
        for rec in yc.parse_rows(rows, freq_map=freq_map, sn=sn):
            freq, br, avg = rec["freq_mhz"], rec["branch"], rec["avg"]
            if freq is None or br is None:
                continue
            kind = measure_kind(rec["measure"])
            if kind == "other":
                continue
            key = (kind, float(freq))
            self._idx.setdefault(key, {}).setdefault(sn, {})[int(br)] = avg
            if avg is not None and len(self._samples.setdefault(key, [])) < 64:
                self._samples[key].append(avg)

    def finalize(self) -> None:
        """全部装载完后算每组的数值语义。用中位数判断，抗离群值。"""
        self._serials = sorted({sn for sns in self._idx.values() for sn in sns})
        for key, vals in self._samples.items():
            s = sorted(vals)
            self._vk[key] = classify(s[len(s) // 2])
        for key in self._idx:
            self._vk.setdefault(key, "unknown")

    # ---------- 可选项 ----------

    @property
    def serials(self) -> list[str]:
        return list(self._serials)

    def kinds(self) -> list[dict]:
        """所有测量类型，带数值语义和可用频率。"""
        out: dict[str, dict] = {}
        for (kind, freq), sns in sorted(self._idx.items()):
            e = out.setdefault(kind, {"kind": kind, "freqs": [],
                                      "value_kind": self._vk[(kind, freq)],
                                      "n_serials": 0})
            e["freqs"].append(freq)
            e["n_serials"] = max(e["n_serials"], len(sns))
        for e in out.values():
            e["freqs"] = sorted(e["freqs"])
        return [out[k] for k in sorted(out)]

    def freqs(self, kind: str) -> list[float]:
        return sorted(f for (k, f) in self._idx if k == kind)

    # ---------- 取曲线 ----------

    def curves(self, *, kind: str, freq_mhz: float,
               serials: Sequence[str] | None = None) -> dict:
        """一组 (测量类型, 频率) 下每台机器沿分支号的曲线。

        返回列式结构，`y` 里缺的分支用 None 占位 —— **不能跳过**，
        否则曲线会左移、把值画到错误的分支上。
        """
        key = (kind, float(freq_mhz))
        if key not in self._idx:
            raise KeyError(f"没有这组数据: kind={kind!r} freq_mhz={freq_mhz!r}")

        per_sn = self._idx[key]
        want = [sn for sn in sorted(per_sn) if not serials or sn in set(serials)]
        branches = sorted({b for sn in want for b in per_sn[sn]})

        series = []
        lo = hi = None
        for sn in want:
            vals = per_sn[sn]
            y: list[float | None] = []
            for b in branches:
                v = vals.get(b)
                y.append(v)
                if v is not None:
                    lo = v if lo is None or v < lo else lo
                    hi = v if hi is None or v > hi else hi
            series.append({"sn": sn, "y": y})

        return {
            "kind": kind,
            "freq_mhz": float(freq_mhz),
            "value_kind": self._vk[key],
            "branches": branches,
            "series": series,
            "yrange": _padded(lo, hi),
            "n_series": len(series),
            "n_points": len(series) * len(branches),
            # ↓ 实时取数的元信息，前端用来提示"查了几台、花了多久"
            "live": True,
            "elapsed_s": round(self.elapsed_s, 1),
            "truncated": self.truncated,
            "n_requested": self.n_requested,
            "failed_serials": list(self.failed_serials),
        }


def from_mi(source, *, product: str, from_date: Any, to_date: Any,
            serials: Sequence[str] | None = None,
            test_category: Any = None,
            max_serials: int | None = None,
            workers: int = DEFAULT_WORKERS) -> CurveIndex:
    """逐台并行查 MI，建出可画曲线的索引。

    为什么必须逐台查：MI 批量查回来的是按测试点聚合的统计，分不出哪行属于
    哪台机器，画不了"一条线一台"。所以 N 台就是 N 个请求。

    单台失败不拖垮整批（记在 failed_serials 里），因为 MI 偶发超时很常见，
    一台超时就整个图出不来太脆弱。
    """
    want = list(serials) if serials else list(source.available_serials())
    idx = CurveIndex()
    idx.n_requested = len(want)

    cap = DEFAULT_MAX_SERIALS if max_serials is None else int(max_serials)
    cap = max(1, min(cap, MAX_SERIALS_HARD_LIMIT))
    if len(want) > cap:
        want = _spread(want, cap)
        idx.truncated = True

    t0 = time.monotonic()

    def one(sn: str):
        return sn, source.test_points(
            product=product, from_date=from_date, to_date=to_date,
            serial_numbers=sn, test_category=test_category,
            test_count="[All]", only_failed=False)

    if want:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(want)))) as pool:
            futs = {pool.submit(one, sn): sn for sn in want}
            for fut in as_completed(futs):
                sn = futs[fut]
                try:
                    sn, rows = fut.result()
                    idx.add_rows(sn, rows)
                except Exception:
                    idx.failed_serials.append(sn)

    idx.elapsed_s = time.monotonic() - t0
    idx.finalize()
    return idx
