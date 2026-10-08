"""PIM 查询工具的后端接口。

**数据一律实时从 MI / RTD 拉取**，不读任何事先下载到本地的副本。
所以这个服务必须跑在有 Windows 域身份的机器上（MI 走集成 Windows 认证）。

只有 GET 接口，没有任何写操作（唯一的例外是刷新 RTD 凭据，只接受本机直连）。

启动::

    # 推荐用项目根目录的 start-local.ps1
    # 默认只绑 127.0.0.1（本机），别人要访问得走 SSH 隧道
    uv run --with fastapi --with uvicorn --with matplotlib --with numpy \
        uvicorn api:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import data_source as ds
import structure as st_mod
import yield_calc as yc
import curves

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"

app = FastAPI(title="PIM 测试数据查询", version="1.0.0",
              docs_url="/api/docs", redoc_url=None)


# ---------------------------------------------------------------------------
# 身份验证：自己写的登录页 + 会话 Cookie
#
# 为什么不用 HTTP Basic Auth：它依赖浏览器弹原生密码框，而这个行为不可控。
# 实测 Chrome 打开 http://100.77.162.146:8000 时不弹框，只显示空白页
# （ERR_INVALID_AUTH_CREDENTIALS）。自己写登录页则 100% 可控，
# 还能做成 EDS 风格、给出友好的错误提示。
#
# 凭据只从环境变量读，绝不写进代码或同步到服务器文件里：
#   PIM_AUTH_USER / PIM_AUTH_PASS
# 两个都没设 = 不启用保护（本机开发方便）。
# ---------------------------------------------------------------------------

_AUTH_USER = os.environ.get("PIM_AUTH_USER", "")
_AUTH_PASS = os.environ.get("PIM_AUTH_PASS", "")
AUTH_ENABLED = bool(_AUTH_USER and _AUTH_PASS)

COOKIE_NAME = "pim_session"
SESSION_MAX_AGE = 12 * 3600          # 12 小时后要重新登录

# 有效的会话令牌。进程重启会全部失效（对这个规模的工具够用，
# 不需要引入 Redis 之类的外部存储）。
_sessions: set[str] = set()

# 这些路径不要求登录
_OPEN_PATHS = {"/api/health", "/login", "/logout", "/favicon.ico"}


def _credentials_ok(user: str, pwd: str) -> bool:
    """比对凭据。

    用 secrets.compare_digest 而不是 == ：后者比较字符串时会在第一个
    不同的字符处提前返回，攻击者能通过测量响应时间逐字符猜出密码
    （时序攻击）。compare_digest 的耗时与内容无关。

    compare_digest 对 str 只接受 ASCII，所以统一转 bytes，这样中文密码也能用。
    """
    ok_user = secrets.compare_digest(user.encode("utf-8"), _AUTH_USER.encode("utf-8"))
    ok_pass = secrets.compare_digest(pwd.encode("utf-8"), _AUTH_PASS.encode("utf-8"))
    return ok_user and ok_pass


def _logged_in(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    return bool(token) and token in _sessions


@app.middleware("http")
async def require_login(request: Request, call_next):
    """用中间件统一保护，而不是给每个接口加装饰器。

    这样新增接口时不会忘记加保护——漏一个就等于没加。

    接口和页面的处理方式不同：
      /api/* 返回 401 JSON（跳转会让前端 fetch 拿到一堆 HTML，没法解析）
      其他   跳转到登录页（用户体验好）
    """
    if not AUTH_ENABLED or request.url.path in _OPEN_PATHS or _logged_in(request):
        return await call_next(request)

    if request.url.path.startswith("/api/"):
        return Response(content='{"detail":"请先登录"}', status_code=401,
                        media_type="application/json")
    return RedirectResponse("/login", status_code=303)


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · PIM 测试数据查询</title>
<style>
/* 按 EDS 规范：三层视觉层次、8px 栅格、字号只用阶梯上的值 */
*{box-sizing:border-box;}
body{margin:0;font-family:"Ericsson Hilda","Arial","Source Han Sans SC",
  "Microsoft YaHei",sans-serif;font-size:14px;line-height:1.5;color:#242424;
  background:#ebebeb;min-height:100vh;}
.sys{height:32px;background:#0c0c0c;color:#ffffff;display:flex;align-items:center;
  gap:4px;padding:0 24px;font-size:12px;}
.sys b{font-weight:700;}
.sys span{color:#8b8b8b;}
.wrap{max-width:420px;margin:0 auto;padding:32px 24px;}
h1{font-size:30px;font-weight:200;line-height:1.2;margin:0 0 8px;}
.lede{font-size:14px;color:#6a6a6a;margin:0 0 24px;}
.tile{background:#fafafa;border:1px solid #dcdcdc;padding:24px;}
label{display:block;font-size:14px;margin-bottom:4px;}
input{width:100%;height:32px;padding:0 8px;font-family:inherit;font-size:14px;
  color:#242424;background:#ffffff;border:1px solid #878787;border-radius:3px;}
input:focus{outline:none;border-color:#1174e6;}
.field{margin-bottom:16px;}
button{width:100%;height:32px;font-family:inherit;font-size:14px;font-weight:400;
  color:#ffffff;background:#1174e6;border:1px solid #1174e6;border-radius:3px;
  cursor:pointer;}
button:hover{background:#0069c2;border-color:#0069c2;}
.err{background:#fafafa;border:1px solid #dcdcdc;border-left:3px solid #ed0e00;
  padding:16px;margin-bottom:16px;display:flex;gap:8px;align-items:flex-start;
  font-size:14px;}
.err i{font-style:normal;color:#ed0e00;}
.foot{font-size:12px;color:#6a6a6a;margin-top:24px;}
</style>
</head>
<body>
<header class="sys"><b>Ericsson</b><span>·</span><span>PIM 测试数据查询</span></header>
<div class="wrap">
  <h1>登录</h1>
  <p class="lede">这是内部生产测试数据，需要凭据才能查看。</p>
  __ERROR__
  <form class="tile" method="post" action="/login">
    <div class="field">
      <label for="u">用户名</label>
      <input type="text" id="u" name="username" autocomplete="username"
             autofocus required>
    </div>
    <div class="field">
      <label for="p">密码</label>
      <input type="password" id="p" name="password"
             autocomplete="current-password" required>
    </div>
    <button type="submit">Sign in</button>
  </form>
  <p class="foot">忘记凭据找管理员。会话 12 小时后失效。</p>
</div>
</body>
</html>"""

_ERR_BLOCK = ('<div class="err"><i>✕</i><div>用户名或密码不正确，请重试。</div></div>')


@app.get("/login")
def login_page(error: int = 0) -> HTMLResponse:
    html = LOGIN_HTML.replace("__ERROR__", _ERR_BLOCK if error else "")
    return HTMLResponse(html)


@app.post("/login")
def do_login(username: str = Form(""), password: str = Form("")) -> Response:
    if not AUTH_ENABLED or _credentials_ok(username, password):
        token = secrets.token_urlsafe(32)      # 不可预测的会话令牌
        _sessions.add(token)
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(COOKIE_NAME, token, max_age=SESSION_MAX_AGE,
                     httponly=True,            # JS 读不到，防 XSS 偷令牌
                     samesite="lax", path="/")
        return r
    # 密码错了：回登录页并提示，不给令牌
    return HTMLResponse(LOGIN_HTML.replace("__ERROR__", _ERR_BLOCK),
                        status_code=401)


@app.get("/logout")
def do_logout(request: Request) -> Response:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        _sessions.discard(token)
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(COOKIE_NAME, path="/")
    return r


# ---------------------------------------------------------------------------
# 数据源：只有一条路 —— 实时查 MI
#
# 原来这里有个 PIM_SOURCE 开关，可以切成读本地快照文件。默认值是快照，
# 所以启动时忘了设开关就会悄悄用过期数据，界面还一切正常。那条路已移除。
#
# 代价：服务必须跑在有 Windows 域身份的机器上，否则取不到数。
# ---------------------------------------------------------------------------

_source: Any = None
_load_error: str | None = None

DEFAULT_PRODUCT = "KRD 901 330/11"

# Crete 平台整机产品号。这些产品**当下不一定在产**，所以 MI 的
# discover_products（只看最近 183 天在产）可能不返回它们；但用户仍需要能
# 在下拉里选到做历史查询，因此作为默认候选硬编码。
# 新增 Crete 产品时往这里加一行。
CRETE_PRODUCTS = [
    "KRD 901 328/11",
    "KRD 901 329/11",
    "KRD 901 316/11",
]

# 下拉默认候选 = 主产品 + Crete 产品。
DEFAULT_PRODUCTS = [DEFAULT_PRODUCT, *CRETE_PRODUCTS]


def _known_serials_products() -> tuple[list[str], list[str]]:
    """给界面填候选值（用户仍可自己输入任何序列号和产品号）。

    序列号来自 data/serials.json（之前从追踪表提取的整机清单）。
    产品号包含主产品 + Crete 平台产品，MI 探测到的其他产品靠后台线程补进来。
    """
    return list(ds.known_serials()), list(DEFAULT_PRODUCTS)


# ★ 模块导入时绝不连网。
#
# 踩过的坑：原来在这里同步调 discover_products（问 MI 最近半年有哪些产品号），
# 结果 `import api` 本身要 90 秒以上 —— 服务启动卡住，测试更是每次重新加载
# 模块都卡一次，几十个用例直接跑不完。
#
# 现在的分工：
#   导入时   只建数据源对象（不发任何请求），瞬间完成
#   启动后   后台线程去问 MI 要产品号清单，拿到了就替换掉默认值
# 所以连不上 MI 不会让服务起不来，只会在真正查询时报错——那时的错误信息
# 也更具体（能说清是哪个查询失败）。
try:
    _sns, _prods = _known_serials_products()
    _source = ds.MiSource(serials=_sns, products=_prods)
except Exception as e:
    _load_error = f"{type(e).__name__}: {e}"


def _discover_products_async() -> None:
    """后台问 MI 最近半年在产的整机产品号，填进前端下拉。

    失败无所谓：下拉里仍有默认产品号，用户也能自己输入。
    所以这里吞掉所有异常，绝不影响服务。
    """
    if _source is None:
        return
    try:
        hi = dt.datetime.now()
        lo = hi - dt.timedelta(days=183)
        _source.discover_products(from_date=lo.strftime("%Y-%m-%d 00:00"),
                                  to_date=hi.strftime("%Y-%m-%d 23:59"))
    except Exception:
        pass


# daemon=True：这个线程不会拖着进程不退出。
threading.Thread(target=_discover_products_async, daemon=True).start()


def src():
    if _source is None:
        raise HTTPException(503, f"数据源初始化失败：{_load_error}")
    return _source


def _split(v: str | None) -> list[str]:
    """把逗号分隔的查询参数拆开。"""
    if not v:
        return []
    return [x.strip() for x in v.split(",") if x.strip()]


def _not_found(msg: str) -> HTTPException:
    """查不到数据时，除了说"没有"，还要给出可行的下一步。

    否则用户只看到 404 无从下手——这是可用性问题，不只是错误处理。
    数据是实时查 MI 的，所以查不到通常是条件写错了，不是数据没准备好。
    """
    s = _source
    extra = ""
    if s is not None:
        extra = ("\n\n检查一下查询条件：\n"
                 "  产品号   要去掉尾部的 R-state，例如填 KRD 901 330/11\n"
                 "  日期     这段时间内这些机器确实被测过吗\n"
                 "  测试类别 PIM 是 212；多个用英文逗号分隔\n"
                 "  序列号   多个用英文逗号分隔（分号和空格会被忽略）\n"
                 "条件没问题却还是查不到，就是 MI 里确实没有这批数据。")
    return HTTPException(404, msg + extra)


def _fetch_pair(product: str, from_date: str, to_date: str,
                sns: list[str], cats: list[str], test_count: str,
                only_pim: bool) -> tuple[list, list, dict]:
    """取一对数据。批量查询时 MI 的 %Fail 列恒为 0，所以良率必须两次相除。

    第三个返回值是"数据是怎么找到的"。实时查询永远是精确命中用户给的条件，
    所以它恒为 exact=True；这个结构保留是因为前端会显示它。
    """
    s = src()
    try:
        all_rows, info = s.test_points_resolved(
            product=product, from_date=from_date, to_date=to_date,
            serial_numbers=sns, test_category=cats, test_count=test_count,
            only_failed=False)
        fail_rows, _ = s.test_points_resolved(
            product=product, from_date=from_date, to_date=to_date,
            serial_numbers=sns, test_category=cats, test_count=test_count,
            only_failed=True)
    except KeyError as e:
        raise _not_found(str(e).strip('"').replace("\\n", "\n"))
    if only_pim:
        all_rows, fail_rows = yc.pim_only(all_rows), yc.pim_only(fail_rows)
    return all_rows, fail_rows, info


# ---------------------------------------------------------------------------
# 元信息
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    """健康检查。这个接口不需要认证（留给监控），也不返回任何测试数据。

    offline=true      证明这个服务不访问 MI
    auth_enabled=false 说明服务是开放状态——对外开放时这个值必须是 true
    """
    return {
        "status": "ok" if _source is not None else "degraded",
        # 这两个字段留着，因为启动脚本和前端会读它们确认"数据是实时的"。
        # 现在只有实时一条路，所以恒为 true / "mi"。
        "live": True,
        "source_mode": "mi",
        "auth_enabled": AUTH_ENABLED,
        "error": _load_error,
        # RTD 是第二数据源，凭据约 11 小时过期，所以把剩余时间暴露出来，
        # 不用等接口报错才发现要刷新。
        "rtd": {
            "available": _rtd is not None and not _rtd.cookie_expired(),
            "cookie_hours_left": (None if _rtd is None
                                  else _rtd.cookie_hours_left()),
            "error": _rtd_error,
        },
    }


@app.get("/api/manifest")
def manifest() -> dict:
    """数据来源摘要。数据实时查 MI，所以这里报告的是当前能查到的范围。"""
    s = src()
    runs = s.all_test_runs()
    lo, hi = s.history_span()
    return {
        "live": True,
        "source": "实时查询 MI（Manufacturing Intelligence）",
        "exported_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_queries": 0,
        "n_serials": len(s.available_serials()),
        "total_rows": len(runs),
        "products": s.available_products(),
        "date_from": lo.strftime("%Y-%m-%d %H:%M") if lo else None,
        "date_to": hi.strftime("%Y-%m-%d %H:%M") if hi else None,
        "size_mb": 0,
        "n_structures": len(s.available_structures()),
    }


@app.get("/api/options")
def options() -> dict:
    """给界面填下拉框用的可选值。"""
    s = src()
    lo, hi = s.date_range()
    return {
        "live": True,
        "products": s.available_products(),
        # 产品列表按字母排序显示，但默认值固定为"主产品"而不是列表第一个 ——
        # 加 Crete 后 316/11 字母序排到第一，用户熟悉的 330/11 反而不见。
        "default_product": DEFAULT_PRODUCT,
        "serials": s.available_serials(),
        "categories": s.available_categories(),
        "test_counts": s.available_test_counts(),
        "date_from": lo,
        "date_to": hi,
        "group_by": sorted(yc.GROUP_KEYS),
    }


@app.get("/api/refresh")
def refresh() -> dict:
    """丢掉内存缓存，下次查询重新向 MI 拉最新数据。

    生产线一直在产数据（实测半小时新增 1,537 次测试），所以需要能主动刷新。
    两份缓存都要清：测试历史（存活 60 秒）和装配结构（存活 10 分钟）。
    """
    s = src()
    s.refresh()
    _struct_cache_clear()
    return {"ok": True, "live": True, "message": "已清空缓存，下次查询取最新数据"}


# ---------------------------------------------------------------------------
# 测试历史：★ 支持任意日期范围
#
# 和下面 /api/yield 的本质区别：
#   /api/yield          读 TestPointSummary。那是 MI 按测试点聚合好的结果，
#                       没有时间戳，所以只能按 MI 支持的筛选条件查。
#   /api/history/yield  读测试历史明细。带 TestStartTime，服务端自己筛，
#                       所以任意日期都能查，没数据就返回 0 条而不是 404。
#
# ⚠️ 两者良率的分母不同，不可混用：
#   /api/yield          测试点级 = 1 − 失败测试点执行次数 ÷ 总执行次数（约 99.8%）
#   /api/history/yield  场次级   = 1 − 失败场次 ÷ 总场次（约 68%）
# 一次测试要跑上千个测试点，任一失败整场就算失败，所以场次良率必然低得多。
# 返回里的 level 和 说明 字段就是为了防止被误读成"数据错了"。
# ---------------------------------------------------------------------------

_RUNS_LIMIT = 500          # 场次明细默认上限，防止一次返回过大

# 供应商筛选要用到**全部整机**的装配结构，实时模式下这是网络请求。
#
# ★ 两个踩过的坑，改这段前先看：
#
# 1. 必须用批量并行接口。原来是串行 `{sn: s.structure(sn) for sn in ...}`，
#    80 台 x 约 12 秒 ≈ 16 分钟 —— 实测页面初始化挂住 5 分钟还没返回。
#    批量 8 路并行实测 171 秒。
#
# 2. 必须会过期。原来以 id(数据源) 为键、永不过期，服务跑一天就一直用
#    早上那份 —— 和已经删掉的文件缓存是同一类问题。
#
# 存活时间刻意比测试历史的 60 秒长很多，这是取舍不是笔误：
# 查一遍要 171 秒，给 60 秒就等于永远在重查；而装配结构只在产线换件时才变，
# 不像测试数据每分钟都在产生新的。想立刻要最新的就点「刷新」。
STRUCT_CACHE_TTL_SECONDS = 600

_struct_cache: dict[str, list] | None = None
_struct_cache_at: float = 0.0


def _struct_cache_clear() -> None:
    """立即丢掉装配结构缓存。"""
    global _struct_cache, _struct_cache_at
    _struct_cache = None
    _struct_cache_at = 0.0


def _all_structures() -> dict[str, list]:
    """全部整机的装配结构 {整机SN: 行数组}。批量并行取数，带存活时间。

    整机范围实时列（serials_for，最近半年），不是固定清单 —— 否则供应商
    台数统计、下层件追踪都只覆盖固定 80 台，漏掉产线新机。
    """
    global _struct_cache, _struct_cache_at
    fresh = (_struct_cache is not None
             and (time.monotonic() - _struct_cache_at) <= STRUCT_CACHE_TTL_SECONDS)
    if not fresh:
        s = src()
        hi = dt.datetime.now()
        lo = hi - dt.timedelta(days=183)
        sns = s.serials_for(product=None,
                            from_date=lo.strftime("%Y-%m-%d 00:00"),
                            to_date=hi.strftime("%Y-%m-%d 23:59"))
        _struct_cache = s.structures(sns) if sns else {}
        _struct_cache_at = time.monotonic()
    return _struct_cache


@app.get("/api/suppliers")
def list_suppliers(
    with_counts: bool = Query(False,
                              description="是否统计每家涉及几台整机（很慢）"),
) -> dict:
    """天线和滤波器有哪些供应商可选。

    ★ 默认**不查 MI**，瞬间返回。

    供应商名字的权威来源是 MI 的 UnitProductStructure 报表的 Mfg. Unit 列。
    实测 628 台的装配明细确认「序列号前 3 位 == Mfg.Unit 代码」且前缀与供应商
    严格一对一，所以选项直接查本地静态表（structure.SUPPLIER_NAMES）。

    产地不算供应商：春兴的苏州厂和深圳厂合并成一个 Chunxing 选项。

    with_counts=true 才会去统计"每家涉及几台整机"，那需要拉**全部整机**的
    装配结构 —— 实测 80 台 8 路并行 9.5 分钟。这个接口是页面一打开就调的，
    所以默认不做；没统计时 n_parents 是 null（不是 0）。
    """
    if not with_counts:
        return {
            **st_mod.known_suppliers(),
            "counted": False,
            "note": ("选项来自序列号前缀对照表，未统计台数（n_parents 为 null）。"
                     "要台数加 with_counts=true，但那要逐台拉装配结构，"
                     "实测超过 20 分钟都不返回 —— 基本不可用，别在界面上调。"),
        }
    opts = st_mod.supplier_options(_all_structures())
    return {
        **opts,
        "counted": True,
        "note": "台数来自实际装配记录。同一供应商的多个产地已合并。",
    }


def _filtered_runs(from_date, to_date, product, sn, category,
                   ant_supplier=None, fu_supplier=None):
    """按条件筛出场次。各条件都可留空。

    ★ 整机范围实时列：用户没填序列号、也没选供应商时，按 (产品, 日期) 实时
    问 MI 要该时段真实在产的全部整机（serials_for），而不是用固定清单。
    产线新产的机器也能算进良率。

    供应商筛选的做法：从 UnitProductStructure 拿"每台整机装了谁家的件"，
    再和序列号条件求交集。没有匹配的整机时结果为空（不是忽略该条件）。

    ⚠️⚠️ 供应商**绝不能**走 _all_structures()（逐台查 SerialNumberStructure）。
    那条路是两步慢操作叠加：列整机不限产品 + 183 天实测 **20 分钟不返回**，
    之后还要逐台查结构（档案实测 80 台 567 秒）。表现就是"选了供应商，
    查询按钮一直转圈"。现在改用 suppliers_for —— 它和上面的 serials_for
    共享同一个响应，实测拿全部 60 台供应商 **0.006 秒**。
    """
    import test_history as th
    s = src()

    sns = [x.strip() for x in sn.split(",") if x.strip()] if sn else None
    if ant_supplier or fu_supplier:
        # 按用户查的范围取（范围越窄越快），和列整机同一个响应、口径一致
        sup_map = s.suppliers_for(product=product or None,
                                  from_date=from_date or None,
                                  to_date=to_date or None)
        for part_type, supplier in (("antenna", ant_supplier),
                                    ("filter_unit", fu_supplier)):
            if not supplier:
                continue
            hit = {k for k, v in sup_map.items()
                   if supplier in (v.get(part_type) or [])}
            sns = sorted(hit) if sns is None else sorted(set(sns) & hit)

    # 供应商选了但一台都没匹配上 -> 结果必须是空，不能退化成"不筛"
    if sns is not None and not sns:
        return []

    # 用户没限定任何序列号时，实时列出该 (产品,日期) 下的全部在产整机。
    # 这就是替代固定 80 台清单的地方。
    pull = sns if sns is not None else s.serials_for(
        product=product or None, from_date=from_date or None,
        to_date=to_date or None)

    return th.filter_runs(
        s.all_test_runs(serials=pull),
        from_date=from_date or None,
        to_date=to_date or None,
        product=product or None,
        serial_numbers=sns,
        categories=[x.strip() for x in category.split(",") if x.strip()] if category else None,
    )


@app.get("/api/history/options")
def history_options() -> dict:
    """历史数据的可选范围。前端用它初始化日期选择器。

    date_free=true 是给前端的信号：日期可以自由填，想查哪段就查哪段。
    """
    s = src()
    lo, hi = s.history_span()
    return {
        "date_free": True,
        "n_serials": len(s.available_histories()),
        "n_runs": len(s.all_test_runs()),
        "date_from": lo.strftime("%Y-%m-%d %H:%M:%S") if lo else None,
        "date_to": hi.strftime("%Y-%m-%d %H:%M:%S") if hi else None,
        "categories": s.history_categories(),
        "products": s.history_products(),
        "serials": s.history_serials(),
        "trend_by": ["hour", "day", "week", "month"],
    }


@app.get("/api/history/yield")
def history_yield(
    from_date: str | None = Query(None, description="开始时间，留空=不限"),
    to_date: str | None = Query(None, description="结束时间，留空=不限"),
    product: str | None = Query(None, description="产品号，留空=全部"),
    sn: str | None = Query(None, description="序列号，逗号分隔，留空=全部"),
    category: str | None = Query(None, description="测试类别，逗号分隔"),
    ant_supplier: str | None = Query(None, description="天线供应商（公司名，如 Comba）"),
    fu_supplier: str | None = Query(None, description="滤波器供应商（公司名，如 Grentech）"),
) -> dict:
    """★ 本项目唯一的良率接口：场次级 FPY。按任意日期范围算。

    口径固定，不提供开关：
      - 场次级 —— 一次整机测试算一个场次，任一测试点失败整场算失败
      - 只算 FPY —— 每台只取 TestCount=1 的那一场，复测修好的不算，
        所以分母天然就是台数

    没数据返回 0 条，不会 404。
    """
    import test_history as th
    runs = _filtered_runs(from_date, to_date, product, sn, category,
                          ant_supplier, fu_supplier)
    out = th.run_yield(runs, first_only=True)
    lo, hi = th.date_span(runs)
    out.update({
        "fpy": True,
        "说明": "场次级 FPY：每台只算第一次测试（TestCount=1），一次测试跑上千个"
                "测试点，任一失败整场算失败。这是本项目唯一的良率口径。"
                "/api/yield 给的是测试点次数级失败率，分母不同，不是良率。",
        "n_serials": len({r.serial_number for r in runs}),
        "actual_from": lo.strftime("%Y-%m-%d %H:%M:%S") if lo else None,
        "actual_to": hi.strftime("%Y-%m-%d %H:%M:%S") if hi else None,
    })
    return out


@app.get("/api/history/trend")
def history_trend(
    by: str = Query("day", description="时间粒度 hour/day/week/month"),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    product: str | None = Query(None),
    sn: str | None = Query(None),
    category: str | None = Query(None),
    ant_supplier: str | None = Query(None),
    fu_supplier: str | None = Query(None),
) -> dict:
    """FPY 随时间的变化。口径和 /api/history/yield 完全一致（场次级 FPY）。

    注意：只有第一次测试进桶，所以某个时间段里如果全是复测，那个桶不会出现。
    """
    import test_history as th
    runs = _filtered_runs(from_date, to_date, product, sn, category,
                          ant_supplier, fu_supplier)
    try:
        rows = th.trend(runs, by=by, first_only=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"by": by, "level": "run", "fpy": True, "rows": rows}


@app.get("/api/history/runs")
def history_runs(
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    product: str | None = Query(None),
    sn: str | None = Query(None),
    category: str | None = Query(None),
    ant_supplier: str | None = Query(None),
    fu_supplier: str | None = Query(None),
    only_failed: bool = Query(False, description="只看失败的场次"),
    with_branches: bool = Query(False,
                                description="失败场次附带失败分支（每台约30秒）"),
    limit: int = Query(_RUNS_LIMIT, ge=1, le=5000),
) -> dict:
    """测试结果列表（测试场次明细）。带 truncated 标记，避免前端以为拿到了全部。

    每行带产品号、序列号、天线/滤波器供应商、测试结果。
    `with_branches=true` 时，**失败的**场次还会附带失败分支号。

    ★ 供应商走 UnitProductStructure（一次请求 15 秒拿全部整机，而且和
    "实时列整机"共享同一个响应，零额外成本）。**绝不要**改成逐台查
    SerialNumberStructure —— 那个 60 台要 270 秒，会把查询卡死（踩过）。
    """
    s = src()
    runs = _filtered_runs(from_date, to_date, product, sn, category,
                          ant_supplier, fu_supplier)
    if only_failed:
        runs = [r for r in runs if not r.passed]
    total = len(runs)

    page = runs[:limit]

    # 供应商：和"实时列整机"共享同一个 UnitProductStructure 响应，不额外花时间
    sup_map = s.suppliers_for(product=product or None,
                              from_date=from_date or None,
                              to_date=to_date or None)

    # 失败分支：只对**失败的**整机查（单台约 30 秒，8 路并行）。
    # 通过的机器查了是白等，所以按需、且只传失败的。
    br_map: dict[str, list[int]] = {}
    if with_branches:
        failed_sns = sorted({r.serial_number for r in page
                             if not r.passed and r.serial_number})
        if failed_sns:
            br_map = s.failed_branches_for(failed_sns)

    rows = []
    for r in page:
        sup = sup_map.get(r.serial_number) or {}
        rows.append({
            "serial_number": r.serial_number,
            "product": r.product,
            "revision": r.revision,
            "criteria": r.criteria,
            "started": r.started.strftime("%Y-%m-%d %H:%M:%S") if r.started else None,
            "ended": r.ended.strftime("%Y-%m-%d %H:%M:%S") if r.ended else None,
            "duration_min": r.duration_min,
            "passed": r.passed,
            "test_count": r.test_count,
            "category": r.category,
            "station": r.station,
            "factory": r.factory,
            "ant_suppliers": sup.get("antenna", []),
            "fu_suppliers": sup.get("filter_unit", []),
            # 只有失败场次才有失败分支（用户明确要求 pass 不加）
            "failed_branches": (br_map.get(r.serial_number, [])
                                if not r.passed else []),
        })
    return {"total": total, "returned": len(rows),
            "truncated": total > len(rows), "rows": rows}


@app.get("/api/history/branches")
def history_branches(
    sns: str = Query("", description="整机序列号，逗号分隔。只传**失败的**那些。"),
) -> dict:
    """这些整机各自失败在哪些分支上。返回：
       {branches: {sn: [...]}, testpoints: {sn: {started: [...]}}, n: N}

    为什么单独开一个接口：前端要「测试结果先显示、失败分支稍后补上」。
    查分支要逐台问 MI 的子件历史（单台约 30 秒，8 路并行），很慢；
    而场次数据前端已经有了，不该为了拿分支把 /api/history/runs 整个重拉
    一遍（那会连带重新拉全部测试历史，实测几十秒，纯浪费）。

    ★ branches 和 testpoints 一次 MI 请求同时得到（同数据源 ChildSerialNumberTestHistory）。
    ★ 只传失败的整机进来。给通过的整机查是白等几十秒。
    """
    want = [x.strip() for x in (sns or "").split(",") if x.strip()]
    if not want:
        return {"branches": {}, "testpoints": {}, "n": 0}
    try:
        detail = _source.failed_detail_for(want)
    except Exception as e:
        # ★ 把 MI 异常变成 502 + 具体错误消息。原来 data_source 层静默吞了，
        #   用户看到"查询失败"4 个字但不知道具体原因，调试需要翻日志。
        #   现在错误直达前端，用户和开发都能一眼看到真实问题。
        raise HTTPException(502, f"分支/测试点查询失败：{type(e).__name__}: {e}")
    return {"branches": detail["branches"],
            "testpoints": detail["testpoints"],
            "n": len(detail["branches"])}


@app.get("/api/history/repairs")
def history_repairs(
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    sn: str | None = Query(None),
    limit: int = Query(_RUNS_LIMIT, ge=1, le=5000),
) -> dict:
    """维修记录：什么故障、换了什么、花了多久。"""
    import test_history as th
    s = src()
    reps = s.all_test_repairs()
    lo, hi = th._as_dt(from_date), th._as_dt(to_date)
    sns = {x.strip().upper() for x in sn.split(",") if x.strip()} if sn else None
    out = []
    for r in reps:
        t = r.started or r.test_started
        if lo is not None and (t is None or t < lo):
            continue
        if hi is not None and (t is None or t >= hi):
            continue
        if sns is not None and r.serial_number.upper() not in sns:
            continue
        out.append(r)
    total = len(out)
    rows = [{
        "serial_number": r.serial_number,
        "test_started": r.test_started.strftime("%Y-%m-%d %H:%M:%S") if r.test_started else None,
        "started": r.started.strftime("%Y-%m-%d %H:%M:%S") if r.started else None,
        "ended": r.ended.strftime("%Y-%m-%d %H:%M:%S") if r.ended else None,
        "minutes": ((r.ended - r.started).total_seconds() / 60.0
                    if r.started and r.ended and r.ended >= r.started else None),
        "fault": r.fault,
        "fault_area": r.fault_area,
        "position": r.position,
        "action": r.action,
    } for r in out[:limit]]
    return {"total": total, "returned": len(rows),
            "truncated": total > len(rows), "rows": rows}


# ---------------------------------------------------------------------------
# 良率
# ---------------------------------------------------------------------------

@app.get("/api/yield")
def get_yield(
    product: str = Query(..., description="产品号，如 KRD 901 330/11"),
    from_date: str = Query(..., description="开始时间 YYYY-MM-DD HH:MM"),
    to_date: str = Query(..., description="结束时间"),
    serial_numbers: str | None = Query(None, description="逗号分隔；留空=全部"),
    test_category: str | None = Query(None, description="逗号分隔；PIM 是 212"),
    test_count: str = Query("[All]", description="[All] / First(=FPY) / Last"),
    group_by: str | None = Query(None, description="分组维度"),
    only_pim: bool = Query(True, description="只看 PIM 测试点"),
) -> dict:
    """测试点次数级的失败统计与分组定位。

    ⚠️ 这个接口**不给良率**。良率只有一种口径：场次级 FPY，在
    /api/history/yield。这里的分母是"测试点被执行的次数"（量级十万），
    算出来总是 99.x%，当良率用会严重高估。字段名叫 fail_rate_pct。

    批量查询（多个序列号）时 MI 返回的 %Fail 列恒为 0 不可用，
    所以失败次数是用 [All] 和 Failed 两次请求的测试次数相除算出来的。
    """
    if group_by and group_by not in yc.GROUP_KEYS:
        raise HTTPException(400, f"不支持按 {group_by!r} 分组。"
                                 f"可用：{sorted(yc.GROUP_KEYS)}")

    sns, cats = _split(serial_numbers), _split(test_category)
    all_rows, fail_rows, resolved = _fetch_pair(product, from_date, to_date,
                                                sns, cats, test_count, only_pim)
    fmap = yc.build_freq_map(all_rows)
    summary = yc.yield_from_pair(all_rows, fail_rows)

    groups: list[dict] = []
    if group_by:
        for g in yc.yield_by_pair(all_rows, fail_rows, group_by, freq_map=fmap):
            groups.append({
                "key": str(g["key"]),
                "total_tested": g["total_tested"],
                "total_failed": g["total_failed"],
                "fail_rate_pct": g["fail_rate_pct"],
                "n_test_points": g["n_test_points"],
                "n_failed_points": g["n_failed_points"],
                "avg_value": g["avg_value"],
                "avg_power": g["avg_power"],
                "avg_passrate": g["avg_passrate"],
                "value_kinds": g["value_kinds"],
            })

    return {
        "summary": summary,
        "groups": groups,
        "group_by": group_by,
        # level 是给调用方的硬提示：这是测试点级，不是整机级
        "level": "testpoint",
        "test_count": test_count,
        "is_fpy": test_count == "First",
        "only_pim": only_pim,
        "n_serials": len(sns),
        # 实际用的查询区间（实时查询恒等于用户填的，保留供前端显示）
        "resolved": resolved,
    }


@app.get("/api/failures")
def get_failures(
    product: str = Query(...),
    from_date: str = Query(...),
    to_date: str = Query(...),
    serial_numbers: str | None = Query(None),
    test_category: str | None = Query(None),
    test_count: str = Query("[All]"),
    only_pim: bool = Query(True),
) -> dict:
    """失败明细。Failed 模式下 #Tested 的语义是失败次数。"""
    sns, cats = _split(serial_numbers), _split(test_category)
    all_rows, fail_rows, resolved = _fetch_pair(product, from_date, to_date,
                                                sns, cats, test_count, only_pim)
    fmap = yc.build_freq_map(all_rows)
    items = []
    for r in yc.parse_rows(fail_rows, freq_map=fmap):
        items.append({
            "code": r["code"], "branch": r["branch"], "freq_mhz": r["freq_mhz"],
            "measure": r["measure"], "group": r["group"],
            "category": r["category"], "value_kind": r["value_kind"],
            "n_failed": int(r["tested"]),        # Failed 模式: tested = 失败次数
            "low_limit": r["low_limit"], "high_limit": r["high_limit"],
            "lowest": r["lowest"], "highest": r["highest"], "avg": r["avg"],
        })
    items.sort(key=lambda x: -x["n_failed"])

    # 分支 x 频率的失败热力图数据
    heat: dict[str, dict[str, int]] = {}
    for it in items:
        if it["branch"] is None or it["freq_mhz"] is None:
            continue
        heat.setdefault(str(it["branch"]), {})
        k = f"{it['freq_mhz']:g}"
        heat[str(it["branch"])][k] = heat[str(it["branch"])].get(k, 0) + it["n_failed"]

    return {"items": items, "heatmap": heat,
            "freq_order": [f"{f:g}" for f in yc.FREQ_ORDER]}


@app.get("/api/testpoints")
def get_testpoints(
    product: str = Query(...),
    from_date: str = Query(...),
    to_date: str = Query(...),
    sn: str = Query(..., description="单个整机序列号"),
    test_category: str | None = Query(None),
    test_count: str = Query("[All]"),
    only_pim: bool = Query(True),
) -> dict:
    """单台明细。**单台查询的 %Fail 列是可信的**，可以直接用。"""
    s = src()
    try:
        rows = s.test_points(product=product, from_date=from_date, to_date=to_date,
                             serial_numbers=[sn], test_category=_split(test_category),
                             test_count=test_count, only_failed=False)
    except KeyError as e:
        raise _not_found(str(e).strip('"').replace("\\n", "\n"))

    fmap = yc.build_freq_map(rows)
    use = yc.pim_only(rows) if only_pim else rows
    recs = yc.parse_rows(use, freq_map=fmap, sn=sn)
    fb = yc.failed_branches(rows)

    items = [{
        "code": r["code"], "branch": r["branch"], "freq_mhz": r["freq_mhz"],
        "measure": r["measure"], "value_kind": r["value_kind"],
        "category": r["category"], "tested": int(r["tested"]),
        "pct_fail": r["pct_fail"], "low_limit": r["low_limit"],
        "high_limit": r["high_limit"], "lowest": r["lowest"],
        "highest": r["highest"], "avg": r["avg"], "std": r["std"], "cpk": r["cpk"],
    } for r in recs]

    return {
        "sn": sn,
        "summary": yc.yield_summary(use),
        "failed_branches": {str(k): v for k, v in sorted(fb.items())},
        "n_failed_branches": len(fb),
        # 18 个以上分支同时失败不像器件坏，更像测试系统异常
        "abnormal": len(fb) >= 18,
        "items": items,
    }


# ---------------------------------------------------------------------------
# 下层件
# ---------------------------------------------------------------------------

def _part_index(part_types: set[str] | None = None,
                product: str | None = None,
                from_date: str | None = None,
                to_date: str | None = None) -> dict[str, list[dict]]:
    """子件 → 装过它的整机列表。

    ★ 读 _ups_cache 里已缓存的 UPS 响应构建反查索引；**不发任何 MI 请求**。

    **联动模式**（传了 product/from_date/to_date 任一）：
    - 只用 key=(product, from_date, to_date) 对应的那一个响应构建索引
    - 对应 key 不在缓存里 → 返回空（避免调 _ups 撞到 "product=None 必填组
      爆炸" 这个档案记录过 20 分钟不返回的坑）
    - 和前面"查询条件"联动，界面显示和实际过滤结果一致

    **累积模式**（三个参数都不传，向后兼容）：
    - 遍历所有已缓存的 UPS 响应合并索引

    代价：UnitProductStructure.Data1 没有装入/拆除时间字段，installed_at/
    removed_at 恒为 None（前端 `|| '?'` 兜底）。
    """
    s = src()
    from mi_client import parts_index_from_unit_structure

    has_cond = (product is not None or from_date is not None
                or to_date is not None)
    merged: dict[str, list[dict]] = {}

    if has_cond:
        key = (product, from_date, to_date)
        ups_obj = s._ups_cache.get(key)
        if ups_obj is None:
            return {}
        try:
            merged = parts_index_from_unit_structure(ups_obj)
        except Exception:
            return {}
    else:
        seen: set[tuple[str, str]] = set()
        for ups_obj in list(s._ups_cache.values()):
            try:
                idx = parts_index_from_unit_structure(ups_obj)
            except Exception:
                continue
            for sn, spans in idx.items():
                for sp in spans:
                    k = (sn, sp["parent_sn"])
                    if k in seen:
                        continue
                    seen.add(k)
                    merged.setdefault(sn, []).append(sp)

    if part_types:
        out = {}
        for k, v in merged.items():
            filtered = [x for x in v if x["part_type"] in part_types]
            if filtered:
                out[k] = filtered
        return out
    return merged


@app.get("/api/parts")
def list_parts(
    product: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
) -> dict:
    """列出有哪些下层件，按"装过几台整机"降序（换过机器的最值得看）。

    传 (product, from_date, to_date) 时和前面的查询条件联动 —— 只用那次
    查询对应的 UPS 响应（已经被 doQuery 期间的 suppliers_for 填进缓存）。

    返回：
      - antenna / filter_unit：按子件维度的清单
      - by_parent：**反向索引** {parent_sn: {antenna: [...], filter_unit: [...]}}
        前端"按整机反查"流程用它做级联下拉：
        先选整机 SN → 显示这台装过的子件 → 查看该整机的测试曲线。
    """
    out: dict[str, list[dict] | dict] = {}
    for kind in ("antenna", "filter_unit"):
        idx = _part_index({kind}, product, from_date, to_date)
        items = [{
            "child_sn": sn,
            "child_product": spans[0].get("child_product", ""),
            "n_parents": len({x["parent_sn"] for x in spans}),
            "n_spans": len(spans),
        } for sn, spans in idx.items()]
        items.sort(key=lambda x: (-x["n_parents"], x["child_sn"]))
        out[kind] = items
    # ★ 反向索引：parent -> {antenna/filter_unit -> [child_sn...]}
    # 用全部 part_types(不过滤)构建一次，key 为 parent_sn，便于前端级联下拉
    full_idx = _part_index(None, product, from_date, to_date)
    by_parent: dict[str, dict[str, list[dict]]] = {}
    for child_sn, spans in full_idx.items():
        for sp in spans:
            parent = sp["parent_sn"]
            pt = sp.get("part_type", "")
            if pt not in ("antenna", "filter_unit"):
                continue
            slot = by_parent.setdefault(parent,
                                        {"antenna": [], "filter_unit": []})
            # 去重：同一父子对可能在 spans 里出现多次（多次装卸）
            if not any(x["child_sn"] == child_sn for x in slot[pt]):
                slot[pt].append({
                    "child_sn": child_sn,
                    "child_product": sp.get("child_product", ""),
                })
    out["by_parent"] = by_parent
    return out


@app.get("/api/parts/{child_sn}")
def track_part(
    child_sn: str,
    product: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
) -> dict:
    """追踪一个下层件：它装过哪些整机，各次装配时那台机器的失败分支。

    用来回答"故障跟着这个件走，还是跟着整机走"。

    传 (product, from_date, to_date) 时联动前面的查询条件（只在那次查询
    对应的 UPS 响应里找这个子件）。
    """
    idx = _part_index(product=product, from_date=from_date, to_date=to_date)
    spans = idx.get(child_sn)
    if not spans:
        raise HTTPException(404, f"查不到 {child_sn} 的装配记录")

    s = src()
    sorted_spans = sorted(spans, key=lambda x: x["installed_at"] or "")
    # ★ 一次批量查失败分支（failed_branches_for 支持批量 + 内部并行）。
    # 原来遍历 s.entries() 查，但 entries() 实时模式下永远返回 []，
    # 循环根本不执行 —— 失败分支全空，前端显示"该整机无 PIM 失败记录"。
    parent_sns = [sp["parent_sn"] for sp in sorted_spans]
    try:
        br_map = s.failed_branches_for(parent_sns)
    except Exception:
        br_map = {}

    out_spans = []
    for sp in sorted_spans:
        parent = sp["parent_sn"]
        # failed_branches_for 返回 {sn: [int分支号, ...]}。前端期望
        # {str: 数值} dict —— 这里给每个分支填 1（前端只用 key 不看 value）。
        fb = {str(b): 1 for b in (br_map.get(parent) or [])}
        out_spans.append({
            "parent_sn": parent,
            "parent_product": sp.get("parent_product", ""),
            "child_product": sp.get("child_product", ""),
            "part_type": sp.get("part_type", ""),
            "installed_at": sp["installed_at"],
            "removed_at": sp["removed_at"],
            "failed_branches": fb,
        })

    # 跨整机重复出现的分支 —— 这些才可能"跟着件走"
    counts: dict[str, int] = {}
    for sp in out_spans:
        for b in sp["failed_branches"]:
            counts[b] = counts.get(b, 0) + 1
    repeated = sorted([b for b, n in counts.items() if n > 1], key=int)

    return {
        "child_sn": child_sn,
        "part_type": spans[0].get("part_type", ""),
        "child_product": spans[0].get("child_product", ""),
        "n_parents": len({x["parent_sn"] for x in spans}),
        "spans": out_spans,
        "repeated_branches": repeated,
    }


# ---------------------------------------------------------------------------
# 多台同屏曲线（RTD 风格）—— 实时从 MI 取数
#
# ★ 这两个接口会**真的逐台请求 MI**，单台 28~70 秒。
#   原因：MI 批量查回来的是按测试点聚合的统计，分不出哪行属于哪台机器，
#   画不了"一条线一台"。所以 N 台就是 N 个请求，8 路并行。
#
#   所以台数有上限（默认 8 台约 1 分钟，硬上限 16 台约 2 分钟）。
#   原来这里读 analyze.py 产出的 data/out/pim_measures.csv，能一次画 79 台，
#   但那份文件是 9 月 29 日生成的、之后再没更新 —— 图上一直是那天的数据。
# ---------------------------------------------------------------------------

@app.get("/api/curves/options")
def curve_options(
    product: str = Query(..., description="产品号，如 KRD 901 330/11"),
    from_date: str = Query(..., description="开始时间 YYYY-MM-DD HH:MM"),
    to_date: str = Query(..., description="结束时间"),
    serial_numbers: str | None = Query(None, description="逗号分隔；留空=自动取"),
    test_category: str | None = Query(None, description="逗号分隔；PIM 是 212"),
) -> dict:
    """有哪些测量类型和频率可画，以及每类的数值语义。

    前端必须靠 value_kind 决定纵轴标题和单位：功率是 dBm，
    通过率是 0~1，两者绝不能画进同一张图。

    ★ 只查 1 台就能得出可选项（实测各台的测试点结构完全一致），
    所以这个接口约 28~70 秒，而不是乘以台数。
    """
    s = src()
    # 留空时实时列该 (产品,日期) 下的全部在产整机（不是固定清单）
    sns = _split(serial_numbers) or s.serials_for(
        product=product, from_date=from_date, to_date=to_date)
    if not sns:
        raise HTTPException(404, "没有可查的序列号。请在上方查询条件里填序列号。")
    idx = curves.from_mi(s, product=product, from_date=from_date,
                         to_date=to_date, serials=sns[:1],
                         test_category=_split(test_category) or None,
                         max_serials=1)
    kinds = idx.kinds()
    if not kinds:
        raise HTTPException(
            404, f"{sns[0]} 在 {from_date} ~ {to_date} 没有可画的 PIM 测量数据。"
                 f"换个日期范围或确认这台机器测过 PIM（类别 212）。")
    return {
        "kinds": kinds,
        "serials": sns,
        "n_serials": len(sns),
        "probed_sn": sns[0],
        "live": True,
        "elapsed_s": round(idx.elapsed_s, 1),
        "default_max_series": curves.DEFAULT_MAX_SERIALS,
        "max_series_limit": curves.MAX_SERIALS_HARD_LIMIT,
        "note": (f"数据实时查 MI，单台约 28~70 秒。最多同屏 "
                 f"{curves.MAX_SERIALS_HARD_LIMIT} 台。"),
    }


@app.get("/api/curves")
def get_curves(
    product: str = Query(..., description="产品号，如 KRD 901 330/11"),
    from_date: str = Query(..., description="开始时间 YYYY-MM-DD HH:MM"),
    to_date: str = Query(..., description="结束时间"),
    kind: str = Query(..., description="测量类型，如 noise_power_level_after_testing"),
    freq_mhz: float = Query(..., description="频率 MHz"),
    serial_numbers: str | None = Query(None, description="逗号分隔；留空=自动取"),
    test_category: str | None = Query(None, description="逗号分隔；PIM 是 212"),
    max_series: int = Query(curves.DEFAULT_MAX_SERIALS, ge=1,
                            le=curves.MAX_SERIALS_HARD_LIMIT,
                            description="最多查几台（每台 28~70 秒）"),
) -> dict:
    """一组 (测量类型, 频率) 下每台机器沿 32 个分支的曲线。

    返回列式紧凑结构：横轴 `branches` 一份，每台一个纯数值数组 `y`。
    缺失的分支是 `null` 占位，前端不能把它当 0 画。

    ★ 会真的逐台请求 MI，max_series 台就是 max_series 个请求。
    返回里的 elapsed_s / truncated / failed_serials 用来告诉用户实际情况。
    """
    s = src()
    # 留空时实时列该 (产品,日期) 下的全部在产整机（不是固定清单）
    sns = _split(serial_numbers) or s.serials_for(
        product=product, from_date=from_date, to_date=to_date)
    if not sns:
        raise HTTPException(404, "没有可查的序列号。请在上方查询条件里填序列号。")
    idx = curves.from_mi(s, product=product, from_date=from_date,
                         to_date=to_date, serials=sns,
                         test_category=_split(test_category) or None,
                         max_serials=max_series)
    try:
        return idx.curves(kind=kind, freq_mhz=freq_mhz)
    except KeyError as e:
        avail = [k["kind"] for k in idx.kinds()]
        raise HTTPException(
            404, f"{e}。这批机器可用的测量类型：{', '.join(avail) or '（无）'}")


# ---------------------------------------------------------------------------
# 单次测试明细（RTD 逐迭代曲线）
#
# 这是 MI 之外的第二条数据源，补的是 MI 结构上给不了的东西：
#   MI   存判定结果 —— knock 类在 MI 里就是 Limit=EQU/1、Value=1 一个标量
#   RTD  存判定依据 —— 同一个测试点背后 200 个采样点的完整曲线
# 已实测确认 MI 的报表接口和 blob 里的 RTD_TestRecord.json 都拿不到这些点
# （后者有 CurveValues 字段但实测 1858 个测量点全是空数组）。
#
# 和上面所有接口的三个不同，都是设计上刻意的：
#   1. 认证不同。MI 走 Kerberos（域身份，不过期），RTD 走浏览器 Cookie
#      （实测约 11 小时过期）。所以 RTD 不可用是常态而非异常，
#      必须降级成 503 + 可读提示，不能拖垮别的接口。
#   2. 会话字典不外泄。RTD 的会话有 18 个字段，其中 id 是
#      {"device_id":..., "start_time":...} 的嵌套结构且带毫秒。
#      对外只认 device_id + start_time 两个字符串，内部用缓存找回原始字典。
#   3. 频率必填。沿用别的机器的频率会**静默返回 0 条曲线**（HTTP 200
#      且不报错），是实测踩过的坑，所以接口层不给默认值。
# ---------------------------------------------------------------------------

RTD_COOKIE_FILE = Path(
    os.environ.get("PIM_RTD_COOKIES", HERE / "data/rtd/cookies.json"))

_rtd: Any = None
_rtd_error: str | None = None

try:
    import rtd_client
    _rtd = rtd_client.RtdClient(cookie_file=RTD_COOKIE_FILE)
except Exception as e:                                   # pragma: no cover
    # 没有 cookie 文件是常见情况（比如服务器上），不该让整个服务起不来
    _rtd_error = f"{type(e).__name__}: {e}"

# device_id|start_time -> RTD 原始会话字典。
# 前端只传两个字符串，取曲线时要靠这里找回完整字典。
_rtd_sess_cache: dict[str, dict] = {}
# 哪些日期已经全量查过（全量 = 不带任何过滤条件）
_rtd_dates_loaded: set[str] = set()


def rtd_sessions_cache_clear() -> None:
    """清掉会话缓存。测试用，也可在凭据换了之后手动调。"""
    _rtd_sess_cache.clear()
    _rtd_dates_loaded.clear()


def _rtd_key(device_id: str, start_time: str) -> str:
    return f"{device_id}|{start_time}"


def _rtd_or_503():
    """拿 RTD 客户端，不可用就给出能照着做的提示。"""
    if _rtd is None:
        raise HTTPException(
            503,
            f"RTD 不可用：{_rtd_error}。"
            f"在 Windows 上跑 refresh_cookies.py 抓一份凭据到 "
            f"{RTD_COOKIE_FILE}。")
    if _rtd.cookie_expired():
        left = _rtd.cookie_hours_left()
        raise HTTPException(
            503,
            f"RTD 凭据已过期（{left:.1f} 小时前失效）。"
            f"在 Windows 上重跑 refresh_cookies.py 刷新。")
    return _rtd


def _rtd_cache(rows: list[dict]) -> None:
    for r in rows:
        k = _rtd_key(str(r.get("device_id")), str(r.get("start_time_short")))
        _rtd_sess_cache[k] = r


def _rtd_public(r: dict) -> dict:
    """把 RTD 的内部会话字典收敛成对外字段。

    刻意不透传 id / prod_slash / Testcat 这些 RTD 自己的命名和结构——
    那是别人家的内部实现，进了我们的接口契约就换不掉了。
    """
    return {
        "device_id": r.get("device_id"),
        "start_time": r.get("start_time_short"),
        "product": r.get("prod_slash"),
        "rstate": r.get("rstate"),
        "testcat": r.get("Testcat"),
        "status": r.get("Status"),
        "factory": r.get("factory_name"),
        "station": r.get("test_station_id"),
        "test_count": r.get("test_count"),
        "n_testpoints": r.get("testpoints_count"),
        "first_fail": r.get("first_fail"),
    }


def _rtd_find(date: str, device_id: str, start_time: str) -> dict:
    """按 device_id + start_time 找回 RTD 的原始会话字典。

    缓存没有就把那天的全量拉一次。RTD 的会话接口本来就是按天返回全量
    （实测一整天约 2 万条、6 秒），所以多拉一次不额外增加调用次数。
    """
    k = _rtd_key(device_id, start_time)
    if k in _rtd_sess_cache:
        return _rtd_sess_cache[k]
    if date not in _rtd_dates_loaded:
        c = _rtd_or_503()
        _rtd_cache(c.sessions(date, date))
        _rtd_dates_loaded.add(date)
    if k in _rtd_sess_cache:
        return _rtd_sess_cache[k]
    raise HTTPException(
        404,
        f"{date} 没有 device_id={device_id} 且 start_time={start_time} "
        f"的测试会话。先用 /api/sessions 列出当天的会话。")


@app.get("/api/sessions")
def rtd_sessions(
    date: str = Query(..., description="日期 YYYY-MM-DD，RTD 按天返回"),
    product: str | None = Query(None, description="产品号，如 KRD 901 330/11"),
    testcat: str | None = Query(None, description="测试类别包含该字符串，如 RADIO_PIM"),
    status: str | None = Query(None, description="Pass 或 Fail"),
    sn: str | None = Query(None, description="整机序列号"),
) -> dict:
    """某一天的测试会话列表，一行一次测试。

    过滤是在服务端本地做的（RTD 接口只认日期），所以加过滤不会更快，
    只是返回更少。
    """
    c = _rtd_or_503()
    try:
        rows = c.sessions(date, date, product=product,
                          testcat_contains=testcat, status=status,
                          device_id=sn)
    except Exception as e:
        raise HTTPException(502, f"RTD 取会话失败：{type(e).__name__}: {e}")
    _rtd_cache(rows)
    if not any([product, testcat, status, sn]):
        _rtd_dates_loaded.add(date)
    return {
        "date": date,
        "n_sessions": len(rows),
        "sessions": [_rtd_public(r) for r in rows],
        "cookie_hours_left": c.cookie_hours_left(),
    }


@app.get("/api/session/options")
def rtd_session_options(
    date: str = Query(..., description="该会话所在日期 YYYY-MM-DD"),
    device_id: str = Query(..., description="整机序列号"),
    start_time: str = Query(..., description="会话开始时间，照抄 /api/sessions 给的值"),
) -> dict:
    """这一次测试有哪些频率 / 分支 / 迭代可选。

    **必须对每个会话单独查。** 这些选项是按会话动态算的，沿用别的机器的
    频率会静默返回 0 条曲线。频率对外一律用 MHz（和 MI 侧统一）。
    """
    sess = _rtd_find(date, device_id, start_time)
    c = _rtd_or_503()
    try:
        o = c.knock_options(sess)
    except Exception as e:
        raise HTTPException(502, f"RTD 取可选参数失败：{type(e).__name__}: {e}")
    return {
        "device_id": device_id,
        "start_time": start_time,
        "freqs_mhz": [f / 1e6 for f in o["freqs"]],
        "branches": o["branches"],
        "iterations": o["iterations"],
    }


@app.get("/api/session/curves")
def rtd_session_curves(
    date: str = Query(..., description="该会话所在日期 YYYY-MM-DD"),
    device_id: str = Query(..., description="整机序列号"),
    start_time: str = Query(..., description="会话开始时间，照抄 /api/sessions 给的值"),
    freq_mhz: float = Query(..., description="频率 MHz，必填：填错会静默返回空曲线"),
    branches: str | None = Query(None, description="逗号分隔的分支号，不填为全部 32 个"),
    iterations: str | None = Query(None, description="逗号分隔的迭代号，不填为全部"),
) -> dict:
    """单次测试的逐迭代曲线，一条线一个分支。

    返回列式结构：横轴 `x` 共享一份，每个分支一个纯数值数组 `y`。
    实测列式比行式省 5.7 倍传输量。

    ★ `x_title` 照搬 RTD 原文。它的值实测是 0.0~61.38 的浮点而不是
      1..20 的迭代序号，真实含义尚未确认，所以界面上不要自己标成
      「第 N 次迭代」。
    """
    sess = _rtd_find(date, device_id, start_time)
    c = _rtd_or_503()
    brs = [int(x) for x in _split(branches)] or None
    its = [int(x) for x in _split(iterations)] or None
    try:
        return c.knock_curves(sess, freq_hz=freq_mhz * 1e6,
                              branches=brs, iterations=its)
    except Exception as e:
        raise HTTPException(502, f"RTD 取曲线失败：{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 从界面触发凭据刷新
#
# ⚠ 这是整个服务里**唯一有副作用的接口**，别的全是只读 GET。它会启动一个
#   真实的浏览器进程，所以必须三重设限，缺一个都不要留着：
#     1. 总开关 PIM_ALLOW_RTD_REFRESH=0 可以随时整个关掉
#     2. 只接受来自回环地址的请求，且带任何代理转发头的一律拒绝
#     3. 同时只能跑一个（两个浏览器抢同一个 profile 目录会互相破坏）
#
# 为什么能全自动：refresh_cookies.py 用独立 profile 存登录态，只有连 profile
# 里的单点登录也失效了才需要人工在弹窗里登录。Cookie 11 小时过期是常态，
# profile 失效是偶发 —— 所以多数时候点一下就好，不用管弹出的窗口。
#
# 为什么不在本进程里直接跑 Playwright：sync_playwright 不能在已有事件循环的
# 线程里跑，而且浏览器崩溃会带崩整个服务。开子进程最省心。
# ---------------------------------------------------------------------------

RTD_REFRESH_ENABLED = os.environ.get(
    "PIM_ALLOW_RTD_REFRESH", "1").strip().lower() not in ("0", "false", "no")

_rtd_refresh: dict[str, Any] = {
    "state": "idle",        # idle | running | done | failed
    "log": [],
    "returncode": None,
    "started_at": None,
    "finished_at": None,
}
_rtd_refresh_lock = threading.Lock()


def _rtd_refresh_reset() -> None:
    """测试用：回到没跑过的状态。"""
    with _rtd_refresh_lock:
        _rtd_refresh.update(state="idle", log=[], returncode=None,
                            started_at=None, finished_at=None)


def _rtd_refresh_worker() -> None:
    """后台跑刷新脚本，边跑边把输出收进 log 供界面轮询。"""
    script = HERE / "refresh_cookies.py"
    cmd = [sys.executable, str(script)]
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(HERE), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1)
    except Exception as e:
        with _rtd_refresh_lock:
            _rtd_refresh.update(
                state="failed", returncode=-1,
                finished_at=dt.datetime.now().isoformat(timespec="seconds"),
                log=[f"起不来：{type(e).__name__}: {e}"])
        return

    for line in iter(proc.stdout.readline, ""):
        line = line.rstrip()
        if not line:
            continue
        with _rtd_refresh_lock:
            # 日志只留最近 200 行，脚本偶尔会刷很多等待信息
            _rtd_refresh["log"] = (_rtd_refresh["log"] + [line])[-200:]
    code = proc.wait()
    with _rtd_refresh_lock:
        _rtd_refresh.update(
            state="done" if code == 0 else "failed", returncode=code,
            finished_at=dt.datetime.now().isoformat(timespec="seconds"))


def _guard_refresh(request: Request) -> None:
    """刷新接口的设限。每一道都别拆。

    （原来这里还有一道"离线模式禁止刷新"的检查。离线模式已经移除，
    那道检查恒不成立，所以删了。真正的保护是下面两道：挡代理头 + 只认本机。）
    """
    if not RTD_REFRESH_ENABLED:
        raise HTTPException(
            403, "凭据刷新已被 PIM_ALLOW_RTD_REFRESH=0 关闭。")
    # ★ 先挡代理头，再看来源地址 —— 顺序不能反。
    #   uvicorn 默认 proxy_headers=True：请求来自 forwarded_allow_ips
    #   （默认就是 127.0.0.1）时，它会信任 X-Forwarded-For 并用它替换
    #   request.client.host。单机直连没问题，但一旦把服务放到反向代理
    #   后面，所有请求来源都变成代理的 127.0.0.1 —— 外部带一个伪造的
    #   X-Forwarded-For: 127.0.0.1 就能骗过下面的本机检查。
    #   本机直连绝不会带这些头，带了就说明来源不可信。
    proxied = [h for h in ("x-forwarded-for", "x-real-ip", "forwarded",
                           "x-forwarded-host", "x-forwarded-proto")
               if h in request.headers]
    if proxied:
        raise HTTPException(
            403, f"请求带了代理转发头 {proxied}，来源无法确认。"
                 f"凭据刷新只接受本机直连，不能经过代理。")
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(
            403, f"凭据刷新只允许本机调用，你是 {host}。"
                 f"这个接口会启动浏览器进程，不对外开放。")


def _playwright_available() -> bool:
    """refresh_cookies.py 要 playwright，而服务跑的虚拟环境可能没装。

    不先检查的话，用户点了按钮等半天，最后拿到一段 ModuleNotFoundError
    的堆栈（实测踩过）。宁可提前拦住，给一条能照着敲的安装命令。
    """
    import importlib.util
    return importlib.util.find_spec("playwright") is not None


@app.post("/api/rtd/refresh")
def rtd_refresh_start(request: Request) -> dict:
    """启动凭据刷新，**立刻返回**，不等它跑完。

    起浏览器加上可能的人工登录要几十秒到几分钟，阻塞着等会超时。
    用 GET 同一路径轮询进度。
    """
    _guard_refresh(request)
    if not _playwright_available():
        raise HTTPException(
            503,
            "当前虚拟环境没装 playwright，刷新脚本跑不起来。"
            "装一下：uv sync --extra rtd")
    with _rtd_refresh_lock:
        if _rtd_refresh["state"] == "running":
            raise HTTPException(
                409, "刷新正在进行中。两个浏览器抢同一个 profile 目录会"
                     "互相破坏，所以同时只能跑一个。")
        _rtd_refresh.update(
            state="running", log=[], returncode=None,
            started_at=dt.datetime.now().isoformat(timespec="seconds"),
            finished_at=None)
    threading.Thread(target=_rtd_refresh_worker, daemon=True).start()
    return {"state": "running",
            "hint": "如果弹出浏览器窗口要求登录，请在窗口里完成单点登录。"}


@app.get("/api/rtd/refresh")
def rtd_refresh_status() -> dict:
    """刷新进度。界面每 2 秒轮一次这个。"""
    with _rtd_refresh_lock:
        d = dict(_rtd_refresh)
        d["log"] = list(d["log"])
    # ★ 明确给出 ok，别让前端自己猜。
    #   踩过：脚本因为缺 playwright 退出码 1，但凭据本来还有 9 小时有效，
    #   前端只看「凭据现在可用」就显示「已刷新」—— 误导。
    d["ok"] = d["state"] == "done"
    # 顺便报告凭据状态 —— 客户端会自己发现文件变了并重载
    d["cookie_hours_left"] = (None if _rtd is None
                              else _rtd.cookie_hours_left())
    d["enabled"] = RTD_REFRESH_ENABLED and _playwright_available()
    return d


# ---------------------------------------------------------------------------
# 前端静态文件（放在最后，避免抢了 /api 的路由）
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    f = STATIC_DIR / "index.html"
    if not f.exists():
        raise HTTPException(503, "前端文件缺失：static/index.html")
    return FileResponse(f, headers={"Cache-Control": NO_CACHE})


@app.get("/favicon.ico")
def favicon() -> Response:
    """浏览器会自动请求这个，不给就在控制台留一条 404。
    用 EDS 主色画一个最小的 SVG 图标。"""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="3" fill="#1174e6"/>'
           '<path d="M8 22V10h5a4 4 0 010 8H8m12 4V10" '
           'stroke="#ffffff" stroke-width="2.5" fill="none"/></svg>')
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# ⚠️⚠️ 前端文件必须让浏览器每次重新验证，否则改了前端用户看不到（踩过）
#
# Starlette 的 StaticFiles 只给 ETag + Last-Modified，**不给 Cache-Control**。
# 没有 Cache-Control 时浏览器用"启发式缓存"：自己猜一个过期时间（常见做法
# 是取"距上次修改时间"的 10%），这段时间内连服务器都不问，直接用本地副本。
#
# 实际后果：改完 app.js 去掉了"改供应商就自动查询"，用户界面上还是老行为，
# 看起来像"没修好"。而 index.html 引脚本是 <script src="/static/app.js">，
# 没有版本号做缓存失效，所以只能靠响应头解决。
#
# `no-cache` 不是"不缓存"，而是"每次都要向服务器确认"。配合已有的 ETag，
# 文件没变就回 304（不传内容），开销很小 —— 实测 app.js 72 KB，304 响应无正文。
NO_CACHE = "no-cache, must-revalidate"

# 第三方库（vendor/，plotly 4.8 MB）相反：只在换版本时变，而且大，
# 每次重新验证不值得，给长缓存。
VENDOR_CACHE = "public, max-age=604800"   # 7 天


class _CachedStatic(StaticFiles):
    """StaticFiles 加上按路径区分的 Cache-Control。

    分两类：vendor/ 下的第三方库长期缓存，应用自己的文件每次重新验证。
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):
        resp = super().file_response(full_path, stat_result, scope, status_code)
        p = str(full_path).replace("\\", "/")
        resp.headers["Cache-Control"] = (
            VENDOR_CACHE if "/vendor/" in p else NO_CACHE)
        return resp


if STATIC_DIR.exists():
    app.mount("/static", _CachedStatic(directory=str(STATIC_DIR)),
              name="static")
