"""PIM 测试数据查询工具 —— 本地网页界面。

启动（在项目目录下执行）：

    uv run --with streamlit --with requests --with requests-negotiate-sspi \
           --with matplotlib --with numpy --with pandas streamlit run app.py

然后浏览器会自动打开 http://localhost:8501。

安全说明：这个服务只监听本机，别人访问不到，所以没有加登录。
它用你当前登录的 Windows 域账号去访问 MI，权限和你在 MI 网页上看到的完全一样。
如果要让别人也能用，必须先加身份验证，否则等于把你的数据权限开放给了同网段的人。
"""
from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path

import pandas as pd
import streamlit as st

import plots
import structure as st_mod
import yield_calc as yc
from mi_client import MiClient

st.set_page_config(page_title="PIM 测试数据查询", page_icon="📊", layout="wide")

DEFAULT_PRODUCT = "KRD 901 330/11"
TEST_COUNT_OPTIONS = ["[All]", "First", "Last"] + [str(i) for i in range(2, 11)]


# ---------------------------------------------------------------------------
# 数据获取（带 Streamlit 缓存，同样的条件不会重复打接口）
# ---------------------------------------------------------------------------

@st.cache_resource
def get_client() -> MiClient:
    return MiClient()


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_pair(product: str, from_date: str, to_date: str,
               sns: tuple[str, ...], categories: tuple[str, ...],
               test_count: str):
    """取一对数据：[All] 用来算总测试次数，Failed 用来算失败次数。

    批量查询时 MI 的 %Fail 列是坏的（恒为 0），所以良率必须这样两次请求相除。
    """
    c = get_client()
    kw = dict(product=product, from_date=from_date, to_date=to_date,
              serial_numbers=list(sns) or None,
              test_category=list(categories) or None,
              test_count=test_count)
    all_rows = c.test_points(**kw, only_failed=False)
    fail_rows = c.test_points(**kw, only_failed=True)
    return all_rows, fail_rows


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_single(product: str, from_date: str, to_date: str, sn: str,
                 test_count: str):
    """单台查询。单台的 %Fail 列是可靠的，可以直接用。"""
    return get_client().test_points(
        product=product, from_date=from_date, to_date=to_date,
        serial_numbers=sn, test_count=test_count, only_failed=False)


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_structures(sns: tuple[str, ...]):
    return get_client().structures(list(sns), workers=8)


def parse_sn_input(text: str) -> list[str]:
    """把输入框里的序列号拆出来。逗号、空格、换行、分号都当分隔符。"""
    if not text:
        return []
    for ch in ";, \t\r\n":
        text = text.replace(ch, "\n")
    return [s.strip().upper() for s in text.split("\n") if s.strip()]


def known_serials() -> list[str]:
    """从之前提取的表格里读已知序列号，方便一键填入。"""
    p = Path("data/serials.json")
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        return list(d.get("all_radio_sn") or [])
    except Exception:
        return []


def df_download(df: pd.DataFrame, label: str, fname: str):
    """给表格加一个下载成 CSV 的按钮。用 utf-8-sig 让 Excel 打开不乱码。"""
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    st.download_button(label, buf.getvalue().encode("utf-8-sig"),
                       file_name=fname, mime="text/csv")


# ---------------------------------------------------------------------------
# 侧边栏：查询条件
# ---------------------------------------------------------------------------

st.sidebar.title("查询条件")

today = dt.date.today()
c1, c2 = st.sidebar.columns(2)
from_date = c1.date_input("开始日期", value=today - dt.timedelta(days=60))
to_date = c2.date_input("结束日期", value=today + dt.timedelta(days=1))

product = st.sidebar.text_input(
    "产品号 (KRD 号)", value=DEFAULT_PRODUCT,
    help="不要带尾部的 R-state，程序会自动去掉。例：KRD 901 330/11")

sn_text = st.sidebar.text_area(
    "序列号（可留空）", height=90,
    help="留空 = 查这个产品的全部机器。多个序列号用逗号或换行分隔。")

kn = known_serials()
if kn and st.sidebar.button(f"填入表格里的 {len(kn)} 台整机"):
    st.session_state["sn_prefill"] = ",".join(kn)
    st.rerun()
if "sn_prefill" in st.session_state and not sn_text:
    sn_text = st.session_state["sn_prefill"]
    st.sidebar.caption(f"已填入 {len(parse_sn_input(sn_text))} 个序列号")

category_text = st.sidebar.text_input(
    "测试类别 (case)", value="",
    help="留空 = 全部。PIM 是 212。多个用逗号分隔，例：212,26")

