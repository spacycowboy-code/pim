"""装配结构解析与"下层序列号"反查。

MI 的 SerialNumberStructure 报表返回一台整机的装卸历史，6 列：
    Factory Code | Product Number | Serial Number | Modified Time
    | Newly Added Serial Number | Removed Serial Number

最后两列写着这次操作装进/拆出了哪些子件，格式是
    "产品号 R-state : 序列号"，多个用逗号分隔
例：
    KRF 901 809 R1A : F98BX0025B,KRF 901 809 R1A : F98BX0025C,KRE 105 518 R1C : DS4W0270J6

**关键限制**：这个接口只支持用父件（整机）序列号查，
用子件序列号（天线、滤波单元）去查会返回空。
所以"这根天线装过哪些整机"必须先把父件结构批量拉下来，在本地建反查索引。
好处是索引建好后反查是查内存，比调接口快得多。
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable, Sequence

# 结构报表的 6 列
S_FACTORY, S_PRODUCT, S_SN, S_TIME, S_ADDED, S_REMOVED = 0, 1, 2, 3, 4, 5

# 产品号前缀 -> 部件类型。产品号形如 "KRE 105 518 R1C"，取前三个字母判断。
KNOWN_PART_TYPES: dict[str, str] = {
    "KRD": "radio",         # 整机（无线单元）
    "KRC": "radio",         # 整机（另一个产品号前缀）
    "KRE": "antenna",       # 天线
    "KRF": "filter_unit",   # FU 滤波单元（一个管两个分支，一台整机 16 个）
}

# "产品号 : 序列号"。产品号可能含空格和斜杠（如 ROX 101 42/11 R1A），
# 所以用" : "作为分隔标志，而不是按空格切。
RE_PART = re.compile(r"^\s*(?P<product>.+?)\s*:\s*(?P<sn>\S+)\s*$")


def parse_parts(cell: Any) -> list[tuple[str, str]]:
    """解析装卸单元格，返回 [(产品号, 序列号), ...]。

    格式不对的片段直接丢掉，不让整行解析失败。
    """
    if not cell:
        return []
    text = str(cell).strip()
    if not text:
        return []
    out: list[tuple[str, str]] = []
    for chunk in text.split(","):
        m = RE_PART.match(chunk)
        if m:
            out.append((m.group("product").strip(), m.group("sn").strip()))
    return out


def classify_part(product: str, serial: str = "") -> str:
    """根据产品号前缀判断这是什么部件。认不出来就返回 'other'。"""
    prefix = (product or "").strip()[:3].upper()
    return KNOWN_PART_TYPES.get(prefix, "other")


def parse_structure(rows: Iterable[Sequence[Any]]) -> list[dict]:
    """把结构报表的行数组拆成一条条装卸事件，按时间升序。

    每条事件::

        {parent_sn, parent_product, factory, time,
         action: 'add' | 'remove',
         child_product, child_sn, part_type}
    """
    events: list[dict] = []
    for r in rows:
        parent_sn = str(r[S_SN] or "")
        base = {
            "parent_sn": parent_sn,
            "parent_product": str(r[S_PRODUCT] or ""),
            "factory": str(r[S_FACTORY] or ""),
            "time": str(r[S_TIME] or ""),
        }
        for action, col in (("add", S_ADDED), ("remove", S_REMOVED)):
            for product, sn in parse_parts(r[col] if col < len(r) else None):
                events.append({**base, "action": action,
                               "child_product": product, "child_sn": sn,
                               "part_type": classify_part(product, sn)})
    events.sort(key=lambda e: (e["time"], e["action"] == "remove"))
    return events


def build_index(structures: dict[str, Sequence[Sequence[Any]]],
                part_types: set[str] | None = None,
                ) -> dict[str, list[dict]]:
    """建"子件序列号 -> 它装过哪些整机"的反查索引。

    structures  {整机序列号: 结构报表行数组}，来自 MiClient.structures()
    part_types  只索引指定类型，如 {"antenna"} 或 {"antenna","filter_unit"}；
                None 表示全要

    返回 {子件序列号: [{parent_sn, parent_product, child_product,
                        installed_at, removed_at}, ...]}
    removed_at 为 None 表示"装上后没记录拆下，应该还在机器上"。

    装入和移除按时间先后配对。同一个件在同一台机器上装拆多次会配成多段。
    """
    # 先把所有事件按 (子件, 父件) 归拢，再按时间配对
    per_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for parent_sn, rows in structures.items():
        for e in parse_structure(rows):
            if part_types and e["part_type"] not in part_types:
                continue
            per_pair[(e["child_sn"], e["parent_sn"])].append(e)

    idx: dict[str, list[dict]] = defaultdict(list)
    for (child_sn, parent_sn), evs in per_pair.items():
        evs.sort(key=lambda e: (e["time"], e["action"] == "remove"))
        open_span: dict | None = None
        for e in evs:
            if e["action"] == "add":
                if open_span is not None:      # 连续两次装入：先收尾前一段
                    idx[child_sn].append(open_span)
                open_span = {
                    "parent_sn": parent_sn,
                    "parent_product": e["parent_product"],
                    "child_product": e["child_product"],
                    "part_type": e["part_type"],
                    "installed_at": e["time"],
                    "removed_at": None,
                }
            else:                               # remove
                if open_span is not None:
                    open_span["removed_at"] = e["time"]
                    idx[child_sn].append(open_span)
                    open_span = None
                else:
                    # 只看到拆、没看到装（拉取的时间窗口之前就装上了）
                    idx[child_sn].append({
                        "parent_sn": parent_sn,
                        "parent_product": e["parent_product"],
                        "child_product": e["child_product"],
                        "part_type": e["part_type"],
                        "installed_at": None,
                        "removed_at": e["time"],
                    })
        if open_span is not None:
            idx[child_sn].append(open_span)

    return dict(idx)


def where_installed(index: dict[str, list[dict]], child_sn: str,
                    at_time: str | None = None) -> str | None:
    """查某个子件在某个时刻装在哪台整机上。

    at_time 不传就取最后一次装配（也就是"现在在哪"）。
    时间用字符串比较，因为 MI 的时间格式 'YYYY-MM-DD HH:MM:SS' 恰好可以直接比大小。
    """
    spans = index.get(child_sn)
    if not spans:
        return None

    if at_time is None:
        latest = max(spans, key=lambda s: s["installed_at"] or "")
        return latest["parent_sn"]

    for s in spans:
        ins, rem = s["installed_at"], s["removed_at"]
        if ins is not None and at_time < ins:
            continue
        if rem is not None and at_time > rem:
            continue
        return s["parent_sn"]
    return None


def children_of(rows: Sequence[Sequence[Any]],
                part_types: set[str] | None = None) -> list[dict]:
    """列出一台整机当前装着哪些子件（装了但没记录拆下的）。

    用于"给整机序列号，看它的下层件"这个方向的查询。
    """
    idx = build_index({"_": rows}, part_types=part_types)
    out = []
    for child_sn, spans in idx.items():
        for s in spans:
            if s["removed_at"] is None:
                out.append({"child_sn": child_sn, **s})
    out.sort(key=lambda x: (x["part_type"], x["child_sn"]))
    return out


def summarize_parts(rows: Sequence[Sequence[Any]]) -> dict[str, int]:
    """统计一台整机装了各类子件各几个。用于快速看装配完整性。"""
    counts: dict[str, int] = defaultdict(int)
    for c in children_of(rows):
        counts[c["part_type"]] += 1
    return dict(counts)


# ---------------------------------------------------------------------------
# 供应商
#
# 权威来源是 MI 的 UnitProductStructure 报表的 `Mfg. Unit` 列（不是元件追溯
# ComponentTraceability* 那一族——那些只覆盖电子板 ROA/ROR，机械件不在里面）。
#
# 实测 2026-02~09 共 628 台整机、9041 行装配明细：
#     序列号前 3 位 == Mfg.Unit 代码，前 5 位与供应商严格一对一（0 个一对多）
#
# 所以这里不用再去请求那个报表，直接按序列号前缀查表即可，省一次网络往返。
# 新增供应商时往 SUPPLIER_NAMES 加一行。
#
# ⚠️ 这条规则只对外购机械件成立。厂内板卡 ROA/ROX 的 Mfg.Unit 是
#    CB4-ESS Nanjing，序列号却是 EA8E*（前 3 位对不上），而且同一家下有 7 种
#    前缀——那是流水号段不是供应商码。别把这条规则推广到板卡。
# ---------------------------------------------------------------------------

SUPPLIER_PREFIX_LEN = 5
MFG_CODE_LEN = 3

# 序列号前 3 位 -> 供应商公司名。
# 产地不算供应商（用户定的）：春兴的苏州厂 DK4 和深圳厂 EC1 是同一家，
# 所以两个代码映射到同一个名字，界面上只出现一个 Chunxing 选项。
SUPPLIER_NAMES = {
    "F98": "Grentech",      # F98-Grentech NanNing，滤波器
    "DK4": "Chunxing",      # DK4-Chunxing Suzhou，滤波器
    "EC1": "Chunxing",      # EC1-Chunxing Shenzhen，滤波器（同一家，另一个厂）
    "DS4": "Comba",         # DS4-Comba Guangzhou，天线
    "EK6": "AMPHENOL",      # EK6-AMPHENOL AIRWAVE，天线
}

# 同一批前缀各属于哪类件。和 SUPPLIER_NAMES 的键必须一一对应
# （有测试锁着），漏一个那家供应商就不会出现在界面的下拉框里。
#
# 只有天线和滤波器两类：它们是外采机械件，才有供应商之分。
# 厂内板卡（ROA/ROX）的序列号前缀是流水号段、不是供应商码，
# 「前 3 位 == Mfg.Unit」这条规则对它们不成立，别加进来。
SUPPLIER_PART_TYPES = {
    "F98": "filter_unit",
    "DK4": "filter_unit",
    "EC1": "filter_unit",
    "DS4": "antenna",
    "EK6": "antenna",
}

# 各类件的产品号，界面上显示"这家供应商供的是哪个件"。
PART_PRODUCTS = {
    "antenna": "KRE 105 518",
    "filter_unit": "KRF 901 809",
}


def known_suppliers() -> dict[str, list[dict]]:
    """列出各类件有哪些供应商可选。**不查 MI，瞬间返回。**

    为什么不从实际装配数据统计（那是原来的做法）：
      1. 要拉全部整机的装配结构，实测 80 台 8 路并行 9.5 分钟。
         这个接口是页面一打开就调的，等不了。
      2. 只列出"实际数据里出现过的"供应商，会漏掉那些机器恰好不在
         当前查询范围内的供应商 —— 用户于是没法筛它，等于悄悄少了选项。

    供应商靠子件序列号前 3 位识别，映射表就在本模块里（已实测验证
    「序列号前 3 位 == MI 的 Mfg.Unit 代码」且前缀与供应商严格一对一），
    所以不需要任何网络请求。

    `n_parents` 恒为 None，表示"没统计台数"。要台数得另外拉装配结构，
    见 supplier_options()。
    """
    out: dict[str, list[dict]] = {"antenna": [], "filter_unit": []}
    seen: set[tuple[str, str]] = set()
    for code, name in SUPPLIER_NAMES.items():
        pt = SUPPLIER_PART_TYPES.get(code)
        if pt not in out:
            continue
        if (pt, name) in seen:
            continue               # 同一家的多个产地只出现一次
        seen.add((pt, name))
        out[pt].append({
            "supplier": name,
            "child_product": PART_PRODUCTS.get(pt, ""),
            "n_children": None,
            "n_parents": None,     # None = 没统计，不是 0 台
        })
    for pt in out:
        out[pt].sort(key=lambda x: x["supplier"])
    return out


def supplier_of(serial: str | None) -> str:
    """从子件序列号得到供应商公司名。

    认不出的前缀退回序列号前 5 位，而不是返回空或报错——出现新供应商时
    界面上照样能把它当一个选项筛，只是显示成前缀，不会悄悄丢数据。
    序列号比 5 位短就原样返回，空值返回空串，因为结构报表里偶尔有残缺数据，
    不该让整批解析失败。
    """
    s = (serial or "").strip()
    if not s:
        return ""
    return SUPPLIER_NAMES.get(s[:MFG_CODE_LEN], s[:SUPPLIER_PREFIX_LEN])


def supplier_options(structures: dict[str, Sequence[Sequence[Any]]],
                     ) -> dict[str, list[dict]]:
    """列出各类子件有哪些供应商前缀，按涉及整机数降序（影响面大的在前）。

    返回 {part_type: [{supplier, child_product, n_children, n_parents}, ...]}
    只包含 antenna 和 filter_unit——这两类是外采件，才有供应商之分。
    """
    # (part_type, supplier) -> {"children": set, "parents": set, "product": str}
    acc: dict[tuple[str, str], dict] = {}
    for parent_sn, rows in structures.items():
        for e in parse_structure(rows):
            pt = e["part_type"]
            if pt not in ("antenna", "filter_unit"):
                continue
            key = (pt, supplier_of(e["child_sn"]))
            if not key[1]:
                continue
            slot = acc.setdefault(key, {"children": set(), "parents": set(),
                                        "product": e["child_product"]})
            slot["children"].add(e["child_sn"])
            slot["parents"].add(e["parent_sn"])

    out: dict[str, list[dict]] = {"antenna": [], "filter_unit": []}
    for (pt, sup), slot in acc.items():
        out[pt].append({
            "supplier": sup,
            "child_product": slot["product"],
            "n_children": len(slot["children"]),
            "n_parents": len(slot["parents"]),
        })
    for pt in out:
        out[pt].sort(key=lambda x: (-x["n_parents"], -x["n_children"],
                                    x["supplier"]))
    return out


def parents_by_supplier(structures: dict[str, Sequence[Sequence[Any]]],
                        part_type: str, supplier: str) -> set[str]:
    """反查：装过这个供应商的这类子件的整机序列号集合。

    装上后又拆下的也算——那段时间的测试数据确实属于这个供应商。
    没有匹配就返回空集合，不抛异常。
    """
    want = (supplier or "").strip()
    if not want:
        return set()
    out: set[str] = set()
    for parent_sn, rows in structures.items():
        for e in parse_structure(rows):
            if e["part_type"] == part_type and supplier_of(e["child_sn"]) == want:
                out.add(e["parent_sn"])
                break
    return out


def suppliers_by_parent(structures: dict[str, Sequence[Sequence[Any]]],
                        ) -> dict[str, dict[str, list[str]]]:
    """每台整机装过哪些供应商的天线和滤波器。

    返回 {整机SN: {"antenna": [前缀...], "filter_unit": [前缀...]}}，
    前缀去重并排序（换过件的机器会有多个供应商，都要列出来）。
    测试结果列表用它显示供应商列。
    """
    out: dict[str, dict[str, set[str]]] = {}
    for parent_sn, rows in structures.items():
        slot = out.setdefault(parent_sn, {"antenna": set(), "filter_unit": set()})
        for e in parse_structure(rows):
            pt = e["part_type"]
            if pt not in ("antenna", "filter_unit"):
                continue
            sup = supplier_of(e["child_sn"])
            if sup:
                slot[pt].add(sup)
    return {sn: {k: sorted(v) for k, v in d.items()} for sn, d in out.items()}
