"""单次测试明细接口（RTD 逐迭代曲线）的测试。

这些接口是 MI 那套之外的第二条数据源：MI 给判定结果，RTD 给判定依据。
测试全部用假的 RtdClient，不碰真实网络 —— 真实 Cookie 11 小时就过期，
拿它当测试前提的话测试会间歇性失败，那比没有测试更糟。

设计约束（测试要钉住的）：
  1. 前端只传 device_id + start_time，不传整个 18 字段的会话字典。
     会话字典是 RTD 的内部结构，泄露到接口契约里以后换不掉。
  2. 频率必填。沿用别的机器的频率会静默返回 0 条曲线（HTTP 200 但无数据），
     这是实测踩过的坑，必须在接口层就拦住。
  3. Cookie 过期要是 503 + 可读提示，不能是 500 堆栈。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# --------------------------------------------------------------------------
# 假的 RTD 客户端：按真实返回结构造数据
# --------------------------------------------------------------------------

SESSIONS = [
    {
        "id": {"device_id": "EA8E583008",
               "start_time": "2026-09-30T01:42:29.552000"},
        "device_id": "EA8E583008",
        "start_time_short": "2026.09.30 01:42:29",
        "start_time_local": "2026-09-30 01:42:29",
        "prod_slash": "KRD 901 330/11",
        "rstate": "R1A",
        "Testcat": "RADIO_PIM(28)",
        "Status": "Pass",
        "factory_name": "Nanjing",
        "test_station_id": "8945",
        "test_count": 1,
        "testpoints_count": 1858,
        "first_fail": None,
    },
    {
        "id": {"device_id": "EA8E573017",
               "start_time": "2026-09-30T14:16:26.123000"},
        "device_id": "EA8E573017",
        "start_time_short": "2026.09.30 14:16:26",
        "start_time_local": "2026-09-30 14:16:26",
        "prod_slash": "KRD 901 330/11",
        "rstate": "R1A",
        "Testcat": "RADIO_PIM(28)",
        "Status": "Fail",
        "factory_name": "Nanjing",
        "test_station_id": "8945",
        "test_count": 2,
        "testpoints_count": 1841,
        "first_fail": "5.2.100.17.0",
    },
]

FREQS = [1736e6, 1776e6, 1856e6, 1896e6, 2502.5e6, 2562.5e6]


class FakeRtd:
    """只实现接口用到的三个方法，并记录收到的参数供断言。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.hours_left = 8.0
        self.expired = False

    def cookie_hours_left(self):
        return self.hours_left

    def cookie_expired(self):
        return self.expired

    def sessions(self, start_date, end_date, **kw):
        self.calls.append(("sessions", start_date, end_date, kw))
        rows = SESSIONS
        if kw.get("device_id"):
            rows = [r for r in rows if r["device_id"] == kw["device_id"]]
        if kw.get("product"):
            rows = [r for r in rows if r["prod_slash"] == kw["product"]]
        if kw.get("testcat_contains"):
            rows = [r for r in rows
                    if kw["testcat_contains"] in r["Testcat"]]
        if kw.get("status"):
            rows = [r for r in rows if r["Status"] == kw["status"]]
        return rows

    def knock_options(self, session):
        self.calls.append(("options", session["device_id"]))
        return {"freqs": list(FREQS),
                "branches": list(range(32)),
                "iterations": list(range(1, 21))}

    def knock_curves(self, session, *, freq_hz, branches=None,
                     iterations=None, **kw):
        self.calls.append(("curves", session["device_id"], freq_hz,
                           tuple(branches or ()), tuple(iterations or ())))
        brs = list(branches) if branches else list(range(32))
        return {
            "device_id": session["device_id"],
            "start_time": session["start_time_short"],
            "product": session["prod_slash"],
            "testcat": session["Testcat"],
            "status": session["Status"],
            "freq_hz": freq_hz,
            "freq_mhz": freq_hz / 1e6,
            "mode": "sequential",
            "x": [i * 0.3 for i in range(200)],
            "x_title": "Knock iteration",
            "y_title": "PIM Power [dB]",
            "series": [
                {"name": f"{session['device_id']}, TC: 1, Br: {b}",
                 "branch": b, "test_count": 1,
                 "y": [-104.5 - 0.001 * i for i in range(200)]}
                for b in brs
            ],
            "n_series": len(brs),
            "n_points": len(brs) * 200,
        }


@pytest.fixture
def fake(monkeypatch):
    """把 api 模块里的 RTD 客户端换成假的。"""
    import api

    f = FakeRtd()
    monkeypatch.setattr(api, "_rtd", f, raising=False)
    monkeypatch.setattr(api, "_rtd_error", None, raising=False)
    api.rtd_sessions_cache_clear()
    return f


@pytest.fixture
def client(fake):
    import api
    return TestClient(api.app)


# --------------------------------------------------------------------------
# 会话列表
# --------------------------------------------------------------------------