test_count = st.sidebar.selectbox(
    "测试次数", TEST_COUNT_OPTIONS, index=0,
    help="First = FPY 首次通过率（只算第一次测试，反映真实工艺水平）；"
         "Last = 复测后的最终良率；[All] = 所有测试次数合并")

only_pim = st.sidebar.checkbox("只看 PIM 测试点", value=True)

st.sidebar.divider()
go = st.sidebar.button("查询", type="primary", use_container_width=True)

sns = tuple(parse_sn_input(sn_text))
cats = tuple(s.strip() for s in category_text.split(",") if s.strip())

st.sidebar.caption(
    f"条件：{len(sns) or '全部'} 台 · "
    f"{'类别 ' + ','.join(cats) if cats else '全部类别'} · {test_count}")
st.sidebar.caption("每次查询约 30 秒/请求，结果会缓存，重复查询是瞬间的。")


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------

st.title("PIM 测试数据查询")

if go:
    st.session_state["ran"] = True

if not st.session_state.get("ran"):
    st.info(
        "左边填好条件后点**查询**。\n\n"
        "几个用法提示：\n"
        "- **序列号留空** = 查整个产品这段时间的所有机器，一次请求就能出良率\n"
        "- **测试次数选 First** = 得到 FPY（首次通过率），只算第一次测试，"
        "复测修好的不算，这个数字才反映真实工艺水平\n"
        "- **测试类别填 212** = 只看 PIM（已验证 212 就是 PIM 测试类别）\n"
        "- 下面的「下层件追踪」页可以用天线或滤波单元的序列号反查它装过哪些整机")
    st.stop()

fd = f"{from_date:%Y-%m-%d} 00:00"
td = f"{to_date:%Y-%m-%d} 00:00"

with st.spinner("正在查 MI（两次请求，约 30~60 秒；命中缓存会很快）..."):
    try:
        all_rows, fail_rows = fetch_pair(product, fd, td, sns, cats, test_count)
    except Exception as e:
        st.error(f"查询失败：{e}")
        st.stop()

if only_pim:
    all_rows, fail_rows = yc.pim_only(all_rows), yc.pim_only(fail_rows)

if not all_rows:
    st.warning("这个条件下没有数据。检查一下产品号、日期范围，或把「只看 PIM」取消试试。")
    st.stop()

summary = yc.yield_from_pair(all_rows, fail_rows)
freq_map = yc.build_freq_map(all_rows)

# --- 顶部指标卡 ---
# 注意：这里给的是测试点次数级失败率，不是良率。
# 良率只有场次级 FPY 一种口径，在新界面（api.py 的 /api/history/yield）里。
scope = {"First": "只算第一次测试", "Last": "只算最后一次测试"}.get(
    test_count, "全部测试次数")
m = st.columns(5)
m[0].metric(f"测试点失败率（非良率，{scope}）",
            f"{summary['fail_rate_pct']:.4f}%"
            if summary["fail_rate_pct"] is not None else "—")
m[1].metric("总测试次数", f"{summary['total_tested']:,}")
m[2].metric("失败次数", f"{summary['total_failed']:,}")
m[3].metric("测试点", f"{summary['n_test_points']:,}")
m[4].metric("有失败的测试点", f"{summary['n_failed_points']:,}")

st.caption(
    "⚠️ 这一页的分母是「测试点被执行的次数」，不是整机台数，所以它**不是良率**，"
    "只能用来定位问题。良率只按整机的场次级 FPY 算（每台只算第一次测试），"
    "在新界面的「FPY 良率与趋势」里。")

if len(sns) > 1 or not sns:
    st.caption(
        "注：批量查询时 MI 返回的 %Fail 列恒为 0 不可用，"
        "所以失败次数是用两次请求（全部 / 只要失败）的测试次数相除算出来的。"
        "已对照逐台查询的真值验证过，完全一致。")

tabs = st.tabs(["失败分解", "失败明细", "单台详情", "下层件追踪", "原始数据"])


