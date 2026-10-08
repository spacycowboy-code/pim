"""
MI 测试点数据解析库。

核心：把测试点编码 5.2.<a>.<b>.<c> 解析成结构化字段
  a = 测量项类型（1-4, 91-106）
  b = 分支号 Branch (0-31)
  c = 频率索引 (0-5)，对应 6 个测试频点

频率映射从数据里带 "DVT:" 前缀的行自动推断（那些行的名称里写了 "xxxx MHz"）。
"""
import gzip
import json
import re
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).parent / "data"
RAW = DATA / "raw"

# 测试点编码：可能带 DVT: 前缀（那是限值定义行，实测值为空）
RE_TPC = re.compile(r"^(DVT:)?5\.2\.(\d+)\.(\d+)\.(\d+)$")
# 从测试点名称里抽频率，如 "... : 1896 MHz"
RE_MHZ = re.compile(r"([\d.]+)\s*MH", re.IGNORECASE)


def load_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def num(x):
    """安全转 float，失败返回 None。"""
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() in ("null", "[null]", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_test_points(sn):
    """读一个序列号的 TestPointSummary，返回 (列名列表, 数据行列表)。"""
    p = RAW / "TestPointSummary" / f"{sn}.json.gz"
    if not p.exists():
        return None, None
    d = load_gz(p)
    cols = [c["name"] for c in d["data"]["columns"]]
    return cols, d["data"]["data"]


def load_events(sn):
    """读一个序列号的 SerialNumberLog，返回 (产品号, 列名, 事件行)。"""
    p = RAW / "SerialNumberLog" / f"{sn}.json.gz"
    if not p.exists():
        return None, None, None
    d = load_gz(p)
    trs = d["data"]["tableResultSet"]
    cols = [c["name"] for c in trs["columns"]]
    rows = trs.get("data") or trs.get("rows") or []
    # 事件名带 HTML 标签，清掉
    clean = []
    for r in rows:
        r = list(r)
        for i, v in enumerate(r):
            if isinstance(v, str):
                r[i] = re.sub(r"<[^>]*>", "", v).strip()
        clean.append(r)
    return d["data"].get("product"), cols, clean


def build_freq_map(cols, rows):
    """从 DVT: 行推断 频率索引 -> 频率(MHz) 的映射。"""
    ix = {n: i for i, n in enumerate(cols)}
    fmap = {}
    for r in rows:
        code = str(r[ix["Test Point Code"]])
        m = RE_TPC.match(code)
        if not m or not m.group(1):        # 只看 DVT: 行
            continue
        c = int(m.group(4))
        mm = RE_MHZ.search(str(r[ix["Test Point Name"]]))
        if mm and c not in fmap:
            fmap[c] = float(mm.group(1))
    return fmap


def parse_pim(sn):
    """
    解析一个序列号的 PIM 测量数据。

    返回 dict:
      sn, product, freq_map,
      measures[]  : 每条 = {item, branch, freq_idx, freq_mhz, category, measure_name,
                            unit, low_limit, high_limit, lowest, highest, avg,
                            std, cpk, tested, pct_fail}
    """
    cols, rows = load_test_points(sn)
    if cols is None:
        return None
    ix = {n: i for i, n in enumerate(cols)}
    fmap = build_freq_map(cols, rows)

    measures = []
    for r in rows:
        grp = str(r[ix["Test Point Group Name"]])
        if "PIM" not in grp:
            continue
        code = str(r[ix["Test Point Code"]])
        m = RE_TPC.match(code)
        if not m or m.group(1):             # 跳过 DVT: 限值定义行
            continue
        item, branch, fidx = int(m.group(2)), int(m.group(3)), int(m.group(4))
        name = str(r[ix["Test Point Name"]])
        # 去掉前缀 "RX PIM Power " 让名字短一点
        mname = name.replace("RX PIM Power", "").strip().rstrip(":").strip()

        # 频率有两种编法：
        #   A 类 (noise_power_level / air_leakage): 编在测试点编码第 5 段
        #   B 类 (knock_response / knock_impulse):  第 5 段恒为 0，频率写在名称里
        # 所以一律优先从名称解析，取不到才回退到编码。
        mm = RE_MHZ.search(mname)
        freq = float(mm.group(1)) if mm else fmap.get(fidx)

        avg = num(r[ix["Avg Value"]])
        unit = str(r[ix["Unit"]])
        # 判断数值语义：负的大数是功率(dBm)，0~1 是通过率
        if avg is None:
            vkind = "unknown"
        elif avg <= -10:
            vkind = "power"          # dBm
        elif 0.0 <= avg <= 1.0:
            vkind = "passrate"       # 通过比例
        else:
            vkind = "other"

        measures.append({
            "item": item,
            "branch": branch,
            "freq_idx": fidx,
            "freq_mhz": freq,
            "value_kind": vkind,
            "category": str(r[ix["Test Category"]]),
            "criteria": str(r[ix["Test Criteria"]]),
            "measure": mname,
            "unit": unit,
            "low_limit": num(r[ix["Low Limit"]]),
            "high_limit": num(r[ix["High Limit"]]),
            "lowest": num(r[ix["Lowest Value"]]),
            "highest": num(r[ix["Highest Value"]]),
            "avg": avg,
            "std": num(r[ix["Std Dev"]]),
            "cpk": num(r[ix["Cpk"]]),
            "tested": num(r[ix["# Tested"]]),
            "pct_fail": num(r[ix["% Fail"]]) or 0.0,
        })

    product, _, _ = load_events(sn)
    return {"sn": sn, "product": product, "freq_map": fmap, "measures": measures}


def all_failures(sn):
    """返回该序列号所有 %Fail>0 的测试点（不限 PIM），用于总览。"""
    cols, rows = load_test_points(sn)
    if cols is None:
        return []
    ix = {n: i for i, n in enumerate(cols)}
    out = []
    for r in rows:
        pf = num(r[ix["% Fail"]]) or 0.0
        if pf <= 0:
            continue
        code = str(r[ix["Test Point Code"]])
        m = RE_TPC.match(code)
        out.append({
            "sn": sn,
            "code": code,
            "name": str(r[ix["Test Point Name"]]),
            "group": str(r[ix["Test Point Group Name"]]),
            "category": str(r[ix["Test Category"]]),
            "branch": int(m.group(3)) if m and not m.group(1) else None,
            "freq_idx": int(m.group(4)) if m and not m.group(1) else None,
            "unit": str(r[ix["Unit"]]),
            "tested": num(r[ix["# Tested"]]),
            "pct_fail": pf,
            "low_limit": num(r[ix["Low Limit"]]),
            "high_limit": num(r[ix["High Limit"]]),
            "lowest": num(r[ix["Lowest Value"]]),
            "highest": num(r[ix["Highest Value"]]),
            "avg": num(r[ix["Avg Value"]]),
        })
    return out


def fetched_serials():
    """列出已抓取到 TestPointSummary 的序列号。"""
    d = RAW / "TestPointSummary"
    if not d.exists():
        return []
    return sorted(p.name[: -len(".json.gz")] for p in d.glob("*.json.gz"))


def load_serials_json():
    p = DATA / "serials.json"
    return json.loads(p.read_text(encoding="utf-8"))
