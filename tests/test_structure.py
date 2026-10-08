"""structure 的单元测试。

装配结构数据的形状（SerialNumberStructure 报表返回的 6 列）：
  Factory Code | Product Number | Serial Number | Modified Time
  | Newly Added Serial Number | Removed Serial Number

装卸记录写在最后两列里，格式是 "产品号 R-state : 序列号"，多个用逗号分隔。
注意产品号本身含空格和斜杠（如 "ROX 101 42/11 R1A"），所以不能简单按空格切。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from structure import (
    KNOWN_PART_TYPES,
    build_index,
    classify_part,
    parents_by_supplier,
    parse_parts,
    parse_structure,
    supplier_of,
    supplier_options,
    suppliers_by_parent,
    where_installed,
)


# ---------- 解析 "产品号 : 序列号" ----------

def test_parse_parts_单个():
    assert parse_parts("KRE 105 518 R1C : DS4W0270J6") == [
        ("KRE 105 518 R1C", "DS4W0270J6")
    ]


def test_parse_parts_多个逗号分隔():
    s = "KRE 105 518 R1C : DS4W0270J6,SDF 111 860/1 R1B : EX20138884"
    assert parse_parts(s) == [
        ("KRE 105 518 R1C", "DS4W0270J6"),
        ("SDF 111 860/1 R1B", "EX20138884"),
    ]


def test_parse_parts_产品号含斜杠和空格():
    """ROX 101 42/11 这种产品号不能被空格或斜杠切断。"""
    assert parse_parts("ROX 101 42/11 R1A : EA8E463457") == [
        ("ROX 101 42/11 R1A", "EA8E463457")
    ]


def test_parse_parts_空值返回空列表():
    assert parse_parts("") == []
    assert parse_parts(None) == []
    assert parse_parts("   ") == []


def test_parse_parts_容忍多余空白():
    assert parse_parts("  KRE 105 518 R1C  :  DS4W0270J6  ") == [
        ("KRE 105 518 R1C", "DS4W0270J6")
    ]


def test_parse_parts_忽略格式不对的片段():
    """没有冒号的片段丢掉，不要让整行解析失败。"""
    assert parse_parts("垃圾数据,KRE 105 518 R1C : DS4W0270J6") == [
        ("KRE 105 518 R1C", "DS4W0270J6")
    ]


# ---------- 子件类型识别 ----------

def test_classify_part_识别天线():
    assert classify_part("KRE 105 518 R1C", "DS4W0270J6") == "antenna"


def test_classify_part_识别滤波单元():
    assert classify_part("KRF 901 809 R1A", "F98BX0025B") == "filter_unit"


def test_classify_part_识别整机():
    assert classify_part("KRD 901 330/11 R1A", "EA8E538914") == "radio"


def test_classify_part_未知类型():
    assert classify_part("SDD 513 1733/2 R1C", "EX42619198") == "other"


def test_known_part_types_含三种主要件():
    assert "antenna" in KNOWN_PART_TYPES.values()
    assert "filter_unit" in KNOWN_PART_TYPES.values()


# ---------- 造假数据的小工具 ----------

def srow(parent_sn, time, added="", removed="",
         product="KRD 901 330/11 R1A", factory="CB4"):
    """造一行假的装配结构数据（6 列）。"""
    return [factory, product, parent_sn, time, added, removed]


# ---------- 供应商（按子件序列号前缀映射到公司名）----------
#
# 权威来源是 MI 的 UnitProductStructure 报表的 Mfg. Unit 列。实测
# 2026-02~09 共 628 台、9041 行装配明细，得到两条事实：
#   1. 序列号前 3 位 == Mfg.Unit 代码，前 5 位与供应商严格一对一（0 个一对多）
#   2. 同一家供应商可能有多个产地（Chunxing 苏州 DK4 + 深圳 EC1），
#      用户明确要求产地不算供应商，所以两个前缀要合并成一家
# 这条规则只对外购机械件成立：厂内板卡 ROA/ROX 的 Mfg.Unit 是 CB4-ESS Nanjing
# 但序列号是 EA8E*，前 3 位对不上，而且同一家下有 7 种前缀（流水号段）。

def test_supplier_of_返回供应商公司名():
    assert supplier_of("F98BX0025B") == "Grentech"
    assert supplier_of("DS4W0270J6") == "Comba"
    assert supplier_of("EK61B0020Z") == "AMPHENOL"


def test_supplier_of_同一供应商的两个产地合并成一家():
    """DK4=春兴苏州、EC1=春兴深圳，是同一家供应商的两个厂，不按产地拆开。"""
    assert supplier_of("DK4G5001Y4") == "Chunxing"
    assert supplier_of("EC1G5001A1") == "Chunxing"


def test_supplier_of_未知前缀退回序列号前5位():
    """认不出的前缀不能丢数据，退回前缀本身，界面上照样能筛。"""
    assert supplier_of("ZZ9QQ00001") == "ZZ9QQ"


def test_supplier_of_短序列号不报错():
    assert supplier_of("AB") == "AB"
    assert supplier_of("") == ""
    assert supplier_of(None) == ""


def test_supplier_options_按类型列出供应商():
    """同一个产品号下的两家供应商要分别列出，并统计涉及多少子件、多少整机。"""
    structures = {
        "EA8E001": [srow("EA8E001", "2026-09-01 10:00:00",
                         added="KRF 901 809 R1A : F98BX00001,"
                               "KRE 105 518 R1C : DS4W0270J6")],
        "EA8E002": [srow("EA8E002", "2026-09-02 10:00:00",
                         added="KRF 901 809 R1A : DK4G5A0001,"
                               "KRF 901 809 R1A : F98BX00002")],
    }
    opts = supplier_options(structures)
    fu = {o["supplier"]: o for o in opts["filter_unit"]}
    assert set(fu) == {"Grentech", "Chunxing"}
    assert fu["Grentech"]["n_children"] == 2    # F98BX00001 / F98BX00002
    assert fu["Grentech"]["n_parents"] == 2     # 装在两台整机上
    assert fu["Chunxing"]["n_children"] == 1
    assert fu["Chunxing"]["n_parents"] == 1
    assert fu["Grentech"]["child_product"].startswith("KRF 901 809")

    ant = {o["supplier"]: o for o in opts["antenna"]}
    assert set(ant) == {"Comba"}


def test_supplier_options_两个产地算同一个选项():
    """春兴的苏州厂和深圳厂装在两台机上，应该合成一个 Chunxing 选项、2 台。"""
    structures = {
        "EA8E001": [srow("EA8E001", "2026-09-01 10:00:00",
                         added="KRF 901 809 R1A : DK4G5A0001")],
        "EA8E002": [srow("EA8E002", "2026-09-02 10:00:00",
                         added="KRF 901 809 R1A : EC1G5B0001")],
    }
    fu = supplier_options(structures)["filter_unit"]
    assert [o["supplier"] for o in fu] == ["Chunxing"]
    assert fu[0]["n_parents"] == 2
    assert fu[0]["n_children"] == 2


def test_supplier_options_按涉及整机数降序():
    structures = {
        "EA8E001": [srow("EA8E001", "2026-09-01 10:00:00",
                         added="KRF 901 809 R1A : DK4G5A0001")],
        "EA8E002": [srow("EA8E002", "2026-09-02 10:00:00",
                         added="KRF 901 809 R1A : F98BX00001")],
        "EA8E003": [srow("EA8E003", "2026-09-03 10:00:00",
                         added="KRF 901 809 R1A : F98BX00002")],
    }
    got = [o["supplier"] for o in supplier_options(structures)["filter_unit"]]
    assert got == ["Grentech", "Chunxing"]


def test_parents_by_supplier_反查整机():
    """选了某个供应商，要能得到装了它的件的整机列表——这是筛选的基础。"""
    structures = {
        "EA8E001": [srow("EA8E001", "2026-09-01 10:00:00",
                         added="KRF 901 809 R1A : F98BX00001")],
        "EA8E002": [srow("EA8E002", "2026-09-02 10:00:00",
                         added="KRF 901 809 R1A : DK4G5A0001")],
        "EA8E003": [srow("EA8E003", "2026-09-03 10:00:00",
                         added="KRF 901 809 R1A : F98BX00002,"
                               "KRE 105 518 R1C : EK61B00001")],
    }
    assert parents_by_supplier(structures, "filter_unit", "Grentech") == {
        "EA8E001", "EA8E003"}
    assert parents_by_supplier(structures, "filter_unit", "Chunxing") == {
        "EA8E002"}
    assert parents_by_supplier(structures, "antenna", "AMPHENOL") == {"EA8E003"}


def test_parents_by_supplier_两个产地都算这家():
    """选 Chunxing 要同时捞到苏州厂和深圳厂的件装在哪些整机上。"""
    structures = {
        "EA8E001": [srow("EA8E001", "2026-09-01 10:00:00",
                         added="KRF 901 809 R1A : DK4G5A0001")],
        "EA8E002": [srow("EA8E002", "2026-09-02 10:00:00",
                         added="KRF 901 809 R1A : EC1G5B0001")],
        "EA8E003": [srow("EA8E003", "2026-09-03 10:00:00",
                         added="KRF 901 809 R1A : F98BX00001")],
    }
    assert parents_by_supplier(structures, "filter_unit", "Chunxing") == {
        "EA8E001", "EA8E002"}


def test_parents_by_supplier_没有匹配返回空集():
    structures = {
        "EA8E001": [srow("EA8E001", "2026-09-01 10:00:00",
                         added="KRF 901 809 R1A : F98BX00001")],
    }
    assert parents_by_supplier(structures, "filter_unit", "XXXXX") == set()
    assert parents_by_supplier(structures, "antenna", "Grentech") == set()


def test_parents_by_supplier_拆下过也算装过():
    """件被拆下来了，但那段时间的测试数据确实属于这个供应商，所以要算。"""
    structures = {
        "EA8E001": [srow("EA8E001", "2026-09-01 10:00:00",
                         added="KRF 901 809 R1A : F98BX00001"),
                    srow("EA8E001", "2026-09-05 10:00:00",
                         removed="KRF 901 809 R1A : F98BX00001")],
    }
    assert parents_by_supplier(structures, "filter_unit", "Grentech") == {
        "EA8E001"}


def test_suppliers_by_parent_每台整机装了哪些供应商的件():
    """测试结果列表里要显示每台的供应商，所以需要按整机聚合。

    一台机器换过滤波器、两次来自不同供应商时，两个都要列出来。
    """
    structures = {
        "EA8E001": [srow("EA8E001", "2026-09-01 10:00:00",
                         added="KRF 901 809 R1A : F98BX00001,"
                               "KRE 105 518 R1C : DS4W0270J6"),
                    srow("EA8E001", "2026-09-05 10:00:00",
                         added="KRF 901 809 R1A : DK4G5A0001")],
        "EA8E002": [srow("EA8E002", "2026-09-02 10:00:00",
                         added="KRF 901 809 R1A : F98BX00002")],
    }
    got = suppliers_by_parent(structures)
    assert got["EA8E001"]["filter_unit"] == ["Chunxing", "Grentech"]  # 排序稳定
    assert got["EA8E001"]["antenna"] == ["Comba"]
    assert got["EA8E002"]["filter_unit"] == ["Grentech"]
    assert got["EA8E002"]["antenna"] == []


def test_suppliers_by_parent_没有结构数据返回空():
    assert suppliers_by_parent({}) == {}


# ---------- 解析整行结构 ----------

def test_parse_structure_装入事件():
    rows = [srow("EA8E538914", "2026-09-14 10:43:32",
                 added="KRE 105 518 R1C : DS4W0270J6")]
    evs = parse_structure(rows)
    assert len(evs) == 1
    e = evs[0]
    assert e["parent_sn"] == "EA8E538914"
    assert e["action"] == "add"
    assert e["child_sn"] == "DS4W0270J6"
    assert e["child_product"] == "KRE 105 518 R1C"
    assert e["part_type"] == "antenna"
    assert e["time"] == "2026-09-14 10:43:32"


def test_parse_structure_移除事件():
    rows = [srow("EA8E538914", "2026-09-14 12:03:12",
                 removed="KRE 105 518 R1C : DS4W0270J6")]
    evs = parse_structure(rows)
    assert evs[0]["action"] == "remove"


def test_parse_structure_同一行既有装又有移():
    rows = [srow("EA8E538914", "2026-09-14 12:00:00",
                 added="KRE 105 518 R1C : DS4W0270AA",
                 removed="KRE 105 518 R1C : DS4W0270J6")]
    evs = parse_structure(rows)
    acts = {(e["action"], e["child_sn"]) for e in evs}
    assert acts == {("add", "DS4W0270AA"), ("remove", "DS4W0270J6")}


def test_parse_structure_一行装入多个子件():
    rows = [srow("EA8E538914", "2026-09-14 07:44:09",
                 added="KRF 901 809 R1A : F98BX0025B,KRF 901 809 R1A : F98BX0025C")]
    evs = parse_structure(rows)
    assert len(evs) == 2
    assert all(e["part_type"] == "filter_unit" for e in evs)


def test_parse_structure_按时间排序():
    rows = [
        srow("EA8E538914", "2026-09-14 12:03:12", removed="KRE 105 518 R1C : DS4W0270J6"),
        srow("EA8E538914", "2026-09-14 10:43:32", added="KRE 105 518 R1C : DS4W0270J6"),
    ]
    evs = parse_structure(rows)
    assert [e["action"] for e in evs] == ["add", "remove"]


# ---------- 反查索引 ----------

def test_build_index_装入后移除配成时间段():
    """接口只支持父件查子件，所以反查必须靠本地索引。"""
    structures = {
        "EA8E538914": [
            srow("EA8E538914", "2026-09-14 10:43:32", added="KRE 105 518 R1C : DS4W0270J6"),
            srow("EA8E538914", "2026-09-14 12:03:12", removed="KRE 105 518 R1C : DS4W0270J6"),
        ]
    }
    idx = build_index(structures)
    spans = idx["DS4W0270J6"]
    assert len(spans) == 1
    assert spans[0]["parent_sn"] == "EA8E538914"
    assert spans[0]["installed_at"] == "2026-09-14 10:43:32"
    assert spans[0]["removed_at"] == "2026-09-14 12:03:12"


def test_build_index_只装没拆则removed_at为None():
    """还装在机器上的件，removed_at 是 None，表示"至今仍在"。"""
    structures = {
        "EA8E538914": [
            srow("EA8E538914", "2026-09-14 10:43:32", added="KRE 105 518 R1C : DS4W0270J6"),
        ]
    }
    idx = build_index(structures)
    assert idx["DS4W0270J6"][0]["removed_at"] is None


def test_build_index_同一天线换过两台机器():
    """这是追踪天线的核心场景：拆下来装到另一台上。"""
    structures = {
        "EA8E538914": [
            srow("EA8E538914", "2026-09-14 10:00:00", added="KRE 105 518 R1C : DS4W0270J6"),
            srow("EA8E538914", "2026-09-14 12:00:00", removed="KRE 105 518 R1C : DS4W0270J6"),
        ],
        "EA8E547590": [
            srow("EA8E547590", "2026-09-15 09:00:00", added="KRE 105 518 R1C : DS4W0270J6",
                 product="KRD 901 330/11 R1A"),
        ],
    }
    idx = build_index(structures)
    spans = sorted(idx["DS4W0270J6"], key=lambda s: s["installed_at"])
    assert len(spans) == 2
    assert spans[0]["parent_sn"] == "EA8E538914"
    assert spans[0]["removed_at"] == "2026-09-14 12:00:00"
    assert spans[1]["parent_sn"] == "EA8E547590"
    assert spans[1]["removed_at"] is None


def test_build_index_按部件类型可过滤():
    structures = {
        "EA8E538914": [
            srow("EA8E538914", "2026-09-14 07:00:00",
                 added="KRF 901 809 R1A : F98BX0025B,KRE 105 518 R1C : DS4W0270J6"),
        ]
    }
    idx = build_index(structures, part_types={"antenna"})
    assert "DS4W0270J6" in idx
    assert "F98BX0025B" not in idx


def test_build_index_空输入():
    assert build_index({}) == {}


# ---------- 查"某时刻装在哪" ----------

def test_where_installed_时间落在区间内():
    idx = {
        "DS4W0270J6": [
            {"parent_sn": "EA8E538914", "installed_at": "2026-09-14 10:00:00",
             "removed_at": "2026-09-14 12:00:00", "child_product": "KRE 105 518 R1C"},
            {"parent_sn": "EA8E547590", "installed_at": "2026-09-15 09:00:00",
             "removed_at": None, "child_product": "KRE 105 518 R1C"},
        ]
    }
    assert where_installed(idx, "DS4W0270J6", "2026-09-14 11:00:00") == "EA8E538914"
    assert where_installed(idx, "DS4W0270J6", "2026-09-16 00:00:00") == "EA8E547590"


def test_where_installed_时间在所有区间之外():
    idx = {
        "DS4W0270J6": [
            {"parent_sn": "EA8E538914", "installed_at": "2026-09-14 10:00:00",
             "removed_at": "2026-09-14 12:00:00", "child_product": "K"},
        ]
    }
    assert where_installed(idx, "DS4W0270J6", "2026-09-13 00:00:00") is None
    assert where_installed(idx, "DS4W0270J6", "2026-09-20 00:00:00") is None


def test_where_installed_不传时间取最后一次():
    idx = {
        "X": [
            {"parent_sn": "P1", "installed_at": "2026-09-14 10:00:00",
             "removed_at": "2026-09-14 12:00:00", "child_product": "K"},
            {"parent_sn": "P2", "installed_at": "2026-09-15 09:00:00",
             "removed_at": None, "child_product": "K"},
        ]
    }
    assert where_installed(idx, "X") == "P2"


def test_where_installed_未知序列号():
    assert where_installed({}, "不存在") is None
