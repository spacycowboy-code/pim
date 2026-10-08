"""前端文件的缓存策略。

为什么需要锁这个契约
--------------------
踩过的坑：改完 app.js，用户界面上还是旧行为（改供应商下拉仍然自动发起
查询），以为"没修好"。实际服务送出的已经是新代码，是**浏览器在用本地
旧副本**。

原因：Starlette 的 StaticFiles 只给 ETag + Last-Modified，**不给
Cache-Control**。没有 Cache-Control 时浏览器会用"启发式缓存"——自己猜一个
过期时间（常见做法是取"距上次修改时间"的 10%），这段时间内连服务器都不问。
所以改了前端用户也看不到。

而这个项目的 index.html 引脚本是 `<script src="/static/app.js">`，
没有版本号做缓存失效，所以只能靠响应头解决。

策略
----
应用自己的前端文件（app.js / eds.css / index.html）→ `no-cache`
    注意 `no-cache` 不是"不缓存"，而是"每次都要向服务器确认一下"。
    配合 ETag，没变就回 304（不传内容），开销很小。
第三方库（static/vendor/，plotly 4.8 MB）→ 可以长期缓存
    它只在换版本时变，而且大，每次重新验证不值得。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.mark.parametrize("path", ["/static/app.js", "/static/eds.css", "/"])
def test_前端文件必须每次向服务器确认(live_client, path):
    """否则改了前端用户看到的还是旧的（浏览器启发式缓存）。"""
    r = live_client.get(path)
    assert r.status_code == 200, f"{path} 取不到"
    cc = r.headers.get("cache-control", "")
    assert "no-cache" in cc, (
        f"{path} 的 Cache-Control 是 {cc!r}，浏览器会用启发式缓存拿旧副本，"
        f"改了前端用户看不到")


def test_前端文件仍然带校验标记(live_client):
    """no-cache 要配合 ETag 才便宜：没变就回 304，不重传内容。"""
    r = live_client.get("/static/app.js")
    assert r.headers.get("etag"), "没有 ETag，每次都要重传整个文件"


def test_没变的文件回304不重传(live_client):
    """带上拿到的 ETag 再请求一次，应该得到 304。"""
    r1 = live_client.get("/static/app.js")
    etag = r1.headers["etag"]
    r2 = live_client.get("/static/app.js", headers={"If-None-Match": etag})
    assert r2.status_code == 304, (
        f"没返回 304 而是 {r2.status_code}，每次刷新都要重传整个文件")


def test_第三方库可以长期缓存(live_client):
    """plotly 4.8 MB，只在换版本时变，不该每次都重新验证。"""
    r = live_client.get("/static/vendor/plotly.min.js")
    if r.status_code == 404:
        pytest.skip("本机没有 vendor/plotly.min.js")
    cc = r.headers.get("cache-control", "")
    assert "max-age=" in cc and "no-cache" not in cc, \
        f"第三方库的 Cache-Control 是 {cc!r}，应允许长期缓存"
