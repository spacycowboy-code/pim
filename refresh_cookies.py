"""刷新 RTD 的登录凭据，并抓取取数用的请求模板。

什么时候要跑
------------
  - 第一次使用
  - `rtd_client` 报 Cookie 过期（实测约 11 小时有效）
  - RTD 升级后取数失败（回调标识可能变了）

做什么
------
  1. 起一个有界面的 Chrome（用独立 profile，不碰你平时的浏览器）
  2. 你在窗口里完成单点登录（登录态会存进 profile，下次通常不用再登）
  3. 脚本自动点开 PIM -> Knock Response，把真实的回调请求抓下来
  4. 保存 Cookie 和请求模板到 data/rtd/

为什么要存模板而不是手写请求
----------------------------
Dash 的回调请求体里 inputs/state 的确切分布是框架实现细节，手工重建很脆弱
（踩过：把 outputs 填成空列表、把 state 里的项放进 inputs，都是静默 500）。
存下真实请求再只改参数值，对协议的依赖最小。

用法
----
    uv run refresh_cookies.py
"""
import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from rtd_config import cookie_path, template_path, profile_path, browser_channels, atomic_json

HOST = "jupyter.radiounits-realtimetestdata.ericsson.net"
DRILL = f"https://{HOST}/app/dashboard/drill_down"
PROFILE = profile_path()
OUT_DIR = cookie_path().parent

# 要抓哪些回调。键是模板名，值是 output 里的特征串。
WANTED = {
    "sessions": "layout_test-sessions_table_element.data",
    "freq_options": "layout-pim_knock-frequency-dropdown.options",
    "branch_options": "layout-pim_knock-branch-dropdown.options",
    "iter_options": "layout-pim_knock-iteration-dropdown.options",
    "curves": "layout-pim_knock-sequential-graph.figure",
}

# 抓不到也不算失败的模板。
#
# `curves` 的触发按钮在页面刚加载时是 disabled 的，要先选完一堆下拉框才
# 能点，自动点击链路很不稳定 —— 所以 rtd_client.py 里**内置了**这个模板
# （CURVES_TEMPLATE），抓不到完全不影响取数。
#
# 把它算成失败会导致：凭据明明刷新成功了，退出码却是 1，调用方（尤其是
# 界面上的刷新按钮）据此显示「刷新失败」—— 实测踩过，很误导。
OPTIONAL = {"curves"}

