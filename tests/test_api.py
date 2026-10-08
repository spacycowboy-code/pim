"""后端接口测试。用 FastAPI 的 TestClient，不用真的起服务。

取数机制说明（这个文件 2026-10 改过一次，原因记在这里）
--------------------------------------------------------
原来这些测试跑在一份**假快照**上：先用 `data_source.QueryKey` 在临时目录里
造几个 `*.json.gz` 文件，再把 `PIM_SNAPSHOT_DIR` 指过去。

快照那条取数路径已经整体移除（它是默认路径，启动时忘了设开关就会悄悄读
过期数据），`QueryKey` 这些东西也随之删掉，所以旧的 fixture 全部失效。

现在的做法：系统照常走**实时查 MI** 这条唯一的路，只把最底层的网络客户端
换成假的（`tests/conftest.py` 里的 `FakeClient`）。好处是上面整套数据源、
计算、接口代码都是真实执行的，只有"发 HTTP 给 MI"这一步是假的。

`live_client` / `live_api` / `make_live_client` 这三个装置来自 conftest.py，
pytest 会自动加载，不需要 import。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from conftest import FakeClient, tp_row


# ---------------------------------------------------------------------------
# 本文件专用的假客户端
#
# conftest 里的 FakeClient 给的是"一切正常"的默认数据。下面三个子类各造一种
# 特殊情形，用来测原来靠"快照里刻意不放某份数据"才能测到的行为。
# ---------------------------------------------------------------------------

class 查不到数据的假客户端(FakeClient):
    """模拟"MI 里确实没有这个条件的数据"。

    为什么用抛 KeyError 来模拟：接口层（api.py 的 `_fetch_pair` /
    `get_testpoints`）就是靠捕获数据源抛出的 KeyError 来返回 404 的，
    这是被测的契约。

    注意别和"MI 返回了空结果"搞混：那种情况按设计返回 200 + 零值，
    见下面 `test_yield_MI没有数据时返回200零值而不是404`。
    """

    def test_points(self, **kw):
        self.calls.append(("test_points", kw))
        raise KeyError("MI 里没有符合这些条件的测试数据")


class 单台明细的假客户端(FakeClient):
    """只有一台机器的明细，且 %Fail 列有真实值。

    单台查询的 %Fail 是可信的（批量查询时这一列恒为 0），所以
    /api/testpoints 会直接用它算失败分支。
    """

    ROWS = [
        tp_row("5.2.100.5.0", tested=6, pct_fail=16.67, avg=-104.0,
               name="noise_power_level_before_testing 1896 MHz"),
        tp_row("5.2.109.5.0", tested=6, pct_fail=16.67, avg=0.83, unit="Dbm",
               name="knock_response 1896 MHz"),
    ]

    def __init__(self):
        super().__init__(tp_all=self.ROWS, tp_failed=[],
                         serials=["EA8E538914"])


class 有拆装记录的假客户端(FakeClient):
    """EA8E538914 装过一次天线又拆下来；另一台没有装配记录。

    用来给 /api/parts/DS4W0270J6 的测试提供"只有 EA8E538914 装过"的场景。

    ★ 快路径（UnitProductStructure.Data1）没有装入/拆除时间字段，所以这里
    覆盖 unit_structure 只返回 EA8E538914 装过 DS4W0270J6，让快路径的
    `_part_index` 构建出的 spans 和旧实现一致（1 条记录）。
    """

    def structure(self, sn, **kw):
        self.calls.append(("structure", sn))
        if sn != "EA8E538914":
            return []
        return [
            ["CB4", "KRD 901 330/11 R1A", "EA8E538914", "2026-09-14 10:43:32",
             "KRE 105 518 R1C : DS4W0270J6", ""],
            ["CB4", "KRD 901 330/11 R1A", "EA8E538914", "2026-09-14 12:03:12",
             "", "KRE 105 518 R1C : DS4W0270J6"],
        ]

    def unit_structure(self, **kw):
        """覆盖基类：只让 EA8E538914 装过 DS4W0270J6，和 structure 行为一致。"""
        self.calls.append(("unit_structure", kw))
        cols = ["Factory", "Product Number", "Product RState",
                "Part Product Number", "Part Product R-State", "Mfg.Unit",
                "First Tests", "Passed Tests", "Test Result",
                "Test Passed W/O Repair", "ParentSN => ChildSN"]
        rows = [
            ["ESS", "KRD 901 330/11", "R1A", "KRE 105 518", "R1C", "DS4",
             "1", "1", "Y", "Y", "EA8E538914 => DS4W0270J6"],
        ]
        return {"data": {"m_Item1": {"resultSet": [
            {"resultSetName": "Data1",
             "columns": [{"name": c} for c in cols],
             "data": rows}]}, "m_Item2": None}}


# 查询条件：一组反复用到的参数，和 conftest 默认数据对得上
Q = {"product": "KRD 901 330/11", "from_date": "2026-08-01 00:00",
     "to_date": "2026-10-01 00:00",
     "serial_numbers": "EA8E538914,EA8E538919"}


# ---------- 健康检查与元信息 ----------

def test_health(live_client):
    r = live_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_标明实时模式(live_client):
    """接口要能证明"数据是实时从 MI 取的"。

    原来这条测的是 `offline is True`（服务器上跑的是离线读快照的那份）。
    离线模式已经不存在了，所以改成断言实时标志——启动脚本和前端都读这两个
    字段来确认数据不是过期副本。
    """
    d = live_client.get("/api/health").json()
    assert d["live"] is True
    assert d["source_mode"] == "mi"


def test_manifest(live_client):
    """数据来源摘要。

    原来这里断言 `n_queries == 5`（快照里预导出了 5 个查询组合）。实时查询
    没有"预导出组合"这个概念，接口固定返回 0，所以改成断言它为 0，并把重点
    移到"能查到哪些机器、哪些产品"上——那才是现在这个接口真正报告的东西。
    """
    r = live_client.get("/api/manifest")
    assert r.status_code == 200
    d = r.json()
    assert d["live"] is True
    assert d["n_queries"] == 0, "实时查询没有预导出的查询组合"
    assert d["n_serials"] == 2
    assert "KRD 901 330/11" in d["products"]
    assert d["n_structures"] == 2
    assert "exported_at" in d


def test_options_给界面填下拉框(live_client):
    """下拉框的候选值。

    原来断言 `date_from == "2026-08-01 00:00"`（快照只覆盖那一段时间，
    界面必须把用户限制在这个区间里）。实时查询任意日期都能查，所以数据源
    返回 None 表示"不设上下界"，界面据此放开日期输入。
    """
    r = live_client.get("/api/options")
    assert r.status_code == 200
    d = r.json()
    assert "KRD 901 330/11" in d["products"]
    assert "EA8E538914" in d["serials"]
    assert d["date_from"] is None, "实时查询不该给日期设上下界"
    assert d["date_to"] is None
    assert "[All]" in d["test_counts"]
    assert "First" in d["test_counts"]
    assert "Last" in d["test_counts"]


def test_options_包含Crete平台产品(live_client):
    """Crete 平台产品号默认就在候选里（即使 MI 当天没探测到它们在产，
    下拉也要能选到）。踩过的坑：讨论新产品时它可能"没在产"，
    MI 不返回 → 用户在界面上根本看不到可选的产品号。
    """
    r = live_client.get("/api/options")
    assert r.status_code == 200
    products = r.json()["products"]
    for p in ("KRD 901 328/11", "KRD 901 329/11", "KRD 901 316/11"):
        assert p in products, f"Crete 产品 {p} 应该默认在候选里，实际 {products}"


def test_options_默认产品是主产品而非字母排序第一个(live_client):
    """加 Crete 后字母排序 316/11 排到第一位，但用户熟悉的默认是 330/11。
    options 要显式返回 default_product 字段，前端据此选中默认值，
    不能靠 products[0] —— 那个随加/删产品会变。
    """
    r = live_client.get("/api/options")
    assert r.status_code == 200
    d = r.json()
    assert d["default_product"] == "KRD 901 330/11", (
        f"默认产品应该是主产品 KRD 901 330/11，实际 {d['default_product']}")


# ---------- 良率 ----------

def test_yield_基本查询(live_client):
    r = live_client.get("/api/yield", params=Q)
    assert r.status_code == 200
    d = r.json()
    # 4 个测试点各测 100 次 = 400 次，失败 8 次 -> 失败率 2%
    assert d["summary"]["total_tested"] == 400
    assert d["summary"]["total_failed"] == 8
    assert d["summary"]["fail_rate_pct"] == pytest.approx(2.0)


def test_yield_不返回良率字段(live_client):
    """这个接口给的是测试点次数级失败率，不是良率。

    良率只有场次级 FPY 一种口径（/api/history/yield）。这里一旦出现
    yield_pct，界面就会把 99.x% 这种虚高数字当良率显示。
    """
    r = live_client.get("/api/yield", params={**Q, "group_by": "branch"})
    d = r.json()
    assert "yield_pct" not in d["summary"]
    assert "point_yield_pct" not in d["summary"]
    assert d["groups"], "应该有分组数据"
    for g in d["groups"]:
        assert "yield_pct" not in g
    assert d["level"] == "testpoint"


def test_yield_分组按分支(live_client):
    r = live_client.get("/api/yield", params={**Q, "group_by": "branch"})
    d = r.json()
    got = {g["key"]: g for g in d["groups"]}
    assert got["9"]["total_failed"] == 8
    assert got["5"]["total_failed"] == 0
    # 最差的排最前面
    assert d["groups"][0]["key"] == "9"


def test_yield_分组给出两类平均值(live_client):
    """功率和通过率不能混在一起平均，必须分开给。"""
    r = live_client.get("/api/yield", params={**Q, "group_by": "branch"})
    g = {x["key"]: x for x in r.json()["groups"]}["5"]
    assert g["avg_power"] == pytest.approx(-104.0)
    assert g["avg_passrate"] == pytest.approx(1.0)
    assert g["avg_value"] is None          # 混杂时必须是 None


def test_yield_FPY用First(live_client):
    r = live_client.get("/api/yield", params={**Q, "test_count": "First"})
    assert r.status_code == 200
    assert r.json()["test_count"] == "First"
    assert r.json()["is_fpy"] is True


def test_yield_查不到数据时返回404(make_live_client):
    """数据源报告查不到时，接口要返回 404，不能把异常漏成 500。

    原来这条叫"快照里没有的查询返回 404"，测的是"这个条件没被预导出"。
    快照没了，这件事的含义变成"MI 里确实没有这批数据"——这才是现在
    唯一可能查不到的原因。
    """
    c = make_live_client(查不到数据的假客户端())
    r = c.get("/api/yield", params={
        "product": "不存在的产品", "from_date": "2020-01-01 00:00",
        "to_date": "2020-02-01 00:00"})
    assert r.status_code == 404
    assert "没有" in r.json()["detail"]


def test_yield_404提示要给出可检查的条件(make_live_client):
    """只说"没有"没用，要告诉用户下一步能检查什么。

    原来快照模式下给的是"快照里实际存了哪些条件"（断言提示里有"可用"或
    "实际"）。实时模式下查不到的原因不是"没导出"，而是条件写错或 MI 里
    真的没数据，所以文案改成逐项列出可检查的条件。
    """
    c = make_live_client(查不到数据的假客户端())
    r = c.get("/api/yield", params={**Q, "test_category": "999"})
    assert r.status_code == 404
    detail = r.json()["detail"]
    # 四个常见写错的条件都要点到，否则用户无从下手
    for 条件 in ("产品号", "日期", "测试类别", "序列号"):
        assert 条件 in detail, f"提示里没提到「{条件}」: {detail}"
    # 快照已经没了，提示里不许再出现这套说法
    for 旧词 in ("快照", "导出", "export_snapshot"):
        assert 旧词 not in detail, f"提示里还在说「{旧词}」"


def test_yield_MI没有数据时返回200零值而不是404(make_live_client):
    """★ 这条是新增的，把"空结果"和"查不到"两种情况区分清楚。

    MI 查一个没有数据的条件时会正常返回空结果（见 test_empty_result.py），
    底层客户端把它变成空列表。空结果是正常业务情况，接口返回 200 + 零值，
    **不是 404**。写这条是为了防止以后有人看到上面两个 404 用例，误以为
    "没数据就该 404"而去改接口。
    """
    c = make_live_client(FakeClient(tp_all=[], tp_failed=[]))
    r = c.get("/api/yield", params=Q)
    assert r.status_code == 200
    s = r.json()["summary"]
    assert s["total_tested"] == 0
    assert s["total_failed"] == 0


def test_yield_不合法的分组维度返回400(live_client):
    r = live_client.get("/api/yield", params={**Q, "group_by": "乱写的维度"})
    assert r.status_code == 400


# ---------- 失败明细 ----------

def test_failures(live_client):
    r = live_client.get("/api/failures", params=Q)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["branch"] == 9
    assert items[0]["n_failed"] == 8
    assert items[0]["freq_mhz"] == pytest.approx(1896.0)


# ---------- 单台明细 ----------

def test_testpoints_单台(make_live_client):
    c = make_live_client(单台明细的假客户端())
    r = c.get("/api/testpoints", params={
        "product": "KRD 901 330/11", "from_date": "2026-08-01 00:00",
        "to_date": "2026-10-01 00:00", "sn": "EA8E538914"})
    assert r.status_code == 200
    d = r.json()
    assert d["sn"] == "EA8E538914"
    assert len(d["items"]) == 2
    # 单台的 %Fail 是可信的，可以直接用
    assert d["failed_branches"] == {"5": pytest.approx(16.67)}


def test_testpoints_查不到这台返回404(make_live_client):
    """原来叫"没有这台返回 404"，指的是"快照里没导出这台的明细"。
    实时模式下含义变成"MI 里查不到这台的数据"，接口行为不变：404。
    """
    c = make_live_client(查不到数据的假客户端())
    r = c.get("/api/testpoints", params={
        "product": "KRD 901 330/11", "from_date": "2026-08-01 00:00",
        "to_date": "2026-10-01 00:00", "sn": "不存在"})
    assert r.status_code == 404


# ---------- 下层件 ----------

def test_parts_列出下层件(live_client):
    r = live_client.get("/api/parts")
    assert r.status_code == 200
    d = r.json()
    assert "DS4W0270J6" in [p["child_sn"] for p in d["antenna"]]


def test_parts_按条件联动_命中缓存(make_live_client):
    """/api/parts 传 (product, from_date, to_date) 时，应该只用对应 key 的
    UPS 响应构建索引。这是"下层件和查询条件联动"的契约。"""
    api = make_live_client(有拆装记录的假客户端())
    # 预填一个具体条件的缓存项（模拟用户查过这个产品+时间后 UPS 响应已缓存）
    import api as api_mod
    key = ("KRD 901 330/11", "2026-09-01 00:00", "2026-10-01 00:00")
    api_mod._source._ups_cache[key] = (
        api_mod._source.client.unit_structure())
    # 传完整条件 -> 应该从该 key 的缓存构建索引,拿到 DS4W0270J6
    r = api.get("/api/parts", params={
        "product": "KRD 901 330/11",
        "from_date": "2026-09-01 00:00",
        "to_date": "2026-10-01 00:00",
    })
    assert r.status_code == 200
    d = r.json()
    assert "DS4W0270J6" in [p["child_sn"] for p in d["antenna"]]


def test_parts_按条件联动_不命中返回空(make_live_client):
    """传了条件但 _ups_cache 里没对应 key 时，应该返回空（不触发 MI 请求）。"""
    api = make_live_client(有拆装记录的假客户端())
    r = api.get("/api/parts", params={
        "product": "KRD 901 999/99",   # 这个产品从没查过
        "from_date": "2099-01-01 00:00",
        "to_date": "2099-12-31 23:59",
    })
    assert r.status_code == 200
    d = r.json()
    assert d["antenna"] == []
    assert d["filter_unit"] == []


def test_parts_返回by_parent反向索引(live_client):
    """/api/parts 响应里带 by_parent 字段：{parent_sn: {antenna: [...], filter_unit: [...]}}。
    让前端能支持"按整机反查装过的子件"的流程。"""
    r = live_client.get("/api/parts")
    assert r.status_code == 200
    d = r.json()
    assert "by_parent" in d
    # fixture 里 EA8E538914 装过 DS4W0270J6
    assert "EA8E538914" in d["by_parent"]
    bp = d["by_parent"]["EA8E538914"]
    assert "antenna" in bp
    assert "filter_unit" in bp
    children_sn = [x["child_sn"] for x in bp["antenna"]]
    assert "DS4W0270J6" in children_sn
    # 每一项要带产品号(前端下拉显示用)
    assert all("child_product" in x for x in bp["antenna"])


def test_part_track_追踪一个下层件(make_live_client):
    c = make_live_client(有拆装记录的假客户端())
    r = c.get("/api/parts/DS4W0270J6")
    assert r.status_code == 200
    d = r.json()
    assert d["child_sn"] == "DS4W0270J6"
    assert len(d["spans"]) == 1
    s = d["spans"][0]
    assert s["parent_sn"] == "EA8E538914"
    # ★ 快路径（UnitProductStructure.Data1）没有时间字段，前端 `|| '?'` 兜底
    assert s["installed_at"] is None
    assert s["removed_at"] is None


def test_part_track_失败分支应从MI查到不是空字典(make_live_client, monkeypatch):
    """追踪下层件时，每台整机的 failed_branches 要从 MI 实时查到，
    不能恒为空 dict。踩过的坑：原来遍历 s.entries() 查失败，但 entries() 在
    实时模式下永远返回 []，循环根本不执行，failed_branches 全空 ——
    前端全是"该整机无 PIM 失败记录"。
    """
    c = make_live_client(有拆装记录的假客户端())
    import api
    # mock s.failed_branches_for，让它给 EA8E538914 返回 [BR0, BR9]
    monkeypatch.setattr(api._source, "failed_branches_for",
                        lambda sns: {sn: [0, 9] for sn in sns})
    r = c.get("/api/parts/DS4W0270J6")
    assert r.status_code == 200
    d = r.json()
    assert len(d["spans"]) == 1
    fb = d["spans"][0]["failed_branches"]
    # 期望 failed_branches 不是空 dict，而是从 MI 查到的分支
    assert fb, f"failed_branches 应该有数据，实际空: {fb}"
    assert set(fb.keys()) == {"0", "9"}, f"分支号应该是 0, 9，实际 {list(fb.keys())}"


def test_part_track_未知件返回404(live_client):
    assert live_client.get("/api/parts/不存在的件").status_code == 404


# ---------- 前端静态文件 ----------

def test_首页返回html(live_client):
    r = live_client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ---------- 安全 ----------

def test_只有登录能用POST其余全是只读(live_api):
    """这个服务只读数据。唯一允许的写操作是提交登录表单。

    断言收紧到"逐个路径检查"而不是"整体没有 POST"，
    这样以后不小心加了个写接口会被抓住。
    """
    # /api/rtd/refresh 是唯一的例外：它会启动浏览器进程刷新 RTD 凭据。
    # 放它进来的同时，下面 test_刷新接口必须受多重保护 会盯着它的限制不被拆掉。
    allowed_post = {"/login", "/api/rtd/refresh"}
    bad = []
    for route in live_api.app.routes:
        methods = (getattr(route, "methods", set()) or set()) - {"GET", "HEAD", "OPTIONS"}
        if not methods:
            continue
        path = getattr(route, "path", "?")
        if path not in allowed_post or methods != {"POST"}:
            bad.append(f"{path} {sorted(methods)}")
    assert not bad, f"出现了不该有的写接口: {bad}"


def test_数据接口全是只读(live_api):
    """所有 /api/ 路径只能是 GET，唯一例外是凭据刷新。"""
    for route in live_api.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/"):
            continue
        if path == "/api/rtd/refresh":
            continue          # 唯一的写接口，限制由专门的测试守着
        methods = (getattr(route, "methods", set()) or set()) - {"HEAD", "OPTIONS"}
        assert methods <= {"GET"}, f"{path} 有非 GET 方法: {methods}"


def test_刷新接口必须受多重保护(live_client, live_api):
    """唯一的写接口会启动进程，这几道限制一个都不能少。

    这个测试的意义是防止以后有人"为了方便"把限制拆掉。

    原来这里数的是"三重保护"，其中一道是"离线模式禁止刷新"。离线模式
    已经移除，那道检查恒不成立所以删了（test_live_only.py 正是断言
    api 里不该再有 LIVE_MODE）。补上的是"挡代理转发头"那道——它比原来
    那道更要紧：反向代理场景下伪造 X-Forwarded-For 就能绕过本机检查。
    """
    # 1. 非本机请求必须被拒。TestClient 的来源是字符串 "testclient"，
    #    不在回环地址白名单里，所以这里天然就是"非本机"。
    r = live_client.post("/api/rtd/refresh")
    assert r.status_code == 403
    assert "本机" in r.json()["detail"]

    # 2. 三道限制的代码都还在
    import inspect
    src = inspect.getsource(live_api._guard_refresh)
    assert "RTD_REFRESH_ENABLED" in src, "总开关被拆了"
    assert "x-forwarded-for" in src, "代理头检查被拆了"
    assert "127.0.0.1" in src, "本机限制被拆了"

    # 3. 并发锁还在（两个浏览器抢同一个 profile 目录会互相破坏）
    assert isinstance(live_api._rtd_refresh_lock,
                      type(live_api.threading.Lock()))
