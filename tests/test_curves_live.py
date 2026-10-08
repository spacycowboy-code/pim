"""分支曲线改成实时查 MI（而不是读本地 CSV）。

背景
----
这一块原来读 `data/out/pim_measures.csv` —— 一个由 analyze.py 事先跑出来的
20 MB 文件。实测那份文件是 9 月 29 日生成的，之后再没更新过，所以图上画的
一直是那天的数据。

用户要求数据必须直接从 MI 拉，所以改成实时查。

★ 为什么要限制台数（这是硬约束，不是偷懒）
------------------------------------------
MI 查**单台**明细实测要 28~70 秒，而且没有"批量拿明细"的接口 ——
批量查回来的是聚合统计，分不出哪行属于哪台，画不了"一条线一台"。
所以 N 台就得发 N 个请求。8 路并行下：

    8 台   约 1 分钟
    16 台  约 2 分钟
    79 台  30~90 分钟   <- 原来 CSV 里是 79 台，实时查这个量级不可用

所以默认 8 台、硬上限 16 台，超了明确截断并告诉前端，而不是让用户
点一下等半小时。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import curves

PRODUCT = "KRD 901 330/11"
FROM, TO = "2026-09-01 00:00", "2026-10-01 00:00"


def tp_row(code, name, avg, *, tested=4, pct_fail=0.0):
    """造一行 TestPointSummary（23 列），只填曲线用到的那几列。"""
    r = [None] * 23
    r[0], r[1], r[3] = code, name, "RX PIM Power"
    r[8], r[10], r[11] = "212", tested, pct_fail
    r[16] = avg
    return r


def rows_for(sn_seed: float):
    """一台机器的测试点：2 个测量类型 x 2 个分支，频率写在名称里。

    刻意同时放功率和通过率两类，因为代码里有条不变量是
    「两类数值绝不混在一起平均」。
    """
    return [
        tp_row("5.2.100.5.0",
               "noise_power_level_after_testing 1896 MHz rfPort 5",
               -104.0 + sn_seed),
        tp_row("5.2.100.9.0",
               "noise_power_level_after_testing 1896 MHz rfPort 9",
               -105.0 + sn_seed),
        tp_row("5.2.109.5.0",
               "re_radiated_intermittent_knock_response 1896 MHz rfPort 5",
               1.0),
        tp_row("5.2.109.9.0",
               "re_radiated_intermittent_knock_response 1896 MHz rfPort 9",
               0.8, pct_fail=25.0),
    ]


class FakeSource:
    """假的数据源：按序列号返回略有差异的数据，并记录查了哪几台。"""

    def __init__(self, serials=None):
        self.asked: list[str] = []
        self._serials = list(serials or ["EA8E000001", "EA8E000002",
                                         "EA8E000003"])

    def available_serials(self):
        return list(self._serials)

    def test_points(self, *, serial_numbers=None, **kw):
        sn = (serial_numbers if isinstance(serial_numbers, str)
              else (list(serial_numbers or [])[:1] or [""])[0])
        self.asked.append(sn)
        seed = self._serials.index(sn) * 0.1 if sn in self._serials else 0.0
        return rows_for(seed)


@pytest.fixture
def src():
    return FakeSource()


# ---------- 不再依赖本地 CSV ----------

def test_不再从本地CSV取数():
    """这些名字都是 CSV 数据源的，实时化之后不该还留着。

    留着最大的风险是静默回退：有人看到 store() 能用就继续用，
    于是图上又画回 9 月那份老数据。
    """
    for name in ("measures_path", "DEFAULT_CSV", "CurveStore", "store",
                 "reset"):
        assert not hasattr(curves, name), f"{name} 还在，CSV 数据源没拆干净"


def test_取数要靠数据源而不是文件路径(src):
    """入口函数必须接受数据源 + 查询条件，而不是一个文件名。"""
    idx = curves.from_mi(src, product=PRODUCT, from_date=FROM, to_date=TO,
                         serials=["EA8E000001"])
    assert idx.serials == ["EA8E000001"]
    assert src.asked == ["EA8E000001"], "没有按序列号逐台查 MI"


# ---------- 台数上限 ----------

def test_默认台数上限是个小数字():
    """单台 28~70 秒，默认值必须小到"点一下一分钟内有结果"。"""
    assert curves.DEFAULT_MAX_SERIALS <= 8
    assert curves.MAX_SERIALS_HARD_LIMIT <= 16


def test_超过上限就截断并说明(src):
    """不能让用户点一下等半小时，也不能默默只画一部分不告诉他。"""
    src._serials = [f"EA8E00{i:04d}" for i in range(20)]
    idx = curves.from_mi(src, product=PRODUCT, from_date=FROM, to_date=TO,
                         serials=src._serials, max_serials=3)
    assert len(idx.serials) == 3
    assert len(src.asked) == 3, "截断后不该还去查多余的台"
    assert idx.truncated is True
    assert idx.n_requested == 20


def test_硬上限不能被参数突破(src):
    """免得有人传个 max_serials=79 把服务卡死半小时。"""
    src._serials = [f"EA8E00{i:04d}" for i in range(50)]
    idx = curves.from_mi(src, product=PRODUCT, from_date=FROM, to_date=TO,
                         serials=src._serials,
                         max_serials=curves.MAX_SERIALS_HARD_LIMIT + 10)
    assert len(idx.serials) <= curves.MAX_SERIALS_HARD_LIMIT


def test_截断时等距抽样而不是取前几台(src):
    """取前 N 台只会留下序列号相邻、同批生产的机器，样本没有代表性。"""
    src._serials = [f"EA8E00{i:04d}" for i in range(20)]
    idx = curves.from_mi(src, product=PRODUCT, from_date=FROM, to_date=TO,
                         serials=src._serials, max_serials=4)
    picked = idx.serials
    assert picked[0] == "EA8E000000"
    assert picked[-1] != "EA8E000003", "看起来是取了前 4 台"


# ---------- 曲线组织逻辑（和数据来源无关，必须保持） ----------

def test_列出可选的测量类型并区分功率与通过率(src):
    idx = curves.from_mi(src, product=PRODUCT, from_date=FROM, to_date=TO)
    kinds = {k["kind"]: k for k in idx.kinds()}
    assert "noise_power_level_after_testing" in kinds
    assert "re_radiated_intermittent_knock_response" in kinds
    assert kinds["noise_power_level_after_testing"]["value_kind"] == "power"
    assert kinds["re_radiated_intermittent_knock_response"]["value_kind"] \
        == "passrate"


def test_返回列式紧凑结构而不是每点一个对象(src):
    idx = curves.from_mi(src, product=PRODUCT, from_date=FROM, to_date=TO)
    d = idx.curves(kind="noise_power_level_after_testing", freq_mhz=1896.0)
    assert d["branches"] == [5, 9]
    assert len(d["series"]) == 3
    for s in d["series"]:
        assert isinstance(s["y"], list)
        assert len(s["y"]) == len(d["branches"])
        # 不能是 [{"branch":5,"avg":..}, ...] 这种行式
        assert all(not isinstance(v, dict) for v in s["y"])


def test_缺失的分支补空而不是错位(src):
    """某台缺某个分支时必须用 null 占位，否则曲线左移、画到错误的分支上。"""
    class Partial(FakeSource):
        def test_points(self, *, serial_numbers=None, **kw):
            rows = super().test_points(serial_numbers=serial_numbers, **kw)
            if self.asked[-1] == "EA8E000002":
                # 这台只有分支 5，没有分支 9
                rows = [r for r in rows if ".9.0" not in str(r[0])]
            return rows

    idx = curves.from_mi(Partial(), product=PRODUCT, from_date=FROM,
                         to_date=TO)
    d = idx.curves(kind="noise_power_level_after_testing", freq_mhz=1896.0)
    assert d["branches"] == [5, 9]
    bad = [s for s in d["series"] if s["sn"] == "EA8E000002"][0]
    assert bad["y"][0] is not None
    assert bad["y"][1] is None, "缺的分支必须是 null 占位"


def test_返回纵轴建议范围用于收窄坐标轴(src):
    idx = curves.from_mi(src, product=PRODUCT, from_date=FROM, to_date=TO)
    d = idx.curves(kind="noise_power_level_after_testing", freq_mhz=1896.0)
    lo, hi = d["yrange"]
    assert lo < -105.0 and hi > -104.0, "纵轴要贴着数据，不能从 0 开始"


def test_没有这组数据时报明确错误(src):
    idx = curves.from_mi(src, product=PRODUCT, from_date=FROM, to_date=TO)
    with pytest.raises(KeyError):
        idx.curves(kind="不存在的测量项", freq_mhz=1896.0)


def test_报告实际耗时让前端能提示用户(src):
    """这个查询要几十秒，界面必须能告诉用户花了多久、查了几台。"""
    idx = curves.from_mi(src, product=PRODUCT, from_date=FROM, to_date=TO)
    d = idx.curves(kind="noise_power_level_after_testing", freq_mhz=1896.0)
    assert d["n_series"] == 3
    assert "elapsed_s" in d
    assert d["live"] is True



# ---------- 接口层 ----------
#
# 用 conftest 的假 MI 客户端。注意它默认数据里只有
# noise_power_level_before_testing 能被 measure_kind 识别
# （"knock_response" 单独不匹配 KEY_MEASURES 里任何前缀，会被归成 other 跳过），
# 分支是 5 和 9。

CURVE_KIND = "noise_power_level_before_testing"
BASE = {"product": PRODUCT, "from_date": FROM, "to_date": TO}


def test_接口给出可选项(live_client):
    r = live_client.get("/api/curves/options", params=BASE)
    assert r.status_code == 200, r.text
    d = r.json()
    kinds = {k["kind"]: k for k in d["kinds"]}
    assert kinds[CURVE_KIND]["value_kind"] == "power"
    assert d["live"] is True


def test_可选项要告诉前端台数上限(live_client):
    """前端得知道最多能同屏几台，才能把滑块范围设对。"""
    d = live_client.get("/api/curves/options", params=BASE).json()
    assert d["default_max_series"] == curves.DEFAULT_MAX_SERIALS
    assert d["max_series_limit"] == curves.MAX_SERIALS_HARD_LIMIT


def test_可选项只探一台避免乘以台数的耗时(live_client):
    """单台 28~70 秒，探全部台数就要几分钟 —— 可选项不该这么贵。"""
    d = live_client.get("/api/curves/options", params=BASE).json()
    assert "probed_sn" in d


def test_接口返回曲线(live_client):
    r = live_client.get("/api/curves", params={
        **BASE, "kind": CURVE_KIND, "freq_mhz": 1896.0})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["branches"] == [5, 9]
    assert d["live"] is True
    for s in d["series"]:
        assert len(s["y"]) == 2


def test_接口必须给查询条件(live_client):
    """不给产品号和日期就不该查 —— 实时取数必须知道查什么。

    原来这个接口不需要任何参数（读死的 CSV 文件），所以很容易让人
    以为"曲线不用填条件"。
    """
    r = live_client.get("/api/curves", params={
        "kind": CURVE_KIND, "freq_mhz": 1896.0})
    assert r.status_code == 422, "缺产品号和日期应该被挡住"


def test_接口对不存在的测量类型报404(live_client):
    r = live_client.get("/api/curves", params={
        **BASE, "kind": "不存在的测量项", "freq_mhz": 1896.0})
    assert r.status_code == 404
    assert "可用的测量类型" in r.json()["detail"]


def test_接口拒绝超过硬上限的台数(live_client):
    """免得有人传 max_series=79 把服务占住半小时。"""
    r = live_client.get("/api/curves", params={
        **BASE, "kind": CURVE_KIND, "freq_mhz": 1896.0,
        "max_series": curves.MAX_SERIALS_HARD_LIMIT + 1})
    assert r.status_code == 422


def test_接口报告实际耗时(live_client):
    """几十秒的查询，界面必须能告诉用户花了多久。"""
    d = live_client.get("/api/curves", params={
        **BASE, "kind": CURVE_KIND, "freq_mhz": 1896.0}).json()
    assert "elapsed_s" in d
    assert "truncated" in d
    assert "failed_serials" in d