# 用哪条测试记录去触发（任意一条 PIM 会话都行，模板里的值后面会被替换）
SEED_B64 = ("eJxVT11vgjAU_SukT1uirC2iwJuJPphl06BvY2nuoGo3bAkty4zxv--WqZl96r3n"
            "6563E1EVycipIJX8VqUUqipIVpD5NJnHk4iySUEGBbEOWiecOsge5ZSPhzQdRnTD"
            "RhkbZ3wc0pjiK8iZDP7Rhd2b1mGCl4Q0DSMaXCX3xNqUUN8TOb8Rt1A60x6FhoNE"
            "0ny9Dl5Bfyq9Q9BJ6wRaOWW06Psk6SgOGUK3Vl5064RA05pK2BrsHpHnfBaklAVR"
            "RJ-Yl7XezQflbIrjBgNK8DXy6WyxFKvFS_DAk8dLdmOUdlaUptPIYWmUDAh0P_fr"
            "EWe4th946ZdqGlkJZ1zfmPp-CiPFFpRf4LzG_M7ifwXWXitenOK_0R0bfyHeIlb5"
            "coakA-gOLga13EF59P_z-y9lOpDj")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="刷新 RTD 登录态；默认打开可见浏览器完成 SSO。")
    parser.add_argument("--headless", action="store_true",
                        help="复用已有登录态；需要交互认证时退出，不会自动输入密码或 MFA。")
    args = parser.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    captured: dict[str, dict] = {}

    with sync_playwright() as p:
        ctx = None
        for ch in browser_channels():
            try:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE), headless=args.headless, channel=ch,
                    args=["--start-maximized"], no_viewport=True,
                    ignore_https_errors=False)
                print(f"用浏览器: {ch}", flush=True)
                break
            except Exception as e:
                print(f"  起 {ch} 失败: {str(e)[:90]}", flush=True)
        if ctx is None:
            print("没有可用浏览器。Linux 请运行 playwright install --with-deps chromium；交互登录需要 DISPLAY。")
            return 3

        def grab(req):
            if "_dash-update-component" not in req.url:
                return
            try:
                body = req.post_data_json
            except Exception:
                return
            out = str((body or {}).get("output"))
            for name, needle in WANTED.items():
                if needle in out and name not in captured:
                    captured[name] = body
                    print(f"  抓到模板 [{name}]", flush=True)

        def wire(pg):
            pg.on("requestfinished", grab)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        wire(page)
        ctx.on("page", wire)

        print("打开钻取页...", flush=True)
        try:
            page.goto(f"{DRILL}?headers_b64={SEED_B64}",
                      wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            print(f"  首次导航异常（通常是跳登录页）: {str(e)[:100]}", flush=True)

        if urlparse(page.url).hostname != HOST:
            if args.headless:
                print("需要人工 SSO 登录：请不带 --headless，在可见浏览器里运行本脚本。", flush=True)
                ctx.close()
                return 2
            print("\n>>> 请在弹出的窗口里完成单点登录，最多等 300 秒 <<<\n",
                  flush=True)
            dl = time.time() + 300
            while time.time() < dl:
                hit = next((pg for pg in ctx.pages
                            if urlparse(pg.url).hostname == HOST), None)
                if hit:
                    page = hit
                    wire(page)
                    break
                time.sleep(3)
            else:
                print("!! 超时没检测到登录完成")
                ctx.close()
                return 2
            if "headers_b64" not in page.url:
                page.goto(f"{DRILL}?headers_b64={SEED_B64}",
                          wait_until="domcontentloaded", timeout=90000)

        print("已登录，等页面加载完...", flush=True)
        for _ in range(2):
            try:
                page.wait_for_load_state("networkidle", timeout=40000)
            except Exception:
                pass
            page.wait_for_timeout(3000)

        # 点开 PIM -> Knock Response -> 查询按钮，触发那几个回调
        for label in ("PIM", "Knock Response"):
            try:
                page.get_by_text(label, exact=True).first.click(timeout=20000)
                print(f"  已点击 {label}", flush=True)
                page.wait_for_timeout(3500)
            except Exception as e:
                print(f"  点 {label} 失败: {str(e)[:80]}", flush=True)

        if "curves" not in captured:
            # 按 id 点，不靠文字 —— 按钮文字会随版本变，id 是回调契约的一部分
            for sel in ("#layout-pim_knock-search-button",
                        "button#layout-pim_knock-search-button",
                        "[id='layout-pim_knock-search-button']"):
                try:
                    el = page.locator(sel).first
                    el.scroll_into_view_if_needed(timeout=8000)
                    el.click(timeout=12000)
                    print(f"  已点击查询按钮 {sel}", flush=True)
                    page.wait_for_timeout(9000)
                    break
                except Exception as e:
                    print(f"  点 {sel} 失败: {str(e)[:70]}", flush=True)
            else:
                for label in ("Create graphs", "Search", "Apply"):
                    try:
                        page.get_by_text(label, exact=True).first.click(timeout=10000)
                        print(f"  已点击 {label}", flush=True)
                        page.wait_for_timeout(9000)
                        break
                    except Exception:
                        continue

        try:
            page.wait_for_selector(".js-plotly-plot", timeout=60000)
        except Exception:
            print("  没等到图渲染出来", flush=True)
        try:
            page.wait_for_load_state("networkidle", timeout=40000)
        except Exception:
            pass
        page.wait_for_timeout(4000)

        cookies = ctx.cookies()
        mine = [c for c in cookies
                if HOST.endswith(str(c.get("domain", "")).lstrip("."))]
        if not mine:
            print("未取得 RTD 域的 Cookie，保留旧凭据。")
            ctx.close()
            return 2
        atomic_json(cookie_path(), mine)
        exps = [c["expires"] for c in mine
                if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0]
        print(f"\nCookie 已存：{len(mine)} 个本域 Cookie")
        if exps:
            print(f"  最早到期：还剩 {(min(exps) - time.time()) / 3600:.1f} 小时")

        ctx.close()

    print(f"\n抓到 {len(captured)} / {len(WANTED)} 个模板")
    for name in WANTED:
        print(f"  {'[OK]' if name in captured else '[缺]'} {name}"
              + ("  （可选，客户端内置）" if name in OPTIONAL else ""))
    missing = [n for n in WANTED if n not in captured]

    if captured:
        old_path = template_path()
        old = {}
        if old_path.exists():
            try:
                old = json.loads(old_path.read_text(encoding="utf-8"))
            except Exception:
                old = {}
        old.update(captured)          # 这次没抓到的保留上次的
        atomic_json(old_path, old)
        print(f"\n模板已存：{old_path}  （共 {len(old)} 个）")

    # 只有缺**必需**模板才算失败。退出码是给调用方（界面上的刷新按钮）
    # 用来判断成败的，不能因为一个可选模板没抓到就报失败。
    required_missing = [n for n in missing if n not in OPTIONAL]
    if missing:
        opt = [n for n in missing if n in OPTIONAL]
        if opt:
            print(f"\n缺 {opt}，但这些客户端内置了，不影响使用。")
    if required_missing:
        print(f"\n!! 缺必需模板 {required_missing}，相关功能会不可用。")
        print("   多半是界面点击路径变了，看一眼弹出的窗口再调整点击顺序。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
