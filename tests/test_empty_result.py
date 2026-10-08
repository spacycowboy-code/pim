"""MI 返回空结果时的处理（TDD：先写这个）。

真实 bug：查一个没有数据的条件时，MI 返回 {"data": null}，
而代码写的是 d.get("data", {}).get("data")——
"data" 键**存在**但值是 None，所以默认值 {} 用不上，直接崩：

    AttributeError: 'NoneType' object has no attribute 'get'

这会影响所有查询路径（实时查询、导出快照都可能遇到）。
空结果是正常业务情况，必须返回空列表而不是崩溃。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import mi_client


class StubClient(mi_client.MiClient):
    """不发网络请求，直接返回预设的响应体。

    （原来构造时要传 cache_dir 指向临时目录，避免污染真实缓存目录。
    文件缓存已移除，不再需要这个参数。）
    """

    def __init__(self, payload):
        super().__init__()
        self._payload = payload

    def query(self, report, **kw):
        return self._payload


# ---------------------------------------------------------------------------
# 各种"空"的写法都不能崩
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload, label", [
    ({"data": None}, "data 是 null（MI 真实返回过）"),
    ({"data": {}}, "data 是空对象"),
    ({"data": {"data": None}}, "内层 data 是 null"),
    ({"data": {"data": []}}, "内层 data 是空数组"),
    ({}, "整个响应是空对象"),
    ({"executionTime": "0.1"}, "只有耗时没有 data"),
])
def test_空结果返回空列表而不是崩(payload, label):
    c = StubClient(payload)
    rows = c.test_points(product="X", from_date="2026-01-01 00:00",
                         to_date="2026-02-01 00:00")
    assert rows == [], f"{label} 应该返回空列表"


def test_正常结果照常返回():
    c = StubClient({"data": {"data": [["a"] * 23, ["b"] * 23]}})
    rows = c.test_points(product="X", from_date="2026-01-01 00:00",
                         to_date="2026-02-01 00:00")
    assert len(rows) == 2


def test_装配结构的空结果也不崩():
    for payload in ({"data": None}, {"data": {}},
                    {"data": {"resultSet": None}},
                    {"data": {"resultSet": []}}):
        c = StubClient(payload)
        assert c.structure("EA001") == []


def test_测试历史的空结果也不崩():
    """test_history 返回原始响应，解析在 test_history 模块里做。"""
    import test_history as th
    for payload in ({"data": None}, {"data": {}}, {}):
        c = StubClient(payload)
        obj = c.test_history("EA001")
        assert th.to_runs(obj) == []
        assert th.to_repairs(obj) == []
