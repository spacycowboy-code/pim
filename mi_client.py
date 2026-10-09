"""MI (Manufacturing Intelligence，制造智能) 接口客户端。

MI 使用 Negotiate 认证：Windows 默认复用 SSPI 域身份；Linux 使用
requests-gssapi 和系统 Kerberos ticket cache（见 LINUX.md）。

两种查询模式，用途完全不同：
  聚合模式  多个序列号逗号传（或干脆不填序列号）-> 按测试点聚合的统计。
            80 个序列号只要 28 秒，但**不知道哪一行属于哪台机器**。适合算良率。
  明细模式  一次填一个序列号 -> 那一台的全部测试点。
            单台 28~70 秒，用线程池并行可以压到几分钟。适合看"哪台坏了"。

所有筛选字段名和类型都是从报表页 HTML 的 filter-id / filter-type 属性实测得来，
详见 .kiro/steering/project-context.md。

**不做任何本地缓存。** 每次调用都真的请求 MI。原来这里有一层文件缓存
（data/cache/*.json.gz，按查询条件命名、永不过期），实测因此算出过 4 天前
的良率，而界面上的「刷新」按钮清不掉它，已整个移除。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, Sequence

MI_HOST = "https://apps.mesmi.internal.ericsson.com"

# 测试点编码 5.2.<测量项>.<分支>.<频率索引>，第 2 组就是分支号。
# 刻意**不**匹配 DVT: 前缀 —— 那是限值定义行，不是真实失败。
RE_TPC_CODE = re.compile(r"^5\.2\.(\d+)\.(\d+)\.(\d+)$")

# ---------------------------------------------------------------------------
# 报表字段清单（实测：服务端要求字段齐全，缺的必须显式填 null，否则 500）
# ---------------------------------------------------------------------------

REPORT_FIELDS: dict[str, list[str]] = {
    "TestPointSummary": [
        "Area", "ControlCategory", "Factory", "file_SerialNumber", "FromDate",
        "HighLimit", "IncludeOutliers", "Line", "LowLimit", "MaxValue",
        "MedianBase", "MinValue", "ProductFamily", "ProductGroup",
        "ProductNumber", "ProductRevisionState", "ProductType", "SerialNumber",
        "TestCategory", "TestCount", "TestCriteria", "TestPoint",
        "TestPointGroup", "TestPointResult", "TestProductionComment",
        "TestProgram", "ToDate",
    ],
    "SerialNumberStructure": ["SerialNumber", "file_SerialNumber"],
    "SerialNumberLog": ["SerialNumber", "file_SerialNumber"],
    # 下面三个是「明细」报表，带真实测试时间戳（TestPointSummary 没有）。
    # 共同限制：只能按序列号筛，不能按产品或日期筛，返回该序列号的全部历史。
    # 所以必须逐台拉；好处是一次拉全，之后能在本地按任意日期切分。
    "FunctionalTestAndRepairHistory": ["SerialNumber", "file_SerialNumber"],
    "ChildSerialNumberTestHistory": ["SerialNumber", "file_SerialNumber"],
    # 唯一能按 日期+产品号 列出在产整机序列号/产品号的报表。
    # 测试历史那几个只能按序列号查，所以要"动态列出序列号"必须靠它。
    # 返回是第三种结构 data.m_Item1.resultSet[]（见 m_item1_rows）。
    "UnitProductStructure": [
        "FromDate", "ToDate", "Factory", "ProductGroup", "ProductFamily",
        "ProductType", "ProductNumber", "ProductBulk", "file_ProductBulk",
        "ProductRevisionState", "ControlCategory", "TestCategory",
        "SerialNumber", "file_SerialNumber", "ChildProductNumber",
        "file_ChildProductNumber", "ChildProductRevisionState",
        "ChildSerialNumber", "file_ChildSerialNumber", "TimeFrequency",
    ],
}

# 不填这几个会 500，所以给默认值
REQUIRED_DEFAULTS: dict[str, dict[str, str]] = {
    "TestPointSummary": {
        "TestCount": "[All]",
        "TestPointResult": "[All]",
        "IncludeOutliers": "No",
        "MedianBase": "[All]",
    },
    # TimeFrequency 不填会 500 Index out of range，且不点名缺哪个参数（踩过）。
    # 要明细就填 None（枚举还有 Day/Week/Month）。
    "UnitProductStructure": {"TimeFrequency": "None"},
}

# 这些字段实测支持"逗号分隔多值"（只认逗号，分号/空格/换行会被静默忽略）
MULTI_VALUE_FIELDS = {"SerialNumber", "TestCategory", "TestPoint", "TestProgram"}

# 日期字段
DATE_FIELDS = {"FromDate", "ToDate"}

# 产品号尾部的 R-state（修订状态），MI 不接受，要去掉
RE_RSTATE = re.compile(r"\s+R\d+[A-Z]?$")

# TestPointSummary 返回的 23 列，顺序固定
TPS_COLUMNS = [
    "Test Point Code", "Test Point Name", "Test Point Group Code",
    "Test Point Group Name", "Test Criteria", "Test Area", "Test Line",
    "Control Category", "Test Category", "Unit", "# Tested", "% Fail",
    "Low Limit", "High Limit", "Lowest Value", "Highest Value", "Avg Value",
    "Std Dev", "Cpk", "CpL", "CpU", "Observed DPMO", "Expected DPMO",
]
COL_TESTED = 10
COL_PCT_FAIL = 11


# ---------------------------------------------------------------------------
# 纯函数：组装请求、缓存键。这些不碰网络，方便单独测。
# ---------------------------------------------------------------------------

def join_multi(v: Any) -> str | None:
    """把多值字段规范成逗号分隔的字符串。

    实测只有逗号生效，分号/空格/换行会让服务端**静默忽略筛选**（返回全量），
    所以这里统一收口，不给调用方自己拼的机会。
    """
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    parts = [str(x).strip() for x in v if x is not None and str(x).strip()]
    return ",".join(parts) if parts else None


def fmt_date(v: Any) -> str | None:
    """MI 要 'YYYY-MM-DD HH:MM' 格式。date 对象补 00:00。"""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, dt.date):
        return v.strftime("%Y-%m-%d 00:00")
    return str(v)


def strip_rstate(product: str | None) -> str | None:
    """去掉产品号尾部的 R-state：'KRD 901 330/11 R1A' -> 'KRD 901 330/11'。"""
    if not product:
        return None
    return RE_RSTATE.sub("", product.strip()) or None


def build_filter(report: str, values: dict[str, Any]) -> dict[str, Any]:
    """组装 reportFilter：先把全部字段填 None，再覆盖用户给的值。

    会做三件规范化，免得调用方踩坑：
      1. 多值字段的列表自动用逗号拼（只有逗号生效）
      2. 日期对象自动格式化
      3. 产品号自动去掉尾部 R-state
    写错字段名会直接抛错，而不是静默发给服务端换一个 500。
    """
    if report not in REPORT_FIELDS:
        raise ValueError(f"未知报表 {report!r}，已知：{sorted(REPORT_FIELDS)}")
    fields = REPORT_FIELDS[report]

    for k in values:
        if k not in fields:
            raise ValueError(f"{k!r} 不是 {report} 的字段。可用：{fields}")

    f: dict[str, Any] = {k: None for k in fields}
    f.update(REQUIRED_DEFAULTS.get(report, {}))

    for k, v in values.items():
        if v is None:
            continue
        if k in DATE_FIELDS:
            f[k] = fmt_date(v)
        elif k == "ProductNumber":
            f[k] = strip_rstate(v if isinstance(v, str) else join_multi(v))
        elif k in MULTI_VALUE_FIELDS:
            f[k] = join_multi(v)
        else:
            f[k] = v
    return f


def split_batches(items: Sequence[Any], size: int) -> list[list[Any]]:
    """把列表切成每批至多 size 个。用于逐台明细查询的并行分批。"""
    if size <= 0:
        raise ValueError("size 必须 > 0")
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def rows_of(obj: Any) -> list[list[Any]]:
    """从 MI 响应里安全取出二维数组（结构 A：data.data）。

    必须用这个而不是 `obj["data"]["data"]` 或 `obj.get("data", {}).get("data")`：
    查不到数据时 MI 返回 `{"data": null}`——"data" 键**存在**但值是 None，
    所以 `.get("data", {})` 的默认值用不上，接着 `.get()` 会抛
    AttributeError: 'NoneType' object has no attribute 'get'。
    空结果是正常业务情况，不该让程序崩。
    """
    if not isinstance(obj, dict):
        return []
    d = obj.get("data")
    if not isinstance(d, dict):
        return []
    rows = d.get("data")
    return [r for r in rows if isinstance(r, (list, tuple))] if isinstance(rows, list) else []


def result_set_rows(obj: Any, index: int = 0) -> list[list[Any]]:
    """从 MI 响应里安全取出 resultSet[index] 的行（结构 B）。"""
    if not isinstance(obj, dict):
        return []
    d = obj.get("data")
    if not isinstance(d, dict):
        return []
    rs = d.get("resultSet")
    if not isinstance(rs, list) or len(rs) <= index:
        return []
    t = rs[index]
    if not isinstance(t, dict):
        return []
    rows = t.get("data") or t.get("rows")
    return [r for r in rows if isinstance(r, (list, tuple))] if isinstance(rows, list) else []


def m_item1_rows(obj: Any, result_set_name: str) -> list[list[Any]]:
    """从第三种返回结构 data.m_Item1.resultSet[] 里按表名取行。

    UnitProductStructure 用这个结构（.NET Tuple 序列化成 m_Item1/m_Item2）。
    用 data.data 或 data.resultSet 解析它会误判成"0 行"。
    表名实测有 "Data"（聚合）和 "Data1"（明细）两张。空结果不报错。
    """
    if not isinstance(obj, dict):
        return []
    d = obj.get("data")
    if not isinstance(d, dict):
        return []
    item1 = d.get("m_Item1")
    if not isinstance(item1, dict):
        return []
    rs = item1.get("resultSet")
    if not isinstance(rs, list):
        return []
    for t in rs:
        if isinstance(t, dict) and t.get("resultSetName") == result_set_name:
            rows = t.get("data") or t.get("rows")
            return ([r for r in rows if isinstance(r, (list, tuple))]
                    if isinstance(rows, list) else [])
    return []


def products_from_unit_structure(obj: Any) -> list[str]:
    """从 UnitProductStructure 的聚合表里取出整机产品号（去重排序）。

    聚合表的 "Product Number" 列是整机产品号，"Part Product Number" 是子件，
    别混。汇总行的子件列是 'ALL'，整机产品号本身是真实的，照收即可——
    去重后自然只剩一个。
    """
    rows = m_item1_rows(obj, "Data")
    if not rows:
        return []
    # 列名可能随报表版本变，按名字定位"Product Number"列更稳
    item1 = obj["data"]["m_Item1"]
    for t in item1["resultSet"]:
        if t.get("resultSetName") == "Data":
            names = [c.get("name") for c in t.get("columns", [])]
            break
    else:
        names = []
    try:
        idx = names.index("Product Number")
    except ValueError:
        idx = 2                     # 实测固定在索引 2，取不到列名时兜底
    out = {r[idx].strip() for r in rows
           if len(r) > idx and isinstance(r[idx], str) and r[idx].strip()}
    return sorted(out)


def serials_from_unit_structure(obj: Any) -> list[str]:
    """从 UnitProductStructure 的明细表（Data1）取出整机序列号（去重排序）。

    为什么用这个列出整机：MI 没有"直接列序列号"的报表，但 Data1 每行的
    "ParentSN => ChildSN" 列，=> 左边就是整机（父件）序列号。按日期+产品号
    查这个报表，就能列出该时间段真实在产的全部整机 —— 不依赖任何固定清单。

    格式实测：`EA8E489776 => DK4G5001Y4, DK4G5001Z9`。同一台整机会在多行
    出现（每个子件一行），所以去重。没有 => 或整机号为空的脏行跳过，
    不让一行坏数据带崩整批。
    """
    rows = m_item1_rows(obj, "Data1")
    if not rows:
        return []
    # 按列名定位 "ParentSN => ChildSN"，取不到时兜底用最后一列（实测在末尾）
    item1 = obj["data"]["m_Item1"]
    names = []
    for t in item1["resultSet"]:
        if t.get("resultSetName") == "Data1":
            names = [c.get("name") for c in t.get("columns", [])]
            break
    try:
        idx = names.index("ParentSN => ChildSN")
    except ValueError:
        idx = -1                    # 实测在最后一列
    out: set[str] = set()
    for r in rows:
        if idx == -1:
            cell = r[-1] if r else None
        else:
            cell = r[idx] if len(r) > idx else None
        if not isinstance(cell, str) or "=>" not in cell:
            continue
        parent = cell.split("=>", 1)[0].strip()
        if parent:
            out.add(parent)
    return sorted(out)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

def _data1_cols(obj: Any) -> list:
    """取 Data1 表的列名（取不到返回空列表）。"""
    try:
        for t in obj["data"]["m_Item1"]["resultSet"]:
            if t.get("resultSetName") == "Data1":
                return [c.get("name") for c in t.get("columns", [])]
    except Exception:
        pass
    return []


def parts_index_from_unit_structure(obj: Any) -> dict[str, list[dict]]:
    """从 UnitProductStructure 的 Data1 构建"子件 → 装过的整机"反查索引。

    返回 {子件SN: [{parent_sn, parent_product, child_product, part_type,
                   installed_at, removed_at}, ...]}。

    ★ 和原来走 SerialNumberStructure 的 `structure.build_index` 相比：
    - **零额外网络开销**：和 serials/suppliers 共享同一个 UPS 响应
    - **快 50 倍以上**：整机范围 15 秒 vs 10+ 分钟
    - **代价**：Data1 没有装入/拆除时间列，所以 installed_at/removed_at = None
      （前端已经用 `|| '?'` 兜底）。要精确时间线仍需走慢路径。

    只索引外购机械件（antenna / filter_unit），因为供应商也只在这两种件上
    区分；其他类型（板卡、外壳）走别的追溯链路。
    """
    rows = m_item1_rows(obj, "Data1")
    if not rows:
        return {}
    import structure as _st            # 延迟导入，避免模块级循环依赖

    names = _data1_cols(obj)
    try:
        i_prod = names.index("Product Number")      # 父件产品号
    except ValueError:
        i_prod = 1
    try:
        i_part = names.index("Part Product Number")  # 子件产品号
    except ValueError:
        i_part = 3
    try:
        i_map = names.index("ParentSN => ChildSN")
    except ValueError:
        i_map = -1

    idx: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()   # 同一 (child, parent) 对只记一次
    for r in rows:
        cell = (r[-1] if r else None) if i_map == -1 else (
            r[i_map] if len(r) > i_map else None)
        if not isinstance(cell, str) or "=>" not in cell:
            continue
        parent, childs = cell.split("=>", 1)
        parent = parent.strip()
        if not parent:
            continue
        parent_product = str(r[i_prod]) if len(r) > i_prod else ""
        child_product = str(r[i_part]) if len(r) > i_part else ""
        pt = _st.classify_part(child_product, "")
        if pt not in ("antenna", "filter_unit"):
            continue
        for ch in childs.split(","):
            ch = ch.strip()
            if not ch:
                continue
            key = (ch, parent)
            if key in seen:
                continue
            seen.add(key)
            idx.setdefault(ch, []).append({
                "parent_sn": parent,
                "parent_product": parent_product,
                "child_product": child_product,
                "part_type": pt,
                "installed_at": None,
                "removed_at": None,
            })
    return idx


def suppliers_from_unit_structure(obj: Any) -> dict[str, dict[str, list[str]]]:
    """从 UnitProductStructure 的明细表取出每台整机的天线/滤波器供应商。

    返回 {整机SN: {"antenna": [公司名...], "filter_unit": [公司名...]}}。

    ★ 为什么走这个报表而不是装配结构（SerialNumberStructure）：
    装配结构要**逐台**查，实测单台约 4.5 秒、60 台 270 秒，列表里每行标供应商
    会把查询卡死（踩过）。而 UnitProductStructure **一次请求**就返回该日期范围
    内全部整机的父子装配关系，实测 15 秒拿到 60 台 —— 快 18 倍。而且
    serials_from_unit_structure（列整机）用的是同一个响应，所以供应商是
    **零额外网络成本**。

    供应商靠子件序列号前 3 位识别（已实测验证前 3 位 == MI 的 Mfg.Unit 代码）。
    换过件的机器会有多个供应商，都列出来。
    """
    rows = m_item1_rows(obj, "Data1")
    if not rows:
        return {}
    import structure as _st            # 延迟导入，避免模块级循环依赖

    names = _data1_cols(obj)
    try:
        i_part = names.index("Part Product Number")
    except ValueError:
        i_part = 3                     # 实测固定在索引 3
    try:
        i_map = names.index("ParentSN => ChildSN")
    except ValueError:
        i_map = -1                     # 实测在最后一列

    agg: dict[str, dict[str, set]] = {}
    for r in rows:
        cell = (r[-1] if r else None) if i_map == -1 else (
            r[i_map] if len(r) > i_map else None)
        if not isinstance(cell, str) or "=>" not in cell:
            continue
        parent, childs = cell.split("=>", 1)
        parent = parent.strip()
        if not parent:
            continue
        part = str(r[i_part]) if len(r) > i_part else ""
        pt = _st.classify_part(part, "")
        if pt not in ("antenna", "filter_unit"):
            continue                   # 只有外购机械件才有供应商之分
        slot = agg.setdefault(parent, {"antenna": set(), "filter_unit": set()})
        for ch in childs.split(","):
            sup = _st.supplier_of(ch.strip())
            if sup:
                slot[pt].add(sup)
    return {sn: {k: sorted(v) for k, v in d.items()} for sn, d in agg.items()}


def failed_branches_from_child_history(obj: Any) -> list[int]:
    """从 ChildSerialNumberTestHistory 取出失败的分支号（去重排序）。

    失败测试点编码是 `5.2.<测量项>.<分支>.<频率索引>`，**分支是第 4 段**。
    实测 `5.2.97.3.0` 的测试点名称里写着 `rfPort 3`，和编码第 4 段一致，
    可互相校验。

    跳过 `[N/A]`、空值、`DVT:` 开头的限值定义行、以及非 5.2.x 的编码。
    """
    d = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(d, dict):
        return []
    rs = d.get("resultSet")
    if not isinstance(rs, list) or not rs:
        return []
    t = rs[0]
    if not isinstance(t, dict):
        return []
    names = [c.get("name") for c in (t.get("columns") or [])]
    rows = t.get("rows") or t.get("data") or []
    try:
        idx = names.index("Failed Test Point Code")
    except ValueError:
        return []
    out: set[int] = set()
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) <= idx:
            continue
        v = r[idx]
        if not isinstance(v, str):
            continue
        m = RE_TPC_CODE.match(v.strip())
        if m:
            out.add(int(m.group(2)))
    return sorted(out)


# 从测试点名称里取频率（优先级比编码高）—— 档案"关键正则"里有同款
_RE_MHZ_IN_NAME = re.compile(r"([\d.]+)\s*MH", re.I)

# 频率索引（编码第 5 段）到 MHz 的映射。档案"数据结构要点"记录。
_FREQ_INDEX_TO_MHZ = {0: 1896.0, 1: 1856.0, 2: 1736.0,
                      3: 1776.0, 4: 2502.5, 5: 2562.5}


def failed_testpoints_from_child_history(obj) -> dict:
    """从 ChildSerialNumberTestHistory 按场次分组取失败测试点详情。

    返回 {started: [{code, name, branch, freq_mhz, measured, fault_code}, ...]}。
    其中 started 格式 "YYYY-MM-DD HH:MM:SS"（取前 19 字符，和 /api/history/runs
    返回的 started 一致），一场次里按分支号升序。

    和 failed_branches_from_child_history 用同一数据源（ChildSerialNumberTestHistory），
    所以调一次请求能同时拿分支号和测试点详情，没有额外网络开销。

    测试点编码 `5.2.<测量项>.<分支>.<频率索引>`，分支=第 4 段。
    频率**优先从名称取**（如 "... 1776 MHz rfPort 18"），取不到才用编码第 5 段。
    这是档案里的硬约束：knock 类编码的第 5 段恒为 0，用编码会出错。

    跳过 `[N/A]`、空值、`DVT:` 限值定义行、非 5.2.x 的编码（同 failed_branches
    的过滤规则）。
    """
    d = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(d, dict):
        return {}
    rs = d.get("resultSet")
    if not isinstance(rs, list) or not rs:
        return {}
    t = rs[0]
    if not isinstance(t, dict):
        return {}
    names = [c.get("name") for c in (t.get("columns") or [])]
    rows = t.get("rows") or t.get("data") or []

    def col_idx(col_name):
        try:
            return names.index(col_name)
        except ValueError:
            return -1

    i_code = col_idx("Failed Test Point Code")
    if i_code < 0:
        return {}
    i_name = col_idx("Failed Test Point Name")
    i_start = col_idx("Test Start Date")
    i_meas = col_idx("Measured Value")
    i_fault = col_idx("Fault Code")

    # 按场次分组：{started: [tp...]}
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) <= i_code:
            continue
        code = r[i_code]
        if not isinstance(code, str):
            continue
        m = RE_TPC_CODE.match(code.strip())
        if not m:
            continue
        branch = int(m.group(2))
        freq_idx = int(m.group(3))

        name = r[i_name] if i_name >= 0 and len(r) > i_name else ""
        name_s = name if isinstance(name, str) else ""

        # 频率：先从名称取（"... 1776 MHz" 这种），没有再退回编码第 5 段的映射
        freq_mhz = None
        mn = _RE_MHZ_IN_NAME.search(name_s)
        if mn:
            try:
                freq_mhz = float(mn.group(1))
            except ValueError:
                pass
        if freq_mhz is None and freq_idx in _FREQ_INDEX_TO_MHZ:
            freq_mhz = _FREQ_INDEX_TO_MHZ[freq_idx]

        started_raw = r[i_start] if i_start >= 0 and len(r) > i_start else ""
        started = str(started_raw)[:19] if started_raw else ""   # YYYY-MM-DD HH:MM:SS

        measured = r[i_meas] if i_meas >= 0 and len(r) > i_meas else None
        fault = r[i_fault] if i_fault >= 0 and len(r) > i_fault else None

        tp = {
            "code": code.strip(),
            "name": name_s,
            "branch": branch,
            "freq_mhz": freq_mhz,
            "measured": None if measured in (None, "", "[N/A]") else str(measured),
            "fault_code": None if fault in (None, "", "[N/A]") else str(fault),
        }
        buckets.setdefault(started, []).append(tp)

    # 每个场次内按分支号升序，稳定排序
    for k in buckets:
        buckets[k].sort(key=lambda x: (x["branch"], x["code"]))
    return buckets


class MiClient:
    """MI 查询客户端。带磁盘缓存（gzip 压缩）和线程池并行。

    用法::

        c = MiClient()
        d = c.query("TestPointSummary",
                    ProductNumber="KRD 901 330/11",
                    FromDate="2026-08-01 00:00", ToDate="2026-10-01 00:00",
                    TestPointResult="Failed")
        rows = d["data"]["data"]
    """

    def __init__(self, timeout: int = 600, host: str = MI_HOST):
        self.host = host.rstrip("/")
        self.timeout = timeout
        self._session = None

    # -- 认证 ------------------------------------------------------------

    @property
    def session(self):
        """延迟创建平台对应的认证会话，离线计算和测试不加载认证依赖。"""
        if self._session is None:
            import requests
            from mi_auth import create_auth
            s = requests.Session()
            s.auth = create_auth()
            self._session = s
        return self._session

    # -- 查询 ------------------------------------------------------------

    def query(self, report: str, *, _retries: int = 2, **values) -> dict:
        """查一次 MI 报表。**每次都真的发请求**，不读也不写任何本地副本。

        原来这里有一层文件缓存（data/cache/*.json.gz），同样条件直接读文件
        不问 MI，而且永不过期。实测因此算出过 4 天前的良率，而界面上的
        「刷新」按钮清不掉它 —— 用户没有任何办法拿到最新数据。已移除。

        失败会重试（MI 偶发超时很常见），退避 1、2 秒。
        """
        flt = build_filter(report, values)
        url = f"{self.host}/MIReports/api/ReportGetData1/{report}Report"
        body = {"reportFilter": flt, "pageSetting": None}
        last_err: Exception | None = None
        for attempt in range(_retries + 1):
            try:
                r = self.session.post(url, json=body, timeout=self.timeout)
                r.raise_for_status()
                return r.json()
            except Exception as e:      # 超时/5xx/JSON 解析失败都在这
                last_err = e
                if attempt < _retries:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"MI 查询失败 report={report} 条件={values}") from last_err

    # -- 便捷方法 --------------------------------------------------------

    def test_points(self, *, product: str, from_date: Any, to_date: Any,
                    serial_numbers: Iterable[str] | str | None = None,
                    test_category: Iterable[str] | str | None = None,
                    test_point: Iterable[str] | str | None = None,
                    test_count: str = "[All]",
                    only_failed: bool = False) -> list[list[Any]]:
        """查测试点数据，返回行数组（23 列，列名见 TPS_COLUMNS）。

        only_failed=True 会让数据量降约 270 倍（实测 3.4 MB -> 12.5 KB），
        分析失效时强烈建议开着。
        test_count='First' 得到 FPY（First Pass Yield 首次通过率）所需的数据。
        """
        d = self.query(
            "TestPointSummary",
            ProductNumber=product,
            FromDate=from_date,
            ToDate=to_date,
            SerialNumber=serial_numbers,
            TestCategory=test_category,
            TestPoint=test_point,
            TestCount=test_count,
            TestPointResult="Failed" if only_failed else "[All]",
        )
        return rows_of(d)

    def test_points_per_sn(self, serial_numbers: Sequence[str], *, product: str,
                           from_date: Any, to_date: Any, test_count: str = "[All]",
                           only_failed: bool = True, workers: int = 8,
                           progress: Callable[[int, int, str], None] | None = None,
                           ) -> dict[str, list[list[Any]]]:
        """逐台查询（并行），返回 {序列号: 行数组}。

        为什么要逐台？因为批量查回来的是聚合统计，**分不出哪行属于哪台**。
        想画"哪台机器的哪个分支坏了"就必须一台一台查。
        并行 8 路能把 79 台从 1.5 小时压到几分钟。
        """
        out: dict[str, list[list[Any]]] = {}
        total = len(serial_numbers)
        done = 0

        def one(sn: str):
            return sn, self.test_points(
                product=product, from_date=from_date, to_date=to_date,
                serial_numbers=sn, test_count=test_count,
                only_failed=only_failed)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(one, sn): sn for sn in serial_numbers}
            for fut in as_completed(futs):
                sn = futs[fut]
                try:
                    sn, rows = fut.result()
                    out[sn] = rows
                except Exception:
                    out[sn] = []        # 单台失败不拖垮整批
                done += 1
                if progress:
                    progress(done, total, sn)
        return out

    def structure(self, serial_number: str) -> list[list[Any]]:
        """查装配结构（哪些子件在什么时候装进/拆出这台机器）。

        返回 6 列：Factory Code | Product Number | Serial Number |
                  Modified Time | Newly Added Serial Number | Removed Serial Number
        注意：只支持用父件（整机）序列号查，用子件序列号反查会返回空。
        """
        d = self.query("SerialNumberStructure",
                       SerialNumber=serial_number)
        return result_set_rows(d, 0)

    def structures(self, serial_numbers: Sequence[str], *, workers: int = 8,
                   progress: Callable[[int, int, str], None] | None = None,
                   ) -> dict[str, list[list[Any]]]:
        """批量查装配结构（并行）。用于建"子件 -> 父件"的本地反查索引。"""
        out: dict[str, list[list[Any]]] = {}
        total, done = len(serial_numbers), 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(self.structure, sn): sn
                    for sn in serial_numbers}
            for fut in as_completed(futs):
                sn = futs[fut]
                try:
                    out[sn] = fut.result()
                except Exception:
                    out[sn] = []
                done += 1
                if progress:
                    progress(done, total, sn)
        return out

    def unit_structure(self, *, from_date: Any, to_date: Any,
                       product: str | None = None) -> dict:
        """按日期（可选产品号）查 UnitProductStructure，返回原始响应。

        这是唯一能按时间段列出"在产整机产品号/序列号"的报表——测试历史那几个
        只能按序列号查。用 products_from_unit_structure() 等解析器取字段。
        """
        return self.query("UnitProductStructure",
                          FromDate=from_date, ToDate=to_date,
                          ProductNumber=product)

    def products_in_range(self, *, from_date: Any, to_date: Any) -> list[str]:
        """列出某时间段内有生产记录的整机产品号。给前端产品号下拉用。"""
        d = self.unit_structure(from_date=from_date, to_date=to_date)
        return products_from_unit_structure(d)

    def serials_in_range(self, *, from_date: Any, to_date: Any,
                         product: str | None = None) -> list[str]:
        """列出某时间段（可选产品号）内真实在产的整机序列号。

        用来替代固定的 data/serials.json 清单：查询"留空=全部"时，用这个
        实时问 MI 要真实整机列表，产线新产的机器也能查到。
        """
        d = self.unit_structure(from_date=from_date, to_date=to_date,
                                product=product)
        return serials_from_unit_structure(d)

    def child_test_histories(self, serial_numbers: Sequence[str], *,
                             workers: int = 8) -> dict[str, dict]:
        """批量查子件测试历史（并行），返回 {序列号: 原始响应}。

        用来拿失败的测试点编码 → 解析出失败分支。实测单台约 30 秒，
        所以**只对失败的整机查**（通过的不需要）。单台失败不拖垮整批。
        """
        return self.test_histories(serial_numbers, workers=workers, child=True)

    # -- 测试历史（带时间戳的明细）----------------------------------------

    def test_history(self, serial_number: str) -> dict:
        """查一台的功能测试与维修历史，返回**原始响应**。

        为什么返回原始响应而不是行数组：这个报表用的是 MI 的「结构 B」
        （data.resultSet[i] 各自带 columns + rows），而且一次返回两张表
        （测试场次 + 维修记录）。列名在响应里，丢掉就没法安全解析了。
        用 test_history.parse_tables() / to_runs() / to_repairs() 去解析。

        关键价值：这里有 TestStartTime / TestEndTime，而 TestPointSummary 没有。
        拉下来之后就能在本地按任意日期筛选，不用再请求 MI。
        """
        return self.query("FunctionalTestAndRepairHistory",
                          SerialNumber=serial_number)

    def child_test_history(self, serial_number: str) -> dict:
        """查一台的子件测试历史（34 列，含失败测试点编码、实测值、故障码、维修动作）。"""
        return self.query("ChildSerialNumberTestHistory",
                          SerialNumber=serial_number)

    def test_histories(self, serial_numbers: Sequence[str], *, workers: int = 8,
                       child: bool = False,
                       progress: Callable[[int, int, str], None] | None = None,
                       ) -> dict[str, dict]:
        """批量查测试历史（并行），返回 {序列号: 原始响应}。

        实测 8 台并行 8 线程约 4.7 秒，80 台约 50 秒。
        单台失败不拖垮整批（存空字典）。
        """
        fn = self.child_test_history if child else self.test_history
        out: dict[str, dict] = {}
        total, done = len(serial_numbers), 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(fn, sn): sn for sn in serial_numbers}
            for fut in as_completed(futs):
                sn = futs[fut]
                try:
                    out[sn] = fut.result()
                except Exception:
                    out[sn] = {}
                done += 1
                if progress:
                    progress(done, total, sn)
        return out
