"""页面初始化绝不能触发 80 台数据的实时拉取。

背景：为什么要有这个文件
------------------------
数据改成实时查 MI 之后，拉一次全部整机的测试历史要约 55 秒。
用户指出一个设计问题：**打开页面本身不该去拉这些重数据** ——
取数应该等用户填好条件、点「查询」之后才发生。

但前端 init() 当时调了 4 个接口，其中两个会触发 all_test_runs()（拉 80 台）：
    /api/manifest          里面 all_test_runs()
    /api/history/options   里面 history_span() + all_test_runs()
它们只是为了填页面顶部的装饰信息（"837 次测试场次"标签、日期默认范围），
却让页面一打开就卡 55 秒。

所以约定：**页面初始化只许调"轻接口"**，这些接口只读内存里已有的东西
（序列号清单、产品号、常量），绝不拉历史。真正拉历史的接口只能由
用户点查询时触发。

这里用 FakeClient 的 calls 记录来证明：调完这些接口，底层没有发生过
test_histories（批量拉历史）或 test_history（单台拉历史）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


# 页面一打开就会调的接口。这些必须全是"轻"的。
# （不含 /api/manifest 和 /api/history/options —— 本次改造要把它们从
#  初始化里拿掉，所以它们可以慢，但初始化不再依赖它们。）
INIT_ENDPOINTS = ["/api/health", "/api/options", "/api/suppliers"]


def _history_calls(fake_client):
    """底层发生过几次"拉历史"（无论单台还是批量）。"""
    return sum(1 for c in fake_client.calls
               if c[0] in ("test_history", "test_histories"))


@pytest.mark.parametrize("path", INIT_ENDPOINTS)
def test_初始化接口不拉历史(live_client, fake_client, path):
    """逐个确认：页面初始化调的每个接口都不触发 80 台历史拉取。"""
    assert live_client.get(path).status_code == 200
    assert _history_calls(fake_client) == 0, \
        f"{path} 触发了历史拉取（会让页面一打开就卡几十秒）"


def test_整轮初始化加起来也不拉历史(live_client, fake_client):
    """把初始化的接口按真实顺序全调一遍，累计也不该拉历史。"""
    for path in INIT_ENDPOINTS:
        live_client.get(path)
    assert _history_calls(fake_client) == 0, \
        "初始化这一整轮触发了历史拉取"


def test_options够初始化用不缺字段(live_client):
    """初始化改成主要靠 /api/options，确认它给齐了填界面要的东西。"""
    d = live_client.get("/api/options").json()
    for k in ("products", "serials", "categories", "test_counts"):
        assert k in d, f"/api/options 缺字段 {k}，初始化会不完整"


def test_查询时才拉历史(live_client, fake_client):
    """对照组：点查询（/api/history/yield）必须真的去拉历史。

    这是为了确认"初始化不拉"不是因为整个链路坏了，而是只有查询才拉。
    """
    assert _history_calls(fake_client) == 0
    r = live_client.get("/api/history/yield", params={
        "product": "KRD 901 330/11",
        "from": "2026-09-01 00:00", "to": "2026-10-01 00:00"})
    assert r.status_code == 200
    assert _history_calls(fake_client) >= 1, "查询居然没拉历史，链路可能断了"
