"""MiSource（实时查 MI）的测试（TDD：先写这个）。

为什么需要它
------------
服务器上装不了 Kerberos 依赖（要 sudo），所以服务器只能读离线快照，
日期和条件都受预导出组合限制。

但**本机是 Windows 域内机器，有域身份，能直接访问 MI**。
所以同一套网页界面在本机跑、数据源换成 MiSource，就能实时查任意条件，
不用导出也不用同步。

这些测试用假客户端，不真的连 MI——单元测试必须离线且快。
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import data_source as ds

TEST_COLS = ["", "Factory", "SerialNumber", "ProductNumber",
             "ProductNumberRevisionState", "TestCriteria", "TestStartTime",
             "TestCount", "TestPassed", "TestPointName", "TestPointValue",
             "ControlCategoryID", "TestCategoryID", "TestStationID",
             "TestEndTime", "SortingId", "SubUnitData"]

PRODUCT = "KRD 901 330/11"


def _hist(sn, rows):
    return {"data": {"resultSet": [{
        "columns": [{"index": i, "name": c} for i, c in enumerate(TEST_COLS)],
        "rows": rows}]}}


def _row(sn, start, end, passed="Y", cat="212", count=1):
    return ["", "ESS Nanjing", sn, PRODUCT, "R1A", "CXC_X", start, str(count),
            passed, "[N/A]", "[N/A]", "[NULL]", cat, "8945:1", end, "0", "None"]


class FakeClient:
    """假的 MiClient。记录调用参数，方便断言"真的把条件传给了 MI"。"""

    def __init__(self):
        self.calls = []
        self.tp_rows = [["1.1", "测量项A", "1", "组A", "CXC", "RADIO", "", "",
                         "212", "", "100", "0.5"] + [""] * 11]

    def test_points(self, **kw):
        self.calls.append(("test_points", kw))
        return list(self.tp_rows)

    def structure(self, sn, **kw):
        self.calls.append(("structure", sn))
        return [["F1", PRODUCT, sn, "2026-09-14 10:00", "CHILD1", ""]]

    def structures(self, sns, **kw):
        self.calls.append(("structures", list(sns)))
        return {sn: self.structure(sn) for sn in sns}

    def test_history(self, sn, **kw):
        self.calls.append(("test_history", sn))
        return _hist(sn, [
            _row(sn, "2026-09-14 08:00:00", "2026-09-14 09:00:00"),
            _row(sn, "2026-09-20 10:00:00", "2026-09-20 11:00:00", passed="N",
                 count=2),
        ])

    def test_histories(self, sns, **kw):
        self.calls.append(("test_histories", list(sns)))
        return {sn: self.test_history(sn) for sn in sns}

    def products_in_range(self, **kw):
        self.calls.append(("products_in_range", kw))
        return ["KRD 901 330/11", "KRD 901 400/1"]

    def serials_in_range(self, **kw):
        self.calls.append(("serials_in_range", kw))
        # 实时列整机：故意比固定清单 ["EA001","EA002"] 多一台，
        # 代表产线新产、固定清单里没有的机器。
        return ["EA001", "EA002", "EA003"]

    def unit_structure(self, **kw):
        """UnitProductStructure 原始响应。整机列表和供应商都从这里解析
        （共享同一个响应，所以供应商零额外网络成本）。"""
        self.calls.append(("unit_structure", kw))
        cols = ["Factory", "Product Number", "Product RState",
                "Part Product Number", "Part Product R-State", "Mfg.Unit",
                "First Tests", "Passed Tests", "Test Result",
                "Test Passed W/O Repair", "ParentSN => ChildSN"]
        rows = [["ESS", PRODUCT, "R1A", "KRE 105 518", "R1C", "DS4",
                 "1", "1", "Y", "Y", f"{sn} => DS4W027{i:03d}"]
                for i, sn in enumerate(["EA001", "EA002", "EA003"])]
        return {"data": {"m_Item1": {"resultSet": [
            {"resultSetName": "Data1",
             "columns": [{"name": c} for c in cols],
             "data": rows}]}, "m_Item2": None}}


# ---------------------------------------------------------------------------
# 实时列整机：替代固定清单（这是本次改造的核心）
# ---------------------------------------------------------------------------

def test_serials_for_实时问MI要整机列表(src):
    """按产品+日期实时列整机，而不是用构造时传进来的固定清单。"""
    sns = src.serials_for(product=PRODUCT,
                          from_date="2026-09-01 00:00",
                          to_date="2026-10-01 00:00")
    assert sns == ["EA001", "EA002", "EA003"]       # 比固定清单多了 EA003
    name, kw = [c for c in src.client.calls if c[0] == "unit_structure"][-1]
    assert kw["product"] == PRODUCT


def test_serials_for_同条件只问一次(src):
    """同一 (产品,日期) 要缓存，否则每次查询都多一次网络往返。"""
    for _ in range(3):
        src.serials_for(product=PRODUCT, from_date="A", to_date="B")
    n = sum(1 for c in src.client.calls if c[0] == "unit_structure")
    assert n == 1


def test_serials_for_换条件重新问(src):
    """换了产品或日期，整机列表会变，必须重新问。"""
    src.serials_for(product=PRODUCT, from_date="A", to_date="B")
    src.serials_for(product=PRODUCT, from_date="A", to_date="C")
    n = sum(1 for c in src.client.calls if c[0] == "unit_structure")
    assert n == 2


def test_all_test_runs_可按给定整机拉(src):
    """给了序列号就拉这批的历史（实时列整机的结果喂进来）。"""
    runs = src.all_test_runs(serials=["EA001", "EA002", "EA003"])
    assert {r.serial_number for r in runs} == {"EA001", "EA002", "EA003"}
    name, sns = [c for c in src.client.calls if c[0] == "test_histories"][-1]
    assert set(sns) == {"EA001", "EA002", "EA003"}


def test_刷新也清掉整机列表缓存(src):
    """点刷新要能拿到产线最新的整机列表，不能还用缓存的旧列表。"""
    src.serials_for(product=PRODUCT, from_date="A", to_date="B")
    src.refresh()
    src.serials_for(product=PRODUCT, from_date="A", to_date="B")
    n = sum(1 for c in src.client.calls if c[0] == "unit_structure")
    assert n == 2


# ---------------------------------------------------------------------------
# 供应商：和列整机共享同一个 UnitProductStructure 响应（零额外网络成本）
# ---------------------------------------------------------------------------

UPS_COLS = ["Factory", "Product Number", "Product RState",
            "Part Product Number", "Part Product R-State", "Mfg.Unit",
            "First Tests", "Passed Tests", "Test Result",
            "Test Passed W/O Repair", "ParentSN => ChildSN"]


class UpsClient(FakeClient):
    """假客户端：实现 unit_structure（原始响应），供应商和整机列表都从它来。"""

    def unit_structure(self, **kw):
        self.calls.append(("unit_structure", kw))
        rows = [
            ["ESS", PRODUCT, "R1A", "KRE 105 518", "R1C", "DS4",
             "1", "1", "Y", "Y", "EA001 => DS4W0270K6"],
            ["ESS", PRODUCT, "R1A", "KRF 901 809", "R1A", "F98",
             "1", "1", "Y", "Y", "EA001 => F98BX00001"],
            ["ESS", PRODUCT, "R1A", "KRE 105 518", "R1C", "EK6",
             "1", "1", "Y", "Y", "EA002 => EK61B00002"],
        ]
        return {"data": {"m_Item1": {"resultSet": [
            {"resultSetName": "Data1",
             "columns": [{"name": c} for c in UPS_COLS],
             "data": rows}]}, "m_Item2": None}}


def test_供应商和整机列表只发一次请求():
    """★ 关键优化：两者用同一个 UnitProductStructure 响应。

    逐台查装配结构要 270 秒（60 台），而这样一次请求 15 秒就都有了。
    """
    s = ds.MiSource(client=UpsClient(), serials=["EA001"])
    sns = s.serials_for(product=PRODUCT, from_date="A", to_date="B")
    sups = s.suppliers_for(product=PRODUCT, from_date="A", to_date="B")
    assert sns == ["EA001", "EA002"]
    assert sups["EA001"] == {"antenna": ["Comba"], "filter_unit": ["Grentech"]}
    assert sups["EA002"]["antenna"] == ["AMPHENOL"]
    n = sum(1 for c in s.client.calls if c[0] == "unit_structure")
    assert n == 1, f"发了 {n} 次请求，应该共享同一个响应只发 1 次"


def test_供应商查询失败不抛异常():
    """MI 偶发超时很常见，供应商列是附加信息，拿不到就空着。"""
    class Boom(FakeClient):
        def unit_structure(self, **kw):
            raise RuntimeError("MI 超时")
    s = ds.MiSource(client=Boom(), serials=["EA001"])
    assert s.suppliers_for(product=PRODUCT, from_date="A", to_date="B") == {}


# ---------------------------------------------------------------------------
# 失败分支：只对失败的整机查（每台约 30 秒，8 路并行）
# ---------------------------------------------------------------------------

class ChildClient(FakeClient):
    def child_test_histories(self, sns, **kw):
        self.calls.append(("child_test_histories", list(sns)))
        cols = ["Serial Number", "Mfg Date", "Test Start Date", "Test Status",
                "Failed Test Point Code", "Failed Test Point Name"]
        out = {}
        for i, sn in enumerate(sns):
            codes = ["5.2.97.3.0", "5.2.102.10.0"] if i == 0 else ["5.2.97.7.0"]
            out[sn] = {"data": {"resultSet": [{
                "columns": [{"name": c} for c in cols],
                "rows": [[sn, "", "", "Fail", c, ""] for c in codes]}]}}
        return out


def test_失败分支按整机返回():
    s = ds.MiSource(client=ChildClient(), serials=["EA001", "EA002"])
    got = s.failed_branches_for(["EA001", "EA002"])
    assert got["EA001"] == [3, 10]
    assert got["EA002"] == [7]


def test_失败分支只查给定的整机():
    """只有失败的整机才需要查（每台约 30 秒），通过的不查。"""
    s = ds.MiSource(client=ChildClient(), serials=["EA001", "EA002", "EA003"])
    s.failed_branches_for(["EA002"])
    name, sns = [c for c in s.client.calls
                 if c[0] == "child_test_histories"][-1]
    assert sns == ["EA002"], "查了不该查的整机，会白等几十秒"


def test_没有失败整机时不发请求():
    s = ds.MiSource(client=ChildClient(), serials=["EA001"])
    assert s.failed_branches_for([]) == {}
    assert not [c for c in s.client.calls if c[0] == "child_test_histories"]


@pytest.fixture
def src():
    return ds.MiSource(client=FakeClient(),
                       serials=["EA001", "EA002"], products=[PRODUCT])


# ---------------------------------------------------------------------------
# 身份标记：界面要靠它显示"实时"还是"离线快照"
# ---------------------------------------------------------------------------

def test_标记为在线数据源(src):
    assert src.is_offline is False


def test_有live标记方便界面区分(src):
    assert src.is_live is True


# ---------------------------------------------------------------------------
# 产品号下拉：实时模式启动时从 MI 检索在产产品号
# ---------------------------------------------------------------------------

def test_可从MI检索在产产品号():
    """启动时按日期范围问 MI "这段时间生产了哪些产品"，填进下拉。"""
    s = ds.MiSource(client=FakeClient())
    prods = s.discover_products(from_date="2026-04-01 00:00",
                                to_date="2026-10-01 00:00")
    assert prods == ["KRD 901 330/11", "KRD 901 400/1"]
    name, kw = s.client.calls[-1]
    assert name == "products_in_range"
    assert kw["from_date"] == "2026-04-01 00:00"
    assert kw["to_date"] == "2026-10-01 00:00"


def test_检索到的产品号进入可选列表():
    """discover_products 之后，available_products 要能返回它们（填下拉用）。"""
    s = ds.MiSource(client=FakeClient())
    s.discover_products(from_date="2026-04-01 00:00", to_date="2026-10-01 00:00")
    assert s.available_products() == ["KRD 901 330/11", "KRD 901 400/1"]


def test_discover_products_合并不覆盖已有的():
    """MI 探测结果不能覆盖构造时传入的默认产品。

    踩过的坑：Crete 平台产品（KRD 901 328/11 等）当下不一定在产，MI 探测
    "最近 183 天在产的产品" 不包含它们；原实现直接把 _products 覆盖掉，
    于是用户在界面上看不到 Crete 的下拉候选。

    合并而不是覆盖后，Crete 永远在候选里；MI 探测到的其他产品也会补进来。
    """
    s = ds.MiSource(client=FakeClient(),
                    products=["KRD 901 328/11", "KRD 901 329/11"])
    s.discover_products(from_date="2026-04-01 00:00", to_date="2026-10-01 00:00")
    all_prods = s.available_products()
    # Crete 保留
    assert "KRD 901 328/11" in all_prods
    assert "KRD 901 329/11" in all_prods
    # MI 探测到的也进来
    assert "KRD 901 330/11" in all_prods
    assert "KRD 901 400/1" in all_prods


def test_检索产品号失败不抛异常():
    """MI 偶发超时很常见，检索失败时产品号下拉可以空，但服务不能挂。"""
    class Boom(FakeClient):
        def products_in_range(self, **kw):
            raise RuntimeError("MI 超时")
    s = ds.MiSource(client=Boom(), products=["KRD 901 330/11"])
    got = s.discover_products(from_date="x", to_date="y")
    assert got == []                        # 返回空而不是抛
    assert s.available_products() == ["KRD 901 330/11"]   # 旧值保留


# ---------------------------------------------------------------------------
# 取测试点数据：条件必须原样传给 MI
# ---------------------------------------------------------------------------

def test_查测试点会把条件传给MI(src):
    rows = src.test_points(product=PRODUCT, from_date="2026-09-01 00:00",
                           to_date="2026-10-01 00:00",
                           serial_numbers=["EA001"], test_category=["212"],
                           test_count="[All]", only_failed=False)
    assert len(rows) == 1
    name, kw = src.client.calls[-1]
    assert name == "test_points"
    assert kw["product"] == PRODUCT
    assert kw["from_date"] == "2026-09-01 00:00"
    assert kw["serial_numbers"] == ["EA001"]


def test_实时查询永远是精确命中(src):
    """在线数据源不存在"快照里没有这个组合"的问题，所以永远 exact。"""
    rows, info = src.test_points_resolved(
        product=PRODUCT, from_date="2026-09-01 00:00", to_date="2026-10-01 00:00",
        serial_numbers=["EA001"], test_category=["212"])
    assert len(rows) == 1
    assert info["exact"] is True
    assert info["substituted"] is False


def test_任意日期都能查不报错(src):
    """这是相对快照版的关键优势。"""
    for a, b in [("2020-01-01 00:00", "2020-02-01 00:00"),
                 ("2026-09-20 00:00", "2026-09-21 00:00")]:
        rows, info = src.test_points_resolved(
            product=PRODUCT, from_date=a, to_date=b, serial_numbers=["EA001"])
        assert info["exact"] is True


# ---------------------------------------------------------------------------
# 可选值：界面要填下拉框和提示
# ---------------------------------------------------------------------------

def test_报告已知序列号(src):
    assert src.available_serials() == ["EA001", "EA002"]


def test_报告已知产品号(src):
    assert src.available_products() == [PRODUCT]


def test_测试次数给全部可选值(src):
    """实时查询不受快照限制，MI 支持的都能选。"""
    counts = src.available_test_counts()
    assert "[All]" in counts and "First" in counts and "Last" in counts


def test_日期范围不设限(src):
    """在线数据源没有"快照覆盖范围"这回事，返回 None 让界面知道不设限。"""
    lo, hi = src.date_range()
    assert lo is None and hi is None


def test_类别给已知常用值(src):
    cats = src.available_categories()
    assert "212" in cats


# ---------------------------------------------------------------------------
# 测试历史：实时拉，支持任意日期筛选
# ---------------------------------------------------------------------------

def test_拉单台测试历史(src):
    runs = src.test_runs("EA001")
    assert len(runs) == 2
    assert runs[1].passed is False


def test_拉全部测试历史(src):
    runs = src.all_test_runs()
    assert len(runs) == 4
    assert {r.serial_number for r in runs} == {"EA001", "EA002"}


def test_全部历史按时间排序(src):
    starts = [r.started for r in src.all_test_runs()]
    assert starts == sorted(starts)


def test_历史只拉一次并缓存(src):
    """每次查询都重拉 80 台会慢到不可用，必须缓存。"""
    src.all_test_runs()
    n1 = sum(1 for c in src.client.calls if c[0] == "test_histories")
    src.all_test_runs()
    n2 = sum(1 for c in src.client.calls if c[0] == "test_histories")
    assert n1 == 1 and n2 == 1


def test_历史时间范围来自真实数据(src):
    lo, hi = src.history_span()
    assert lo == dt.datetime(2026, 9, 14, 8, 0, 0)
    assert hi == dt.datetime(2026, 9, 20, 11, 0, 0)


def test_历史里的类别和产品(src):
    assert src.history_categories() == ["212"]
    assert src.history_products() == [PRODUCT]
    assert src.history_serials() == ["EA001", "EA002"]


def test_能刷新缓存拿最新数据(src):
    """生产线一直在产数据，用户要能主动刷新。"""
    src.all_test_runs()
    src.refresh()
    src.all_test_runs()
    assert sum(1 for c in src.client.calls if c[0] == "test_histories") == 2


# ---------------------------------------------------------------------------
# 装配结构
# ---------------------------------------------------------------------------

def test_查装配结构(src):
    rows = src.structure("EA001")
    assert len(rows) == 1


def test_报告可查结构的序列号(src):
    """在线版能查任意序列号，所以返回已知列表即可。"""
    assert src.available_structures() == ["EA001", "EA002"]


# ---------------------------------------------------------------------------
# 元信息：界面顶部要显示数据来源
# ---------------------------------------------------------------------------

def test_元信息标明是实时数据(src):
    mf = src.manifest()
    assert mf["live"] is True
    assert "实时" in mf["source"] or "MI" in mf["source"]


def test_没有快照条目(src):
    """在线版没有"预导出的查询组合"这个概念。"""
    assert src.entries() == []
