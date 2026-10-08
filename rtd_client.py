"""RTD（Realtime Test Data）客户端 —— 取单台单次测试的逐迭代曲线。

为什么需要它
------------
架构分工：
    MI  -> FPY / 良率 / 趋势 / 跨机队统计（mi_client.py）
    RTD -> 单台单次测试的逐迭代 200 点曲线（本模块）

MI 拿不到迭代维度 —— 实测确认 MI 的 knock 类测量项返回的是通过率标量
（0~1），200 次 knock 采样在测试台上被判定后才写进 MI。**MI 存判定结果，
RTD 存判定依据。** 详见 .kiro/steering/project-context.md。

为什么是模板驱动
----------------
RTD 是个 Plotly Dash 应用，没有取数接口，所有数据都走一个
`_dash-update-component` 回调端点。请求体里 inputs / state / changedPropIds
的确切分布是 Dash 的实现细节，**手工重建极其脆弱**：

    踩过的坑（全部静默 500 或返回空，没有任何有用报错）
      - outputs 填成空列表                      -> 500
      - 把会话 store 放进 inputs 而不是 state   -> 500
      - 沿用别的机器的频率                      -> 200 但 0 条曲线

所以改成：`refresh_cookies.py` 抓下真实请求存成模板，本模块只改参数值。
对协议的依赖降到最低，RTD 升级后重抓一次模板即可。

取数三步，顺序不能换
--------------------
    1. sessions()       -> 拿会话行（含 device_id + 带毫秒的 start_time）
    2. knock_options()  -> 拿该会话可用的频率/分支/迭代
    3. knock_curves()   -> 用上一步的值取曲线

第 2 步不能省，见上面"沿用别的机器的频率"那条。

认证
----
靠浏览器单点登录后的 Cookie，实测有效期约 11 小时。过期后跑
`refresh_cookies.py`。命令行拿不到这个 Cookie —— Cloudflare Access 不认
Kerberos，必须走浏览器。
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any, Sequence

import requests

HOST = "jupyter.radiounits-realtimetestdata.ericsson.net"
BASE = f"https://{HOST}/app/dashboard"
URL = f"{BASE}/_dash-update-component"

HERE = Path(__file__).resolve().parent
DEFAULT_COOKIE_FILE = HERE / "data" / "rtd" / "cookies.json"
DEFAULT_TEMPLATE_FILE = HERE / "data" / "rtd" / "templates.json"

# 界面上的"全选"项。传给接口会被忽略，组装参数时必须剔掉。
ALL_SENTINEL = "ALL"

# 组件 id（回调契约的一部分，改了取数就失败）
ID_SESS_STORE = "layout_drill_down_selected_sessions_store"
ID_DATE_STORE = "layout_test_sessions_start_end_date_store"
ID_TESTSESSION = "layout-pim-testsession-dropdown"
ID_FREQ = "layout-pim_knock-frequency-dropdown"
ID_BRANCH = "layout-pim_knock-branch-dropdown"
ID_ITER = "layout-pim_knock-iteration-dropdown"
ID_SEARCH_BTN = "layout-pim_knock-search-button"
ID_SESS_TABLE = "layout_test-sessions_table_element"

# 曲线那个回调的模板是内置的 —— 它要点"查询"按钮才触发，而按钮在参数选全
# 之前是 disabled，自动抓取不稳定。这个结构是从真实抓包得来并实测验证过的。
CURVES_TEMPLATE: dict[str, Any] = {
    "output": ("..layout-pim_knock-overlay-graph.figure..."
               "layout-pim_knock-overlay-graph.style..."
               "layout-pim_knock-sequential-graph.figure..."
               "layout-pim_knock-sequential-graph.style..."
               "layout-pim_knock-3d_panel1-graph.figure..."
               "layout-pim_knock-3d_panel1-graph.style..."
               "layout-pim_knock-3d_panel2-graph.figure..."
               "layout-pim_knock-3d_panel2-graph.style.."),
    "outputs": [
        {"id": "layout-pim_knock-overlay-graph", "property": "figure"},
        {"id": "layout-pim_knock-overlay-graph", "property": "style"},
        {"id": "layout-pim_knock-sequential-graph", "property": "figure"},
        {"id": "layout-pim_knock-sequential-graph", "property": "style"},
        {"id": "layout-pim_knock-3d_panel1-graph", "property": "figure"},
        {"id": "layout-pim_knock-3d_panel1-graph", "property": "style"},
        {"id": "layout-pim_knock-3d_panel2-graph", "property": "figure"},
        {"id": "layout-pim_knock-3d_panel2-graph", "property": "style"},
    ],
    "inputs": [{"id": ID_SEARCH_BTN, "property": "n_clicks", "value": 1}],
    "state": [
        {"id": "layout-pim_knock-radio_button", "property": "value",
         "value": "sequential"},
        {"id": ID_FREQ, "property": "value", "value": None},
        {"id": ID_BRANCH, "property": "value", "value": []},
        {"id": "layout-pim_knock-branch_3d_panel2-dropdown", "property": "value",
         "value": []},
        {"id": ID_ITER, "property": "value", "value": []},
        {"id": ID_TESTSESSION, "property": "value", "value": 0},
        {"id": ID_SESS_STORE, "property": "data", "value": []},
    ],
    "changedPropIds": [f"{ID_SEARCH_BTN}.n_clicks"],
    "parsedChangedPropsIds": [f"{ID_SEARCH_BTN}.n_clicks"],
}


def _set(body: dict, id_contains: str, prop: str, value: Any) -> bool:
    """把请求体里某个组件的某个属性设成新值。

    inputs 和 state 都要找 —— 同一个组件在不同回调里可能出现在不同位置，
    这是 Dash 的设计，不是写法不统一。
    """
    hit = False
    for bucket in ("inputs", "state"):
        for x in body.get(bucket) or []:
            if id_contains in str(x.get("id")) and x.get("property") == prop:
                x["value"] = value
                hit = True
    return hit


def _get(body: dict, id_contains: str, prop: str) -> Any:
    for bucket in ("inputs", "state"):
        for x in body.get(bucket) or []:
            if id_contains in str(x.get("id")) and x.get("property") == prop:
                return x.get("value")
    return None


class RtdClient:
    """只读客户端。不改 RTD 任何数据。"""

    def __init__(self, cookie_file: str | Path | None = None,
                 template_file: str | Path | None = None,
                 timeout: int = 240):
        self.cookie_file = Path(cookie_file) if cookie_file else DEFAULT_COOKIE_FILE
        if not self.cookie_file.exists():
            raise FileNotFoundError(
                f"找不到 RTD 的 Cookie 文件：{self.cookie_file}\n"
                "先跑 refresh_cookies.py 在浏览器里登录一次。")
        self.template_file = (Path(template_file) if template_file
                              else DEFAULT_TEMPLATE_FILE)
        self._templates: dict[str, dict] = {}
        if self.template_file.exists():
            self._templates = json.loads(
                self.template_file.read_text(encoding="utf-8"))
        self._templates.setdefault("curves", CURVES_TEMPLATE)

        self._raw = []
        self._cookie_text: str = ""
        self.cookies: dict[str, str] = {}
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": f"{BASE}/drill_down",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        })
        self._load_cookies()

    # ---------- Cookie ----------

    def _load_cookies(self) -> None:
        """把 cookies.json 读进来并装到 session 上。"""
        self._apply(self.cookie_file.read_text(encoding="utf-8"))

    def _apply(self, text: str) -> None:
        self._raw = json.loads(text)
        self.cookies = {c["name"]: c["value"] for c in self._raw
                        if HOST.endswith(str(c.get("domain", "")).lstrip("."))}
        self._cookie_text = text
        # 换凭据时必须先清掉旧的，否则同名 Cookie 会留着老值
        self._session.cookies.clear()
        self._session.cookies.update(self.cookies)

    def _reload_if_changed(self) -> None:
        """凭据文件变了就重新读。

        服务是长期跑着的，而凭据约 11 小时过期。刷新脚本写了新文件之后，
        如果还用内存里那份旧的，刷新就等于没做。

        ★ 为什么是比内容而不是比 mtime：Windows 上文件时间戳的实际更新
          粒度实测约 0.4 ms（300 次快速写入只产生 131 个不同的
          `st_mtime_ns`），连续两次写入能拿到完全相同的 mtime 和 size，
          于是改动被漏掉。这个坑在测试里表现为「单独跑失败、全量跑通过」
          —— 全量跑时前面的用例耗掉了时间，碰巧跨过了时间刻度。
          实测读这个 8 KB 文件单次 0.68 ms，相对一次 RTD 请求（约 2 秒）
          只占 0.03%，所以没必要为这点开销去赌一个不可靠的时间戳。

        读不到文件（刷新脚本正在写、或文件被挪走）时沿用内存里的，
        不往外抛 —— 让一次取数失败比让整个服务 500 好。
        """
        try:
            text = self.cookie_file.read_text(encoding="utf-8")
        except OSError:
            return
        if text == self._cookie_text:
            return
        try:
            self._apply(text)
        except Exception:
            pass

    def cookie_expiry(self) -> float | None:
        """本域 Cookie 里最早的到期时间戳。会话级 Cookie 不计入。"""
        self._reload_if_changed()
        exps = [c["expires"] for c in self._raw
                if HOST.endswith(str(c.get("domain", "")).lstrip("."))
                and isinstance(c.get("expires"), (int, float)) and c["expires"] > 0]
        return min(exps) if exps else None

    def cookie_expired(self) -> bool:
        exp = self.cookie_expiry()
        return exp is not None and exp <= time.time()

    def cookie_hours_left(self) -> float | None:
        exp = self.cookie_expiry()
        return None if exp is None else (exp - time.time()) / 3600

    # ---------- 底层 ----------

    def _tpl(self, name: str) -> dict:
        if name not in self._templates:
            raise RuntimeError(
                f"缺少请求模板 [{name}]：{self.template_file}\n"
                "跑 refresh_cookies.py 重新抓一次。")
        return copy.deepcopy(self._templates[name])

    def _call(self, body: dict) -> dict:
        # 凭据可能在服务跑着的时候被刷新脚本换掉，发请求前对一下文件
        self._reload_if_changed()
        # ★ 网络瞬时错误自动重试：Windows 下偶尔出现 ConnectionResetError
        #   (10054, "connection was forcibly closed by the remote host")，
        #   代理/CDN/VPN 都可能触发。重试一次通常就通，不该让用户每次抖动
        #   都看到报错。
        #   只对**连接层**错误重试（ConnectionError / Timeout）；HTTP 401/403
        #   是凭据过期、500 可能是模板不匹配 —— 这些重试无用，直接报错。
        #   Dash 的 _dash-update-component 是纯查询（取数据），重试幂等。
        import requests as _rq
        max_attempts = 3
        last_err = None
        r = None
        for attempt in range(max_attempts):
            try:
                r = self._session.post(URL, json=body, timeout=self.timeout)
                break
            except (_rq.exceptions.ConnectionError,
                    _rq.exceptions.Timeout) as e:
                last_err = e
                if attempt == max_attempts - 1:
                    raise RuntimeError(
                        f"RTD 连接失败（重试 {max_attempts} 次仍不通）："
                        f"{type(e).__name__}: {str(e)[:200]}。"
                        "通常是网络抖动或代理/VPN 中断，稍等再试；"
                        "持续失败就看看机器能不能正常访问 RTD。"
                    ) from e
                time.sleep(1 + attempt * 2)   # 1s、3s 指数退避
        if r.status_code != 200:
            hint = ("很可能是 Cookie 过期了，重跑 refresh_cookies.py。"
                    if r.status_code in (302, 401, 403)
                    else "500 通常意味着请求体结构和 RTD 当前版本不匹配，"
                         "重跑 refresh_cookies.py 抓新模板。")
            raise RuntimeError(
                f"RTD 回调失败 HTTP {r.status_code}：{r.text[:160]}\n{hint}")
        try:
            return r.json().get("response") or {}
        except ValueError:
            raise RuntimeError(
                "RTD 返回的不是 JSON，通常意味着被重定向到登录页（Cookie 过期）。")

    # ---------- 第 1 步：会话列表 ----------

    def sessions(self, start_date: str, end_date: str, *,
                 product: str | None = None,
                 testcat_contains: str | None = None,
                 status: str | None = None,
                 device_id: str | None = None) -> list[dict]:
        """某个日期范围内的全部测试会话。

        日期给 `2026-09-30` 这种，自动补成当天 00:00:00 ~ 23:59:59.999999。
        实测一整天约 2 万条、1.3 MB、6 秒，所以按天取并用参数过滤。

        **过滤是在本地做的** —— 接口本身只认日期和产品族，
        不要指望它帮你按序列号筛。
        """
        body = self._tpl("sessions")
        _set(body, ID_DATE_STORE, "data",
             {"start_date_time": f"{start_date}T00:00:00",
              "end_date_time": f"{end_date}T23:59:59.999999"})
        resp = self._call(body)
        rows = (resp.get(ID_SESS_TABLE) or {}).get("data") or []
        out = []
        for r in rows:
            if product and r.get("prod_slash") != product:
                continue
            if testcat_contains and testcat_contains not in str(r.get("Testcat", "")):
                continue
            if status and str(r.get("Status")) != status:
                continue
            if device_id and r.get("device_id") != device_id:
                continue
            out.append(r)
        return out

    # ---------- 第 2 步：可选参数 ----------

    def knock_options(self, session: dict) -> dict:
        """这个会话有哪些频率 / 分支 / 迭代可选。

        ★ 必须对每个会话单独查。这些选项是按会话动态算的，沿用别的机器的
          频率会**静默返回 0 条曲线**，不报错 —— 这是最容易踩的坑。
        """
        out: dict[str, list] = {}
        for tpl_name, comp, key in (("freq_options", ID_FREQ, "freqs"),
                                    ("branch_options", ID_BRANCH, "branches"),
                                    ("iter_options", ID_ITER, "iterations")):
            body = self._tpl(tpl_name)
            _set(body, ID_SESS_STORE, "data", [session])
            resp = self._call(body)
            node = resp.get(comp) or {}
            vals = []
            for o in node.get("options") or []:
                v = o.get("value") if isinstance(o, dict) else o
                if v == ALL_SENTINEL:        # 界面上的"全选"，接口不认
                    continue
                vals.append(v)
            out[key] = vals
        return out

    # ---------- 第 3 步：取曲线 ----------

    def knock_curves(self, session: dict, *, freq_hz: float,
                     branches: Sequence[int] | None = None,
                     iterations: Sequence[int] | None = None,
                     mode: str = "sequential") -> dict:
        """某个会话、某个频率下，每个分支的逐迭代曲线。

        `freq_hz` 是必填关键字参数，单位赫兹（1896 MHz 写 1896000000）。
        刻意不给默认值 —— 默认值会诱导人沿用上一台机器的频率，而那会静默
        返回空曲线。

        `branches` / `iterations` 不填就自动先查可选项并全要。
        32 个分支全要约 230 KB / 1~2 秒；只要 4 个约 36 KB。
        """
        if branches is None or iterations is None:
            o = self.knock_options(session)
            if branches is None:
                branches = o["branches"]
            if iterations is None:
                iterations = o["iterations"]
        branches = [b for b in branches if b != ALL_SENTINEL]
        iterations = [i for i in iterations if i != ALL_SENTINEL]

        body = self._tpl("curves")
        _set(body, "layout-pim_knock-radio_button", "value", mode)
        _set(body, ID_FREQ, "value", freq_hz)
        _set(body, ID_BRANCH, "value", list(branches))
        _set(body, ID_ITER, "value", list(iterations))
        _set(body, ID_SESS_STORE, "data", [session])
        _set(body, ID_SEARCH_BTN, "n_clicks", 1)

        resp = self._call(body)
        fig = (resp.get(f"layout-pim_knock-{mode}-graph") or {}).get("figure") or {}
        traces = fig.get("data") or []
        layout = fig.get("layout") or {}

        series, x = [], []
        for t in traces:
            name = str(t.get("name") or "")
            series.append({
                "name": name,
                "branch": _tail_int(name, "Br:"),
                "test_count": _tail_int(name, "TC:"),
                "y": list(t.get("y") or []),
            })
            if not x:
                x = list(t.get("x") or [])

        return {
            "device_id": session.get("device_id"),
            "start_time": session.get("start_time_short"),
            "product": session.get("prod_slash"),
            "testcat": session.get("Testcat"),
            "status": session.get("Status"),
            "freq_hz": freq_hz,
            "freq_mhz": freq_hz / 1e6 if freq_hz else None,
            "mode": mode,
            "x": x,
            "x_title": _axis_title(layout, "xaxis"),
            "y_title": _axis_title(layout, "yaxis"),
            "series": series,
            "n_series": len(series),
            "n_points": sum(len(s["y"]) for s in series),
        }


def _tail_int(name: str, marker: str) -> int | None:
    """从 'EA8E583008, TC: 1, Br: 0' 里抠出 TC / Br 的数字。"""
    i = name.find(marker)
    if i < 0:
        return None
    num = ""
    for ch in name[i + len(marker):].strip():
        if ch.isdigit():
            num += ch
        else:
            break
    return int(num) if num else None


def _axis_title(layout: dict, axis: str) -> str | None:
    t = (layout.get(axis) or {}).get("title")
    return t.get("text") if isinstance(t, dict) else t
