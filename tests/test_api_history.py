"""测试历史相关接口（/api/history/*）的测试。

这批接口的核心价值：**支持任意日期范围**。因为测试历史报表返回的是明细、
带真实时间戳（TestStartTime），所以服务端拿到全量历史后可以自己按时间切，
换个日期不需要重新准备任何东西。

和 /api/yield 的本质区别（别混用）：
    /api/history/*   场次级 FPY 良率，level="run"。一次整机测试算一个场次，
                     任一测试点失败整场算失败。**这是本项目唯一的良率口径。**
    /api/yield       测试点次数级失败率，level="testpoint"。分母是"测试点被
                     执行的次数"（量级十万），算出来总是 99.x%，不是良率。

数据从哪来
----------
系统只有一条取数路径——实时查 MI。测试里把最底层的网络客户端换成
conftest.py 里的 `FakeClient`（假客户端，不碰网络），上面整套数据源、
计算、接口代码都走真实路径。

（原来这个文件是造一个假的快照目录 snapshot/*.json.gz 来喂数据的。
快照那条取数路径已经整体移除，所以改成喂假客户端。测的行为没变。）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

# conftest.py 的 fixture 不用导入，但里面的辅助函数和类要显式导入。
# tests/ 目录在 pytest 的 sys.path 里，所以能直接按模块名导入。
from conftest import HIST_COLS, FakeClient, hist_row

# MI 测试历史报表的第二张表：维修记录，8 列（顺序照实测）
REPAIR_COLS = ["SerialNumber", "TestStartTime", "RepairStartTime",
               "RepairEndTime", "FaultName", "FaultAreaName", "PositionID",
               "RepairActionName"]

# 装配结构报表的 6 列
STRUCT_COLS = ["Factory Code", "Product Number", "Serial Number",
               "Modified Time", "Newly Added Serial Number",
               "Removed Serial Number"]


def _srow(parent_sn, time, added="", removed=""):
    """造一行装配结构数据。added/removed 格式是 "产品号 R-state : 序列号"。"""
    return ["CB4", "KRD 901 330/11 R1A", parent_sn, time, added, removed]


class HistClient(FakeClient):
    """在共享的 FakeClient 上补两样这个文件专用的东西。

    1. **维修记录**：FakeClient 只返回测试历史那一张表，而 MI 的
       FunctionalTestAndRepairHistory 响应里是两张表（resultSet[0] 测试历史、
       resultSet[1] 维修历史）。测 /api/history/repairs 需要第二张。
    2. **每台不同的装配结构**：FakeClient 给所有机器返回同一份结构，
       而供应商筛选的用例要求两台机器装不同供应商的件。

    不改 conftest.py，所以在这里局部扩展。
    """

    def __init__(self, hist, repairs=None, structs=None, **kw):
        super().__init__(hist=hist, serials=sorted(hist), **kw)
        self._repairs = dict(repairs or {})
        self._structs = dict(structs or {})

    def test_history(self, sn, **kw):
        self.calls.append(("test_history", sn))
        return {"data": {"resultSet": [
            {"columns": [{"index": i, "name": c}
                         for i, c in enumerate(HIST_COLS)],
             "rows": [list(r) for r in self._hist.get(sn, [])]},
            {"columns": [{"index": i, "name": c}
                         for i, c in enumerate(REPAIR_COLS)],
             "rows": [list(r) for r in self._repairs.get(sn, [])]},
        ]}}

    def structure(self, sn, **kw):
        self.calls.append(("structure", sn))
        return [list(r) for r in self._structs.get(sn, [])]

    def unit_structure(self, **kw):
        """每台不同供应商的 UnitProductStructure 响应。

        供应商筛选走这个报表（一次请求拿全部整机），**不是**逐台查
        装配结构 —— 后者实测不限产品 + 183 天跑 20 分钟都不返回，
        选个供应商就把查询卡死。所以假客户端也得按这条路径供数据，
        否则测试覆盖不到真实走法。

        内容和上面 STRUCTS 保持一致：
            EA001  Comba 天线 + Grentech 滤波器
            EA002  只有 Chunxing 滤波器（没天线）
        """
        self.calls.append(("unit_structure", kw))
        cols = ["Factory", "Product Number", "Product RState",
                "Part Product Number", "Part Product R-State", "Mfg.Unit",
                "First Tests", "Passed Tests", "Test Result",
                "Test Passed W/O Repair", "ParentSN => ChildSN"]
        # 前缀决定供应商（前 3 位 == Mfg.Unit 代码，只对外购机械件成立）
        plan = {
            "EA001": [("KRE 105 518", "DS4", "DS4W0270J6"),
                      ("KRF 901 809", "F98", "F98BX00001")],
            "EA002": [("KRF 901 809", "DK4", "DK4G5A0001")],
        }
        rows = []
        for sn in self.serials:
            for part, mfg, child in plan.get(sn, []):
                rows.append(["ESS", "KRD 901 330/11", "R1A", part, "R1A",
                             mfg, "1", "1", "Y", "Y", f"{sn} => {child}"])
        return {"data": {"m_Item1": {"resultSet": [
            {"resultSetName": "Data1",
             "columns": [{"name": c} for c in cols],
             "data": rows}]}, "m_Item2": None}}


# ---------------------------------------------------------------------------
# 测试数据：两台机器，一共 5 个测试场次、3 个首次测试场次
#
#   EA001  09-14 通过(第1次)  09-20 失败(第2次)  09-25 通过(第3次)   类别 212
#   EA002  09-15 通过(第1次)  10-02 失败(第1次，类别 26)
#
# FPY 口径只看"第几次测试 == 1"的场次，所以 5 场里只有 3 场计入良率。
# EA002 有两条第一次测试，是因为类别 212 和类别 26 各有自己的第一次——
# 所以分母是"首次测试场次数"，不等于机器台数。
# ---------------------------------------------------------------------------

HIST = {
    "EA001": [
        hist_row("EA001", "2026-09-14 08:00:00", "2026-09-14 09:00:00"),
        hist_row("EA001", "2026-09-20 10:00:00", "2026-09-20 11:00:00",
                 passed="N", count=2),
        hist_row("EA001", "2026-09-25 10:00:00", "2026-09-25 11:00:00",
                 count=3),
    ],
    "EA002": [
        hist_row("EA002", "2026-09-15 08:00:00", "2026-09-15 09:00:00"),
        hist_row("EA002", "2026-10-02 08:00:00", "2026-10-02 09:00:00",
                 passed="N", cat="26"),
    ],
}

REPAIRS = {
    "EA001": [["EA001", "2026-09-20 10:00:00", "2026-09-20 12:00:00",
               "2026-09-20 12:30:00", "TEST Instability", "Test", "3",
               "Retest"]],
}

# 装配结构：EA001 装 F98BX 的滤波器（Grentech）+ DS4W0 的天线（Comba），
# EA002 只装 DK4G5 的滤波器（Chunxing）。用来测供应商筛选。
STRUCTS = {
    "EA001": [_srow("EA001", "2026-09-10 10:00:00",
                    added="KRF 901 809 R1A : F98BX00001,"
                          "KRE 105 518 R1C : DS4W0270J6")],
    "EA002": [_srow("EA002", "2026-09-11 10:00:00",
                    added="KRF 901 809 R1A : DK4G5A0001")],
}


@pytest.fixture
def client(monkeypatch, make_live_client):
    """接口测试客户端：真实的接口代码 + 假的 MI 客户端（不碰网络）。"""
    # 登录开关是在 api 模块导入时根据环境变量定的，所以要在重新加载 api
    # 之前清掉，否则本机设了这两个变量时所有请求都会被要求登录。
    monkeypatch.delenv("PIM_AUTH_USER", raising=False)
    monkeypatch.delenv("PIM_AUTH_PASS", raising=False)
    return make_live_client(
        HistClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))


# ---------------------------------------------------------------------------
# 可选范围：前端用它初始化日期选择器
# ---------------------------------------------------------------------------

def test_报告历史的可选范围(client):
    r = client.get("/api/history/options")
    assert r.status_code == 200
    j = r.json()
    assert j["n_serials"] == 2
    assert j["date_from"].startswith("2026-09-14")
    assert j["date_to"].startswith("2026-10-02")
    assert j["categories"] == ["212", "26"]
    assert j["products"] == ["KRD 901 330/11"]


def test_可选范围里说明日期可自由选(client):
    """前端要靠这个标记决定日期框是否解锁。"""
    j = client.get("/api/history/options").json()
    assert j["date_free"] is True


# ---------------------------------------------------------------------------
# ★ 任意日期范围查询——这是整个改动的目的
# ---------------------------------------------------------------------------

def test_按任意日期查良率(client):
    """口径固定为 FPY，所以只有第 1 次测试的场次计入：09-14、09-15。"""
    r = client.get("/api/history/yield", params={
        "from_date": "2026-09-14 00:00", "to_date": "2026-09-21 00:00"})
    assert r.status_code == 200
    j = r.json()
    assert j["n_runs"] == 2            # 09-14, 09-15（09-20 是第 2 次，不算 FPY）
    assert j["n_failed"] == 0
    assert j["level"] == "run"


def test_换个日期得到不同结果(client):
    """任意日期都能查，换个区间就是另一批场次，不受任何预置组合限制。"""
    a = client.get("/api/history/yield", params={
        "from_date": "2026-09-14 00:00", "to_date": "2026-09-16 00:00"}).json()
    b = client.get("/api/history/yield", params={
        "from_date": "2026-09-20 00:00", "to_date": "2026-10-03 00:00"}).json()
    assert a["n_runs"] == 2
    assert a["n_failed"] == 0
    # 09-20 和 09-25 都是复测（第 2/3 次），FPY 口径下只剩 10-02 这一场
    assert b["n_runs"] == 1
    assert b["n_failed"] == 1


def test_任意日期都能查不报404(client):
    """没数据的日期要返回 0 条，而不是 404 —— 这才符合直觉。

    （原用例名叫「没导出过的日期也不报 404」。「导出」是快照时代的概念，
    已经不存在了；但"任意日期都能查、没数据给空结果"这个意图依然成立，
    所以断言原样保留，只改名字。）
    """
    r = client.get("/api/history/yield", params={
        "from_date": "2020-01-01 00:00", "to_date": "2020-02-01 00:00"})
    assert r.status_code == 200
    assert r.json()["n_runs"] == 0


def test_不传日期就是全部(client):
    j = client.get("/api/history/yield").json()
    assert j["n_runs"] == 3            # FPY 口径：5 场里只有 3 场是第一次测试


def test_按产品号筛(client):
    j = client.get("/api/history/yield",
                   params={"product": "KRD 901 330/11"}).json()
    assert j["n_runs"] == 3
    j2 = client.get("/api/history/yield", params={"product": "别的"}).json()
    assert j2["n_runs"] == 0


def test_按测试类别筛(client):
    j = client.get("/api/history/yield", params={"category": "26"}).json()
    assert j["n_runs"] == 1


def test_按序列号筛(client):
    """EA001 有 3 场，但只有 1 场是第一次测试。"""
    j = client.get("/api/history/yield", params={"sn": "EA001"}).json()
    assert j["n_runs"] == 1


def test_良率口径固定为FPY(client):
    """良率只有一种口径：场次级 FPY。不传任何参数就是 FPY。

    注意 EA002 有两条第 1 次测试的记录（类别 212 和 26 各有自己的
    第一次测试），所以一共 3 条，不是按机器台数算的。
    """
    j = client.get("/api/history/yield").json()
    assert j["n_runs"] == 3
    assert j["first_only"] is True
    assert j["fpy"] is True


def test_不能用参数切回非FPY口径(client):
    """接口不再接受 first_only 开关——传了也必须被忽略，否则会出现第二种良率。"""
    j = client.get("/api/history/yield", params={"first_only": "false"}).json()
    assert j["n_runs"] == 3
    assert j["first_only"] is True


def test_良率结果标明是场次级(client):
    """必须和 /api/yield 的测试点级失败率区分，否则会被误读成数据错了。"""
    j = client.get("/api/history/yield").json()
    assert j["level"] == "run"
    assert "说明" in j or "note" in j
    # yield_pct 这个名字只属于场次级 FPY；测试点级的字段绝不能混进来
    assert "point_yield_pct" not in j


# ---------------------------------------------------------------------------
# 趋势
# ---------------------------------------------------------------------------

def test_按天趋势(client):
    """趋势也是 FPY 口径，复测场次不进桶。"""
    r = client.get("/api/history/trend", params={"by": "day"})
    assert r.status_code == 200
    j = r.json()
    assert j["fpy"] is True
    assert [x["bucket"] for x in j["rows"]] == [
        "2026-09-14", "2026-09-15", "2026-10-02"]


def test_趋势支持周和月(client):
    for by in ("week", "month", "hour"):
        assert client.get("/api/history/trend", params={"by": by}).status_code == 200


def test_不支持的粒度返回400(client):
    r = client.get("/api/history/trend", params={"by": "世纪"})
    assert r.status_code == 400


def test_趋势也能按日期筛(client):
    """09-20 ~ 09-26 之间只有复测场次，FPY 口径下这段是空的。"""
    rows = client.get("/api/history/trend", params={
        "by": "day", "from_date": "2026-09-20 00:00",
        "to_date": "2026-09-26 00:00"}).json()["rows"]
    assert rows == []


# ---------------------------------------------------------------------------
# 场次明细与维修
# ---------------------------------------------------------------------------

def test_场次明细(client):
    rows = client.get("/api/history/runs", params={
        "from_date": "2026-09-20 00:00", "to_date": "2026-09-21 00:00"}).json()["rows"]
    assert len(rows) == 1
    r = rows[0]
    assert r["serial_number"] == "EA001"
    assert r["passed"] is False
    assert r["started"].startswith("2026-09-20")
    assert r["duration_min"] == pytest.approx(60.0)


def test_场次明细有上限防止返回过大(client):
    j = client.get("/api/history/runs", params={"limit": 2}).json()
    assert len(j["rows"]) == 2
    assert j["total"] == 5
    assert j["truncated"] is True


def test_场次明细不查装配结构(make_live_client):
    """★ 这是"查询一直转圈"的根因修复，别回退。

    装配结构报表（SerialNumberStructure）极慢（实测单台约 4.5 秒、
    60 台 270 秒）。runs 列表要标供应商**不能**走它，只能走
    UnitProductStructure（一次请求 15 秒拿全部整机，和列整机共享响应）。

    判据：runs 请求里不出现任何 structure / structures 调用。
    """
    c = make_live_client(HistClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))
    import api
    api._struct_cache_clear()
    api._source.client.calls.clear()
    c.get("/api/history/runs", params={"limit": 500})
    struct_calls = [x for x in api._source.client.calls
                    if x[0] in ("structure", "structures")]
    assert not struct_calls, \
        f"runs 查了装配结构（{struct_calls[:2]}），装配结构报表极慢会把查询卡死"


# ---------------------------------------------------------------------------
# DUT 明细：每天的良率下面列出具体机器（用户要的新显示内容）
# ---------------------------------------------------------------------------

def test_场次明细带产品号和供应商(make_live_client):
    """每行要有产品号、SN、天线供应商、滤波器供应商、测试结果。

    供应商走 UnitProductStructure（和列整机共享一次请求），不是逐台查
    装配结构 —— 后者 60 台要 270 秒，会把查询卡死。
    """
    c = make_live_client(HistClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))
    rows = c.get("/api/history/runs", params={"limit": 500}).json()["rows"]
    assert rows
    r = rows[0]
    assert r["product"]                      # 产品号
    assert r["serial_number"]                # SN
    assert r["ant_suppliers"] == ["Comba"]       # 天线供应商
    assert r["fu_suppliers"] == ["Grentech"]     # 滤波器供应商
    assert isinstance(r["passed"], bool)     # 测试结果


def test_sn模式查供应商时按产品日期调unit_structure(make_live_client):
    """★ sn 模式(只给序列号)下,product/from/to 都是空。直接调
    suppliers_for(None, None, None) 会让 MI 的 UnitProductStructure
    产品必填组爆炸(>20 分钟不返回),被 try/except 吞成空供应商,
    用户看到两列全空 —— cond 模式有、sn 模式没,就是这个坑。

    修法:从 runs 的 product + started 反推产品+日期范围,按产品分组
    分别调 suppliers_for。供应商不随时间变,这个"反推日期"只是绕过
    MI 接口的"必填组"限制。
    """
    c = make_live_client(HistClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))
    import api
    # 清掉 fixture 预填的 (None,None,None),让后端必须真调 unit_structure
    # 否则 cache 命中会掩盖 bug —— fixture 预填掩盖生产行为是档案记过的
    # "验证手段自己要可信"的实例。
    api._source._ups_cache.clear()
    api._source.client.calls.clear()

    rows = c.get("/api/history/runs", params={"sn": "EA001"}).json()["rows"]
    assert rows, "sn 查应该有数据"

    # 行为层:供应商要有值
    for r in rows:
        assert r["ant_suppliers"], \
            f"sn 模式天线供应商为空: {r['serial_number']}"
        assert r["fu_suppliers"], \
            f"sn 模式滤波器供应商为空: {r['serial_number']}"

    # 实现层:绝不能用 product=None 调 unit_structure(MI 必填组爆炸)
    us_calls = [kw for name, kw in api._source.client.calls
                if name == "unit_structure"]
    assert us_calls, "sn 模式应该调 unit_structure 拿供应商"
    for kw in us_calls:
        assert kw.get("product") is not None, \
            f"sn 模式 unit_structure 调用必须带 product(从 runs 反推), 实际 {kw}"
        assert kw.get("from_date") is not None, \
            f"sn 模式 unit_structure 调用必须带 from_date, 实际 {kw}"


def test_失败的场次才带失败分支(make_live_client):
    """用户明确要求：fail 才显示对应的 branch，pass 不用加。

    这不只是显示问题 —— 查失败分支要逐台请求（单台约 30 秒），
    给通过的机器查是白等几十秒。
    """
    c = make_live_client(HistClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))
    rows = c.get("/api/history/runs",
                 params={"limit": 500, "with_branches": "true"}).json()["rows"]
    failed = [r for r in rows if not r["passed"]]
    passed = [r for r in rows if r["passed"]]
    assert failed and passed, "测试数据里要同时有失败和通过的场次"
    for r in failed:
        assert r["failed_branches"] == [3, 10]
    for r in passed:
        assert r["failed_branches"] == [], "通过的场次不该带失败分支"


def test_失败分支只对失败整机发请求(make_live_client):
    """通过的整机不该去查（每台约 30 秒，白等）。"""
    c = make_live_client(HistClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))
    import api
    api._source.client.calls.clear()
    c.get("/api/history/runs", params={"limit": 500, "with_branches": "true"})
    calls = [x for x in api._source.client.calls
             if x[0] == "child_test_histories"]
    assert calls, "没查失败分支"
    asked = set(calls[-1][1])
    # HIST 里只有 EA001(第2次失败) 和 EA002(类别26那场失败) 有失败场次
    assert asked <= {"EA001", "EA002"}, f"查了不该查的整机: {asked}"


def test_不要失败分支时不发额外请求(make_live_client):
    """默认不查失败分支（省几十秒），要才查。"""
    c = make_live_client(HistClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))
    import api
    api._source.client.calls.clear()
    c.get("/api/history/runs", params={"limit": 500})
    assert not [x for x in api._source.client.calls
                if x[0] == "child_test_histories"]


def test_维修记录(client):
    j = client.get("/api/history/repairs").json()
    assert j["total"] == 1
    assert j["rows"][0]["action"] == "Retest"
    assert j["rows"][0]["fault"] == "TEST Instability"


# ---------------------------------------------------------------------------
# 只读约束不能被破坏
# ---------------------------------------------------------------------------

def test_新接口全是GET(client):
    import api
    for route in api.app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/history"):
            methods = (getattr(route, "methods", set()) or set()) - {"HEAD", "OPTIONS"}
            assert methods <= {"GET"}, f"{path} 有非 GET 方法"


# ---------------------------------------------------------------------------
# 供应商筛选
#
# 供应商名字的权威来源是 MI 的 UnitProductStructure 报表的 Mfg. Unit 列，
# 实测确认「子件序列号前 3 位 == Mfg.Unit 代码」，所以直接按序列号前缀查表，
# 省一次网络往返。选了供应商就先反查出装了它的整机，再筛测试场次。
# ---------------------------------------------------------------------------

def test_列出可选供应商(client):
    """★ 语义变了：现在列出**全部**可筛选的供应商，不只是当前数据里出现过的。

    原来这个接口从实际装配记录统计，有两个问题：
      1. 要拉全部整机的装配结构，实测 80 台 8 路并行 9.5 分钟。
         这是页面一打开就调的接口，等不了。
      2. 只列出数据里出现过的供应商，会漏掉机器恰好不在当前范围内的那些 ——
         用户于是没法筛它。这里假数据只有 Comba 的天线，但 AMPHENOL
         也该出现在下拉框里。

    所以默认改成查本地的序列号前缀对照表，瞬间返回、选项齐全，
    代价是不带台数统计（n_parents 为 null，不是 0）。
    """
    j = client.get("/api/suppliers").json()
    fu = {o["supplier"]: o for o in j["filter_unit"]}
    ant = {o["supplier"]: o for o in j["antenna"]}
    assert set(fu) == {"Grentech", "Chunxing"}
    assert set(ant) == {"Comba", "AMPHENOL"}, "选项要齐全，不能只给数据里有的"
    assert j["counted"] is False
    assert fu["Grentech"]["n_parents"] is None, "没统计要是 null，不能是 0"
    assert j["note"], "必须说明供应商是从序列号前缀推出来的"


def test_明确要台数时才从装配记录统计(client):
    """这个开关会拉全部装配结构（实测 9.5 分钟），所以必须是主动要求。"""
    j = client.get("/api/suppliers", params={"with_counts": "true"}).json()
    fu = {o["supplier"]: o for o in j["filter_unit"]}
    assert j["counted"] is True
    assert fu["Grentech"]["n_parents"] == 1


def test_按滤波器供应商筛良率(client):
    """EA001 装 Grentech 的滤波器，EA002 装 Chunxing 的。"""
    a = client.get("/api/history/yield",
                   params={"fu_supplier": "Grentech"}).json()
    b = client.get("/api/history/yield",
                   params={"fu_supplier": "Chunxing"}).json()
    assert a["n_serials"] == 1
    assert b["n_serials"] == 1
    assert a["n_runs"] == 1          # EA001 只有 1 场首次测试
    assert b["n_runs"] == 2          # EA002 有 2 场（类别 212 和 26 各一次）


def test_按天线供应商筛良率(client):
    j = client.get("/api/history/yield",
                   params={"ant_supplier": "Comba"}).json()
    assert j["n_serials"] == 1       # 只有 EA001 装了天线


def test_供应商和其他条件叠加(client):
    """供应商筛选要和日期、类别一起生效，不能互相覆盖。"""
    j = client.get("/api/history/yield", params={
        "fu_supplier": "Chunxing", "category": "26"}).json()
    assert j["n_runs"] == 1          # EA002 的类别 26 那一场


def test_供应商筛选不查装配结构(make_live_client):
    """★ 这是"选了供应商查询就一直转圈"的根因修复，别回退。

    原来选供应商会调 _all_structures()，它是两步慢操作叠加：
      1. serials_for(product=None, 最近183天)  实测 **20 分钟不返回**
      2. s.structures(那一批整机)              档案实测 80 台 567 秒
    整个查询必然卡死。

    改成从 UnitProductStructure 拿供应商（按查询条件、和"实时列整机"
    共享同一个响应），实测拿全部 60 台供应商 **0.006 秒**。

    判据：选供应商的请求里不出现任何 structure / structures 调用。
    """
    c = make_live_client(HistClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))
    import api
    api._struct_cache_clear()
    api._source.client.calls.clear()
    c.get("/api/history/yield", params={"fu_supplier": "Grentech"})
    slow = [x for x in api._source.client.calls
            if x[0] in ("structure", "structures")]
    assert not slow, \
        f"供应商筛选查了装配结构（{slow[:2]}），那条路实测 20 分钟不返回"


def test_供应商筛选按查询范围取数(make_live_client):
    """供应商要按用户查的 (产品, 日期) 取，不是固定"最近半年、不限产品"。

    范围越窄越快，而且和"实时列整机"用同一个响应 —— 两者口径一致，
    不会出现"列出来的整机"和"供应商认的整机"对不上。
    """
    c = make_live_client(HistClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))
    import api
    api._source.client.calls.clear()
    c.get("/api/history/yield", params={
        "fu_supplier": "Grentech", "product": "KRD 901 330/11",
        "from_date": "2026-09-01 00:00", "to_date": "2026-09-30 23:59"})
    ups = [kw for name, kw in api._source.client.calls if name == "unit_structure"]
    assert ups, "没走 UnitProductStructure"
    assert any(kw.get("product") == "KRD 901 330/11" for kw in ups), \
        f"没把产品号传下去，取数范围会白白放大: {ups}"


def test_没装过的供应商返回零条不报错(client):
    j = client.get("/api/history/yield", params={"fu_supplier": "XXXXX"}).json()
    assert j["n_runs"] == 0
    assert j["yield_pct"] is None


def test_供应商筛选对趋势和场次明细同样生效(client):
    t = client.get("/api/history/trend", params={
        "by": "day", "fu_supplier": "Grentech"}).json()
    assert [x["bucket"] for x in t["rows"]] == ["2026-09-14"]
    r = client.get("/api/history/runs",
                   params={"fu_supplier": "Grentech"}).json()
    assert {x["serial_number"] for x in r["rows"]} == {"EA001"}



def test_只查失败分支的轻接口(make_live_client):
    """前端要"测试结果先出来、失败分支稍后补上"，所以需要一个只查分支的接口。

    不能让前端为了拿分支把整个 runs 再拉一遍（那会重复拉全部测试历史，
    实测几十秒）。这个接口只做一件事：给定整机序列号，返回各自的失败分支。
    """
    c = make_live_client(HistClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))
    r = c.get("/api/history/branches", params={"sns": "EA001,EA002"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["branches"] == {"EA001": [3, 10], "EA002": [3, 10]}


def test_只查失败分支不拉测试历史(make_live_client):
    """这个接口的全部价值就是"轻"：它不该去拉全部测试历史。

    拉历史是几十秒的操作，前端已经有场次数据了，再拉一次纯属浪费。
    """
    c = make_live_client(HistClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))
    import api
    api._source.client.calls.clear()
    c.get("/api/history/branches", params={"sns": "EA001"})
    kinds = [x[0] for x in api._source.client.calls]
    assert "child_test_histories" in kinds, "没查失败分支"
    assert "test_histories" not in kinds, (
        f"不该拉测试历史，实际调用了：{kinds}")


def test_只查失败分支空序列号不报错(make_live_client):
    """没有失败的机器时前端不该发请求，但万一发了也要干净返回。"""
    c = make_live_client(HistClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))
    r = c.get("/api/history/branches", params={"sns": ""})
    assert r.status_code == 200, r.text
    assert r.json()["branches"] == {}



def test_三个接口并发查时慢操作只做一遍(make_live_client):
    """前端点一次「查询」会**并发**打 yield + trend + runs 三个接口。

    三者走同一个取数路径（实时列整机 serials_for -> 拉测试历史 all_test_runs），
    这两步各约 55 秒。它们之间靠内存缓存复用，但缓存是朴素的
    "检查-然后-填充"，没有「进行中去重」—— 三个请求同时进来时都发现缓存
    是空的，于是**各自把慢操作做一遍**（缓存踩踏 cache stampede）。

    后果就是用户看到的现象：一次查询要等好几倍的时间，而且先发的
    yield/trend 先抢到、先返回，最后发的 runs 要等自己那一遍跑完，
    表现为"良率出来了，DUT 明细还没有"。

    这个测试用假客户端数底层调用次数，不碰网络。

    ★ 假客户端必须**人为放慢**：它本来是微秒级的，第一个请求在第二个开始前
    就填好缓存了，踩踏窗口根本不存在（测试会假装通过）。真实世界慢操作要
    几十秒，窗口巨大，三个请求必然重叠。所以这里给它加个延迟来还原竞态。
    """
    import threading
    import time as _time
    c = make_live_client(HistClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))
    import api
    fake = api._source.client
    fake.calls.clear()

    # 给两个慢操作套一层延迟，模拟真实的几十秒取数窗口
    _orig_hist = fake.test_histories
    _orig_ups = fake.unit_structure

    def slow_hist(sns, **kw):
        _time.sleep(0.4)
        return _orig_hist(sns, **kw)

    def slow_ups(**kw):
        _time.sleep(0.4)
        return _orig_ups(**kw)

    fake.test_histories = slow_hist
    fake.unit_structure = slow_ups
    try:
        params = {"product": "KRD 901 330/11", "from_date": "2026-09-01 00:00",
                  "to_date": "2026-09-30 23:59"}
        paths = ["/api/history/yield", "/api/history/trend", "/api/history/runs"]
        errors = []

        def hit(path):
            try:
                r = c.get(path, params=params)
                assert r.status_code == 200, (path, r.text)
            except Exception as e:      # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=hit, args=(p,)) for p in paths]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        assert not errors, errors
    finally:
        fake.test_histories = _orig_hist
        fake.unit_structure = _orig_ups

    kinds = [x[0] for x in fake.calls]
    n_hist = kinds.count("test_histories")
    n_ups = kinds.count("unit_structure")
    # 三个接口要的是同一批数据，慢操作各自只该做一遍
    assert n_hist == 1, f"测试历史被拉了 {n_hist} 遍（并发踩踏），应为 1 遍"
    assert n_ups == 1, f"UnitProductStructure 被拉了 {n_ups} 遍（并发踩踏），应为 1 遍"


def test_失败分支查询出错时返回502带具体错误(make_live_client):
    """子件历史接口抛异常时，不能静默返回空 dict（前端分不清是"查了没有"
    还是"查询失败"），要**把错误透传给前端**。

    用户今天反复看到"查询失败" 4 个字但不知道具体原因 —— 根本原因就是
    之前 data_source.failed_detail_for 的 except Exception: return {}
    把真实错误吞掉了。
    """
    class BoomClient(HistClient):
        def child_test_histories(self, sns, **kw):
            raise RuntimeError("模拟 MI 鉴权失败")

    c = make_live_client(BoomClient(hist=HIST, repairs=REPAIRS, structs=STRUCTS))
    r = c.get("/api/history/branches", params={"sns": "EA001"})
    assert r.status_code == 502, f"应该 502，实际 {r.status_code}: {r.text}"
    d = r.json()
    # 错误消息里要能看到"MI 鉴权失败"（具体原因），而不是只有"查询失败"
    assert "鉴权失败" in str(d.get("detail", "")), \
        f"502 响应应该带具体错误，实际: {d}"