# --- 失败分解 ---
with tabs[0]:
    dim = st.radio("分组维度", ["branch", "freq_mhz", "category", "group", "measure"],
                   format_func=lambda k: {"branch": "分支", "freq_mhz": "频率",
                                          "category": "测试类别", "group": "测试点组",
                                          "measure": "测量项"}[k],
                   horizontal=True)
    groups = yc.yield_by_pair(all_rows, fail_rows, dim, freq_map=freq_map)
    if not groups:
        st.info("没有可分组的数据")
    else:
        fmt = (lambda k: f"BR{k}") if dim == "branch" else (
            (lambda k: f"{k:g}") if dim == "freq_mhz" else (lambda k: str(k)[:26]))
        st.pyplot(plots.fig_fail_rate_bar(
            groups, title=f"按{dim}的失败率（柱子越高越差，柱上数字是失败次数）",
            key_fmt=fmt, sort_by_key=(dim in ("branch", "freq_mhz"))),
            use_container_width=True)

        df = pd.DataFrame([{
            "分组": fmt(g["key"]), "测试次数": g["total_tested"],
            "失败次数": g["total_failed"],
            "失败率%": round(g["fail_rate_pct"], 4)
                       if g["fail_rate_pct"] is not None else None,
            "测试点数": g["n_test_points"], "有失败的点": g["n_failed_points"],
            "功率均值dBm": round(g["avg_power"], 3) if g["avg_power"] is not None else None,
            "通过率均值": round(g["avg_passrate"], 4) if g["avg_passrate"] is not None else None,
        } for g in groups])
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.caption("功率（dBm，约 −104）和通过率（0~1）是两种不同的物理量，"
                   "所以分成两列——它们**不能混在一起平均**，"
                   "混起来会算出 −52 这种无意义的数。")
        df_download(df, "下载这张表 (CSV)", f"yield_by_{dim}.csv")


# --- 失败明细 ---
with tabs[1]:
    if not fail_rows:
        st.success("这个条件下没有任何失败记录。")
    else:
        recs = yc.parse_rows(fail_rows, freq_map=freq_map)
        bf: dict[tuple[int, float], float] = {}
        for r in recs:
            if r["branch"] is not None and r["freq_mhz"] is not None:
                k = (r["branch"], r["freq_mhz"])
                bf[k] = bf.get(k, 0) + r["tested"]
        if bf:
            st.pyplot(plots.fig_fail_heatmap(
                bf, title="失败次数分布（分支 x 频率）",
                freqs=[f for f in yc.FREQ_ORDER
                       if any(k[1] == f for k in bf)]),
                use_container_width=True)

        df = pd.DataFrame([{
            "测试点": r["code"], "分支": r["branch"], "频率MHz": r["freq_mhz"],
            "测量项": r["measure"][:48], "测试类别": r["category"],
            "失败次数": int(r["tested"]), "单位": r["unit"],
            "下限": r["low_limit"], "上限": r["high_limit"],
            "最低": r["lowest"], "最高": r["highest"], "平均": r["avg"],
        } for r in recs]).sort_values("失败次数", ascending=False)
        st.dataframe(df, use_container_width=True, hide_index=True)
        df_download(df, "下载失败明细 (CSV)", "failures.csv")


# --- 单台详情 ---
with tabs[2]:
    st.caption("单台查询的 %Fail 列是可靠的，所以这里能画出完整的功率曲线和通过率。"
               "每台约 30 秒。")
    cand = list(sns) if sns else []
    if not cand:
        sn_one = st.text_input("输入一个整机序列号", value="")
    else:
        sn_one = st.selectbox("选一台整机", cand)

    if sn_one and st.button("查这台的详情"):
        with st.spinner(f"正在查 {sn_one} ..."):
            try:
                one = fetch_single(product, fd, td, sn_one.strip().upper(), test_count)
            except Exception as e:
                st.error(f"查询失败：{e}")
                one = []
        if not one:
            st.warning("这台没有数据")
        else:
            fm1 = yc.build_freq_map(one)
            pim1 = yc.pim_only(one)
            s1 = yc.yield_summary(pim1 if only_pim else one)
            k = st.columns(4)
            k[0].metric("测试点失败率",
                        f"{s1['fail_rate_pct']:.4f}%"
                        if s1["fail_rate_pct"] is not None else "—")
            k[1].metric("测试次数", f"{s1['total_tested']:,}")
            k[2].metric("失败次数", f"{s1['total_failed']:,}")
            k[3].metric("失败测试点", f"{s1['n_failed_points']}")

            fb = yc.failed_branches(one)
            if fb:
                st.write("**失败分支**：" +
                         "、".join(f"BR{b}({v:g}%)" for b, v in sorted(fb.items())))
                if len(fb) >= 18:
                    st.warning(
                        f"这台有 {len(fb)} 个分支同时失败（共 32 个）。"
                        "这种形态不像单个器件坏了，更可能是测试系统异常、"
                        "连接问题或测试被中断，建议单独核查，统计良率时作为异常样本剔除。")
            else:
                st.success("这台没有 PIM 失败分支")

            recs1 = yc.parse_rows(pim1, freq_map=fm1, sn=sn_one)
            prefixes = sorted({r["measure"].split()[0] for r in recs1
                               if r["value_kind"] == "power" and r["measure"]})
            for pre in prefixes[:4]:
                st.pyplot(plots.fig_pim_power(recs1, sn=sn_one, measure_prefix=pre),
                          use_container_width=True)
            st.pyplot(plots.fig_knock_passrate(recs1, sn=sn_one),
                      use_container_width=True)


