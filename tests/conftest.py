"""测试共享装置（pytest 自动加载，不用 import）。

这里放一个**假的 MI 客户端**。为什么需要它：

系统只有一条取数路径了 —— 实时查 MI。但单元测试绝不能真的连 MI
（慢、要域身份、结果会随产线数据变）。所以测试时把最底层的网络客户端
换成这个假的，上面整套数据源、计算、接口代码都走真实路径。

`conftest.py` 是 pytest 的约定文件名：放在这里的 fixture（测试装置），
所有测试文件直接按名字用，不需要导入。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

PRODUCT = "KRD 901 330/11"

# MI 测试历史报表的 17 列（顺序照实测，别改）
HIST_COLS = ["", "Factory", "SerialNumber", "ProductNumber",
             "ProductNumberRevisionState", "TestCriteria", "TestStartTime",
             "TestCount", "TestPassed", "TestPointName", "TestPointValue",
             "ControlCategoryID", "TestCategoryID", "TestStationID",
             "TestEndTime", "SortingId", "SubUnitData"]


def tp_row(code, *, tested=6, pct_fail=0.0, avg=None, name="test", unit="",
           group="RX PIM Power", category="212"):
    """造一行 TestPointSummary 数据（23 列）。

    列的位置是实测确认的，不能挪：
      0 测试点编码   1 测试点名称   3 测试点组名   8 测试类别
      9 单位         10 被测次数    11 失败百分比   16 平均值
    """
    r = [None] * 23
    r[0], r[1], r[3] = code, name, group
    r[8], r[9], r[10], r[11] = category, unit, tested, pct_fail
    r[16] = avg
    return r


def hist_row(sn, start, end, passed="Y", cat="212", count=1,
             product=PRODUCT):
    """造一行测试历史数据（17 列）。一行 = 一个测试场次。"""
    return ["", "ESS Nanjing", sn, product, "R1A", "CXC_X", start, str(count),
            passed, "[N/A]", "[N/A]", "[NULL]", cat, "8945:1", end, "0", "None"]


def hist_payload(rows):
    """把若干行包成 MI 测试历史报表的响应结构（结构 B：自带列名）。"""
    return {"data": {"resultSet": [{
        "columns": [{"index": i, "name": c} for i, c in enumerate(HIST_COLS)],
        "rows": rows}]}}


# 默认的测试点数据：两个分支（5 和 9）各一个功率项、一个通过率项。
# 刻意同时放两类数值，因为代码里有一条不变量是「功率和通过率绝不混着平均」。
DEFAULT_TP_ALL = [
    tp_row("5.2.100.5.0", tested=100, avg=-104.0,
           name="noise_power_level_before_testing 1896 MHz"),
    tp_row("5.2.109.5.0", tested=100, avg=1.0, unit="Dbm",
           name="knock_response 1896 MHz"),
    tp_row("5.2.100.9.0", tested=100, avg=-105.0,
           name="noise_power_level_before_testing 1896 MHz"),
    tp_row("5.2.109.9.0", tested=100, avg=0.8, unit="Dbm",
           name="knock_response 1896 MHz"),
]

# 失败查询返回的行。注意 MI 的坑：only_failed=True 时 `# Tested` 这一列的
# 语义变成「失败次数」，不是「被测次数」。所以这里的 8 代表失败了 8 次。
DEFAULT_TP_FAILED = [
    tp_row("5.2.109.9.0", tested=8, avg=0.8, unit="Dbm",
           name="knock_response 1896 MHz"),
]

DEFAULT_SERIALS = ["EA8E538914", "EA8E538919"]


class FakeClient:
    """假的 MiClient：不碰网络，按参数返回预置数据，并记录每次调用。

    记录调用（`self.calls`）是为了能断言"查询条件真的传到了底层"，
    而不只是"接口返回了 200"。
    """

    def __init__(self, tp_all=None, tp_failed=None, serials=None,
                 products=None, hist=None):
        self.calls = []
        self.tp_all = list(DEFAULT_TP_ALL if tp_all is None else tp_all)
        self.tp_failed = list(DEFAULT_TP_FAILED if tp_failed is None
                              else tp_failed)
        self.serials = list(DEFAULT_SERIALS if serials is None else serials)
        self.products = list([PRODUCT] if products is None else products)
        # hist: {序列号: [历史行, ...]}。不给就自动造「首测通过」的一场。
        self._hist = hist

    # -- 测试点 ----------------------------------------------------------

    def test_points(self, **kw):
        self.calls.append(("test_points", kw))
        rows = self.tp_failed if kw.get("only_failed") else self.tp_all
        return [list(r) for r in rows]

    # -- 装配结构 --------------------------------------------------------

    def structure(self, sn, **kw):
        self.calls.append(("structure", sn))
        return [["CB4", f"{PRODUCT} R1A", sn, "2026-09-14 10:43:32",
                 "KRE 105 518 R1C : DS4W0270J6,"
                 "KRF 901 809 R1A : F98BX00001", ""]]

    def structures(self, sns, **kw):
        self.calls.append(("structures", list(sns)))
        return {sn: self.structure(sn) for sn in sns}

    # -- 测试历史 --------------------------------------------------------

    def test_history(self, sn, **kw):
        self.calls.append(("test_history", sn))
        if self._hist is not None:
            return hist_payload(self._hist.get(sn, []))
        return hist_payload([
            hist_row(sn, "2026-09-14 08:00:00", "2026-09-14 09:00:00"),
            hist_row(sn, "2026-09-20 10:00:00", "2026-09-20 11:00:00",
                     passed="N", count=2),
        ])

    def test_histories(self, sns, **kw):
        self.calls.append(("test_histories", list(sns)))
        return {sn: self.test_history(sn) for sn in sns}

    # -- 元信息 ----------------------------------------------------------

    def products_in_range(self, **kw):
        self.calls.append(("products_in_range", kw))
        return list(self.products)

    def serials_in_range(self, **kw):
        self.calls.append(("serials_in_range", kw))
        # 实时列整机：默认就返回这批假机器（和 serials 一致），
        # 需要模拟"产线比固定清单多"的场景时自己构造 FakeClient 覆盖。
        return list(self.serials)

    def unit_structure(self, **kw):
        """UnitProductStructure 原始响应。整机列表和供应商共享它
        （所以供应商零额外网络成本）。默认给每台装 Comba 天线 + Grentech 滤波器。"""
        self.calls.append(("unit_structure", kw))
        cols = ["Factory", "Product Number", "Product RState",
                "Part Product Number", "Part Product R-State", "Mfg.Unit",
                "First Tests", "Passed Tests", "Test Result",
                "Test Passed W/O Repair", "ParentSN => ChildSN"]
        rows = []
        for sn in self.serials:
            rows.append(["ESS", PRODUCT, "R1A", "KRE 105 518", "R1C", "DS4",
                         "1", "1", "Y", "Y", f"{sn} => DS4W0270J6"])
            rows.append(["ESS", PRODUCT, "R1A", "KRF 901 809", "R1A", "F98",
                         "1", "1", "Y", "Y", f"{sn} => F98BX00001"])
        return {"data": {"m_Item1": {"resultSet": [
            {"resultSetName": "Data1",
             "columns": [{"name": c} for c in cols],
             "data": rows}]}, "m_Item2": None}}

    def child_test_histories(self, sns, **kw):
        """子件测试历史（批量）。用来拿失败测试点编码 → 解析失败分支。
        默认让每台都失败在分支 3 和 10（编码 5.2.<项>.<分支>.<频率>）。"""
        self.calls.append(("child_test_histories", list(sns)))
        cols = ["Serial Number", "Mfg Date", "Test Start Date", "Test Status",
                "Failed Test Point Code", "Failed Test Point Name"]
        return {sn: {"data": {"resultSet": [{
            "columns": [{"name": c} for c in cols],
            "rows": [[sn, "", "", "Fail", code, f"rfPort {code.split('.')[3]}"]
                     for code in ("5.2.97.3.0", "5.2.102.10.0")]}]}}
                for sn in sns}


@pytest.fixture
def fake_client():
    """一个干净的假 MI 客户端。需要定制数据时自己构造 FakeClient。"""
    return FakeClient()


def _build_live_client(monkeypatch, client):
    """用给定的假客户端重新加载后端，返回接口测试客户端。

    为什么要 reload：数据源是在模块加载时建好的（进程级只建一次），
    所以换数据源必须重新加载模块。
    """
    import importlib

    import mi_client
    # 把真客户端换成假的。MiSource 内部是 `from mi_client import MiClient`
    # 延迟导入，取的是模块属性，所以这里替换能生效。
    monkeypatch.setattr(mi_client, "MiClient", lambda **kw: client)
    # 清掉可能残留的环境变量，确保测的是「默认行为」
    monkeypatch.delenv("PIM_SOURCE", raising=False)
    monkeypatch.delenv("PIM_SNAPSHOT_DIR", raising=False)

    import api
    importlib.reload(api)
    # 候选清单平时从 data/serials.json 读真实文件，测试里换成假数据，
    # 这样断言不会随那个文件的内容变化而失败。
    # ★ 产品号候选 = 假客户端的 products + CRETE_PRODUCTS（Crete 是代码里
    #   硬编码的默认，就算 fake_client 没列出来也要在候选里）。
    api._source._serials = list(client.serials)
    api._source._products = list(dict.fromkeys(
        list(client.products) + list(api.CRETE_PRODUCTS)))
    # 预填 _ups_cache：/api/parts 现在读的是已缓存的 UPS 响应
    # （不自动触发 MI 请求，避免 product=None 183 天的 20 分钟坑）。
    # 测试 fixture 要先预调一次 unit_structure 让缓存有数据。
    try:
        api._source._ups(None, None, None)
    except Exception:
        pass
    return api


@pytest.fixture
def live_api(monkeypatch, fake_client):
    """实时模式的后端模块（底层是假客户端，不碰网络）。"""
    return _build_live_client(monkeypatch, fake_client)


@pytest.fixture
def live_client(live_api):
    """实时模式的接口测试客户端（不起真服务，也不碰网络）。"""
    from fastapi.testclient import TestClient
    return TestClient(live_api.app)


@pytest.fixture
def make_live_client(monkeypatch):
    """工厂：用**定制**的假客户端起一个后端。

    需要特定数据的测试用这个，例如要造多台机器、多个日期的测试历史::

        def test_按日期筛(make_live_client):
            c = make_live_client(FakeClient(hist={"EA8E538914": [...]}))
            assert c.get("/api/history/yield", params={...}).status_code == 200
    """
    from fastapi.testclient import TestClient

    def build(client):
        api = _build_live_client(monkeypatch, client)
        return TestClient(api.app)

    return build
