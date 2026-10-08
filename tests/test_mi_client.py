"""mi_client 的单元测试。

这些测试只验证"组装请求"的纯逻辑，不打真实接口，所以离线也能跑、很快。

（原来还有一组"缓存键"测试。文件缓存已整个移除——它会让同样条件的查询
直接读几天前的本地文件而不问 MI——所以那些用例随之删除。
"不缓存"这个契约现在由 tests/test_no_cache.py 锁住。）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from mi_client import (
    REPORT_FIELDS,
    build_filter,
    fmt_date,
    join_multi,
    m_item1_rows,
    products_from_unit_structure,
    split_batches,
)


# ---------- 筛选器组装 ----------

def test_build_filter_填满全部字段_缺的为None():
    """服务端要求 reportFilter 字段齐全，缺的必须显式填 null，否则 500。"""
    f = build_filter("TestPointSummary", {"ProductNumber": "KRD 901 330/11"})
    assert len(f) == len(REPORT_FIELDS["TestPointSummary"])
    assert f["ProductNumber"] == "KRD 901 330/11"
    # 没指定的字段必须存在且为 None
    assert "SerialNumber" in f
    assert f["SerialNumber"] is None
    assert f["TestCategory"] is None


def test_build_filter_补上必填默认值():
    """TestCount/TestPointResult/IncludeOutliers/MedianBase 不填会 500。"""
    f = build_filter("TestPointSummary", {"ProductNumber": "X"})
    assert f["TestCount"] == "[All]"
    assert f["TestPointResult"] == "[All]"
    assert f["IncludeOutliers"] == "No"
    assert f["MedianBase"] == "[All]"


def test_build_filter_用户值覆盖默认值():
    f = build_filter("TestPointSummary", {"ProductNumber": "X", "TestCount": "First"})
    assert f["TestCount"] == "First"


def test_build_filter_拒绝未知字段():
    """写错字段名应该立刻报错，而不是静默发给服务端导致 500。"""
    with pytest.raises(ValueError, match="不是 TestPointSummary 的字段"):
        build_filter("TestPointSummary", {"SerialNo": "X"})


def test_build_filter_产品号自动去掉Rstate():
    """MI 的 ProductNumber 不接受尾部 R-state，要去掉。"""
    f = build_filter("TestPointSummary", {"ProductNumber": "KRD 901 330/11 R1A"})
    assert f["ProductNumber"] == "KRD 901 330/11"
    f2 = build_filter("TestPointSummary", {"ProductNumber": "KRE 105 518 R1C"})
    assert f2["ProductNumber"] == "KRE 105 518"


def test_build_filter_结构报表只有两个字段():
    f = build_filter("SerialNumberStructure", {"SerialNumber": "EA8E538914"})
    assert set(f.keys()) == {"SerialNumber", "file_SerialNumber"}


# ---------- 多值（实测只认逗号）----------

def test_join_multi_列表变逗号分隔():
    assert join_multi(["A", "B", "C"]) == "A,B,C"


def test_join_multi_单个字符串原样返回():
    assert join_multi("A") == "A"


def test_join_multi_去空白和空值():
    assert join_multi([" A ", "", "B", None]) == "A,B"


def test_join_multi_空列表返回None():
    """空列表等于"不筛选"，要变成 None 而不是空字符串。"""
    assert join_multi([]) is None
    assert join_multi(None) is None


def test_build_filter_多值字段自动join():
    f = build_filter("TestPointSummary", {"SerialNumber": ["A", "B"]})
    assert f["SerialNumber"] == "A,B"


# ---------- 日期格式 ----------

def test_fmt_date_接受字符串原样():
    assert fmt_date("2026-09-14 00:00") == "2026-09-14 00:00"


def test_fmt_date_date对象补零点():
    import datetime as dt
    assert fmt_date(dt.date(2026, 9, 14)) == "2026-09-14 00:00"


def test_fmt_date_datetime保留时分():
    import datetime as dt
    assert fmt_date(dt.datetime(2026, 9, 14, 13, 45)) == "2026-09-14 13:45"


# ---------- 批次拆分（逐台明细查询用）----------

def test_split_batches_按大小切分():
    assert split_batches(list("abcdefg"), 3) == [
        ["a", "b", "c"], ["d", "e", "f"], ["g"]
    ]


def test_split_batches_不足一批():
    assert split_batches(["a"], 5) == [["a"]]


def test_split_batches_空列表():
    assert split_batches([], 5) == []


# ---------- UnitProductStructure：按日期列产品号（动态下拉的数据源）----------
#
# 这个报表能按 FromDate+ToDate+ProductNumber 筛，是唯一能"按时间段列出
# 在产整机序列号/产品号"的报表（测试历史那几个只能按序列号查）。
# 它的返回是第三种结构 data.m_Item1.resultSet[]（.NET Tuple 序列化）。

def test_build_filter_unit_structure_必填TimeFrequency():
    """不填 TimeFrequency 会 500 Index out of range（踩过），所以要有默认值。"""
    f = build_filter("UnitProductStructure", {"ProductNumber": "KRD 901 330/11"})
    assert f["TimeFrequency"] == "None"
    assert f["ProductNumber"] == "KRD 901 330/11"
    assert f["FromDate"] is None            # 没给的字段仍要显式为 None


def test_m_item1_rows_取第三种结构的行():
    """data.m_Item1.resultSet 里按 resultSetName 找表，取它的 data。"""
    obj = {"data": {"m_Item1": {"resultSet": [
        {"resultSetName": "Data",
         "columns": [{"name": "Part Product Number"}, {"name": "Mfg. Unit"}],
         "data": [["KRF 901 809", "F98-Grentech"], ["KRE 105 518", "DS4-Comba"]]},
        {"resultSetName": "Data1", "columns": [], "data": [["x"]]},
    ]}, "m_Item2": None}}
    rows = m_item1_rows(obj, "Data")
    assert rows == [["KRF 901 809", "F98-Grentech"], ["KRE 105 518", "DS4-Comba"]]
    assert m_item1_rows(obj, "Data1") == [["x"]]


def test_m_item1_rows_空结果不报错():
    assert m_item1_rows({"data": None}, "Data") == []
    assert m_item1_rows({}, "Data") == []
    assert m_item1_rows({"data": {"m_Item1": None}}, "Data") == []


def test_products_from_unit_structure_去重整机产品号():
    """聚合表的 Product Number 列（索引 2）就是整机产品号，去重排序返回。

    注意 Part Product Number（索引 4）是子件产品号，不要混进来。
    'ALL' 这种汇总行要排除。
    """
    obj = {"data": {"m_Item1": {"resultSet": [{
        "resultSetName": "Data",
        "columns": [{"name": c} for c in (
            "", "Factory", "Product Number", "Product Revision State",
            "Part Product Number", "Part Product Revision State", "Mfg. Unit")],
        "data": [
            ["", "ESS Nanjing", "KRD 901 330/11", "R1A", "ALL", "ALL", "ALL"],
            ["", "ESS Nanjing", "KRD 901 330/11", "R1A", "KRF 901 809", "R1A",
             "F98-Grentech"],
            ["", "ESS Nanjing", "KRD 901 400/1", "R2A", "KRE 105 518", "R1C",
             "DS4-Comba"],
        ],
    }]}}}
    prods = products_from_unit_structure(obj)
    assert prods == ["KRD 901 330/11", "KRD 901 400/1"]


# ---------- 从 UnitProductStructure 列出整机序列号（替代固定清单）----------
#
# 为什么要这个：原来"留空=全部整机"退回的是 data/serials.json 里固定的
# 80 台（9 月从一份 Excel 提取的）。产线新产的机器永远不在里面。
# UnitProductStructure 的明细表 Data1 每行有 "ParentSN => ChildSN" 列，
# => 左边就是整机序列号，所以能按日期+产品号实时列出真实在产的全部整机。

from mi_client import serials_from_unit_structure  # noqa: E402


def _ups(rows):
    """造一个 UnitProductStructure 的明细表（Data1）假响应。
    rows 是每行的 'ParentSN => ChildSN' 字符串。"""
    cols = ["Factory", "Product Number", "Product Revision State",
            "Part Product Number", "Part Product Revision State", "Mfg. Unit",
            "First Tests", "Passed Tests", "Test Result",
            "Test Passed W/O Repair", "ParentSN => ChildSN"]
    data = [["ESS", "KRD 901 330/11", "R1A", "KRF 901 809", "R1A", "F98",
             "1", "1", "Pass", "Y", r] for r in rows]
    return {"data": {"m_Item1": {"resultSet": [
        {"resultSetName": "Data", "columns": [], "data": []},
        {"resultSetName": "Data1",
         "columns": [{"name": c} for c in cols],
         "data": data},
    ]}, "m_Item2": None}}


def test_serials_取出整机序列号():
    """=> 左边是整机序列号，右边是子件（可能多个，逗号分隔）。"""
    obj = _ups([
        "EA8E489776 => DK4G5001Y4, DK4G5001Z9",
        "EA8E538914 => F98BX00001",
    ])
    assert serials_from_unit_structure(obj) == ["EA8E489776", "EA8E538914"]


def test_serials_去重排序():
    """同一台整机会在多行出现（每个子件一行），要去重。"""
    obj = _ups([
        "EA8E538914 => F98BX00001",
        "EA8E489776 => DK4G5001Y4",
        "EA8E538914 => DS4W0270J6",   # 同一台，另一个子件
    ])
    assert serials_from_unit_structure(obj) == ["EA8E489776", "EA8E538914"]


def test_serials_空结果不报错():
    assert serials_from_unit_structure({"data": None}) == []
    assert serials_from_unit_structure(_ups([])) == []


# ---------- 下层件装配反查索引（从 UnitProductStructure 快路径）----------
#
# 原来是走 SerialNumberStructure 逐台查（60 台 270 秒 / 整机范围 10+ 分钟），
# 下层件追踪模块初始化要卡几分钟才出现。改成和 serials/suppliers 共享同一个
# UnitProductStructure 响应 —— 零额外网络开销。
#
# 注意：UnitProductStructure.Data1 **没有装入/拆除时间字段**，所以 installed_at
# 和 removed_at 都是 None（前端 `|| '?'` 兜底）。要精确时间线仍需走慢路径。

from mi_client import parts_index_from_unit_structure  # noqa: E402


def _ups_with_products(rows):
    """造带产品号的 UnitProductStructure 响应。rows: [(parent_product, part_product, 'A => B'), ...]"""
    cols = ["Factory", "Product Number", "Product Revision State",
            "Part Product Number", "Part Product Revision State", "Mfg. Unit",
            "First Tests", "Passed Tests", "Test Result",
            "Test Passed W/O Repair", "ParentSN => ChildSN"]
    data = [["ESS", pp, "R1A", pt, "R1A", "F98",
             "1", "1", "Pass", "Y", m] for pp, pt, m in rows]
    return {"data": {"m_Item1": {"resultSet": [
        {"resultSetName": "Data", "columns": [], "data": []},
        {"resultSetName": "Data1",
         "columns": [{"name": c} for c in cols],
         "data": data},
    ]}, "m_Item2": None}}


def test_parts_index_从unit_structure_构建反查索引():
    """子件 → 装过它的整机列表。
    用例：F98BX00001 装过 EA8E538914；DS4W0270J6 装过 EA8E538914 和 EA8E538920。
    """
    obj = _ups_with_products([
        ("KRD 901 330/11", "KRF 901 809", "EA8E538914 => F98BX00001"),
        ("KRD 901 330/11", "KRE 105 518", "EA8E538914 => DS4W0270J6"),
        ("KRD 901 330/11", "KRE 105 518", "EA8E538920 => DS4W0270J6"),
    ])
    idx = parts_index_from_unit_structure(obj)
    assert set(idx.keys()) == {"F98BX00001", "DS4W0270J6"}

    # 滤波器只装过一台
    f98 = idx["F98BX00001"]
    assert len(f98) == 1
    assert f98[0]["parent_sn"] == "EA8E538914"
    assert f98[0]["parent_product"] == "KRD 901 330/11"
    assert f98[0]["child_product"] == "KRF 901 809"
    assert f98[0]["part_type"] == "filter_unit"
    # 时间字段恒 None（快路径没有时间信息）
    assert f98[0]["installed_at"] is None
    assert f98[0]["removed_at"] is None

    # 天线换过一台机器 → 两条记录
    ds4 = idx["DS4W0270J6"]
    assert len(ds4) == 2
    assert {x["parent_sn"] for x in ds4} == {"EA8E538914", "EA8E538920"}


def test_parts_index_逗号多子件():
    """一行 ParentSN => ChildSN 可能带多个子件，用逗号分隔。"""
    obj = _ups_with_products([
        ("KRD 901 330/11", "KRE 105 518",
         "EA8E538914 => DS4W0270J6, DS4W0270J7, DS4W0270J8"),
    ])
    idx = parts_index_from_unit_structure(obj)
    assert set(idx.keys()) == {"DS4W0270J6", "DS4W0270J7", "DS4W0270J8"}
    for sn in idx:
        assert idx[sn][0]["parent_sn"] == "EA8E538914"


def test_parts_index_空结果不报错():
    assert parts_index_from_unit_structure({"data": None}) == {}
    assert parts_index_from_unit_structure(_ups_with_products([])) == {}


def test_parts_index_跳过非机械件():
    """只保留 antenna / filter_unit —— 其他类型的子件（板卡、外壳等）
    走别的追溯链路，这个 index 不管。"""
    obj = _ups_with_products([
        ("KRD 901 330/11", "KRF 901 809", "EA8E1 => F98BX00001"),  # 滤波器 ✓
        ("KRD 901 330/11", "ROX 101 42/11", "EA8E1 => CB4XX00001"),  # 板卡 ✗
        ("KRD 901 330/11", "KRE 105 518", "EA8E1 => DS4W0270J6"),  # 天线 ✓
    ])
    idx = parts_index_from_unit_structure(obj)
    assert set(idx.keys()) == {"F98BX00001", "DS4W0270J6"}


def test_serials_跳过脏行():
    """没有 => 的行、空整机号都要跳过，不能让一行脏数据带崩整批。"""
    obj = _ups([
        "EA8E538914 => F98BX00001",
        "没有箭头的脏行",
        " => DK4G5001Y4",             # 整机号为空
        "EA8E489776 => ",
    ])
    assert serials_from_unit_structure(obj) == ["EA8E489776", "EA8E538914"]


# ---------- 从同一份 UnitProductStructure 响应里顺带拿供应商 ----------
#
# ★ 关键优化：列整机（serials_from_unit_structure）和取供应商用的是**同一个
# 响应**，所以供应商是零额外网络成本。实测一次请求 15 秒拿到 60 台的供应商，
# 而逐台查装配结构（SerialNumberStructure）要 270 秒 —— 快 18 倍。

from mi_client import suppliers_from_unit_structure  # noqa: E402


def _ups2(rows):
    """造 Data1 假响应，rows 是 (子件产品号, 'ParentSN => ChildSN') 元组。"""
    cols = ["Factory", "Product Number", "Product RState",
            "Part Product Number", "Part Product R-State", "Mfg.Unit",
            "First Tests", "Passed Tests", "Test Result",
            "Test Passed W/O Repair", "ParentSN => ChildSN"]
    data = [["ESS", "KRD 901 330/11", "R1A", part, "R1C", "X",
             "1", "1", "Y", "Y", m] for part, m in rows]
    return {"data": {"m_Item1": {"resultSet": [
        {"resultSetName": "Data1",
         "columns": [{"name": c} for c in cols],
         "data": data},
    ]}, "m_Item2": None}}


def test_suppliers_按整机给出天线和滤波器供应商():
    """子件序列号前 3 位 -> 供应商公司名（KRE=天线，KRF=滤波器）。"""
    obj = _ups2([
        ("KRE 105 518", "EA8E538919 => DS4W0270K6"),      # Comba 天线
        ("KRF 901 809", "EA8E538919 => F98BX00001"),      # Grentech 滤波器
    ])
    got = suppliers_from_unit_structure(obj)
    assert got["EA8E538919"] == {"antenna": ["Comba"],
                                 "filter_unit": ["Grentech"]}


def test_suppliers_一行多个子件都算上():
    """ChildSN 可能逗号分隔多个（一台 16 个滤波器）。"""
    obj = _ups2([
        ("KRF 901 809", "EA001 => F98BX00001, DK4G5001Y4"),
    ])
    got = suppliers_from_unit_structure(obj)
    # 两个不同供应商的滤波器都要列出（换过件的机器会这样）
    assert got["EA001"]["filter_unit"] == ["Chunxing", "Grentech"]


def test_suppliers_只认天线和滤波器():
    """别的子件（板卡等）没有供应商之分，不该混进来。"""
    obj = _ups2([
        ("ROX 101 42/11", "EA001 => EA8E000001"),         # 厂内板卡，跳过
        ("KRE 105 518", "EA001 => DS4W0270K6"),
    ])
    got = suppliers_from_unit_structure(obj)
    assert got["EA001"]["antenna"] == ["Comba"]
    assert got["EA001"]["filter_unit"] == []


def test_suppliers_空结果不报错():
    assert suppliers_from_unit_structure({"data": None}) == {}
    assert suppliers_from_unit_structure(_ups2([])) == {}


# ---------- 失败分支：从子件测试历史的失败测试点编码解析 ----------

from mi_client import failed_branches_from_child_history  # noqa: E402


def _child_hist(codes):
    """造 ChildSerialNumberTestHistory 假响应，codes 是失败测试点编码列表。"""
    cols = ["Serial Number", "Mfg Date", "Test Start Date", "Test Status",
            "Failed Test Point Code", "Failed Test Point Name"]
    data = [["F98BX00001", "", "2026-09-20 10:00", "Fail", c, f"name {c}"]
            for c in codes]
    return {"data": {"resultSet": [
        {"columns": [{"name": c} for c in cols], "rows": data}]}}


def test_失败分支从测试点编码取第4段():
    """编码 5.2.<测量项>.<分支>.<频率> —— 分支是第 4 段。

    实测：5.2.97.3.0 的名称里写着 rfPort 3，和编码第 4 段一致。
    """
    obj = _child_hist(["5.2.97.3.0", "5.2.102.10.0"])
    assert failed_branches_from_child_history(obj) == [3, 10]


def test_失败分支去重排序():
    obj = _child_hist(["5.2.97.3.0", "5.2.100.3.0", "5.2.102.1.0"])
    assert failed_branches_from_child_history(obj) == [1, 3]


def test_失败分支忽略非测试点编码():
    """[N/A]、空值、限值定义行（DVT: 前缀）都要跳过。"""
    obj = _child_hist(["[N/A]", "", "5.2.97.5.0", "DVT:5.2.97.9.0", "1.01.0.1"])
    assert failed_branches_from_child_history(obj) == [5]


def test_失败分支空结果不报错():
    assert failed_branches_from_child_history({"data": None}) == []
    assert failed_branches_from_child_history(_child_hist([])) == []



def test_失败测试点按场次分组(tmp_path):
    """ChildSerialNumberTestHistory 的每一行是一个"失败测试点"。
    同一次测试（同一个 Test Start Date）可能失败多个点。
    对外要按场次分组：{started: [{code, name, branch, freq_mhz, measured}, ...]}。
    """
    from mi_client import failed_testpoints_from_child_history
    cols = ["Serial Number", "Mfg Date", "Test Start Date", "Test Status",
            "Failed Test Point Code", "Failed Test Point Name",
            "Measured Value", "Fault Code"]
    data = [
        # 第 1 次测试：失败两个点
        ["EA8E573021", "", "2026-09-28 01:33:43", "Fail",
         "5.2.100.28.3", "RX PIM Power re_radiated ... 1896 MHz rfPort 28",
         "0.5", "FC100"],
        ["EA8E573021", "", "2026-09-28 01:33:43", "Fail",
         "5.2.102.14.2", "RX PIM knock_response 1856 MHz rfPort 14",
         "0.6", "FC100"],
        # 第 2 次测试：失败一个点
        ["EA8E573021", "", "2026-09-28 02:01:48", "Fail",
         "5.2.97.3.0", "noise_power_level 1896 MHz rfPort 3",
         "-100.1", "FC200"],
    ]
    obj = {"data": {"resultSet": [
        {"columns": [{"name": c} for c in cols], "rows": data}]}}
    r = failed_testpoints_from_child_history(obj)

    # 两次测试
    assert set(r.keys()) == {"2026-09-28 01:33:43", "2026-09-28 02:01:48"}

    # 第 1 次失败两个点，顺序按分支号升序（14 在前）
    first = r["2026-09-28 01:33:43"]
    assert len(first) == 2
    assert first[0]["branch"] == 14 and first[0]["freq_mhz"] == 1856.0
    assert first[1]["branch"] == 28 and first[1]["freq_mhz"] == 1896.0
    assert first[0]["code"] == "5.2.102.14.2"
    assert "knock_response" in first[0]["name"]
    assert first[0]["measured"] == "0.6"

    # 第 2 次失败一个点
    second = r["2026-09-28 02:01:48"]
    assert len(second) == 1
    assert second[0]["branch"] == 3


def test_失败测试点空结果不报错():
    from mi_client import failed_testpoints_from_child_history
    assert failed_testpoints_from_child_history({"data": None}) == {}
    assert failed_testpoints_from_child_history({}) == {}


def test_失败测试点频率优先从名称取():
    """档案里写过：频率优先从名称 ' 1776 MH' 取，取不到才回退编码。
    knock 类测量项的编码第 5 段恒为 0，不能当频率用。
    """
    from mi_client import failed_testpoints_from_child_history
    cols = ["Serial Number", "Mfg Date", "Test Start Date", "Test Status",
            "Failed Test Point Code", "Failed Test Point Name"]
    data = [
        ["X", "", "2026-10-01 10:00:00", "Fail",
         "5.2.100.14.0",  # 第 5 段是 0
         "RX PIM knock_response 2502.5 MHz rfPort 14"],  # 名称有 2502.5
    ]
    obj = {"data": {"resultSet": [
        {"columns": [{"name": c} for c in cols], "rows": data}]}}
    r = failed_testpoints_from_child_history(obj)
    assert r["2026-10-01 10:00:00"][0]["freq_mhz"] == 2502.5




