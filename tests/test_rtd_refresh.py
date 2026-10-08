"""RTD 凭据刷新：文件变了要自动生效 + 前端按钮触发。

为什么要自动重载
----------------
RtdClient 原来在 __init__ 里把 cookies.json 读进内存就不再看文件。
服务是长期跑着的，而凭据约 11 小时过期 —— 刷新了文件但服务还拿旧的用，
等于白刷。所以每次用之前要看一眼文件改没改。

为什么刷新端点要上锁和限本机
----------------------------
它会启动一个真实的浏览器进程，是整个服务里唯一有副作用的接口。
如果不限制来源，等于给了外部一个"让服务器启动任意进程"的入口。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DOMAIN = ".radiounits-realtimetestdata.ericsson.net"


def _cookie_blob(hours_from_now: float) -> list[dict]:
    return [{"name": "CF_Authorization", "value": "tok",
             "domain": DOMAIN, "expires": time.time() + hours_from_now * 3600}]


# ==========================================================================
# 一、凭据文件变了要自动生效
# ==========================================================================

@pytest.fixture
def cli(tmp_path):
    import rtd_client
    p = tmp_path / "cookies.json"
    p.write_text(json.dumps(_cookie_blob(-3)), encoding="utf-8")   # 先放个过期的
    return rtd_client.RtdClient(cookie_file=p,
                                template_file=tmp_path / "none.json")


def test_凭据文件被刷新后不用重建客户端就生效(cli):
    """这是整件事的核心：服务长期跑着，刷新完要立刻可用。"""
    assert cli.cookie_expired() is True

    # 模拟 refresh_cookies.py 写入了新凭据
    cli.cookie_file.write_text(json.dumps(_cookie_blob(11)), encoding="utf-8")

    assert cli.cookie_expired() is False
    assert 10 < cli.cookie_hours_left() < 12


def test_刷新后请求用的是新cookie值(cli, tmp_path):
    """只更新过期时间不够，实际发请求带的 Cookie 值也得换成新的。"""
    old = cli.cookies["CF_Authorization"]
    blob = _cookie_blob(11)
    blob[0]["value"] = "brand-new-token"
    cli.cookie_file.write_text(json.dumps(blob), encoding="utf-8")

    cli.cookie_expired()                      # 触发一次检查
    assert cli.cookies["CF_Authorization"] == "brand-new-token"
    assert cli.cookies["CF_Authorization"] != old
    assert cli._session.cookies.get("CF_Authorization") == "brand-new-token"


def test_文件没变时不重建session():
    """没改动就不该做无用功。

    这里刻意**不**断言"不读盘"—— 读盘是必须的。Windows 上文件时间戳的
    实际更新粒度实测约 0.4 ms，连续两次写入会拿到完全相同的 mtime 和
    size，靠它判断变更会漏掉改动（表现为测试单独跑失败、全量跑通过）。
    所以改成每次读内容比较，实测 8 KB 读一次 0.68 ms，相对一次 RTD
    请求 2 秒只占 0.03%，不值得为此去赌时间戳。
    真正该省的是解析 JSON 和重建 session。
    """
    import rtd_client
    import tempfile
    d = Path(tempfile.mkdtemp())
    p = d / "cookies.json"
    p.write_text(json.dumps(_cookie_blob(5)), encoding="utf-8")
    c = rtd_client.RtdClient(cookie_file=p, template_file=d / "none.json")

    n = {"applies": 0}
    orig = rtd_client.RtdClient._apply

    def counting(self, text):
        n["applies"] += 1
        return orig(self, text)

    rtd_client.RtdClient._apply = counting
    try:
        for _ in range(5):
            c.cookie_expired()
        assert n["applies"] == 0, "文件没变却重新解析了"
        # 文件真的变了就该重建
        p.write_text(json.dumps(_cookie_blob(9)), encoding="utf-8")
        c.cookie_expired()
        assert n["applies"] == 1, "文件变了却没重新解析"
    finally:
        rtd_client.RtdClient._apply = orig


def test_凭据文件被删掉不崩只是沿用内存里的(cli):
    """刷新脚本写文件的瞬间可能短暂不可读，不该让请求直接炸。"""
    cli.cookie_file.unlink()
    assert cli.cookie_expired() is True       # 沿用删除前读到的那份


# ==========================================================================
# 二、前端按钮触发刷新
# ==========================================================================

class FakeProc:
    """假的子进程：按脚本给定的输出和退出码走完。"""

    def __init__(self, lines, code=0, delay=0.0):
        self._lines = list(lines)
        self._code = code
        self._delay = delay
        self.stdout = self
        self.killed = False

    def readline(self):
        if self._delay:
            time.sleep(self._delay)
        return self._lines.pop(0) if self._lines else ""

    def wait(self, timeout=None):
        return self._code

    def poll(self):
        return None if self._lines else self._code

    def kill(self):
        self.killed = True


@pytest.fixture
def app_rtd(tmp_path, monkeypatch):
    """一个本机模式的 app，RTD 可用，子进程被换成假的。"""
    import api

    api._rtd_refresh_reset()
    monkeypatch.setattr(api, "RTD_REFRESH_ENABLED", True, raising=False)

    launched: list[list[str]] = []

    def fake_popen(cmd, **kw):
        launched.append(list(cmd))
        return FakeProc([
            "用浏览器: chrome\n",
            "打开钻取页...\n",
            "Cookie 已存：3 个本域 Cookie\n",
            "  最早到期：还剩 15.4 小时\n",
            "抓到 4 / 5 个模板\n",
        ])

    monkeypatch.setattr(api.subprocess, "Popen", fake_popen)
    api._rtd_launched = launched
    return api


@pytest.fixture
def client(app_rtd):
    # ★ TestClient 默认把来源 IP 设成字符串 "testclient"，会被本机白名单
    #   拦成 403。要显式伪装成回环地址才能测到正常路径。
    return TestClient(app_rtd.app, client=("127.0.0.1", 50000))


def _wait_done(client, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = client.get("/api/rtd/refresh").json()
        if d["state"] in ("done", "failed"):
            return d
        time.sleep(0.05)
    raise AssertionError("刷新一直没结束")


def test_按钮能触发刷新并立刻返回(client):
    """起浏览器加等登录可能要几分钟，接口绝不能阻塞着等它。"""
    t0 = time.time()
    r = client.post("/api/rtd/refresh")
    assert r.status_code == 200, r.text
    assert time.time() - t0 < 2.0, "接口阻塞了，应该后台跑"
    assert r.json()["state"] == "running"


def test_能查到进度和日志(client):
    client.post("/api/rtd/refresh")
    d = _wait_done(client)
    assert d["state"] == "done"
    assert any("Cookie 已存" in x for x in d["log"])
    assert d["returncode"] == 0


def test_跑的是刷新脚本本身(client, app_rtd):
    client.post("/api/rtd/refresh")
    _wait_done(client)
    cmd = app_rtd._rtd_launched[0]
    assert any("refresh_cookies.py" in str(x) for x in cmd)


def test_同时只能跑一个(client, app_rtd, monkeypatch):
    """两个浏览器抢同一个 profile 目录会互相破坏。"""
    monkeypatch.setattr(
        app_rtd.subprocess, "Popen",
        lambda cmd, **kw: FakeProc(["慢慢跑\n"] * 50, delay=0.05))
    assert client.post("/api/rtd/refresh").status_code == 200
    r2 = client.post("/api/rtd/refresh")
    assert r2.status_code == 409
    assert "正在" in r2.json()["detail"]


def test_没启动过时状态是idle(client):
    d = client.get("/api/rtd/refresh").json()
    assert d["state"] == "idle"


def test_缺playwright时提前报明确错不让它跑起来再崩(app_rtd, monkeypatch):
    """refresh_cookies.py 要 playwright，而服务跑的虚拟环境可能没装。

    不检查的话用户点了按钮等半天，最后拿到一段 ModuleNotFoundError
    的堆栈 —— 实测踩过。要在启动前就拦住并给出能照着敲的安装命令。
    """
    monkeypatch.setattr(app_rtd, "_playwright_available", lambda: False)
    c = TestClient(app_rtd.app, client=("127.0.0.1", 50000))
    r = c.post("/api/rtd/refresh")
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "playwright" in detail.lower()
    assert "uv sync" in detail, "没给出安装命令"
    # 不该启动任何子进程
    assert not app_rtd._rtd_launched


def test_失败时如实报告(client, app_rtd, monkeypatch):
    monkeypatch.setattr(
        app_rtd.subprocess, "Popen",
        lambda cmd, **kw: FakeProc(["没等到登录\n"], code=2))
    client.post("/api/rtd/refresh")
    d = _wait_done(client)
    assert d["state"] == "failed"
    assert d["returncode"] == 2


def test_状态里带上成功与否方便前端判断(client, app_rtd, monkeypatch):
    """前端不能只看「凭据现在有效」就说刷新成功了。

    实测踩过：脚本因为缺 playwright 退出码 1，但凭据本来还有 9 小时
    有效期，前端于是显示「凭据已刷新」—— 误导。
    所以状态里要明确给出 ok，由退出码决定。
    """
    monkeypatch.setattr(
        app_rtd.subprocess, "Popen",
        lambda cmd, **kw: FakeProc(["炸了\n"], code=1))
    client.post("/api/rtd/refresh")
    d = _wait_done(client)
    assert d["ok"] is False
    assert d["state"] == "failed"


# ---- 安全限制 ----

def test_非本机请求被拒(app_rtd):
    """这是唯一能启动进程的接口，必须只给本机。"""
    c = TestClient(app_rtd.app, client=("10.1.2.3", 51234))
    r = c.post("/api/rtd/refresh")
    assert r.status_code == 403
    assert "本机" in r.json()["detail"]


@pytest.mark.parametrize("hdr", [
    "x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host",
])
def test_带代理头的请求一律拒绝(app_rtd, hdr):
    """这一条是防反向代理场景下的伪造。

    uvicorn 默认 proxy_headers=True：当请求来自 forwarded_allow_ips
    （默认就是 127.0.0.1）时，它会信任 X-Forwarded-For 并用它替换
    request.client.host。单机直连时这没问题，但一旦把服务放到 nginx
    后面，所有请求的来源都变成代理的 127.0.0.1 —— 外部就能带一个
    伪造的 X-Forwarded-For: 127.0.0.1 骗过本机检查。

    本机直连绝不会带这些头，带了就说明中间有代理，来源不可信，直接拒。
    """
    c = TestClient(app_rtd.app, client=("127.0.0.1", 50000))
    r = c.post("/api/rtd/refresh", headers={hdr: "127.0.0.1"})
    assert r.status_code == 403, f"带 {hdr} 的请求应该被拒"
    assert "代理" in r.json()["detail"]


def test_不带代理头的本机请求正常(client):
    assert client.post("/api/rtd/refresh").status_code == 200


def test_可以用开关整个关掉(app_rtd, monkeypatch):
    monkeypatch.setattr(app_rtd, "RTD_REFRESH_ENABLED", False, raising=False)
    c = TestClient(app_rtd.app, client=("127.0.0.1", 50000))
    r = c.post("/api/rtd/refresh")
    assert r.status_code == 403


def test_刷新是唯一的写接口其余仍是只读(app_rtd):
    """确认没有顺手引入别的副作用接口。"""
    posts = [r for r in app_rtd.app.routes
             if "POST" in getattr(r, "methods", set())]
    paths = sorted(r.path for r in posts)
    assert paths == ["/api/rtd/refresh", "/login"], paths
