"""RTD 客户端测试（TDD：先写这个）。

RTD（Realtime Test Data）是另一套系统，架构分工：
    MI  -> FPY / 良率 / 趋势（已有）
    RTD -> 单台单次测试的逐迭代 200 点曲线（这里）

RTD 没有干净接口，只有一个 Dash 回调端点 `_dash-update-component`。
取数必须三步走，顺序不能换：
    1. 会话列表            -> 拿 (device_id, start_time)
    2. 按会话查可选参数     -> 拿该机可用的频率/分支/迭代
    3. 取曲线              -> 用上一步的值

第 2 步不能省：实测沿用别的机器的频率会静默返回 0 条曲线。

这些测试全部用假的 HTTP 层，不碰真实网络 —— 真实连通性由
scripts 里的探测脚本手动验证，单元测试只管协议组装对不对。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

COOKIES = [
    {"name": "CF_Authorization", "value": "tok123",
     "domain": ".radiounits-realtimetestdata.ericsson.net", "expires": 9e9},
    {"name": "CF_AppSession", "value": "sess1",
     "domain": "jupyter.radiounits-realtimetestdata.ericsson.net", "expires": 9e9},
    {"name": "irrelevant", "value": "x", "domain": ".example.com", "expires": 9e9},
]

SESSION_ROW = {
    "id": '{"device_id":"EA8E583008","start_time":"2026-09-30T01:42:29.552000"}',
    "start_time_short": "2026.09.30 01:42:29",
    "start_time_local": "2026.09.30 09:42:29",
    "factory_name": "ESS Nanjing", "test_station_id": "8945.1",
    "device_id": "EA8E583008", "prod_slash": "KRD 901 330/11", "rstate": "R1A",
    "Testcat": "RADIO_PIM (28)", "testpoints_count": 1938,
    "auxpoints_count": 4218, "sbt_skipped_total": "0", "first_fail": "",
    "Status": "Pass", "test_count": 1, "testtype": "PIM_PROD",
    "manual": "", "legacy": "",
}
OTHER_ROW = dict(SESSION_ROW, device_id="EA8E585224", Status="Fail",
                 Testcat="RADIO_PERF_PIM (212)", prod_slash="KRD 901 330/11")


class FakePost:
    """假的 HTTP 层：记录收到的请求体，按 output 决定回什么。"""

    def __init__(self):
        self.calls = []

    def __call__(self, url, json=None, timeout=None, **kw):
        body = json or {}
        self.calls.append({"url": url, "body": body})
        out = str(body.get("output"))
        if "layout_test-sessions_table_element.data" in out:
            return _Resp({"response": {
                "layout_test-sessions_table_element":
                    {"data": [SESSION_ROW, OTHER_ROW]}}})
        if "frequency-dropdown.options" in out:
            return _Resp({"response": {
                "layout-pim_knock-frequency-dropdown": {
                    "options": [{"label": "1736 MHz", "value": 1736000000.0},
                                {"label": "1896 MHz", "value": 1896000000.0}],
                    "value": None}}})
        if "branch-dropdown.options" in out:
            return _Resp({"response": {
                "layout-pim_knock-branch-dropdown": {
                    "options": [{"label": "ALL", "value": "ALL"},
                                {"label": "0", "value": 0},
                                {"label": "1", "value": 1}],
                    "value": None}}})
        if "iteration-dropdown.options" in out:
            return _Resp({"response": {
                "layout-pim_knock-iteration-dropdown": {
                    "options": [{"label": "ALL", "value": "ALL"},
                                {"label": "1", "value": 1},
                                {"label": "2", "value": 2}],
                    "value": None}}})
        if "sequential-graph.figure" in out:
            # 按请求里的分支数生成对应条数的曲线
            brs = _state_of(body, "layout-pim_knock-branch-dropdown") or [0]
            data = [{"name": f"EA8E583008, TC: 1, Br: {b}",
                     "x": [i * 0.1 for i in range(200)],
                     "y": [-105.0 - b * 0.1] * 200,
                     "mode": "lines", "type": "scatter"} for b in brs]
            return _Resp({"response": {
                "layout-pim_knock-sequential-graph": {"figure": {
                    "data": data,
                    "layout": {"xaxis": {"title": {"text": "Knock iteration"}},
                               "yaxis": {"title": {"text": "PIM Power [dB]"}}}}}}})
        return _Resp({"response": {}})


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.content = json.dumps(payload).encode()
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._p

    @property
    def text(self):
        return json.dumps(self._p)


def _state_of(body, sid):
    for x in (body.get("state") or []) + (body.get("inputs") or []):
        if sid in str(x.get("id")):
            return x.get("value")
    return None


@pytest.fixture
def cookie_file(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text(json.dumps(COOKIES), encoding="utf-8")
    return p


def _tpl(output, outputs, inputs, state, changed):
    return {"output": output, "outputs": outputs, "inputs": inputs,
            "state": state, "changedPropIds": changed,
            "parsedChangedPropsIds": changed}


@pytest.fixture
def template_file(tmp_path):
    """模拟 refresh_cookies.py 抓下来的真实请求模板。

    这里的 inputs/state 分布刻意照抄真实抓包 —— 比如 options 回调的
    inputs 是 testsession-dropdown，会话 store 在 state 里。放反了会 500，
    测试要能挡住这种退化。
    """
    t = {
        "sessions": _tpl(
            "..layout_test-sessions_table_element.data..."
            "layout-test-sessions_table_size_limit_text.children..",
            [{"id": "layout_test-sessions_table_element", "property": "data"},
             {"id": "layout-test-sessions_table_size_limit_text",
              "property": "children"}],
            [{"id": "layout_drilldown_test_sessions_fam_prod_store",
              "property": "data", "value": ["ALL"]},
             {"id": "layout_test_sessions_start_end_date_store",
              "property": "data",
              "value": {"start_date_time": "2026-10-01T00:00:00",
                        "end_date_time": "2026-10-01T23:59:59.999999"}}],
            [{"id": "layout_test-sessions_table_element", "property": "data",
              "value": []}],
            ["layout_test_sessions_start_end_date_store.data"]),
    }
    for name, comp in (("freq_options", "layout-pim_knock-frequency-dropdown"),
                       ("branch_options", "layout-pim_knock-branch-dropdown"),
                       ("iter_options", "layout-pim_knock-iteration-dropdown")):
        t[name] = _tpl(
            f"..{comp}.options...{comp}.value..",
            [{"id": comp, "property": "options"},
             {"id": comp, "property": "value"}],
            [{"id": "layout-pim-testsession-dropdown", "property": "value",
              "value": 0}],
            [{"id": "layout_drill_down_selected_sessions_store",
              "property": "data", "value": []}],
            ["layout-pim-testsession-dropdown.value"])
    p = tmp_path / "templates.json"
    p.write_text(json.dumps(t), encoding="utf-8")
    return p


@pytest.fixture
def cli(cookie_file, template_file, monkeypatch):
    import rtd_client
    import importlib
    importlib.reload(rtd_client)
    c = rtd_client.RtdClient(cookie_file=cookie_file, template_file=template_file)
    fake = FakePost()
    monkeypatch.setattr(c._session, "post", fake)
    c._fake = fake
    return c


# ---------- Cookie ----------

def test_只取本域的cookie(cli):
    assert set(cli.cookies) == {"CF_Authorization", "CF_AppSession"}
    assert "irrelevant" not in cli.cookies


def test_cookie过期能被检出(tmp_path):
    import rtd_client
    p = tmp_path / "old.json"
    p.write_text(json.dumps([
        {"name": "CF_Authorization", "value": "t",
         "domain": ".radiounits-realtimetestdata.ericsson.net", "expires": 1.0}]),
        encoding="utf-8")
    c = rtd_client.RtdClient(cookie_file=p,
                             template_file=tmp_path / "none.json")
    assert c.cookie_expired() is True


def test_cookie未过期(cli):
    assert cli.cookie_expired() is False


def test_没有cookie文件时报明确错误(tmp_path):
    import rtd_client
    with pytest.raises(FileNotFoundError, match="Cookie"):
        rtd_client.RtdClient(cookie_file=tmp_path / "nope.json",
                             template_file=tmp_path / "nope2.json")


# ---------- 第 1 步：会话列表 ----------

def test_按日期取会话列表(cli):
    rows = cli.sessions("2026-09-30", "2026-09-30")
    assert len(rows) == 2
    assert rows[0]["device_id"] == "EA8E583008"
    body = cli._fake.calls[-1]["body"]
    # 日期必须被塞进那个 store，且补全成全天
    v = _state_of(body, "start_end_date_store")
    assert v["start_date_time"] == "2026-09-30T00:00:00"
    assert v["end_date_time"].startswith("2026-09-30T23:59:59")


def test_会话列表可按产品和类别过滤(cli):
    rows = cli.sessions("2026-09-30", "2026-09-30",
                        product="KRD 901 330/11", testcat_contains="RADIO_PIM")
    assert [r["device_id"] for r in rows] == ["EA8E583008"]


def test_会话列表按状态过滤(cli):
    assert [r["device_id"] for r in
            cli.sessions("2026-09-30", "2026-09-30", status="Fail")] == ["EA8E585224"]


# ---------- 第 2 步：可选参数 ----------

def test_按会话查可选参数并剔除ALL(cli):
    o = cli.knock_options(SESSION_ROW)
    assert o["freqs"] == [1736000000.0, 1896000000.0]
    # 'ALL' 是界面上的全选项，传给接口会被忽略，必须剔掉
    assert o["branches"] == [0, 1]
    assert o["iterations"] == [1, 2]


def test_查可选参数时把会话塞进store(cli):
    cli.knock_options(SESSION_ROW)
    for call in cli._fake.calls:
        v = _state_of(call["body"], "selected_sessions_store")
        assert v == [SESSION_ROW]


# ---------- 第 3 步：取曲线 ----------

def test_取曲线返回每分支一条(cli):
    o = cli.knock_options(SESSION_ROW)
    cur = cli.knock_curves(SESSION_ROW, freq_hz=o["freqs"][0],
                           branches=o["branches"], iterations=o["iterations"])
    assert cur["freq_hz"] == 1736000000.0
    assert cur["device_id"] == "EA8E583008"
    assert len(cur["series"]) == 2
    s0 = cur["series"][0]
    assert s0["branch"] == 0
    assert s0["test_count"] == 1
    assert len(s0["y"]) == 200
    assert len(cur["x"]) == 200
    assert cur["y_title"] == "PIM Power [dB]"
    assert cur["x_title"] == "Knock iteration"


def test_取曲线时分支列表被真正传过去(cli):
    cli.knock_curves(SESSION_ROW, freq_hz=1736000000.0, branches=[0],
                     iterations=[1, 2])
    body = cli._fake.calls[-1]["body"]
    assert _state_of(body, "layout-pim_knock-branch-dropdown") == [0]
    assert _state_of(body, "iteration-dropdown") == [1, 2]
    assert _state_of(body, "frequency-dropdown") == 1736000000.0
    # 触发按钮必须是 n_clicks=1，否则回调不执行
    assert _state_of(body, "search-button") == 1


def test_不传分支时自动查可选并全要(cli):
    cur = cli.knock_curves(SESSION_ROW, freq_hz=1736000000.0)
    assert len(cur["series"]) == 2        # 自动用了 [0, 1]


def test_曲线里的x是迭代位置不是整数序号(cli):
    cur = cli.knock_curves(SESSION_ROW, freq_hz=1736000000.0, branches=[0],
                           iterations=[1])
    # 200 个采样点铺在 20 次迭代上，x 是小数
    assert cur["x"][1] != int(cur["x"][1]) or cur["x"][1] == 0.1


def test_频率必填防止误用别的机器的值(cli):
    with pytest.raises(TypeError):
        cli.knock_curves(SESSION_ROW)


def test_options请求沿用模板的inputs与state分布(cli):
    """防退化：options 回调的 inputs 必须是 testsession-dropdown，
    会话 store 必须在 state 里。放反了真实接口会 500。"""
    cli.knock_options(SESSION_ROW)
    body = cli._fake.calls[0]["body"]
    assert [x["id"] for x in body["inputs"]] == ["layout-pim-testsession-dropdown"]
    assert [x["id"] for x in body["state"]] == [
        "layout_drill_down_selected_sessions_store"]
    assert body["outputs"], "outputs 不能为空，填空会 500"


def test_曲线模板内置且结构完整(cli):
    import rtd_client
    tpl = rtd_client.CURVES_TEMPLATE
    assert tpl["inputs"][0]["id"] == "layout-pim_knock-search-button"
    assert tpl["inputs"][0]["property"] == "n_clicks"
    ids = [x["id"] for x in tpl["state"]]
    for need in ("layout-pim_knock-frequency-dropdown",
                 "layout-pim_knock-branch-dropdown",
                 "layout-pim_knock-iteration-dropdown",
                 "layout_drill_down_selected_sessions_store"):
        assert need in ids
    assert len(tpl["outputs"]) == 8



# ---------- 网络瞬时错误重试 ----------

def test_网络瞬时错误自动重试(cli, monkeypatch):
    """Windows 下偶尔会出现 ConnectionResetError (10054) —— 远程强制关闭了
    连接。这是网络瞬时错误（代理、CDN 边缘节点、VPN 都会导致），重试一次
    通常就好。不该让用户每次网络抖动都看到报错。
    """
    import requests
    # 自造一个"前两次抛 ConnectionError、第三次成功"的 fake post
    calls = {"n": 0}
    good_resp = cli._fake   # 原来会成功返回的 fake

    def flaky_post(url, json=None, timeout=None, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectionError(
                "('Connection aborted.', ConnectionResetError(10054, "
                "'An existing connection was forcibly closed by the remote host'))")
        return good_resp(url, json=json, timeout=timeout, **kw)

    monkeypatch.setattr(cli._session, "post", flaky_post)
    # 不该抛错 —— 应该重试到成功
    sess = cli.sessions("2026-09-30", "2026-09-30")
    assert calls["n"] == 3, f"应该重试到第 3 次才成功，实际发了 {calls['n']} 次"
    assert sess, "重试成功后应该拿到会话数据"


def test_重试超过上限还是失败就报错(cli, monkeypatch):
    """重试是为了对付瞬时错误。真的持续失败（断网、服务挂了）要让用户看到。"""
    import requests
    calls = {"n": 0}

    def always_fail(url, json=None, timeout=None, **kw):
        calls["n"] += 1
        raise requests.exceptions.ConnectionError("模拟断网")

    monkeypatch.setattr(cli._session, "post", always_fail)
    with pytest.raises(RuntimeError, match="RTD 连接失败"):
        cli.sessions("2026-09-30", "2026-09-30")
    assert calls["n"] >= 3, f"至少应该重试到总共 3 次，实际 {calls['n']}"


def test_凭据错误不重试(cli, monkeypatch):
    """HTTP 302/401/403 是凭据过期 —— 重试没用，立刻报错省得干等。"""
    class Fake401:
        def __init__(self):
            self.status_code = 401
            self.text = "Unauthorized"
        def json(self):
            return {}
    calls = {"n": 0}

    def unauth(url, json=None, timeout=None, **kw):
        calls["n"] += 1
        return Fake401()

    monkeypatch.setattr(cli._session, "post", unauth)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        cli.sessions("2026-09-30", "2026-09-30")
    assert calls["n"] == 1, f"凭据错误不该重试，实际 {calls['n']} 次"
