"""真浏览器实测：多台同屏曲线的内存与渲染耗时。

为什么必须用真浏览器：JS 堆占用、SVG 节点数、WebGL 上下文这些东西
只能从运行中的页面读，源码里看不出来。用 Chrome 的 --enable-precise-memory-info
让 performance.memory 给出准确值（默认会被量化成 bucket，不够用）。
"""
import json
import statistics
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8000/"
OUT = Path(__file__).parent

MEASURE = r"""
async ([nUnits, mode]) => {
  const $ = id => document.getElementById(id);
  $('fCurveN').value = String(nUnits);
  $('outCurveN').textContent = String(nUnits);
  $('fCurveMode').value = mode;
  // 先取数（change 事件会触发 loadCurves）
  $('fCurveN').dispatchEvent(new Event('change'));
  // 等到 KPI 里的台数更新成目标值
  const want = String(nUnits);
  for (let i = 0; i < 200; i++) {
    await new Promise(r => setTimeout(r, 100));
    const el = document.querySelector('#curveKpiRow .kpi .kpi-value');
    if (el && el.textContent.split(' /')[0].replace(/,/g, '') === want) break;
  }
  await new Promise(r => setTimeout(r, 400));

  const box = $('curvePlot');
  const heap = performance.memory ? performance.memory.usedJSHeapSize : null;
  const kpi = {};
  document.querySelectorAll('#curveKpiRow .kpi').forEach(k => {
    kpi[k.querySelector('.kpi-label').textContent] =
      k.querySelector('.kpi-value').textContent;
  });
  return {
    heap: heap,
    svgNodes: box.querySelectorAll('svg *').length,
    paths: box.querySelectorAll('path.js-line, .scatterlayer path').length,
    canvases: box.querySelectorAll('canvas').length,
    kpi: kpi,
    plotlyVersion: (window.Plotly && Plotly.version) || null,
  };
}
"""

with sync_playwright() as p:
    br = p.chromium.launch(
        headless=True, channel="chrome",
        args=["--enable-precise-memory-info", "--js-flags=--expose-gc"])
    page = br.new_page(viewport={"width": 1440, "height": 900})
    errs = []
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))

    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector("#tileCurves:not(.hidden)", timeout=60000)
    page.wait_for_selector("#curvePlot.js-plotly-plot", timeout=60000)
    page.wait_for_timeout(1500)

    v = page.evaluate("() => (window.Plotly && Plotly.version) || '(未加载)'")
    print("Plotly 版本:", v)
    print("默认这组数据的测量类型:",
          page.eval_on_selector("#fCurveKind", "e => e.value"))
    print()

    base = page.evaluate("() => performance.memory ? performance.memory.usedJSHeapSize : 0")
    print("页面基线 JS 堆: %.1f MB" % (base / 1048576))
    print()

    hdr = ("台数", "渲染方式", "trace", "点数", "返回KB", "画图ms", "JS堆MB", "增量MB",
           "SVG节点", "canvas")
    print("%-6s %-10s %6s %8s %8s %8s %8s %8s %9s %7s" % hdr)
    print("-" * 92)

    rows = []
    for mode in ("multi", "single", "gl"):
        for n in (10, 40, 79):
            r = page.evaluate(MEASURE, [n, mode])
            k = r["kpi"]
            heap_mb = (r["heap"] or 0) / 1048576
            rows.append({"mode": mode, "n": n, **r})
            print("%-6d %-10s %6s %8s %8s %8s %8.1f %8.1f %9d %7d" % (
                n, mode, k.get("trace 条数", "?"), k.get("数据点", "?"),
                k.get("返回体", "?").replace(" KB", ""), k.get("画图", "?").replace(" ms", ""),
                heap_mb, heap_mb - base / 1048576,
                r["svgNodes"], r["canvases"]))

    # 极端场景：把点数推到 6 万（= 所有 24 个功率组合叠在一张图上的量级）
    print()
    print("=== 推到极端：79 台 x 32 分支 x 24 组 ≈ 6 万点，同一张图 ===")
    print("%-10s %7s %8s %9s %9s %9s %7s" %
          ("渲染方式", "trace", "点数", "画图ms", "JS堆MB", "SVG节点", "canvas"))
    print("-" * 68)
    STRESS = r"""
    async (mode) => {
      const box = document.getElementById('curvePlot');
      const kind = document.getElementById('fCurveKind').value;
      const freq = document.getElementById('fCurveFreq').value;
      const r = await fetch(`/api/curves?kind=${encodeURIComponent(kind)}&freq_mhz=${freq}`);
      const d = await r.json();
      const x = d.branches, out = [];
      const t0 = performance.now();
      if (mode === 'single') {
        const xs = [], ys = [];
        for (let rep = 0; rep < 24; rep++) d.series.forEach(s => {
          for (let i = 0; i < x.length; i++) { xs.push(x[i]); ys.push(s.y[i]); }
          xs.push(null); ys.push(null);
        });
        out.push({x: xs, y: ys, type: 'scatter', mode: 'lines',
                  line: {width: 1, color: '#878787'}, hoverinfo: 'skip'});
      } else {
        for (let rep = 0; rep < 24; rep++) d.series.forEach(s => out.push({
          x: x, y: s.y, type: mode === 'gl' ? 'scattergl' : 'scatter',
          mode: 'lines', line: {width: 1, color: '#878787'},
          showlegend: false, hoverinfo: 'skip'}));
      }
      await Plotly.react(box, out,
        {height: 420, margin: {l: 60, r: 10, t: 10, b: 40}});
      const ms = performance.now() - t0;
      await new Promise(r => setTimeout(r, 800));
      return {ms: ms, traces: out.length,
              pts: out.reduce((a, t) => a + t.y.length, 0),
              heap: performance.memory ? performance.memory.usedJSHeapSize : 0,
              svg: box.querySelectorAll('svg *').length,
              canvas: box.querySelectorAll('canvas').length};
    }
    """
    stress_rows = []
    for mode in ("single", "gl", "multi"):
        try:
            s = page.evaluate(STRESS, mode)
        except Exception as e:
            print("%-10s  失败: %s" % (mode, str(e)[:60]))
            continue
        stress_rows.append({"mode": mode, **s})
        print("%-10s %7d %8d %9.0f %9.1f %9d %7d" % (
            mode, s["traces"], s["pts"], s["ms"], s["heap"] / 1048576,
            s["svg"], s["canvas"]))
        page.wait_for_timeout(500)

    print()
    print("console 错误:", errs or "无")
    (OUT / "mem.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
    page.screenshot(path=str(OUT / "curve.png"))
    page.evaluate("() => document.getElementById('tileCurves').scrollIntoView()")
    page.wait_for_timeout(600)
    page.screenshot(path=str(OUT / "curve_tile.png"))
    br.close()
print("\n已写 mem.json / curve.png / curve_tile.png")