def test_按日期取会话列表(client):
    r = client.get("/api/sessions", params={"date": "2026-09-30"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["n_sessions"] == 2
    assert d["date"] == "2026-09-30"
    s0 = d["sessions"][0]
    # 前端要用的字段必须在，且不含 RTD 的内部 id 结构
    assert s0["device_id"] == "EA8E583008"
    assert s0["start_time"] == "2026.09.30 01:42:29"
    assert s0["status"] == "Pass"
    assert s0["testcat"] == "RADIO_PIM(28)"


def test_会话列表不泄露RTD内部结构(client):
    """id 字段是 RTD 自己的 {device_id, start_time} 嵌套结构，
    一旦进了接口契约，以后 RTD 改结构我们就被绑死了。"""
    d = client.get("/api/sessions", params={"date": "2026-09-30"}).json()
    for s in d["sessions"]:
        assert "id" not in s
        assert "prod_slash" not in s          # 内部命名，对外叫 product


def test_会话列表可按产品和类别过滤(client, fake):
    client.get("/api/sessions", params={
        "date": "2026-09-30", "product": "KRD 901 330/11",
        "testcat": "RADIO_PIM", "status": "Fail"})
    kw = fake.calls[0][3]
    assert kw["product"] == "KRD 901 330/11"
    assert kw["testcat_contains"] == "RADIO_PIM"
    assert kw["status"] == "Fail"


def test_会话列表可按序列号过滤(client):
    d = client.get("/api/sessions", params={
        "date": "2026-09-30", "sn": "EA8E573017"}).json()
    assert d["n_sessions"] == 1
    assert d["sessions"][0]["device_id"] == "EA8E573017"


def test_日期必填(client):
    assert client.get("/api/sessions").status_code == 422


# --------------------------------------------------------------------------
# 可选参数
# --------------------------------------------------------------------------

def test_按会话查可选参数(client):
    r = client.get("/api/session/options", params={
        "date": "2026-09-30", "device_id": "EA8E583008",
        "start_time": "2026.09.30 01:42:29"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["freqs_mhz"] == [1736.0, 1776.0, 1856.0, 1896.0, 2502.5, 2562.5]
    assert d["branches"] == list(range(32))
    assert d["iterations"] == list(range(1, 21))


def test_查不到会话时报404而不是500(client):
    r = client.get("/api/session/options", params={
        "date": "2026-09-30", "device_id": "NOSUCH",
        "start_time": "2026.09.30 01:42:29"})
    assert r.status_code == 404
    assert "NOSUCH" in r.json()["detail"]


# --------------------------------------------------------------------------
# 曲线
# --------------------------------------------------------------------------

def test_取曲线返回每分支一条(client):
    r = client.get("/api/session/curves", params={
        "date": "2026-09-30", "device_id": "EA8E583008",
        "start_time": "2026.09.30 01:42:29", "freq_mhz": 1896})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["n_series"] == 32
    assert len(d["x"]) == 200
    assert len(d["series"][0]["y"]) == 200
    assert d["y_title"] == "PIM Power [dB]"
    assert d["freq_mhz"] == 1896.0


def test_曲线的x是共享的一份不是每条一份(client):
    """200 点 × 32 条，x 重复 32 份就是 5.7 倍的浪费。
    实测列式比行式省 5.7 倍，这个结构不能退化。"""
    d = client.get("/api/session/curves", params={
        "date": "2026-09-30", "device_id": "EA8E583008",
        "start_time": "2026.09.30 01:42:29", "freq_mhz": 1896}).json()
    assert isinstance(d["x"], list)
    for s in d["series"]:
        assert "x" not in s


def test_频率必填(client):
    """沿用别的机器的频率会静默返回 0 条曲线，所以不给默认值。"""
    r = client.get("/api/session/curves", params={
        "date": "2026-09-30", "device_id": "EA8E583008",
        "start_time": "2026.09.30 01:42:29"})
    assert r.status_code == 422


def test_可以只要部分分支(client, fake):
    d = client.get("/api/session/curves", params={
        "date": "2026-09-30", "device_id": "EA8E583008",
        "start_time": "2026.09.30 01:42:29",
        "freq_mhz": 1896, "branches": "0,1,2,3"}).json()
    assert d["n_series"] == 4
    call = [c for c in fake.calls if c[0] == "curves"][-1]
    assert call[3] == (0, 1, 2, 3)


def test_频率用MHz传入但按Hz调用底层(client, fake):
    """界面上一律用 MHz（和 MI 侧一致），底层 RTD 要 Hz。
    换算放在接口层，前端不该关心这个差异。"""
    client.get("/api/session/curves", params={
        "date": "2026-09-30", "device_id": "EA8E583008",
        "start_time": "2026.09.30 01:42:29", "freq_mhz": 2502.5})
    call = [c for c in fake.calls if c[0] == "curves"][-1]
    assert call[2] == 2502.5e6


def test_x轴标题照搬RTD不自己编(client):
    """x 值实测是 0.0~61.38 的浮点，不是迭代序号 1..20，
    含义尚未确认，所以标题必须照搬 RTD 的原文，不能写成「第 N 次迭代」。"""
    d = client.get("/api/session/curves", params={
        "date": "2026-09-30", "device_id": "EA8E583008",
        "start_time": "2026.09.30 01:42:29", "freq_mhz": 1896}).json()
    assert d["x_title"] == "Knock iteration"


# --------------------------------------------------------------------------
# 凭据过期
# --------------------------------------------------------------------------

def test_cookie过期返回503和可读提示(client, fake):
    fake.expired = True
    fake.hours_left = -3.0
    r = client.get("/api/sessions", params={"date": "2026-09-30"})
    assert r.status_code == 503
    assert "过期" in r.json()["detail"]
    assert "refresh_cookies" in r.json()["detail"]


def test_RTD不可用时其他接口照常(client, monkeypatch):
    """RTD 是第二数据源，它挂了不该影响 MI 那套接口。"""
    import api
    monkeypatch.setattr(api, "_rtd", None, raising=False)
    monkeypatch.setattr(api, "_rtd_error", "没有 cookie 文件", raising=False)
    r = client.get("/api/sessions", params={"date": "2026-09-30"})
    assert r.status_code == 503
    assert "没有 cookie 文件" in r.json()["detail"]
    assert client.get("/api/health").status_code == 200


def test_健康检查里报告RTD状态(client, fake):
    d = client.get("/api/health").json()
    assert d["rtd"]["available"] is True
    assert d["rtd"]["cookie_hours_left"] == 8.0
