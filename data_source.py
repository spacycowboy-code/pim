"""数据源：实时查 MI（Manufacturing Intelligence，制造智能）。

**只有这一条取数路径。** 数据一律现查现用，不读任何事先下载到本地的副本。

为什么只留一条
--------------
原来这里还有一个 `CacheSource`，读事先导出的快照文件（snapshot/*.json.gz），
给装不了 Kerberos 依赖的 Linux 服务器用。问题是它是**默认**路径：启动时
忘了设 `PIM_SOURCE=mi` 就会悄悄读旧快照 —— 界面正常、不报错、数据过期。
这种静默回退比直接报错危险得多。所以整条快照路径已经移除。

代价（明确记下来，别在排查部署问题时再推一遍）：
  本服务现在**必须跑在有 Windows 域身份的机器上**。MI 用集成 Windows 认证，
  没有域身份就取不到数。原来服务器那套"只读快照、绝不出网"的部署方式
  已经不可用。

注意：**没有任何本地数据副本**。`MiClient` 原来有一层文件缓存
（data/cache/*.json.gz，同样条件直接读文件、永不过期），实测因此算出过
4 天前的良率，已经整个移除。现在每次查询都真的请求 MI。

唯一的例外是 `MiSource` 里一份**内存**测试历史缓存，带 60 秒存活时间。
保留它是硬约束：`/api/history/options` 一个请求里要用 5 次全部历史，
而拉一次 80 台约 18 秒，不缓存单个请求就要 90 秒。它和文件缓存性质不同 ——
进程内、几十秒过期、`refresh()` 能立即清掉。
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path
from typing import Any, Sequence

HERE = Path(__file__).resolve().parent


def known_serials() -> list[str]:
    """从之前提取的表格里读整机序列号，给界面当候选值。

    只是候选：用户仍然可以自己输入任何序列号。文件不存在或格式不对就返回
    空列表 —— 候选可以空，但不该因此让服务起不来。

    （这个函数原来在 export_snapshot.py 里。那个脚本随快照路径一起删了，
    但它读的 data/serials.json 和快照无关，所以搬到这里继续用。）
    """
    p = HERE / "data" / "serials.json"
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        return list(d.get("all_radio_sn") or [])
    except Exception:
        return []


class MiSource:
    """实时查 MI。需要 Windows 域身份（MI 走集成 Windows 认证）。

    任意产品号、任意日期、任意条件都能查，不需要预先导出任何东西。
    """

    # 这两个标志留着，因为接口和前端会读它们来显示"数据是实时的"。
    is_offline = False
    is_live = True

    # 已知的常用测试类别。MI 里没有"列出所有类别"的接口，
    # 所以给一个实测见过的清单当候选，用户仍可自己输入别的。
    KNOWN_CATEGORIES = ["212", "26", "28", "29", "32", "38"]

    # ★ 内存缓存的存活时间（TTL，time to live，到期自动失效），单位秒。
    #
    # 为什么必须有缓存：/api/history/options 这一个接口在一次请求里就要用到
    # 5 次全部测试历史（范围、类别、产品号、序列号、场次数），而拉一次 80 台
    # 约 18 秒。不缓存的话单个请求要 90 秒。
    #
    # 为什么必须会过期：服务是长期跑着的，永久缓存等于又回到"看旧数据"。
    # 60 秒的取舍是：连续点几个区块能复用同一份（秒级操作），
    # 但绝不会让人看到一分钟以前的数据。想立刻要最新的就调 refresh()。
    CACHE_TTL_SECONDS = 60

    def __init__(self, client=None,
                 serials: Sequence[str] | None = None,
                 products: Sequence[str] | None = None):
        if client is None:
            from mi_client import MiClient      # 延迟导入，方便测试替换
            client = MiClient()
        self.client = client
        self._serials = list(serials or [])
        self._products = list(products or [])
        self._runs_cache: list | None = None
        self._reps_cache: list | None = None
        self._cache_at: float = 0.0        # 历史缓存是什么时候装进来的
        self._cache_sns: tuple[str, ...] | None = None  # 历史缓存对应哪批整机
        # UnitProductStructure 原始响应的缓存：{(产品, 起, 止): 响应}。
        # 整机列表和供应商都从同一份响应解析（一次请求实测 15 秒）。
        self._ups_cache: dict[tuple, Any] = {}
        # ★ 「进行中去重」用的锁。前端点一次查询会并发打 yield/trend/runs
        #   三个接口，它们要的是同一批数据。朴素的"检查-然后-填充"缓存在
        #   这种并发下会**各自把慢操作做一遍**（缓存踩踏）—— 实测 3 遍。
        #   每个缓存键一把锁：第一个去取，其余等它取完直接复用。
        #   按键加锁（而不是一把全局锁）是为了让不相关的查询不互相阻塞。
        self._locks: dict[Any, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, key) -> "threading.Lock":
        """取某个缓存键的锁（没有就建一把）。"""
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            return lock

    # -- 取数 ------------------------------------------------------------

    def test_points(self, **kw) -> list[list[Any]]:
        return self.client.test_points(**kw)

    def test_points_resolved(self, **kw) -> tuple[list[list[Any]], dict]:
        """取数 + 一份"这份数据是怎么来的"的说明。

        实时查询永远是精确命中用户给的条件，所以 exact 恒为 True。
        这个返回结构是接口契约的一部分（前端会显示 reason），所以保留。
        """
        rows = self.client.test_points(**kw)
        return rows, {
            "exact": True, "substituted": False,
            "used_from": kw.get("from_date"), "used_to": kw.get("to_date"),
            "reason": "",
        }

    def structure(self, serial_number: str, **kw) -> list[list[Any]]:
        return self.client.structure(serial_number, **kw)

    def structures(self, serial_numbers: Sequence[str], **kw):
        return self.client.structures(serial_numbers, **kw)

    # -- 元信息 ----------------------------------------------------------

    def manifest(self) -> dict:
        return {
            "version": 1,
            "live": True,
            "source": "实时查询 MI（Manufacturing Intelligence）",
            "exported_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "entries": [],
        }

    def entries(self) -> list[dict]:
        return []           # 实时查询没有"预导出的查询组合"这个概念

    def available_products(self) -> list[str]:
        return sorted(self._products)

    def discover_products(self, *, from_date: Any, to_date: Any) -> list[str]:
        """问 MI 这段时间生产了哪些整机产品号，填进可选列表（前端下拉用）。

        启动时调一次。MI 偶发超时很常见，所以失败只返回空、不抛异常，
        也不覆盖已有的 _products —— 下拉可以空，但服务不能因此起不来。

        ★ 合并而不是覆盖：构造时传入的产品（如 Crete 平台产品）当下不一定
        在产，MI 探测结果不包含它们；合并保留后，用户在下拉里永远能选到。
        """
        try:
            prods = self.client.products_in_range(from_date=from_date,
                                                  to_date=to_date)
        except Exception:
            return []
        if prods:
            # dict.fromkeys 保序去重：已有的在前，MI 新发现的追加
            self._products = list(dict.fromkeys(list(self._products) + list(prods)))
        return list(prods)

    def available_serials(self) -> list[str]:
        return sorted(self._serials)

    def available_categories(self) -> list[str]:
        return list(self.KNOWN_CATEGORIES)

    def available_test_counts(self) -> list[str]:
        """MI 支持的全部取值。实时查询不受任何预导出组合限制。"""
        return ["[All]", "First", "Last"] + [str(i) for i in range(2, 11)]

    def date_range(self) -> tuple[str | None, str | None]:
        """实时查询不设日期上限，返回 None 让界面知道随便填。"""
        return (None, None)

    def available_structures(self) -> list[str]:
        return sorted(self._serials)

    # -- 测试历史 --------------------------------------------------------

    def refresh(self) -> None:
        """立即丢掉所有内存缓存，下次取数重新问 MI。
        用户点「刷新」就是想马上要最新数据，不该还等 TTL 到期。"""
        self._runs_cache = None
        self._reps_cache = None
        self._cache_at = 0.0
        self._cache_sns = None
        self._ups_cache.clear()        # 整机列表+供应商也要重取（产线可能新产了机器）

    def _ups(self, product=None, from_date=None, to_date=None):
        """取（并缓存）UnitProductStructure 的原始响应。

        ★ 整机列表和供应商都从这一个响应解析，所以供应商是**零额外网络成本**。
        实测一次请求 15 秒拿到 60 台的整机+供应商；而逐台查装配结构
        （SerialNumberStructure）要 270 秒 —— 快 18 倍。
        """
        key = (product, from_date, to_date)
        if key in self._ups_cache:          # 快路径：已经有了，不用加锁
            return self._ups_cache[key]
        # ★ 双重检查加锁：并发进来的请求只让第一个去问 MI，其余等它完成后
        #   直接复用（否则三个并发接口会各拉一遍，白等好几十秒）。
        with self._lock_for(("ups", key)):
            if key not in self._ups_cache:
                self._ups_cache[key] = self.client.unit_structure(
                    product=product, from_date=from_date, to_date=to_date)
        return self._ups_cache[key]

    def serials_for(self, *, product=None, from_date=None,
                    to_date=None) -> list[str]:
        """实时列出该 (产品, 日期范围) 下真实在产的全部整机序列号。

        ★ 替代固定的 data/serials.json 清单，产线新产的机器也能查到。
        靠 MI 的 UnitProductStructure 报表（明细表每行的
        "ParentSN => ChildSN"，=> 左边就是整机）。

        失败时退回固定清单（self._serials）—— MI 偶发超时很常见，
        宁可用旧清单也别整个查崩。
        """
        from mi_client import serials_from_unit_structure
        try:
            return serials_from_unit_structure(
                self._ups(product, from_date, to_date))
        except Exception:
            return list(self._serials)     # 兜底：别让列整机失败带崩整个查询

    def suppliers_for(self, *, product=None, from_date=None,
                      to_date=None) -> dict[str, dict[str, list[str]]]:
        """该范围内每台整机的天线/滤波器供应商。

        {整机SN: {"antenna": [公司名...], "filter_unit": [公司名...]}}

        和 serials_for 共享同一个响应，所以不额外花时间。
        供应商是附加信息，拿不到就返回空字典，别拖垮整个查询。
        """
        from mi_client import suppliers_from_unit_structure
        try:
            return suppliers_from_unit_structure(
                self._ups(product, from_date, to_date))
        except Exception:
            return {}

    def failed_branches_for(self, serials) -> dict[str, list[int]]:
        """这些整机各自失败在哪些分支上。{整机SN: [分支号...]}

        ★ 只传**失败的**整机进来：单台约 30 秒（8 路并行），
        给通过的整机查是白等。
        """
        from mi_client import failed_branches_from_child_history
        want = [s for s in (serials or []) if s]
        if not want:
            return {}
        try:
            hists = self.client.child_test_histories(want, workers=8)
        except Exception:
            return {}
        out = {}
        for sn, obj in hists.items():
            try:
                out[sn] = failed_branches_from_child_history(obj)
            except Exception:
                out[sn] = []
        return out

    def failed_testpoints_for(self, serials) -> dict:
        """这些整机各自失败在哪些测试点上，按场次分组。
        {整机SN: {started: [{code, name, branch, freq_mhz, measured, fault_code}, ...]}}

        和 failed_branches_for 用同一数据源（ChildSerialNumberTestHistory），
        所以一次拉数据能同时得到分支号和测试点详情 —— 调用方自己决定要哪种。
        ★ 只传**失败的**整机。
        """
        from mi_client import failed_testpoints_from_child_history
        want = [s for s in (serials or []) if s]
        if not want:
            return {}
        try:
            hists = self.client.child_test_histories(want, workers=8)
        except Exception:
            return {}
        out = {}
        for sn, obj in hists.items():
            try:
                out[sn] = failed_testpoints_from_child_history(obj)
            except Exception:
                out[sn] = {}
        return out

    def failed_detail_for(self, serials) -> dict:
        """一次 MI 请求同时拿**分支号**和**失败测试点详情**。
        返回 {"branches": {sn: [...]}, "testpoints": {sn: {started: [...]}}}。

        branches 和 testpoints 都来自 ChildSerialNumberTestHistory 的同一响应，
        所以合在一个方法里，省掉一次 60 秒级的 MI 调用。前端需要哪种就用哪种。
        ★ 只传**失败的**整机。

        ★ 异常**不**吞 —— 原来 except: return {} 会让前端看到"200 OK + 空数据"，
        分不清是"查过了没有"还是"查询失败"，用户只能看到"查询失败"4 个字
        猜根因。现在直接把异常传给 API 层，由 API 转成 502 + 具体错误消息。
        """
        from mi_client import (failed_branches_from_child_history,
                                failed_testpoints_from_child_history)
        want = [s for s in (serials or []) if s]
        if not want:
            return {"branches": {}, "testpoints": {}}
        hists = self.client.child_test_histories(want, workers=8)
        br, tp = {}, {}
        for sn, obj in hists.items():
            try: br[sn] = failed_branches_from_child_history(obj)
            except Exception: br[sn] = []
            try: tp[sn] = failed_testpoints_from_child_history(obj)
            except Exception: tp[sn] = {}
        return {"branches": br, "testpoints": tp}

    def test_runs(self, serial_number: str) -> list:
        import test_history as th
        return th.to_runs(self.client.test_history(serial_number))

    def test_repairs(self, serial_number: str) -> list:
        import test_history as th
        return th.to_repairs(self.client.test_history(serial_number))

    def _load_histories(self, serials: Sequence[str]) -> None:
        """拉给定整机的测试历史，结果缓存在内存（带存活时间 + 记住是哪批）。"""
        import test_history as th
        runs: list = []
        reps: list = []
        if serials:
            hists = self.client.test_histories(list(serials), workers=8)
            for obj in hists.values():
                runs.extend(th.to_runs(obj))
                reps.extend(th.to_repairs(obj))
        # started 可能是 None（时间解析失败），排序时放最后
        runs.sort(key=lambda r: (r.started is None, r.started))
        reps.sort(key=lambda r: (r.started is None, r.started))
        self._runs_cache, self._reps_cache = runs, reps
        self._cache_at = time.monotonic()
        self._cache_sns = tuple(sorted(serials))

    def _hist_fresh(self, want: tuple[str, ...]) -> bool:
        """缓存里装的就是这批整机、而且没过期。"""
        return (self._runs_cache is not None
                and (time.monotonic() - self._cache_at) <= self.CACHE_TTL_SECONDS
                and self._cache_sns == want)

    def _ensure(self, serials: Sequence[str] | None) -> None:
        """按需加载历史。serials=None 时用固定清单兜底（没指定范围的旧调用）。

        缓存命中条件：没过期，且装的就是这批整机。换了整机集合要重拉 ——
        否则换了查询范围还用上一批的历史，良率就错了。

        ★ 带「进行中去重」：前端一次查询并发打三个接口，它们要的是同一批
        历史。不加锁的话三个都发现缓存空、各拉一遍（实测 3 遍，每遍几十秒）。
        """
        want = tuple(sorted(serials)) if serials is not None \
            else tuple(sorted(self._serials))
        if self._hist_fresh(want):          # 快路径：不用加锁
            return
        with self._lock_for(("hist", want)):
            if not self._hist_fresh(want):  # 双重检查：等锁期间别人可能已经装好了
                self._load_histories(want)

    def all_test_runs(self, serials: Sequence[str] | None = None) -> list:
        self._ensure(serials)
        return list(self._runs_cache or [])

    def all_test_repairs(self, serials: Sequence[str] | None = None) -> list:
        self._ensure(serials)
        return list(self._reps_cache or [])

    def available_histories(self) -> list[str]:
        return sorted(self._serials)

    def history_span(self):
        """历史数据实际覆盖的时间范围，给前端当日期选择器的上下界。"""
        import test_history as th
        return th.date_span(self.all_test_runs())

    def history_categories(self) -> list[str]:
        return sorted({r.category for r in self.all_test_runs() if r.category})

    def history_products(self) -> list[str]:
        return sorted({r.product for r in self.all_test_runs() if r.product})

    def history_serials(self) -> list[str]:
        return sorted({r.serial_number for r in self.all_test_runs()
                       if r.serial_number})
