"""锁住「每次查询都真的问 MI，不读本地副本」这个契约。

背景：为什么要有这个文件
------------------------
`MiClient.query()` 原来会把 MI 的响应按查询条件存成 `data/cache/<键>.json.gz`，
下次同样条件直接读文件、**不再问 MI**。而且这个缓存**没有任何过期时间** ——
文件在那儿就永远用它。

实测后果：查 FPY 良率只花 0.2 秒就返回了（真连 MI 拉 80 台历史要十几秒），
算出来的 72.43% 用的是 4 天前的数据。当时 `data/cache` 里 214 个文件中有
206 个（96%）是 4 天前写的。产线这 4 天一直在产新数据。

更糟的是界面上的「刷新」按钮只清内存缓存、不清这些文件，所以点了刷新
照样拿到旧数据 —— 用户没有任何办法拿到最新数据。

用户的要求是数据必须直接从 MI / RTD 拉，所以文件缓存整个移除。

关于内存缓存（刻意保留，别当成漏掉了）
--------------------------------------
`MiSource` 还有一份**内存**里的测试历史缓存，它保留了，但加了存活时间
（TTL，time to live，到期自动失效）。原因是硬约束：`/api/history/options`
这一个接口在一次请求里就要用到 5 次全部测试历史，而拉一次 80 台约 18 秒。
完全不缓存的话单个请求要 90 秒。

两者性质不同：
    文件缓存  跨进程、永不过期  -> 会读到几天前的数据，已删除
    内存缓存  进程内、几十秒过期 -> 最多滞后几十秒，且 refresh() 能立即清掉
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import data_source as ds
import mi_client


class CountingClient(mi_client.MiClient):
    """数一数真的"发出去"几次请求。不碰网络。"""

    def __init__(self):
        super().__init__()
        self.n_posts = 0

    # query() 内部是 self.session.post(...)，所以替掉 session 就能拦住
    @property
    def session(self):
        client = self

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"data": [["5.2.100.0.0"] + [None] * 22]}}

        class FakeSession:
            def post(self, url, **kw):
                client.n_posts += 1
                return FakeResp()

        return FakeSession()


# ---------- 不再有文件缓存 ----------

def test_客户端不再有缓存目录():
    """cache_dir 指向 data/cache，是文件缓存的落盘位置。"""
    c = mi_client.MiClient()
    assert not hasattr(c, "cache_dir"), "cache_dir 还在，文件缓存没拆干净"


def test_客户端不再有读写缓存的方法():
    c = mi_client.MiClient()
    for name in ("_cache_path", "_read_cache", "_write_cache"):
        assert not hasattr(c, name), f"{name} 还在，文件缓存没拆干净"


def test_模块不再提供缓存键函数():
    """cache_key 的唯一用途是算缓存文件名。"""
    assert not hasattr(mi_client, "cache_key"), "cache_key 还在，是缓存的残留"


def test_构造客户端不接受缓存目录参数():
    """留着这个参数会让调用方以为还能配缓存。"""
    with pytest.raises(TypeError):
        mi_client.MiClient(cache_dir="data/cache")


def test_实时数据源不接受缓存目录参数():
    with pytest.raises(TypeError):
        ds.MiSource(client=object(), cache_dir="data/cache")


def test_查询不往磁盘写任何东西(tmp_path, monkeypatch):
    """最直接的证据：查完一圈，工作目录里不该多出任何文件。"""
    monkeypatch.chdir(tmp_path)
    c = CountingClient()
    c.query("TestPointSummary", ProductNumber="KRD 901 330/11",
            FromDate="2026-09-01 00:00", ToDate="2026-10-01 00:00")
    leftover = [p.name for p in tmp_path.rglob("*") if p.is_file()]
    assert leftover == [], f"查询往磁盘写了东西：{leftover}"


def test_同样条件查两次就真的请求两次():
    """这是整件事的核心：第二次绝不能偷偷用第一次的结果。

    原来第二次会命中缓存文件直接返回，于是产线出了新数据也看不到。
    """
    c = CountingClient()
    kw = dict(ProductNumber="KRD 901 330/11", FromDate="2026-09-01 00:00",
              ToDate="2026-10-01 00:00")
    c.query("TestPointSummary", **kw)
    c.query("TestPointSummary", **kw)
    assert c.n_posts == 2, f"只发了 {c.n_posts} 次请求，说明用了缓存"


def test_取数方法不再有force参数():
    """没有缓存就不存在"忽略缓存"，force 这个参数失去意义。

    留着它更危险：调用方会以为"不传 force 就是用缓存"，
    从而以为存在一个可以沿用旧数据的模式。
    """
    import inspect
    for name in ("query", "test_points", "structure", "test_history"):
        sig = inspect.signature(getattr(mi_client.MiClient, name))
        assert "force" not in sig.parameters, \
            f"MiClient.{name}() 还有 force 参数"


# ---------- 内存缓存必须会过期 ----------

def test_内存缓存有存活时间():
    """进程长期跑着，内存缓存不能永久有效，否则同样是看旧数据。"""
    assert hasattr(ds.MiSource, "CACHE_TTL_SECONDS"), \
        "内存缓存必须有明确的存活时间"
    assert 0 < ds.MiSource.CACHE_TTL_SECONDS <= 300, \
        "存活时间应该是几十秒级别，太长就等于没过期"


def test_内存缓存过期后重新问MI(monkeypatch):
    """到期就必须重新拉，不能继续用旧的。"""
    calls = {"n": 0}

    class FakeClient:
        def test_histories(self, sns, **kw):
            calls["n"] += 1
            return {sn: {"data": {"resultSet": [{"columns": [], "rows": []}]}}
                    for sn in sns}

    src = ds.MiSource(client=FakeClient(), serials=["EA8E538914"])

    now = {"t": 1000.0}
    monkeypatch.setattr(ds.time, "monotonic", lambda: now["t"])

    src.all_test_runs()
    assert calls["n"] == 1

    # 还在存活期内：复用，不重新拉
    now["t"] += ds.MiSource.CACHE_TTL_SECONDS / 2
    src.all_test_runs()
    assert calls["n"] == 1, "存活期内不该重复拉"

    # 超过存活期：必须重新拉
    now["t"] += ds.MiSource.CACHE_TTL_SECONDS + 1
    src.all_test_runs()
    assert calls["n"] == 2, "过期了却没重新问 MI"


def test_刷新能立即让内存缓存失效():
    """用户点「刷新」就是想立刻要最新数据，不该还等 TTL 到期。"""
    calls = {"n": 0}

    class FakeClient:
        def test_histories(self, sns, **kw):
            calls["n"] += 1
            return {sn: {"data": {"resultSet": [{"columns": [], "rows": []}]}}
                    for sn in sns}

    src = ds.MiSource(client=FakeClient(), serials=["EA8E538914"])
    src.all_test_runs()
    src.refresh()
    src.all_test_runs()
    assert calls["n"] == 2



# ---------- 装配结构：必须批量并行，缓存必须会过期 ----------
#
# 为什么单独测这个：供应商筛选要用到**全部整机**的装配结构。
# 删掉文件缓存之后这里暴露了两个问题：
#
#   1. 原来是串行逐台查：`{sn: s.structure(sn) for sn in ...}`
#      80 台 x 约 12 秒 ≈ 16 分钟。实测页面初始化调 /api/suppliers
#      挂起 5 分钟以上还没返回。必须用批量并行接口（8 路，实测 171 秒）。
#   2. 缓存以 id(数据源) 为键、**永不过期** —— 和刚删掉的文件缓存
#      是同一类问题，服务跑一天就一直用早上那份。

class StructSource:
    """假数据源：记录装配结构是"批量查"还是"逐台串行查"。"""

    def __init__(self, n=5):
        self._sns = [f"EA8E00{i:04d}" for i in range(n)]
        self.batch_calls = 0
        self.single_calls = 0

    def available_structures(self):
        return list(self._sns)

    def available_serials(self):
        return list(self._sns)

    def serials_for(self, **kw):
        # 默认实时列整机就等于它的固定清单；需要模拟"多出新机"的用例自己覆盖。
        return list(self._sns)

    def structure(self, sn, **kw):
        self.single_calls += 1
        return [["CB4", "KRD 901 330/11 R1A", sn, "2026-09-14 10:00",
                 "KRE 105 518 R1C : DS4W0270J6", ""]]

    def structures(self, sns, **kw):
        self.batch_calls += 1
        return {sn: self.structure(sn) for sn in sns}

    def refresh(self):
        """/api/refresh 会调数据源的刷新 + 清结构缓存，所以这里要有。"""
        pass


def test_装配结构用批量并行而不是逐台串行(monkeypatch):
    """串行查 80 台约 16 分钟，页面会直接挂住。"""
    import importlib

    import api
    importlib.reload(api)
    s = StructSource()
    monkeypatch.setattr(api, "_source", s)
    api._struct_cache_clear()

    api._all_structures()
    assert s.batch_calls == 1, "没有用批量接口，还在逐台串行查"


def test_装配结构缓存会过期(monkeypatch):
    """永不过期的缓存等于又回到看旧数据。"""
    import importlib

    import api
    importlib.reload(api)
    s = StructSource()
    monkeypatch.setattr(api, "_source", s)
    api._struct_cache_clear()

    now = {"t": 5000.0}
    monkeypatch.setattr(api.time, "monotonic", lambda: now["t"])

    api._all_structures()
    assert s.batch_calls == 1

    # 存活期内复用
    now["t"] += api.STRUCT_CACHE_TTL_SECONDS / 2
    api._all_structures()
    assert s.batch_calls == 1, "存活期内不该重复查"

    # 过期后重新查
    now["t"] += api.STRUCT_CACHE_TTL_SECONDS + 1
    api._all_structures()
    assert s.batch_calls == 2, "过期了却没重新查 MI"


def test_装配结构的存活时间比测试历史长(monkeypatch):
    """这是刻意的取舍，不是写错了。

    装配结构查一遍要 171 秒（80 台 8 路并行），如果也给 60 秒存活时间，
    就等于永远在重查。而装配结构只在产线换件时才变，不像测试数据那样
    每分钟都在产生新的，所以给更长的存活时间是合理的。
    """
    import api
    assert api.STRUCT_CACHE_TTL_SECONDS > ds.MiSource.CACHE_TTL_SECONDS


def test_刷新能清掉装配结构缓存(monkeypatch):
    """用户点「刷新」是想要最新数据，结构缓存也得跟着清。"""
    import importlib

    import api
    importlib.reload(api)
    s = StructSource()
    monkeypatch.setattr(api, "_source", s)
    api._struct_cache_clear()

    api._all_structures()
    assert s.batch_calls == 1

    from fastapi.testclient import TestClient
    TestClient(api.app).get("/api/refresh")

    api._all_structures()
    assert s.batch_calls == 2, "刷新后没有重新查装配结构"


def test_装配结构基于实时列整机而非固定清单(monkeypatch):
    """★ 全改的一部分：装配结构的整机范围也要实时列，不能用固定清单。

    否则供应商台数统计、下层件追踪都只覆盖固定 80 台，漏掉产线新机。
    """
    import importlib

    import api
    importlib.reload(api)

    class RangeStructSource(StructSource):
        def __init__(self):
            super().__init__()
            self.serials_for_called = False

        def serials_for(self, **kw):
            # 实时列整机：比固定清单多一台
            self.serials_for_called = True
            return self._sns + ["EA8E009999"]

    s = RangeStructSource()
    monkeypatch.setattr(api, "_source", s)
    api._struct_cache_clear()

    structs = api._all_structures()
    assert s.serials_for_called, "装配结构没走实时列整机，还在用固定清单"
    assert "EA8E009999" in structs, "产线新机没被纳入装配结构"
