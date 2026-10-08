"""锁住「数据只从 MI / RTD 实时拉」这个契约。

背景：为什么要有这个文件
------------------------
系统原来有两条取数路径，靠环境变量 `PIM_SOURCE` 切换：

    PIM_SOURCE=mi        实时查 MI
    不设（默认 cache）    读事先下载到本地的快照文件 snapshot/*.json.gz

默认值是 `cache`，所以只要启动时忘了设这个开关，系统就**悄悄去读旧快照**：
界面一切正常、不报任何错，数据却是过期的。这类「静默回退」比直接报错危险，
因为没人会发现。

用户的要求是：数据必须直接从 MI 或 RTD 拉取，不许从下载后的本地库里取。

所以下面断言的是「**快照那条路已经不存在**」，而不只是「默认值改成了实时」。
只要那条路还留着，就迟早还会被走到。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import data_source as ds

ROOT = Path(__file__).resolve().parent.parent


# ---------- 启动模式：不存在"回退读快照"这件事 ----------

def test_不设环境变量时就是实时模式(live_client):
    """默认行为必须是实时查 MI。

    原来默认是 cache，这是整个问题的根源。
    """
    d = live_client.get("/api/health").json()
    assert d["live"] is True
    assert d["source_mode"] == "mi"


def test_把PIM_SOURCE设成cache也不会去读快照(monkeypatch, fake_client):
    """旧开关值不能让系统回退到快照——那条路必须是不可达的。

    这一条专门防「静默回退」：有人照着旧文档设了 PIM_SOURCE=cache，
    期望得到一个错误，结果拿到了一份过期数据。
    """
    import importlib

    import mi_client
    monkeypatch.setattr(mi_client, "MiClient", lambda **kw: fake_client)
    monkeypatch.setenv("PIM_SOURCE", "cache")

    import api
    importlib.reload(api)
    from fastapi.testclient import TestClient

    d = TestClient(api.app).get("/api/health").json()
    assert d["live"] is True, "PIM_SOURCE=cache 不该让系统回到读快照"
    assert d["source_mode"] == "mi"


def test_数据源就是实时源(live_api):
    """进程里装的必须是实时数据源，不是任何形式的离线源。"""
    assert isinstance(live_api._source, ds.MiSource)
    assert live_api._source.is_live is True
    assert live_api._source.is_offline is False


# ---------- 代码里不再有快照这套东西 ----------

def test_数据源模块不再提供离线快照源():
    """CacheSource / Snapshot 是读本地快照文件的类，必须已删除。"""
    assert not hasattr(ds, "CacheSource"), "CacheSource 还在，快照路径没拆干净"
    assert not hasattr(ds, "Snapshot"), "Snapshot 还在，快照路径没拆干净"


def test_数据源模块不再提供快照查询键():
    """QueryKey / build_manifest / MANIFEST_NAME 只服务于快照文件命名和清单，
    快照没了它们就该一起走。"""
    for name in ("QueryKey", "build_manifest", "MANIFEST_NAME"):
        assert not hasattr(ds, name), f"{name} 还在，是快照的残留"


def test_不再有快照导出脚本():
    """export_snapshot.py 的唯一职责是把 MI 数据导成快照文件给服务器用。"""
    assert not (ROOT / "export_snapshot.py").exists(), \
        "export_snapshot.py 还在，快照路径没拆干净"


def test_后端不再有快照目录配置(live_api):
    """PIM_SNAPSHOT_DIR / SNAPSHOT_DIR 指向快照文件夹，已无意义。"""
    assert not hasattr(live_api, "SNAPSHOT_DIR")
    assert not hasattr(live_api, "LIVE_MODE"), \
        "LIVE_MODE 是双模式开关，只有一种模式就不该有这个标志"


def test_序列号候选不再从快照目录读(live_api):
    """候选序列号原来优先从快照目录里读。快照没了，改从 data/serials.json 读。

    这个函数必须存在于 data_source，而不是靠导入已删除的导出脚本。
    """
    assert hasattr(ds, "known_serials"), \
        "known_serials 应该搬到 data_source（原来在 export_snapshot 里）"
    assert isinstance(ds.known_serials(), list)


# ---------- 接口不再暴露快照概念 ----------

def test_health不再暴露快照字段(live_client):
    d = live_client.get("/api/health").json()
    for k in ("snapshot_dir", "snapshot_ready", "offline"):
        assert k not in d, f"/api/health 还在返回快照字段 {k}"


def test_manifest报告实时来源(live_client):
    """/api/manifest 接口保留（前端可能用），但只能报告实时来源。"""
    r = live_client.get("/api/manifest")
    assert r.status_code == 200
    d = r.json()
    assert d["live"] is True
    assert "MI" in d["source"]


def test_查不到数据时不再提示去导出快照(live_client):
    """原来查不到会提示「请在 Windows 上重新导出快照」，现在没有快照这回事了。"""
    r = live_client.get("/api/yield", params={
        "product": "不存在的产品号", "from": "2026-01-01 00:00",
        "to": "2026-01-02 00:00"})
    body = r.text
    for word in ("快照", "导出", "export_snapshot"):
        assert word not in body, f"错误提示里还在说「{word}」"
