"""
从 Comba ANT PIM 追踪表里提取结构化数据。

输出 data/serials.json，包含：
  radios[]      : 整机记录（序列号 + 各环节问题描述 + 处理动作）
  antennas[]    : 天线记录（序列号 + 各轮次装在哪台整机上 + 结果描述）
  fail_list[]   : 失效天线清单
  summary       : 汇总统计数字
  fu_events[]   : 所有 FU（滤波单元）更换事件，含关联的 BR 分支号
"""
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from openpyxl import load_workbook

SRC = r"C:\Users\enanshn\OneDrive - Ericsson\Downloads\Copy of Comba ANT Remove Screen 2nd 0914.xlsx"
OUT = Path(__file__).parent / "data" / "serials.json"

# 序列号的正则：整机 EA8E...，天线 DS4W...，滤波单元 F98BX...
RE_RADIO = re.compile(r"\bEA8[A-Z]\d{6}\b")
RE_ANT = re.compile(r"\bDS4W[A-Z0-9]{6}\b")
RE_FU = re.compile(r"\bF98BX[A-Z0-9]{5}\b")
# BR 分支号：br7 / BR31 / br19/18 这类写法
RE_BR = re.compile(r"\bBR\s*(\d{1,2})\b", re.IGNORECASE)


def cell(v):
    """单元格转成干净字符串，空值返回空串。"""
    if v is None:
        return ""
    return str(v).strip()


def open_copy(path):
    """OneDrive 同步时原文件会被锁，先复制一份再读。"""
    tmpdir = tempfile.mkdtemp(prefix="pimx_")
    dst = os.path.join(tmpdir, "book.xlsx")
    shutil.copy2(path, dst)
    return dst, tmpdir


def main():
    if not os.path.exists(SRC):
        print(f"找不到源文件: {SRC}", file=sys.stderr)
        return 1

    tmp, tmpdir = open_copy(SRC)
    try:
        wb = load_workbook(tmp, data_only=True, read_only=True)
        sheets = {ws.title.strip(): ws for ws in wb.worksheets}

        # ---------- 表1: 整机测试问题日志 ----------
        ws1 = next(w for name, w in sheets.items() if "issue" in name.lower())
        rows1 = [[cell(c) for c in r] for r in ws1.iter_rows(values_only=True)]
        hdr1 = rows1[0]
        # 列含义：0=Time 1=SN，之后是 (问题, 动作) 成对出现
        stage_cols = [
            ("OE_1", 2, 3),
            ("KRC_PIM_1", 4, 5),
            ("KRD_PIM_1", 6, 7),
            ("GBI", 8, None),
            ("After_GBI", 9, 10),
            ("OE_2", 11, 12),
            ("KRC_PIM_2", 13, 14),
            ("MOB", 15, 16),
        ]
        radios, cur_date = [], ""
        for r in rows1[1:]:
            if not any(x for x in r):
                continue
            if len(r) > 0 and r[0]:
                cur_date = r[0][:10]
            sn = r[1] if len(r) > 1 else ""
            if not RE_RADIO.fullmatch(sn):
                continue
            rec = {"sn": sn, "date": cur_date, "stages": {}}
            for name, ci, ai in stage_cols:
                issue = r[ci] if len(r) > ci else ""
                action = r[ai] if (ai is not None and len(r) > ai) else ""
                if issue or action:
                    rec["stages"][name] = {"issue": issue, "action": action}
            radios.append(rec)

        # ---------- 表2: 天线跨轮次追踪 ----------
        ws2 = sheets["NFF ANT status"]
        rows2 = [[cell(c) for c in r] for r in ws2.iter_rows(values_only=True)]
        # 第1行是轮次名（跨两列合并），第2行是 NO./discribe
        round_names = []
        for ci in range(2, len(rows2[0])):
            nm = rows2[0][ci] if ci < len(rows2[0]) else ""
            if nm:
                round_names.append((nm, ci))
        antennas = []
        for r in rows2[2:]:
            if not any(x for x in r):
                continue
            ant = r[0] if len(r) > 0 else ""
            m = RE_ANT.search(ant)
            if not m:
                continue
            rec = {
                "sn": m.group(0),
                "sn_raw": ant,
                "pim_screen": r[1] if len(r) > 1 else "",
                "rounds": [],
                "conclusion": " | ".join(x for x in r[16:] if x) if len(r) > 16 else "",
            }
            for nm, ci in round_names:
                radio = r[ci] if len(r) > ci else ""
                desc = r[ci + 1] if len(r) > ci + 1 else ""
                if radio or desc:
                    rm = RE_RADIO.search(radio)
                    rec["rounds"].append({
                        "round": nm,
                        "radio_sn": rm.group(0) if rm else radio,
                        "radio_raw": radio,
                        "result": desc,
                    })
            antennas.append(rec)

        # ---------- 表3: 失效汇总 ----------
        ws3 = sheets["ANT fail status"]
        rows3 = [[cell(c) for c in r] for r in ws3.iter_rows(values_only=True)]
        summary = {}
        if len(rows3) >= 2:
            keys, vals = rows3[0], rows3[1]
            for i, k in enumerate(keys):
                v = vals[i] if i < len(vals) else ""
                if k or v:
                    summary[k if k else f"_col{i}"] = v
        fail_list = []
        for r in rows3[2:]:
            for x in r:
                m = RE_ANT.search(x)
                if m:
                    fail_list.append({"sn": m.group(0), "raw": x.strip()})
                    break

        # ---------- FU 更换事件（从表1所有文本里挖） ----------
        fu_events = []
        for rec in radios:
            for stage, d in rec["stages"].items():
                text = f"{d['issue']} {d['action']}"
                fus = RE_FU.findall(text)
                if not fus:
                    continue
                brs = sorted({int(x) for x in RE_BR.findall(text)})
                fu_events.append({
                    "radio_sn": rec["sn"],
                    "date": rec["date"],
                    "stage": stage,
                    "fu_sns": sorted(set(fus)),
                    "branches": brs,
                    "text": text.strip()[:300],
                })

        # ---------- 汇总所有序列号 ----------
        all_radio = sorted({r["sn"] for r in radios} |
                           {rd["radio_sn"] for a in antennas for rd in a["rounds"]
                            if RE_RADIO.fullmatch(rd["radio_sn"])})
        all_ant = sorted({a["sn"] for a in antennas} | {f["sn"] for f in fail_list})

        out = {
            "source": SRC,
            "radios": radios,
            "antennas": antennas,
            "fail_list": fail_list,
            "summary": summary,
            "fu_events": fu_events,
            "all_radio_sn": all_radio,
            "all_antenna_sn": all_ant,
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"整机记录       : {len(radios)}")
        print(f"天线记录       : {len(antennas)}")
        print(f"失效天线清单   : {len(fail_list)}")
        print(f"FU 更换事件    : {len(fu_events)}")
        print(f"去重整机序列号 : {len(all_radio)}")
        print(f"去重天线序列号 : {len(all_ant)}")
        print(f"汇总统计       : {summary}")
        print(f"\n已写出 -> {OUT}")
        wb.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
