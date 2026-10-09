"""Platform-specific MI authentication; imports stay lazy for offline tests."""
import os
import sys


def create_auth():
    mode = os.environ.get("PIM_MI_AUTH", "auto").strip().lower()
    if mode == "auto":
        mode = "sspi" if sys.platform == "win32" else "kerberos"
    if mode == "sspi":
        if sys.platform != "win32":
            raise RuntimeError("SSPI 只支持 Windows；Linux 请设 PIM_MI_AUTH=kerberos。")
        from requests_negotiate_sspi import HttpNegotiateAuth
        return HttpNegotiateAuth()
    if mode == "kerberos":
        try:
            from requests_gssapi import HTTPSPNEGOAuth
        except ImportError as exc:
            raise RuntimeError("缺少 Linux Kerberos 依赖：uv sync --extra linux。") from exc
        # Credentials come from the OS ticket cache (KRB5CCNAME), never from HTTP users.
        return HTTPSPNEGOAuth()
    raise ValueError("PIM_MI_AUTH 必须是 auto、sspi 或 kerberos。")
