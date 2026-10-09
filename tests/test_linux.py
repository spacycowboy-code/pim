"""Linux migration regressions; no domain/network/browser required."""
import json
import sys
from types import SimpleNamespace

import pytest
import mi_auth
import rtd_config


def test_linux_auth_uses_ticket_cache(monkeypatch):
    sentinel = object()
    monkeypatch.delenv("PIM_MI_AUTH", raising=False)
    monkeypatch.setattr(mi_auth, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setitem(sys.modules, "requests_gssapi",
                        SimpleNamespace(HTTPSPNEGOAuth=lambda: sentinel))
    assert mi_auth.create_auth() is sentinel


def test_windows_auth_preserved(monkeypatch):
    sentinel = object()
    monkeypatch.delenv("PIM_MI_AUTH", raising=False)
    monkeypatch.setattr(mi_auth, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(sys.modules, "requests_negotiate_sspi",
                        SimpleNamespace(HttpNegotiateAuth=lambda: sentinel))
    assert mi_auth.create_auth() is sentinel


def test_invalid_auth_fails_before_network(monkeypatch):
    monkeypatch.setenv("PIM_MI_AUTH", "none")
    with pytest.raises(ValueError):
        mi_auth.create_auth()
    monkeypatch.setenv("PIM_MI_AUTH", "sspi")
    monkeypatch.setattr(mi_auth, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(RuntimeError, match="Windows"):
        mi_auth.create_auth()


def test_shared_paths_and_private_atomic_write(monkeypatch, tmp_path):
    cookie = tmp_path / "private" / "cookies.json"
    monkeypatch.setenv("PIM_RTD_COOKIES", str(cookie))
    monkeypatch.delenv("PIM_RTD_TEMPLATES", raising=False)
    assert rtd_config.cookie_path() == cookie
    assert rtd_config.template_path() == cookie.parent / "templates.json"
    rtd_config.atomic_json(cookie, [{"name": "test", "value": "first"}])
    rtd_config.atomic_json(cookie, [{"name": "test", "value": "second"}])
    assert json.loads(cookie.read_text())[0]["value"] == "second"
    assert cookie.stat().st_mode & 0o077 == 0
    assert list(cookie.parent.iterdir()) == [cookie]


def test_linux_default_browser(monkeypatch):
    monkeypatch.delenv("PIM_RTD_BROWSER", raising=False)
    monkeypatch.setattr(rtd_config, "sys", SimpleNamespace(platform="linux"))
    assert rtd_config.browser_channels() == [None]


def test_api_recovers_first_cookie_without_restart(monkeypatch, tmp_path):
    import api
    cookie = tmp_path / "cookies.json"
    monkeypatch.setattr(api, "RTD_COOKIE_FILE", cookie)
    monkeypatch.setattr(api, "_rtd", None)
    monkeypatch.setattr(api, "_rtd_error", "missing cookie")
    cookie.write_text(json.dumps([{"name": "CF_Authorization", "value": "test",
        "domain": "jupyter.radiounits-realtimetestdata.ericsson.net", "expires": 9999999999}]))
    client = api._rtd_or_503()
    assert client.cookies["CF_Authorization"] == "test"
    assert api._rtd_error is None


def test_rtd_reloads_new_templates(tmp_path):
    from rtd_client import RtdClient
    cookie = tmp_path / "cookies.json"
    cookie.write_text("[]")
    template = tmp_path / "templates.json"
    client = RtdClient(cookie_file=cookie, template_file=template)
    template.write_text(json.dumps({"sessions": {"output": "new"}}))
    assert client._tpl("sessions") == {"output": "new"}
    template.write_text(json.dumps({"sessions": {"output": "updated"}}))
    assert client._tpl("sessions") == {"output": "updated"}


def test_container_requires_both_web_credentials(monkeypatch):
    import linux_server
    monkeypatch.setenv("PIM_AUTH_USER", "user")
    monkeypatch.delenv("PIM_AUTH_PASS", raising=False)
    with pytest.raises(SystemExit, match="PIM_AUTH_PASS"):
        linux_server.main()
