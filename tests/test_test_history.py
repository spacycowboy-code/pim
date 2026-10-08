"""测试历史模块的测试（TDD：先写这个，看它因为功能没做而失败）。

这个模块的存在意义：TestPointSummary 是聚合结果、没有时间戳，
所以换日期必须重新请求 MI。测试历史报表给的是明细、带 TestStartTime，
一次拉全后就能在本地按任意日期筛选——这是解锁日期框的关键。
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import test_history as th


# ---------------------------------------------------------------------------
# 造假数据：模仿 MI 结构 B（data.resultSet[i] 带 columns + rows）
# ---------------------------------------------------------------------------

TEST_COLS = ["", "Factory", "SerialNumber", "ProductNumber",
             "ProductNumberRevisionState", "TestCriteria", "TestStartTime",
             "TestCount", "TestPassed", "TestPointName", "TestPointValue",
             "ControlCategoryID", "TestCategoryID", "TestStationID",
             "TestEndTime", "SortingId", "SubUnitData"]

REPAIR_COLS = ["SerialNumber", "TestStartTime", "RepairStartTime",
               "RepairEndTime", "FaultName", "FaultAreaName", "PositionID",
               "RepairActionName"]


def _test_row(sn, start, end, passed, count=1, cat="212", station="8945:1"):
    return ["", "ESS Nanjing", sn, "KRD 901 330/11", "R1A",
            "CXC1744995/1_F212_X", start, str(count), passed,
            "[N/A]", "[N/A]", "[NULL]", cat, station, end, "0", "None"]


def _payload(test_rows, repair_rows=()):
    """拼一个和 MI 真实返回同形状的对象。"""
    return {
        "data": {
            "resultCount": len(test_rows),
            "resultSet": [
                {"columns": [{"index": i, "name": c} for i, c in enumerate(TEST_COLS)],
                 "rows": list(test_rows)},
                {"columns": [{"index": i, "name": c} for i, c in enumerate(REPAIR_COLS)],
                 "rows": list(repair_rows)},
            ],
        },
        "executionTime": "0.1",
    }


SAMPLE = _payload([
    _test_row("EA001", "2026-09-14 08:40:05", "2026-09-14 09:13:07", "Y"),
    _test_row("EA001", "2026-09-15 10:00:00", "2026-09-15 10:30:00", "N", count=2),
    _test_row("EA001", "2026-09-20 11:00:00", "2026-09-20 11:20:00", "Y", count=3),
    _test_row("EA001", "2026-10-05 08:00:00", "2026-10-05 08:30:00", "Y"),
], repair_rows=[
    ["EA001", "2026-09-15 10:00:00", "2026-09-15 12:06:02", "2026-09-15 12:06:07",
     "MATERIAL Unknown", "Material", "3", "SWAP"],
])


# ---------------------------------------------------------------------------
# 解析 MI 的两种返回结构
# ---------------------------------------------------------------------------

def test_解析带列名的表结构():
    tables = th.parse_tables(SAMPLE)
    assert len(tables) == 2
    assert tables[0].columns[6] == "TestStartTime"
    assert len(tables[0].rows) == 4


def test_表能按列名取值():
    t = th.parse_tables(SAMPLE)[0]
    assert t.get(t.rows[0], "SerialNumber") == "EA001"
    assert t.get(t.rows[0], "TestPassed") == "Y"


def test_取不存在的列要报错而不是静默返回None():
    """静默返回 None 会让拼错列名的 bug 藏到很后面才发现。"""
    t = th.parse_tables(SAMPLE)[0]
    with pytest.raises(KeyError):
        t.get(t.rows[0], "NoSuchColumn")


def test_也能解析tableResultSet结构():
    """SerialNumberLog 用的是 tableResultSet（单张表），不是 resultSet。"""
    obj = {"data": {"tableResultSet": {
        "columns": [{"index": 0, "name": "Event"},
                    {"index": 1, "name": "Event Start Time"}],
        "rows": [["self:Created", "2026-09-11 19:03:19"]]}}}
    tables = th.parse_tables(obj)
    assert len(tables) == 1
    assert tables[0].columns == ["Event", "Event Start Time"]


def test_空返回不报错():
    assert th.parse_tables({"data": None}) == []
    assert th.parse_tables({}) == []


# ---------------------------------------------------------------------------
# 转成测试场次记录
# ---------------------------------------------------------------------------

def test_转成测试场次记录():
    runs = th.to_runs(SAMPLE)
    assert len(runs) == 4
    r = runs[0]
    assert r.serial_number == "EA001"
    assert r.product == "KRD 901 330/11"
    assert r.started == dt.datetime(2026, 9, 14, 8, 40, 5)
    assert r.ended == dt.datetime(2026, 9, 14, 9, 13, 7)
    assert r.passed is True
    assert r.test_count == 1
    assert r.category == "212"


def test_失败场次的passed是False():
    runs = th.to_runs(SAMPLE)
    assert runs[1].passed is False


def test_时间戳带毫秒也能解析():
    """ChildSerialNumberTestHistory 的时间是 '2026-09-14 11:00:25.000'。"""
    assert th.parse_dt("2026-09-14 11:00:25.000") == dt.datetime(2026, 9, 14, 11, 0, 25)


def test_美式日期也能解析():
    """Mfg Date 是 '9/11/2026 12:00:00 AM' 这种格式。"""
    assert th.parse_dt("9/11/2026 12:00:00 AM") == dt.datetime(2026, 9, 11, 0, 0, 0)


def test_解析不了的时间返回None而不是崩():
    assert th.parse_dt("") is None
    assert th.parse_dt("[NULL]") is None
    assert th.parse_dt(None) is None


# ---------------------------------------------------------------------------
# ★ 核心：按任意日期范围筛选（这是解锁日期框的关键能力）
# ---------------------------------------------------------------------------

def test_按日期范围筛选():
    runs = th.to_runs(SAMPLE)
    got = th.filter_runs(runs, from_date="2026-09-15 00:00", to_date="2026-09-21 00:00")
    assert len(got) == 2
    assert {r.started.day for r in got} == {15, 20}


def test_日期范围含左不含右():
    """避免边界上的记录被两个相邻区间各算一次。"""
    runs = th.to_runs(SAMPLE)
    got = th.filter_runs(runs, from_date="2026-09-15 10:00", to_date="2026-09-20 11:00")
    assert len(got) == 1
    assert got[0].started.day == 15


def test_不给日期就是全部():
    runs = th.to_runs(SAMPLE)
    assert len(th.filter_runs(runs)) == 4


def test_能按测试类别筛():
    runs = th.to_runs(_payload([
        _test_row("EA001", "2026-09-14 08:00:00", "2026-09-14 09:00:00", "Y", cat="212"),
        _test_row("EA001", "2026-09-14 10:00:00", "2026-09-14 11:00:00", "Y", cat="26"),
    ]))
    assert len(th.filter_runs(runs, categories=["212"])) == 1
    assert len(th.filter_runs(runs, categories=["212", "26"])) == 2


def test_能按序列号筛():
    runs = th.to_runs(_payload([
        _test_row("EA001", "2026-09-14 08:00:00", "2026-09-14 09:00:00", "Y"),
        _test_row("EA002", "2026-09-14 10:00:00", "2026-09-14 11:00:00", "Y"),
    ]))
    assert len(th.filter_runs(runs, serial_numbers=["EA001"])) == 1


def test_能按产品号筛():
    runs = th.to_runs(SAMPLE)
    assert len(th.filter_runs(runs, product="KRD 901 330/11")) == 4
    assert len(th.filter_runs(runs, product="别的产品")) == 0


# ---------------------------------------------------------------------------
# 按场次算良率
# ---------------------------------------------------------------------------

def test_算场次良率():
    runs = th.to_runs(SAMPLE)
    y = th.run_yield(runs)
    assert y["n_runs"] == 4
    assert y["n_failed"] == 1
    assert y["n_passed"] == 3
    assert y["yield_pct"] == pytest.approx(75.0)


def test_良率标明是场次级别():
    """必须和 TestPointSummary 的测试点级良率区分开，否则会被误读。"""
    y = th.run_yield(th.to_runs(SAMPLE))
    assert y["level"] == "run"


def test_空数据的良率不除零():
    y = th.run_yield([])
    assert y["n_runs"] == 0
    assert y["yield_pct"] is None


def test_首次通过率只看第一次测试():
    """FPY（First Pass Yield，首次通过率）只统计 TestCount=1 的那次。"""
    runs = th.to_runs(_payload([
        _test_row("EA001", "2026-09-14 08:00:00", "2026-09-14 09:00:00", "N", count=1),
        _test_row("EA001", "2026-09-14 10:00:00", "2026-09-14 11:00:00", "Y", count=2),
        _test_row("EA002", "2026-09-14 08:00:00", "2026-09-14 09:00:00", "Y", count=1),
    ]))
    fpy = th.run_yield(runs, first_only=True)
    assert fpy["n_runs"] == 2            # 只有两次「第一次测试」
    assert fpy["n_failed"] == 1
    assert fpy["yield_pct"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# 时间趋势（有了时间戳才能做的事）
# ---------------------------------------------------------------------------

def test_按天统计趋势():
    runs = th.to_runs(SAMPLE)
    days = th.trend(runs, by="day")
    assert [d["bucket"] for d in days] == ["2026-09-14", "2026-09-15",
                                           "2026-09-20", "2026-10-05"]
    assert days[1]["n_failed"] == 1
    assert days[1]["yield_pct"] == pytest.approx(0.0)


def test_按周统计趋势():
    runs = th.to_runs(SAMPLE)
    weeks = th.trend(runs, by="week")
    assert len(weeks) >= 2
    assert all("bucket" in w and "yield_pct" in w for w in weeks)


def test_趋势可以只算FPY():
    """良率口径统一为 FPY，所以趋势也要能只统计每台的第一次测试。

    09-14 两台各第一次（一失败一通过）-> 50%
    09-15 只有一条复测记录，FPY 口径下这天没有数据，整个桶都不出现
    """
    runs = th.to_runs(_payload([
        _test_row("EA001", "2026-09-14 08:00:00", "2026-09-14 09:00:00", "N", count=1),
        _test_row("EA002", "2026-09-14 08:00:00", "2026-09-14 09:00:00", "Y", count=1),
        _test_row("EA001", "2026-09-15 10:00:00", "2026-09-15 11:00:00", "Y", count=2),
    ]))
    days = th.trend(runs, by="day", first_only=True)
    assert [d["bucket"] for d in days] == ["2026-09-14"]
    assert days[0]["n_runs"] == 2
    assert days[0]["n_failed"] == 1
    assert days[0]["yield_pct"] == pytest.approx(50.0)
    # 对照：不限 FPY 时 09-15 这个桶是存在的
    assert len(th.trend(runs, by="day")) == 2


def test_趋势按时间正序():
    runs = th.to_runs(SAMPLE)
    buckets = [d["bucket"] for d in th.trend(runs, by="day")]
    assert buckets == sorted(buckets)


# ---------------------------------------------------------------------------
# 实际可选的日期范围（前端要用它来提示用户）
# ---------------------------------------------------------------------------

def test_报告数据实际覆盖的时间范围():
    runs = th.to_runs(SAMPLE)
    lo, hi = th.date_span(runs)
    assert lo == dt.datetime(2026, 9, 14, 8, 40, 5)
    assert hi == dt.datetime(2026, 10, 5, 8, 30, 0)


def test_空数据的时间范围是None():
    assert th.date_span([]) == (None, None)


# ---------------------------------------------------------------------------
# 维修记录
# ---------------------------------------------------------------------------

def test_解析维修记录():
    reps = th.to_repairs(SAMPLE)
    assert len(reps) == 1
    assert reps[0].serial_number == "EA001"
    assert reps[0].fault == "MATERIAL Unknown"
    assert reps[0].action == "SWAP"
    assert reps[0].started == dt.datetime(2026, 9, 15, 12, 6, 2)


def test_没有维修表也不报错():
    obj = _payload([_test_row("EA001", "2026-09-14 08:00:00",
                              "2026-09-14 09:00:00", "Y")])
    assert th.to_repairs(obj) == []