# --- 下层件追踪 ---
with tabs[3]:
    st.caption("MI 的装配结构接口只支持「父件查子件」，用天线或滤波单元的序列号"
               "直接查会返回空。所以这里先把整机结构批量拉下来建本地索引，再反查。")
    base = list(sns) if sns else known_serials()
    if not base:
        st.info("需要先在左边填入整机序列号（或点「填入表格里的整机」），"
                "才能建立反查索引。")
    else:
        st.write(f"将从 **{len(base)}** 台整机建立索引"
                 f"（首次约 {len(base)*12//8} 秒，之后走缓存）")
        if st.button("建立/刷新索引"):
            with st.spinner("并行拉取装配结构 ..."):
                st.session_state["structs"] = fetch_structures(tuple(base))

        structs = st.session_state.get("structs")
        if structs:
            ok = {k: v for k, v in structs.items() if v}
            idx_ant = st_mod.build_index(ok, part_types={"antenna"})
            idx_fu = st_mod.build_index(ok, part_types={"filter_unit"})
            i = st.columns(3)
            i[0].metric("有结构数据的整机", f"{len(ok)}/{len(structs)}")
            i[1].metric("天线", f"{len(idx_ant)}")
            i[2].metric("滤波单元 FU", f"{len(idx_fu)}")

            kind = st.radio("追踪什么", ["antenna", "filter_unit"], horizontal=True,
                            format_func=lambda k: {"antenna": "天线",
                                                   "filter_unit": "滤波单元 FU"}[k])
            idx = idx_ant if kind == "antenna" else idx_fu
            # 默认列出换过机器的（最值得看）
            multi = sorted(((sn, len({x["parent_sn"] for x in sp}))
                            for sn, sp in idx.items()), key=lambda x: -x[1])
            opts = [sn for sn, n in multi]
            if opts:
                st.caption(f"装过多台整机的：{sum(1 for _, n in multi if n > 1)} 个"
                           f"（排在最前面）")
                pick = st.selectbox("选一个下层件序列号", opts,
                                    format_func=lambda s: f"{s}  （装过 "
                                    f"{len({x['parent_sn'] for x in idx[s]})} 台）")
                parents = sorted({x["parent_sn"] for x in idx[pick]})
                with st.spinner(f"查这 {len(parents)} 台整机的失败分支 ..."):
                    fails_by_parent = {}
                    for p in parents:
                        try:
                            rows_p = fetch_single(product, fd, td, p, test_count)
                            fails_by_parent[p] = {
                                b: 1 for b in yc.failed_branches(rows_p)}
                        except Exception:
                            fails_by_parent[p] = {}
                st.pyplot(plots.fig_part_timeline(
                    idx[pick], part_sn=pick, fails_by_parent=fails_by_parent),
                    use_container_width=True)

                dfs = pd.DataFrame([{
                    "整机": s["parent_sn"], "子件产品号": s["child_product"],
                    "装入时间": s["installed_at"],
                    "拆出时间": s["removed_at"] or "(仍在机器上)",
                    "该整机失败分支": "、".join(
                        f"BR{b}" for b in sorted(fails_by_parent.get(s["parent_sn"], {})))
                    or "无",
                } for s in sorted(idx[pick], key=lambda x: x["installed_at"] or "")])
                st.dataframe(dfs, use_container_width=True, hide_index=True)
                df_download(dfs, "下载追踪表 (CSV)", f"track_{pick}.csv")


# --- 原始数据 ---
with tabs[4]:
    st.caption("解析后的全部测量记录。功率类单位是 dBm，通过率类是 0~1 的比例，"
               "两者**不可混在一起平均**。")
    recs = yc.parse_rows(all_rows, freq_map=freq_map)
    fails = yc.fail_counts(fail_rows)
    df = pd.DataFrame([{
        "测试点": r["code"], "分支": r["branch"], "频率MHz": r["freq_mhz"],
        "测量项": r["measure"][:48], "测试点组": r["group"][:26],
        "测试类别": r["category"], "数值类型": r["value_kind"], "单位": r["unit"],
        "测试次数": int(r["tested"]), "失败次数": fails.get(r["code"], 0),
        "下限": r["low_limit"], "上限": r["high_limit"],
        "最低": r["lowest"], "最高": r["highest"], "平均": r["avg"],
        "标准差": r["std"], "Cpk": r["cpk"],
    } for r in recs])
    st.write(f"共 {len(df):,} 条")
    st.dataframe(df, use_container_width=True, hide_index=True, height=460)
    df_download(df, "下载全部数据 (CSV)", "measures.csv")
