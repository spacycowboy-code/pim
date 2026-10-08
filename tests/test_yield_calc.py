"""yield_calc 的单元测试。

全部用构造的假数据，不碰网络也不读文件，所以很快。
重点验证三件容易算错的事：
  1. 失败率的加权平均（不同测试点的测试次数不同，不能简单平均 %Fail）
  2. 测试点编码的解析（分支号、频率、DVT 限值行要跳过）
  3. 功率(dBm) 和 通过率(0~1) 两类数值绝不混在一起统计

★ 口径约定：这个模块算的全部是**测试点次数级的失败率**，不是良率。
  本项目唯一的良率口径是场次级 FPY，在 test_history.run_yield 里。
  所以这里的字段叫 fail_rate_pct，不叫 yield_pct——避免被当成良率引用。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from yield_calc import (
    fail_counts,
    parse_rows,
    pim_only,
    yield_by,
    yield_by_pair,
    yield_from_pair,
    yield_summary,
)


# ---------- 构造假数据的小工具 ----------

def row(code, name, *, group="RX PIM Power", tested=6, pct_fail=0.0,
        avg=None, unit="dBm", category="26", low=None, high=None,
        lowest=None, highest=None, std=None, cpk=None):
    """按 TestPointSummary 的 23 列顺序造一行假数据。"""
    r = [None] * 23
    r[0] = code
    r[1] = name
    r[2] = ".".join(code.replace("DVT:", "").split(".")[:3])
    r[3] = group
    r[4] = "CXC1745245/1_TEST"
    r[5] = "RADIO"
    r[8] = category
    r[9] = unit
    r[10] = tested
    r[11] = pct_fail
    r[12] = low
    r[13] = high
    r[14] = lowest
    r[15] = highest
    r[16] = avg
    r[17] = std
    r[18] = cpk
    return r


# ---------- 良率汇总：加权平均 ----------

def test_yield_summary_加权平均而非简单平均():
    """两个测试点：一个测90次全过，一个测10次失败一半。
    正确答案：总测100次、失败5次、失败率5%。
    如果错误地对 %Fail 简单平均会得到 (0+50)/2=25%，这里刻意用不同的
    tested 让两种算法结果不同。"""
    rows = [
        row("5.2.100.1.0", "a", tested=90, pct_fail=0.0),
        row("5.2.100.2.0", "b", tested=10, pct_fail=50.0),
    ]
    s = yield_summary(rows)
    assert s["total_tested"] == 100
    assert s["total_failed"] == 5          # 10 * 50%
    assert s["fail_rate_pct"] == pytest.approx(5.0)
    # 简单平均会算成 (0+50)/2 = 25%，明显不同
    assert s["fail_rate_pct"] != pytest.approx(25.0)


def test_yield_summary_不暴露良率字段():
    """测试点次数级的比率不是良率，绝不能叫 yield_pct。

    这条是硬约束：一旦这里出现 yield_pct，界面就会把它当良率显示，
    而本项目的良率只有场次级 FPY 一种口径。
    """
    s = yield_summary([row("5.2.100.1.0", "a", tested=10, pct_fail=10.0)])
    assert "yield_pct" not in s
    assert "point_yield_pct" not in s


def test_yield_summary_测试点级别的统计():
    rows = [
        row("5.2.100.1.0", "a", tested=6, pct_fail=0.0),
        row("5.2.100.2.0", "b", tested=6, pct_fail=16.67),
        row("5.2.100.3.0", "c", tested=6, pct_fail=50.0),
    ]
    s = yield_summary(rows)
    assert s["n_test_points"] == 3
    assert s["n_failed_points"] == 2
    assert s["point_fail_rate_pct"] == pytest.approx(100 * 2 / 3)


def test_yield_summary_空数据不崩():
    s = yield_summary([])
    assert s["total_tested"] == 0
    assert s["fail_rate_pct"] is None  # 没数据就是 None，不是 0 也不是 100


def test_yield_summary_跳过DVT限值行():
    """DVT: 前缀的行是限值定义，没有实测值，必须排除，否则分母虚高。"""
    rows = [
        row("DVT:5.2.100.1.0", "limit def", tested=0, pct_fail=0.0),
        row("5.2.100.1.0", "real", tested=6, pct_fail=50.0),
    ]
    s = yield_summary(rows)
    assert s["n_test_points"] == 1
    assert s["total_tested"] == 6


# ---------- 编码解析 ----------

def test_parse_rows_解析分支和频率索引():
    rows = [row("5.2.100.18.3", "noise_power_level_before_testing", avg=-104.5)]
    m = parse_rows(rows)[0]
    assert m["item"] == 100
    assert m["branch"] == 18
    assert m["freq_idx"] == 3


def test_parse_rows_频率优先从名称取():
    """knock 类测量项的第5段恒为0，真实频率写在名称里。
    如果从编码取就全变成同一个频率，图会画错。"""
    rows = [row("5.2.109.16.0", "re_radiated_intermittent_knock_response 2502.5 MHz",
                avg=0.83, unit="")]
    m = parse_rows(rows)[0]
    assert m["freq_mhz"] == pytest.approx(2502.5)
    assert m["freq_idx"] == 0          # 编码里确实是 0
    assert m["value_kind"] == "passrate"


def test_parse_rows_名称无频率时回退到编码映射():
    rows = [row("5.2.100.18.3", "noise_power_level_before_testing", avg=-104.5)]
    m = parse_rows(rows, freq_map={3: 1776.0})[0]
    assert m["freq_mhz"] == pytest.approx(1776.0)


def test_parse_rows_跳过DVT行():
    rows = [
        row("DVT:5.2.100.1.0", "x : 1896 MHz"),
        row("5.2.100.1.0", "y", avg=-104.0),
    ]
    assert len(parse_rows(rows)) == 1


def test_parse_rows_非PIM编码也能处理():
    """像 1.01.0.1 这种非 5.2.x 编码不该让解析崩，只是没有分支/频率。"""
    rows = [row("1.01.0.1", "DC/DC sensor", group="DC/DC Check", avg=0.43, unit="A")]
    m = parse_rows(rows)[0]
    assert m["branch"] is None
    assert m["item"] is None


# ---------- 两类数值不能混 ----------

def test_parse_rows_区分功率和通过率():
    rows = [
        row("5.2.100.1.0", "noise_power_level_before_testing", avg=-104.5),
        row("5.2.109.1.0", "knock_response 1896 MHz", avg=0.5, unit=""),
    ]
    ms = parse_rows(rows)
    assert ms[0]["value_kind"] == "power"
    assert ms[1]["value_kind"] == "passrate"


def test_yield_by_按数值类型分开统计():
    """功率 -104 dBm 和通过率 0.5 混在一起平均会得到 -52 这种无意义的数。"""
    rows = [
        row("5.2.100.1.0", "noise_power_level_before_testing", avg=-104.0, tested=6),
        row("5.2.100.2.0", "noise_power_level_before_testing", avg=-105.0, tested=6),
        row("5.2.109.1.0", "knock_response 1896 MHz", avg=0.5, tested=6, unit=""),
    ]
    groups = {g["key"]: g for g in yield_by(rows, "value_kind")}
    assert groups["power"]["avg_value"] == pytest.approx(-104.5)
    assert groups["passrate"]["avg_value"] == pytest.approx(0.5)


def test_yield_by_组内类型混杂时拒绝求平均():
    """这是最关键的一道守卫。

    按 value_kind 分组时每组天然只有一种类型，考验不到这个守卫；
    必须按别的维度（这里是分支）分组，让功率和通过率落进同一组，
    才能验证代码真的拒绝了混合平均。

    如果守卫失效，-104.0 和 0.5 会被平均成 -51.75 —— 物理上毫无意义的数，
    这正是历史上真实发生过的 bug（算出 "-51.89 dBm"）。
    """
    rows = [
        # 同一个分支 1，一个是功率，一个是通过率
        row("5.2.100.1.0", "noise_power_level_before_testing", avg=-104.0, tested=6),
        row("5.2.109.1.1", "knock_response 1896 MHz", avg=0.5, tested=6, unit=""),
    ]
    got = {g["key"]: g for g in yield_by(rows, "branch")}
    g1 = got[1]
    assert set(g1["value_kinds"]) == {"power", "passrate"}, "这一组应该是混杂的"
    assert g1["avg_value"] is None, (
        f"组内类型混杂时必须拒绝求平均，但得到了 {g1['avg_value']}"
        f"（混合平均会是 -51.75，物理上无意义）")


def test_yield_by_混杂时仍分别给出两类平均值():
    """拒绝混合平均是对的，但不能什么都不给——那样界面上会显示一列 None。

    正确做法：avg_value 保持 None（表示"不存在单一平均值"），
    同时用 avg_power / avg_passrate 分别给出两类的平均值。
    """
    rows = [
        row("5.2.100.1.0", "noise_power_level_before_testing", avg=-104.0, tested=6),
        row("5.2.100.1.1", "noise_power_level_after_testing", avg=-105.0, tested=6),
        row("5.2.109.1.2", "knock_response 1896 MHz", avg=0.5, tested=6, unit=""),
        row("5.2.109.1.3", "knock_response 1856 MHz", avg=0.9, tested=6, unit=""),
    ]
    g = {x["key"]: x for x in yield_by(rows, "branch")}[1]
    assert g["avg_value"] is None
    assert g["avg_power"] == pytest.approx(-104.5)
    assert g["avg_passrate"] == pytest.approx(0.7)


def test_yield_by_单一类型时两个字段也要对():
    """只有功率时，avg_power 有值、avg_passrate 为 None。"""
    rows = [row("5.2.100.1.0", "noise_power_level_before_testing", avg=-104.0)]
    g = {x["key"]: x for x in yield_by(rows, "branch")}[1]
    assert g["avg_value"] == pytest.approx(-104.0)
    assert g["avg_power"] == pytest.approx(-104.0)
    assert g["avg_passrate"] is None


def test_yield_by_pair_组内类型混杂时拒绝求平均():
    """批量数据的分组函数也要有同一道守卫。"""
    all_rows = [
        row("5.2.100.1.0", "noise_power_level_before_testing", avg=-104.0, tested=6),
        row("5.2.109.1.1", "knock_response 1896 MHz", avg=0.5, tested=6, unit=""),
    ]
    got = {g["key"]: g for g in yield_by_pair(all_rows, [], "branch")}
    assert got[1]["avg_value"] is None
    assert set(got[1]["value_kinds"]) == {"power", "passrate"}


def test_yield_by_pair_混杂时也分别给出两类平均值():
    all_rows = [
        row("5.2.100.1.0", "noise_power_level_before_testing", avg=-104.0, tested=6),
        row("5.2.100.1.1", "noise_power_level_after_testing", avg=-105.0, tested=6),
        row("5.2.109.1.2", "knock_response 1896 MHz", avg=0.5, tested=6, unit=""),
    ]
    g = {x["key"]: x for x in yield_by_pair(all_rows, [], "branch")}[1]
    assert g["avg_value"] is None
    assert g["avg_power"] == pytest.approx(-104.5)
    assert g["avg_passrate"] == pytest.approx(0.5)


# ---------- 分组 ----------

def test_yield_by_按分支分组():
    rows = [
        row("5.2.100.5.0", "a", tested=6, pct_fail=0.0, avg=-104.0),
        row("5.2.100.5.1", "b", tested=6, pct_fail=50.0, avg=-104.0),
        row("5.2.100.9.0", "c", tested=6, pct_fail=0.0, avg=-104.0),
    ]
    got = {g["key"]: g for g in yield_by(rows, "branch")}
    assert got[5]["total_tested"] == 12
    assert got[5]["total_failed"] == 3
    assert got[5]["fail_rate_pct"] == pytest.approx(25.0)
    assert got[9]["fail_rate_pct"] == pytest.approx(0.0)


def test_yield_by_按测试类别分组():
    rows = [
        row("5.2.100.1.0", "a", category="26", tested=10, pct_fail=10.0),
        row("5.2.100.2.0", "b", category="212", tested=10, pct_fail=0.0),
    ]
    got = {g["key"]: g for g in yield_by(rows, "category")}
    assert got["26"]["fail_rate_pct"] == pytest.approx(10.0)
    assert got["212"]["fail_rate_pct"] == pytest.approx(0.0)


def test_yield_by_结果按失败率降序_最差的在前():
    rows = [
        row("5.2.100.1.0", "a", category="A", tested=10, pct_fail=0.0),
        row("5.2.100.2.0", "b", category="B", tested=10, pct_fail=50.0),
        row("5.2.100.3.0", "c", category="C", tested=10, pct_fail=10.0),
    ]
    got = yield_by(rows, "category")
    assert [g["key"] for g in got] == ["B", "C", "A"]


def test_yield_by_分组不暴露良率字段():
    """分支/频率这类维度不是交付单位，它们的比率不能叫良率。"""
    rows = [row("5.2.100.5.0", "a", tested=6, pct_fail=50.0)]
    for g in yield_by(rows, "branch"):
        assert "yield_pct" not in g


# ---------- 只要 PIM ----------

def test_pim_only_按测试点组过滤():
    rows = [
        row("5.2.100.1.0", "a", group="RX PIM Power"),
        row("1.01.0.1", "b", group="DC/DC External Sensor Check"),
    ]
    assert len(pim_only(rows)) == 1


# ---------- 机器级别良率已移除 ----------
#
# 原来这里有 unit_yield()（整机台数级良率：一台有一个点失败就算不良）。
# 已删除：本项目的良率口径统一为**场次级 FPY**，由 test_history.run_yield
# 提供，测试在 tests/test_test_history.py。留着第二个良率函数只会让调用方
# 选错口径。



# ---------- 批量查询专用：两次请求相除 ----------
#
# 背景：批量查询（序列号多于一个）时 MI 返回的 %Fail 列恒为 0，不可用。
# 所以良率必须用两次请求：
#   [All] 模式的 #Tested = 总测试次数
#   Failed 模式的 #Tested = 失败次数
# 这两个数相除才是真良率。下面的测试锁住这个算法。

def test_yield_from_pair_两次请求相除():
    all_rows = [
        row("5.2.100.1.0", "a", tested=100, pct_fail=0.0),   # 批量模式 %Fail 恒为 0
        row("5.2.100.2.0", "b", tested=100, pct_fail=0.0),
    ]
    failed_rows = [
        row("5.2.100.2.0", "b", tested=5, pct_fail=100.0),   # Failed 模式 #Tested=失败次数
    ]
    s = yield_from_pair(all_rows, failed_rows)
    assert s["total_tested"] == 200
    assert s["total_failed"] == 5
    assert s["fail_rate_pct"] == pytest.approx(2.5)
    assert s["n_failed_points"] == 1
    assert s["n_test_points"] == 2


def test_yield_from_pair_完全忽略pctfail列():
    """批量模式的 %Fail 是坏的。就算它写着 99，也不该影响结果。"""
    all_rows = [row("5.2.100.1.0", "a", tested=100, pct_fail=99.0)]
    s = yield_from_pair(all_rows, [])
    assert s["total_failed"] == 0
    assert s["fail_rate_pct"] == pytest.approx(0.0)


def test_yield_from_pair_没有失败时失败率0():
    all_rows = [row("5.2.100.1.0", "a", tested=50)]
    s = yield_from_pair(all_rows, [])
    assert s["fail_rate_pct"] == pytest.approx(0.0)
    assert s["n_failed_points"] == 0


def test_yield_from_pair_空输入不崩():
    s = yield_from_pair([], [])
    assert s["total_tested"] == 0
    assert s["fail_rate_pct"] is None


def test_yield_from_pair_不暴露良率字段():
    s = yield_from_pair([row("5.2.100.1.0", "a", tested=10)], [])
    assert "yield_pct" not in s
    assert "point_yield_pct" not in s


def test_yield_from_pair_跳过DVT行():
    all_rows = [
        row("DVT:5.2.100.1.0", "limit", tested=0),
        row("5.2.100.1.0", "a", tested=100),
    ]
    s = yield_from_pair(all_rows, [])
    assert s["n_test_points"] == 1
    assert s["total_tested"] == 100


def test_yield_from_pair_失败行不在All里也计入():
    """批量返回的测试点集合可能不完全重合，失败的点必须算进失败数，
    否则会低估失败、高估良率。"""
    all_rows = [row("5.2.100.1.0", "a", tested=100)]
    failed_rows = [row("5.2.100.99.0", "别的点", tested=7)]
    s = yield_from_pair(all_rows, failed_rows)
    assert s["total_failed"] == 7


def test_yield_by_pair_按分支分组():
    all_rows = [
        row("5.2.100.5.0", "a", tested=100),
        row("5.2.100.9.0", "b", tested=100),
    ]
    failed_rows = [row("5.2.100.5.0", "a", tested=10)]
    got = {g["key"]: g for g in yield_by_pair(all_rows, failed_rows, "branch")}
    assert got[5]["fail_rate_pct"] == pytest.approx(10.0)
    assert got[5]["total_failed"] == 10
    assert got[9]["fail_rate_pct"] == pytest.approx(0.0)
    assert "yield_pct" not in got[5]


def test_yield_by_pair_最差的排前面():
    all_rows = [
        row("5.2.100.1.0", "a", tested=100),
        row("5.2.100.2.0", "b", tested=100),
        row("5.2.100.3.0", "c", tested=100),
    ]
    failed_rows = [
        row("5.2.100.2.0", "b", tested=30),
        row("5.2.100.3.0", "c", tested=10),
    ]
    got = yield_by_pair(all_rows, failed_rows, "branch")
    assert [g["key"] for g in got] == [2, 3, 1]


def test_fail_counts_by_testpoint():
    """Failed 模式下每个测试点的失败次数，用于热力图。"""
    failed_rows = [
        row("5.2.100.5.0", "a", tested=3),
        row("5.2.100.9.0", "b", tested=1),
    ]
    got = fail_counts(failed_rows)
    assert got["5.2.100.5.0"] == 3
    assert got["5.2.100.9.0"] == 1
