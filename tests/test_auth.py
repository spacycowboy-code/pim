"""身份验证测试（登录页 + 会话 Cookie 方案）。

为什么不用 HTTP Basic Auth：它依赖浏览器弹原生密码框，而这个行为不可控——
实测 Chrome 打开 http://100.77.162.146:8000 时不弹框，只显示空白页
（ERR_INVALID_AUTH_CREDENTIALS）。自己写登录页则完全可控。

安全要求：
  1. 没登录访问任何数据接口/页面 -> 跳转或 401
  2. 密码比对用 compare_digest（防时序攻击）
  3. 会话令牌用 secrets 生成，不可预测
  4. Cookie 带 HttpOnly（JS 读不到，防 XSS 偷令牌）
  5. 凭据只从环境变量读，不写死在代码里
  6. 健康检查不需要登录（留给监控）

★ 架构变更后这个文件的装置换了（2026-10）
--------------------------------------------
原来这里借 test_api.py 的 `snapshot` 装置造一份假快照文件，再用
`PIM_SNAPSHOT_DIR` 指过去。快照那条取数路径已经整条删除（默认值是快照，
忘了设开关就会悄悄读过期数据，这种静默回退太危险），所以：

  * 不再 import test_api 的任何东西
  * 改用 conftest.py 的 `make_live_client` + `fake_client`：
    它把最底层的 MI 网络客户端换成假的，上面整套数据源和接口代码走真实路径
  * 环境变量 PIM_AUTH_USER / PIM_AUTH_PASS 必须在调用工厂**之前**设好，
    因为工厂内部会 importlib.reload(api)，而 AUTH_ENABLED 是模块加载时算的

**访问控制比以前更重要**：服务现在实时连 MI，带的是使用者的 Windows 域权限，
放进去一个人等于把域权限借给他。所以下面每条断言的强度都不许放宽。
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

USER, PASS = "tester", "s3cret-pass"

API_SRC = Path(__file__).resolve().parent.parent / "api.py"


def _client(make_live_client, fake_client) -> TestClient:
    """用假 MI 客户端起后端，返回**不自动跟随跳转**的接口测试客户端。

    为什么不直接用 conftest 的 live_client：那个默认 follow_redirects=True，
    会把「未登录 -> 303 跳登录页」这一步悄悄走掉，看不到跳转本身。
    这里先调工厂完成「注入假客户端 + 重载模块」，再按自己的要求建客户端。
    """
    make_live_client(fake_client)      # 只为触发注入与重载
    import api                         # 取重载后的同一个模块对象
    return TestClient(api.app, follow_redirects=False)


@pytest.fixture
def client_auth(monkeypatch, make_live_client, fake_client):
    """启用了密码保护的后端。"""
    # 必须在调用工厂（内部会 reload api）之前设好，否则 AUTH_ENABLED 还是旧值
    monkeypatch.setenv("PIM_AUTH_USER", USER)
    monkeypatch.setenv("PIM_AUTH_PASS", PASS)
    return _client(make_live_client, fake_client)


@pytest.fixture
def client_open(monkeypatch, make_live_client, fake_client):
    """没设凭据、处于开放状态的后端（本机开发时的样子）。"""
    monkeypatch.delenv("PIM_AUTH_USER", raising=False)
    monkeypatch.delenv("PIM_AUTH_PASS", raising=False)
    return _client(make_live_client, fake_client)


def _login(c: TestClient, user=USER, pwd=PASS):
    return c.post("/login", data={"username": user, "password": pwd})


# ---------- 登录页本身 ----------

def test_登录页可直接访问(client_auth):
    """登录页当然不能要求先登录，否则死锁。"""
    r = client_auth.get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_登录页是EDS风格(client_auth):
    html = client_auth.get("/login").text
    # 三层视觉层次的色值必须出现
    assert "#0c0c0c" in html      # 系统层
    assert "#ebebeb" in html      # 应用层
    assert "#1174e6" in html      # primary
    assert "Ericsson Hilda" in html


def test_登录页不泄露密码(client_auth):
    html = client_auth.get("/login").text
    assert PASS not in html


# ---------- 未登录必须被拦 ----------

def test_未登录访问首页跳到登录页(client_auth):
    r = client_auth.get("/")
    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers["location"]


def test_未登录访问数据接口返回401(client_auth):
    """接口返回 401 JSON，不跳转——跳转会让前端 fetch 拿到一堆 HTML。"""
    r = client_auth.get("/api/manifest")
    assert r.status_code == 401
    assert "json" in r.headers["content-type"]


def test_所有数据接口都受保护(client_auth):
    """清单覆盖全部读数据的接口。

    架构变更后补了后来新增的那几族（history / suppliers / curves /
    sessions / refresh / docs）。保护是靠一个中间件统一做的，所以这里真正
    在验的是「有没有哪条路径漏在 _OPEN_PATHS 之外的白名单里」。
    /api/docs 也要拦：它会把全部接口和参数名列给未登录的人看。
    """
    paths = ["/api/manifest", "/api/options", "/api/docs", "/api/refresh",
             "/api/yield?product=X&from_date=A&to_date=B",
             "/api/failures?product=X&from_date=A&to_date=B",
             "/api/testpoints?product=X&from_date=A&to_date=B&sn=S",
             "/api/parts", "/api/parts/DS4W0270J6",
             "/api/suppliers",
             "/api/history/options", "/api/history/yield",
             "/api/history/trend", "/api/history/runs",
             "/api/history/repairs",
             "/api/curves/options", "/api/curves?kind=K&freq_mhz=1896",
             "/api/sessions?date=2026-09-30",
             "/api/session/options?date=2026-09-30&device_id=S&start_time=T",
             "/api/rtd/refresh"]
    for p in paths:
        assert client_auth.get(p).status_code == 401, f"{p} 没被保护"


def test_未登录不能触发凭据刷新(client_auth):
    """唯一有副作用的接口（会起一个真实浏览器进程），未登录必须 401。

    它本身还有「只接受本机直连」的设限，但那道检查在接口函数里，
    中间件得先把未登录的人挡在外面，不能让请求走到那一步。
    """
    r = client_auth.post("/api/rtd/refresh")
    assert r.status_code == 401
    assert "json" in r.headers["content-type"]


def test_未登录访问静态资源被拦(client_auth):
    r = client_auth.get("/static/app.js")
    assert r.status_code in (302, 303, 307, 401)


# ---------- 登录流程 ----------

def test_密码正确能登录(client_auth):
    r = _login(client_auth)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/"
    assert "pim_session" in r.cookies


def test_会话Cookie带HttpOnly(client_auth):
    """HttpOnly 让 JavaScript 读不到 Cookie，防止 XSS 偷走会话。"""
    r = _login(client_auth)
    sc = r.headers.get("set-cookie", "")
    assert "httponly" in sc.lower()
    assert "samesite" in sc.lower()


def test_登录后能访问数据(client_auth):
    """登录后真能拿到数据（而不是「拦住了但也查不出东西」）。

    n_serials 的来源变了：原来数到的是快照文件里的序列号，现在是数据源
    当前的候选清单（conftest 的假客户端给 2 台），所以期望值仍是 2。
    """
    _login(client_auth)
    r = client_auth.get("/api/manifest")
    assert r.status_code == 200
    assert r.json()["n_serials"] == 2


def test_登录后首页正常返回(client_auth):
    _login(client_auth)
    r = client_auth.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_密码错误不给令牌(client_auth):
    r = _login(client_auth, pwd="错误密码")
    assert "pim_session" not in r.cookies
    # 回到登录页并提示错误
    assert r.status_code in (200, 401)
    assert "错误" in r.text or "不正确" in r.text


def test_用户名错误不给令牌(client_auth):
    r = _login(client_auth, user="错误用户")
    assert "pim_session" not in r.cookies


def test_伪造的令牌无效(client_auth):
    """Cookie 值只能是 ASCII，所以这里用 ASCII 字符串伪造。"""
    client_auth.cookies.set("pim_session", "forged-token-1234567890")
    assert client_auth.get("/api/manifest").status_code == 401


def test_退出登录后令牌失效(client_auth):
    _login(client_auth)
    assert client_auth.get("/api/manifest").status_code == 200
    client_auth.get("/logout")
    assert client_auth.get("/api/manifest").status_code == 401


# ---------- 健康检查例外 ----------

def test_健康检查不需要登录(client_auth):
    """留给监控用，所以在白名单里。但它只能报状态，不能报数据。"""
    r = client_auth.get("/api/health")
    assert r.status_code == 200
    d = r.json()
    assert d["auth_enabled"] is True
    # 快照那条路删了，这两个字段恒为真/"mi"，启动脚本靠它们确认数据是实时的
    assert d["live"] is True
    assert d["source_mode"] == "mi"
    # 这些字段随快照路径一起删除，不该再出现
    for gone in ("offline", "snapshot_dir", "snapshot_ready"):
        assert gone not in d, f"{gone} 属于已删除的快照路径，不该还在返回里"


def test_健康检查不泄露凭据(client_auth):
    """这个接口不要求登录，所以返回里任何敏感信息都等于公开。

    架构变更后更要紧：服务实时连 MI（集成 Windows 认证）和 RTD（浏览器
    Cookie），所以除了登录口令，连 MI 主机名、认证方式、RTD 令牌这类
    线索也不能漏出去——那是给人攻击面的信息。
    """
    r = client_auth.get("/api/health")
    body = r.text
    # 登录凭据
    assert PASS not in body and USER not in body
    # 连环境变量名都别给，省得提示「这服务靠什么变量开保护」
    assert "PIM_AUTH" not in body
    # MI / RTD 侧的敏感线索（注意不能笼统查 "cookie"：
    # 返回里有个合法字段叫 cookie_hours_left，只报剩余小时数）
    for bad in ("mesmi", "ericsson.com", "Negotiate", "NTLM", "Kerberos",
                "CF_Authorization", "CF_AppSession", "password", "token"):
        assert bad not in body, f"健康检查泄露了 {bad!r}"
    # 也不该顺手发会话 Cookie
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_健康检查不回显会话令牌(client_auth):
    """已登录状态下再查健康检查，不能把 Cookie 里的令牌回显出来。"""
    token = _login(client_auth).cookies["pim_session"]
    assert token                       # 先确认真拿到了令牌，否则这条白测
    assert token not in client_auth.get("/api/health").text


# ---------- 不设密码时开放 ----------

def test_没设密码时直接可访问(client_open):
    assert client_open.get("/api/manifest").status_code == 200
    assert client_open.get("/").status_code == 200


def test_没设密码时健康检查要标明(client_open):
    """开放状态必须自己承认，运维才能发现「忘了设密码」。

    原来这里还顺带断言 offline/snapshot_ready。快照路径删除后那些字段没了，
    换成断言实时标志：live=True、source_mode="mi"。
    """
    d = client_open.get("/api/health").json()
    assert d["auth_enabled"] is False
    assert d["live"] is True
    assert d["source_mode"] == "mi"


# ---------- 实现方式检查 ----------

def test_用compare_digest比对密码():
    assert "compare_digest" in API_SRC.read_text(encoding="utf-8")


def test_令牌用secrets生成():
    """会话令牌必须不可预测。用 random 会被猜到。"""
    src = API_SRC.read_text(encoding="utf-8")
    assert "secrets.token_urlsafe" in src or "token_hex" in src


def test_凭据不写在代码里():
    src = API_SRC.read_text(encoding="utf-8")
    assert "PIM_AUTH_PASS" in src
    for bad in ('password = "', "password = '", 'PASS = "', "PASS = '"):
        assert bad not in src, f"疑似硬编码凭据: {bad}"
