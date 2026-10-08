"""plots 的冒烟测试（smoke test，只验证"能跑通不报错"）。

图形长得对不对要靠人眼看，自动化测试能保证的是：
各种输入（包括空数据、缺字段、极端值）都不会让程序崩掉。
界面上一个函数抛异常就整页白屏，所以这类测试很有价值。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pytest

import plots
import yield_calc as yc


@pytest.fixture(autouse=True)
def close_figs():
    """每个测试跑完把图关掉，否则 matplotlib 会警告开了太多图。"""
    yield
    plt.close("all")


def row(code, name, *, group="RX PIM Power", tested=6, pct_fail=0.0, avg=None,
        unit="dBm", lowest=None, highest=None, high=None):
    r = [None] * 23
    r[0], r[1], r[3] = code, name, group
    r[8], r[9], r[10], r[11] = "212", unit, tested, pct_fail
    r[13], r[14], r[15], r[16] = high, lowest, highest, avg
    return r


# ---------- 失败率柱状图 ----------
#
# 这张图画的是测试点次数级**失败率**，不是良率。
# 良率只有场次级 FPY 一种口径，不按分支/频率这类维度拆。

def test_fail_rate_bar_正常数据():
    groups = [
        {"key": 28, "fail_rate_pct": 0.6, "total_failed": 83, "total_tested": 14791},
        {"key": 7, "fail_rate_pct": 0.5, "total_failed": 77, "total_tested": 14692},
    ]
    fig = plots.fig_fail_rate_bar(groups, title="测试")
    assert fig is not None


def test_fail_rate_bar_空数据不崩():
    assert plots.fig_fail_rate_bar([]) is not None


def test_fail_rate_bar_失败率全是None不崩():
    groups = [{"key": 1, "fail_rate_pct": None, "total_failed": 0, "total_tested": 0}]
    assert plots.fig_fail_rate_bar(groups) is not None


def test_fail_rate_bar_全部零失败不崩():
    """失败率都是 0 时纵轴范围会退化，容易出除零或范围为 0 的错。"""
    groups = [{"key": i, "fail_rate_pct": 0.0, "total_failed": 0, "total_tested": 10}
              for i in range(5)]
    assert plots.fig_fail_rate_bar(groups) is not None


def test_fail_rate_bar_长标签也能画():
    groups = [{"key": "AirLeakageTest 很长的组名称", "fail_rate_pct": 8.1,
               "total_failed": 11, "total_tested": 137}]
    assert plots.fig_fail_rate_bar(groups) is not None


# ---------- 热力图 ----------

def test_fail_heatmap_正常数据():
    data = {(5, 1736.0): 3, (9, 2502.5): 1}
    assert plots.fig_fail_heatmap(data) is not None


def test_fail_heatmap_空数据不崩():
    assert plots.fig_fail_heatmap({}) is not None


def test_fleet_heatmap_正常数据():
    per_sn = {"EA8E538914": {17: 1, 29: 2}, "EA8E538915": {i: 1 for i in range(32)}}
    assert plots.fig_fleet_heatmap(per_sn) is not None


def test_fleet_heatmap_空数据不崩():
    assert plots.fig_fleet_heatmap({}) is not None


def test_fleet_heatmap_异常机器被标记():
    """32 个分支全失败的机器应该被标上 [!]（用 ASCII 不用 ⚠，字体缺那个字）。"""
    per_sn = {"BAD": {i: 1 for i in range(32)}, "OK": {3: 1}}
    fig = plots.fig_fleet_heatmap(per_sn, abnormal_threshold=18)
    labels = [t.get_text() for t in fig.axes[0].get_yticklabels()]
    assert any("[!]" in l and "BAD" in l for l in labels)
    assert any(l == "OK" for l in labels)


# ---------- PIM 明细 ----------

def test_pim_power_正常数据():
    rows = []
    for b in range(4):
        for f, name in [(1736.0, "1736"), (1896.0, "1896")]:
            rows.append(row(f"5.2.100.{b}.0",
                            f"noise_power_level_before_testing {name} MHz",
                            avg=-104.5, lowest=-105, highest=-104))
            rows.append(row(f"5.2.101.{b}.0",
                            f"noise_power_level_after_testing {name} MHz",
                            avg=-104.4, lowest=-105, highest=-103.9, pct_fail=16.7))
    recs = yc.parse_rows(rows)
    assert plots.fig_pim_power(recs, sn="TEST") is not None


def test_pim_power_空数据不崩():
    assert plots.fig_pim_power([]) is not None


def test_pim_power_只有通过率没有功率不崩():
    """全是 0~1 的通过率时，功率图应该显示"没有数据"而不是崩掉。"""
    rows = [row("5.2.109.1.0", "knock_response 1896 MHz", avg=0.5, unit="")]
    recs = yc.parse_rows(rows)
    assert plots.fig_pim_power(recs) is not None


def test_knock_passrate_正常数据():
    rows = [row(f"5.2.109.{b}.0", "re_radiated_knock_response 2502.5 MHz",
                avg=0.83 if b % 3 else 1.0, unit="",
                pct_fail=16.7 if b % 3 else 0.0)
            for b in range(8)]
    recs = yc.parse_rows(rows)
    assert plots.fig_knock_passrate(recs, sn="TEST") is not None


def test_knock_passrate_空数据不崩():
    assert plots.fig_knock_passrate([]) is not None


def test_knock_passrate_很多测量项也能画():
    """互调组合多达十几个时图例会挤爆，要能自动处理。"""
    rows = [row(f"5.2.{100+i}.{b}.0", f"combo_{i} knock_response 1896 MHz",
                avg=0.9, unit="")
            for i in range(20) for b in range(4)]
    recs = yc.parse_rows(rows)
    assert plots.fig_knock_passrate(recs) is not None


# ---------- 下层件追踪 ----------

def test_part_timeline_正常数据():
    spans = [
        {"parent_sn": "EA8E538914", "installed_at": "2026-09-14 10:43:32",
         "removed_at": "2026-09-14 12:03:12", "child_product": "KRE 105 518 R1C",
         "part_type": "antenna", "parent_product": "KRD 901 330/11 R1A"},
        {"parent_sn": "EA8E547590", "installed_at": "2026-09-15 13:26:16",
         "removed_at": None, "child_product": "KRE 105 518 R1C",
         "part_type": "antenna", "parent_product": "KRD 901 330/11 R1A"},
    ]
    fails = {"EA8E538914": {17: 1, 29: 1}, "EA8E547590": {28: 2}}
    assert plots.fig_part_timeline(spans, part_sn="DS4W0270J6",
                                  fails_by_parent=fails) is not None


def test_part_timeline_没有失败记录也能画():
    spans = [{"parent_sn": "P1", "installed_at": "2026-09-14 10:00:00",
              "removed_at": None, "child_product": "K", "part_type": "antenna",
              "parent_product": "KRD"}]
    assert plots.fig_part_timeline(spans, part_sn="X", fails_by_parent={}) is not None


def test_part_timeline_空装配记录不崩():
    assert plots.fig_part_timeline([], part_sn="X") is not None


def test_part_timeline_安装时间为None不崩():
    """只看到拆、没看到装的情况（拉取窗口之前就装上了）。"""
    spans = [{"parent_sn": "P1", "installed_at": None,
              "removed_at": "2026-09-14 10:00:00", "child_product": "K",
              "part_type": "antenna", "parent_product": "KRD"}]
    assert plots.fig_part_timeline(spans, part_sn="X") is not None
