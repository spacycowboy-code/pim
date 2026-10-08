"""供应商选项必须瞬间可用，不能等 MI。

背景：页面初始化卡住 9 分半
--------------------------
`/api/suppliers` 给界面填「天线供应商 / 滤波器供应商」两个下拉框，
页面一打开就会调它。它原来的做法是把**全部 80 台整机的装配结构**都拉下来，
再从里面统计有哪些供应商。

删掉文件缓存之后这就变成了真实的网络开销：
    串行逐台          80 台 x 约 12 秒 ≈ 16 分钟（实测挂起 5 分钟还没返回）
    改成 8 路并行     实测 567 秒 = 9.5 分钟
并行化有效，但 9.5 分钟的页面初始化照样不可用。

关键观察：**列出"有哪些供应商"根本不需要查 MI。**
供应商是靠子件序列号前 3 位识别的（已实测验证「序列号前 3 位 == MI 的
Mfg.Unit 代码」且前缀与供应商严格一对一），映射表 `SUPPLIER_NAMES` 就在
代码里。需要查 MI 的只有「这家供应商涉及几台整机」这个附加统计。

所以拆成两件事：
    列出选项（下拉框要的）      查静态表，0 次网络请求，瞬间
    统计涉及台数（附加信息）    要拉全部装配结构，9.5 分钟，按需才查
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import structure as st


# ---------- 静态表就够列出选项 ----------

def test_列出供应商不需要任何装配数据():
    """这是整件事的关键：函数签名里不该有"装配结构"这个参数。"""
    opts = st.known_suppliers()
    assert set(opts) == {"antenna", "filter_unit"}


def test_天线有两家供应商():
    names = {x["supplier"] for x in st.known_suppliers()["antenna"]}
    assert names == {"Comba", "AMPHENOL"}


def test_滤波器有两家供应商而不是三家():
    """产地不算供应商：春兴苏州 DK4 和春兴深圳 EC1 是同一家，要合并。

    实测 MI 里是 3 个 Mfg.Unit 代码，但那是 3 个厂、2 家公司。
    """
    names = {x["supplier"] for x in st.known_suppliers()["filter_unit"]}
    assert names == {"Grentech", "Chunxing"}, "春兴的两个厂应该合并成一家"


def test_每个供应商都带上子件产品号():
    """界面上要显示这家供应商供的是哪个件。"""
    for pt, prod in (("antenna", "KRE 105 518"),
                     ("filter_unit", "KRF 901 809")):
        for x in st.known_suppliers()[pt]:
            assert x["child_product"] == prod


def test_静态表和分类表必须一一对应():
    """漏一个前缀，那家供应商就不会出现在下拉框里，等于悄悄丢数据。"""
    assert set(st.SUPPLIER_NAMES) == set(st.SUPPLIER_PART_TYPES), \
        "SUPPLIER_NAMES 和 SUPPLIER_PART_TYPES 的前缀必须完全一致"


def test_分类只能是天线或滤波器():
    """只有这两类是外采机械件，才有供应商之分。

    厂内板卡（ROA/ROX）的序列号前缀是流水号段不是供应商码，
    这条前缀规则对它们不成立，别混进来。
    """
    assert set(st.SUPPLIER_PART_TYPES.values()) == {"antenna", "filter_unit"}


# ---------- 接口层 ----------

class NoNetSource:
    """假数据源：一旦有人去查装配结构就记一笔，用来证明"没查网络"。"""

    def __init__(self):
        self.struct_calls = 0

    def available_structures(self):
        return ["EA8E538914", "EA8E538919"]

    def available_serials(self):
        return ["EA8E538914", "EA8E538919"]

    def serials_for(self, **kw):
        # _all_structures 现在靠它实时列整机。这个假源返回固定两台即可。
        return ["EA8E538914", "EA8E538919"]

    def structures(self, sns, **kw):
        self.struct_calls += 1
        return {sn: [["CB4", "KRD 901 330/11 R1A", sn, "2026-09-14 10:00",
                      "KRE 105 518 R1C : DS4W0270J6,"
                      "KRF 901 809 R1A : F98BX00001", ""]] for sn in sns}

    def structure(self, sn, **kw):
        self.struct_calls += 1
        return self.structures([sn])[sn]

    def refresh(self):
        pass


@pytest.fixture
def api_mod(monkeypatch):
    import importlib

    import api
    importlib.reload(api)
    src = NoNetSource()
    monkeypatch.setattr(api, "_source", src)
    api._struct_cache_clear()
    api._probe = src          # 方便用例拿到它数调用次数
    return api


def test_供应商接口默认不查装配结构(api_mod):
    """页面一打开就会调它，所以它必须是瞬间的。"""
    from fastapi.testclient import TestClient
    r = TestClient(api_mod.app).get("/api/suppliers")
    assert r.status_code == 200
    assert api_mod._probe.struct_calls == 0, \
        "默认就去拉装配结构了，页面初始化会卡住 9 分钟"


def test_供应商接口给出可筛选的选项(api_mod):
    from fastapi.testclient import TestClient
    d = TestClient(api_mod.app).get("/api/suppliers").json()
    assert {x["supplier"] for x in d["antenna"]} == {"Comba", "AMPHENOL"}
    assert {x["supplier"] for x in d["filter_unit"]} == {"Grentech", "Chunxing"}


def test_默认不给台数统计时要明确标出来(api_mod):
    """不能让"没统计"看起来像"0 台"。"""
    from fastapi.testclient import TestClient
    d = TestClient(api_mod.app).get("/api/suppliers").json()
    assert d["counted"] is False
    for x in d["antenna"]:
        assert x["n_parents"] is None, "没统计就该是 null，不能是 0"


def test_明确要台数统计时才去查MI(api_mod):
    """这个开关会触发 9.5 分钟的查询，必须是用户主动要。"""
    from fastapi.testclient import TestClient
    d = TestClient(api_mod.app).get("/api/suppliers",
                                    params={"with_counts": "true"}).json()
    assert api_mod._probe.struct_calls >= 1
    assert d["counted"] is True
    comba = [x for x in d["antenna"] if x["supplier"] == "Comba"][0]
    assert comba["n_parents"] == 2


def test_要台数统计时也要提示这很慢(api_mod):
    from fastapi.testclient import TestClient
    d = TestClient(api_mod.app).get("/api/suppliers").json()
    assert "note" in d
