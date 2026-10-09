/* PIM 查询工具前端。原生 JS，不依赖框架。
 *
 * EDS 约束（写代码时必须守住）：
 *   - 状态一律"颜色 + 图标 + 文字"三者齐全（色盲无障碍）
 *   - 强调只用 bold，彩色文字专属于链接
 *   - 每个可见上下文最多一个 primary 按钮
 *   - 图表多系列按 dataviz-1..20 顺序取色
 */
'use strict';

const $ = (id) => document.getElementById(id);

/* 读复选框组里**所有打勾的值**，用逗号拼接。
   fCat 这类"多选复选框组"是用 <div> 包一组 <input type=checkbox>，div 本身
   没有 value 属性 —— 直接 `$('fCat').value` 永远是 undefined，筛选不会生效。
   踩过的坑：早期代码里有多处 `$('fCat').value`，意图是读勾选，实际一直
   没传给后端。2026-10-08 加 38 类别时发现并修了。 */
function readCheckedValues(id) {
  const el = $(id);
  if (!el) return '';
  const boxes = el.querySelectorAll('input[type="checkbox"]:checked');
  return Array.from(boxes).map(b => b.value).filter(Boolean).join(',');
}
const EDS = {
  primary:'#1174e6', onSecondary:'#6a6a6a', borderSubtle:'#dcdcdc',
  on:'#242424', surface:'#fafafa', app:'#ebebeb',
  cleared:'#329864', minor:'#fad22d', major:'#d46617', critical:'#ed0e00',
  info:'#1174e6', indeterminate:'#878787',
  // 数据可视化配色，和 eds.css 里的 --dv1..--dv6 保持一致
  dv1:'#878787', dv2:'#976cf4', dv3:'#23969a',
  dv4:'#e65d6a', dv5:'#333333', dv6:'#b98327',
  font:'"Ericsson Hilda","Arial","Microsoft YaHei",sans-serif',
  // 热力图从浅到深（都在调色板内：surface → tag红底 → major → critical）
  heat:['#fafafa','#fef3f2','#fad22d','#d46617','#ed0e00'],
};
// 频率固定顺序，和 RTD 报表一致
const FREQ_ORDER = ['1736','1776','1856','1896','2502.5','2562.5'];

let OPTS = null;     // /api/options 的结果
let HOPTS = null;    // /api/history/options 的结果（测试历史的可选范围）
let LIVE = true;     // 数据恒为实时查 MI（离线快照模式已删除，这个标志保留只为少改调用点）
let TREND = null;    // 最近一次趋势查询结果，导出 CSV 用

/* ── 工具 ─────────────────────────────────────── */

function tag(text, kind, icon) {
  const span = document.createElement('span');
  span.className = 'tag ' + (kind || 'gray');
  const i = document.createElement('i');
  i.className = 'ic'; i.textContent = icon || '○';
  span.appendChild(i);
  span.appendChild(document.createTextNode(text));
  return span;
}

function notice(msg, kind, detail) {
  const n = $('notice');
  n.className = 'notice ' + (kind || 'info');
  const icons = {ok:'✓', err:'✕', warn:'◐', info:'ⓘ'};
  n.innerHTML = '';
  const i = document.createElement('i');
  i.className = 'ic'; i.textContent = icons[kind] || 'ⓘ';
  n.appendChild(i);
  const box = document.createElement('div');
  const p = document.createElement('div'); p.textContent = msg; box.appendChild(p);
  if (detail) { const pre = document.createElement('pre'); pre.textContent = detail; box.appendChild(pre); }
  n.appendChild(box);
  n.classList.remove('hidden');
}
function hideNotice(){ $('notice').classList.add('hidden'); }

function fmtNum(v, digits) {
  if (v === null || v === undefined) return '—';
  if (typeof v !== 'number') return String(v);
  return v.toLocaleString('zh-CN', {minimumFractionDigits: digits||0,
                                    maximumFractionDigits: digits||0});
}
function parseList(s) {
  if (!s) return [];
  return s.split(/[,;\s\n]+/).map(x => x.trim()).filter(Boolean);
}

async function api(path, params, method) {
  const url = new URL(path, location.origin);
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== '') url.searchParams.set(k, v);
  });
  // method 默认 GET。整个服务只有凭据刷新一个 POST 接口。
  const r = await fetch(url, method && method !== 'GET' ? {method} : undefined);
  const body = await r.json().catch(() => ({detail: '响应不是 JSON'}));
  if (!r.ok) throw new Error(errText(body, r.status));
  return body;
}

/* 把后端的错误体转成一句人话。
   FastAPI 参数校验失败（422）时 detail 是一个数组，形如
       [{type:'missing', loc:['query','product'], msg:'Field required'}]
   直接塞进 Error 会显示成 "[object Object]"，对用户毫无用处。 */
function errText(body, status) {
  const d = body && body.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    return d.map(x => {
      const field = Array.isArray(x.loc) ? x.loc[x.loc.length - 1] : '';
      return field ? `${field}: ${x.msg || '参数不合法'}` : (x.msg || '参数不合法');
    }).join('；');
  }
  if (d) return JSON.stringify(d);
  return `HTTP ${status}`;
}

function table(el, cols, rows, rowClass) {
  el.innerHTML = '';
  const thead = document.createElement('thead');
  const tr = document.createElement('tr');
  cols.forEach(c => { const th = document.createElement('th'); th.textContent = c.label; tr.appendChild(th); });
  thead.appendChild(tr); el.appendChild(thead);
  const tb = document.createElement('tbody');
  rows.forEach(row => {
    const r = document.createElement('tr');
    cols.forEach(c => {
      const td = document.createElement('td');
      const v = c.get(row);
      if (v instanceof Node) td.appendChild(v);
      else { td.textContent = v === null || v === undefined ? '—' : v; }
      if (c.num) td.className = 'num';
      if (c.muted) td.classList.add('muted');
      r.appendChild(td);
    });
    // rowClass 可选：返回一个 CSS 类名给整行用（比如失败的行标黄）
    if (rowClass) { const c = rowClass(row); if (c) r.classList.add(c); }
    tb.appendChild(r);
  });
  el.appendChild(tb);
}

/* ── 初始化 ───────────────────────────────────── */

async function init() {
  try {
    // ★ 页面初始化只调"轻接口"，绝不拉 80 台历史。
    //
    // 原来这里还调了 /api/manifest 和 /api/history/options，它们都要
    // all_test_runs()（实时拉全部整机历史，约 55 秒），于是页面一打开就
    // 卡 55 秒 —— 而它们只是为了填几个装饰标签和日期默认值。
    // 真正拉历史的事，一律等用户点「查询」。
    //
    // health / options / suppliers 都只读内存里已有的东西（序列号清单、
    // 产品号、常量、前缀对照表），实测各 0.0x 秒。
    const [health, opts] = await Promise.all([
      api('/api/health'), api('/api/options')]);
    OPTS = opts;
    HOPTS = null;        // 等点查询时再填（见 doQuery）

    $('sysMeta').textContent = '实时查询 MI';

    const tags = $('metaTags');
    tags.innerHTML = '';
    tags.appendChild(tag('任意日期与产品号', 'blue', 'ⓘ'));
    // 不显示固定台数 ——"多少台"要按查询的 (产品,日期) 实时列才准，
    // 查询之后由结果去说（见 doTrend 的 n_serials）。

    LIVE = true;

    // 产品号：可输入 + 候选列表（自定义 combobox 替代 datalist，让下拉可见、
    // 样式受控、符合 EDS 风格）。
    PROD_OPTIONS = Array.from(new Set(opts.products || []));
    renderProdMenu(PROD_OPTIONS);     // 填面板
    $('fProduct').value = opts.default_product
                       || (opts.products || [])[0] || '';
    updateProdHint(PROD_OPTIONS.length, true);
    wireProdCombo();                  // 装上点击/键盘/焦点行为
    // ★ 后端是在**后台线程**问 MI"最近半年有哪些产品号"的（要几十秒），
    //   而这里的 /api/options 是页面一打开就调的 —— 那时探测通常还没完成，
    //   所以只能先拿到默认的那一个。延迟再取几次把新发现的补进来。
    pollProducts();

    // 日期默认填"今天 00:00 → 现在"：产线一直在产数据，用户最关心
    // "刚测的那些机器"，默认范围窄一点查得快（30 天跨度要 60~120 秒，
    // 今天通常几秒到十几秒就回）。要别的区间用户自己改日期即可。
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0);
    setDt('fFrom', fmtLocal(todayStart));
    setDt('fTo', fmtLocal(now));
    $('fromHint').textContent = '实时查 MI，任意日期都能查';
    $('toHint').textContent = '默认今天，可自行调整';

    // 测试类别：28 (RADIO_PIM) / 212 (RADIO_PIM after GBI) / 38 (辅助/诊断)。
    // 默认勾选由 HTML 的 `checked` 属性决定（28、212 默认勾，38 默认不勾）。
    // 用户要筛哪些 → 勾哪个 → readCheckedValues('fCat') 读出来发给后端。
    // ★ catHint / snHint 的提示语按用户要求移除，但保留 HTML 的 <span>
    //   节点（删节点要同步改 CSS 排版，收益不大）—— 永远空字符串。
    $('catHint').textContent = '';
    $('snHint').textContent = '';
    await loadSuppliers();

    // 单台详情不再自己选序列号 —— 从结果列表点「查看」进来才显示，
    // 所以这里不预填、也不默认展开。

    $('footMeta').textContent =
      '数据实时查自 Manufacturing Intelligence（MI），每次查询都取当前最新。';

    // ★ 不在 init 阶段调 /api/parts：下层件索引需要 UPS 响应作为数据源，
    //   而初始化时后端的 _ups_cache 是空的（用户还没查过任何产品）。
    //   现在 /api/parts 读的是已缓存的 UPS —— 用户 doQuery 完成后，
    //   doQuery 里会调 loadParts()，那时 tilePart 才有数据可显示。
    notice('已就绪，填好条件点 Search 查询。', 'ok');
  } catch (e) {
    notice('初始化失败', 'err', String(e.message || e));
  }
}

/* ── 产品号 Combobox ──────────────────────────────────────
   自定义下拉替代 HTML native datalist：
   - 右侧 ▾ 按钮：永远可见、可点开（datalist 的下拉浏览器自己画，用户看不到）
   - 面板：EDS 风格（白底、淡蓝 hover、项目一致字号）
   - 保留自由输入：input 不是 readonly，用户仍可手打任意产品号
   - 实时筛选：输入时按前缀/包含过滤面板项
   - 键盘：↑↓ 导航 / Enter 选中 / Esc 关闭 */
let PROD_OPTIONS = [];     // 所有候选产品号
let PROD_HIGHLIGHT = -1;   // 面板里当前高亮项的 index（键盘导航用）

/* 用 PROD_OPTIONS + 当前输入筛选后，重新填面板 <ul>。 */
function renderProdMenu(options, filterText) {
  const menu = $('prodMenu');
  if (!menu) return;
  const q = (filterText || '').trim().toLowerCase();
  const items = q
    ? options.filter(p => p.toLowerCase().includes(q))
    : options.slice();
  menu.innerHTML = '';
  PROD_HIGHLIGHT = -1;
  if (!items.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = q ? `没有匹配 "${filterText}" 的产品号` : '暂无候选';
    menu.appendChild(li);
    return;
  }
  items.forEach((p, i) => {
    const li = document.createElement('li');
    li.setAttribute('role', 'option');
    li.textContent = p;
    li.dataset.value = p;
    li.addEventListener('mousedown', e => {
      // mousedown 而不是 click：避免 input blur 先触发关闭、鼠标抬起时 click 落空
      e.preventDefault();
      pickProdOption(p);
    });
    menu.appendChild(li);
  });
}

function openProdMenu(withInputFilter) {
  const menu = $('prodMenu');
  if (!menu) return;
  // withInputFilter=true 时按当前输入筛选（input 事件触发用）；
  // 否则显示全部候选（点 ▾ 或 focus 时用，让用户看到完整列表）
  renderProdMenu(PROD_OPTIONS, withInputFilter ? $('fProduct').value : '');
  menu.hidden = false;
  $('fProduct').setAttribute('aria-expanded', 'true');
}

function closeProdMenu() {
  const menu = $('prodMenu');
  if (!menu) return;
  menu.hidden = true;
  PROD_HIGHLIGHT = -1;
  $('fProduct').setAttribute('aria-expanded', 'false');
}

function pickProdOption(value) {
  const input = $('fProduct');
  input.value = value;
  closeProdMenu();
  input.dispatchEvent(new Event('change', {bubbles: true}));
  input.focus();
}

/* 键盘导航。 */
function moveProdHighlight(delta) {
  const menu = $('prodMenu');
  const items = [...menu.querySelectorAll('li:not(.empty)')];
  if (!items.length) return;
  // 先清除旧高亮
  items.forEach(li => li.classList.remove('active'));
  PROD_HIGHLIGHT = (PROD_HIGHLIGHT + delta + items.length) % items.length;
  const cur = items[PROD_HIGHLIGHT];
  cur.classList.add('active');
  cur.scrollIntoView({block: 'nearest'});
}

function wireProdCombo() {
  const input = $('fProduct');
  const trigger = $('prodTrigger');
  const menu = $('prodMenu');
  if (!input || !trigger || !menu) return;

  // 点 ▾：面板已打开就关闭，否则打开。鼠标按下阻止 input 失焦。
  trigger.addEventListener('mousedown', e => {
    e.preventDefault();
    if (menu.hidden) openProdMenu(); else closeProdMenu();
    if (menu.hidden) return;    // 刚关闭的话不要再聚焦（否则又触发 focus 打开）
    input.focus();
  });

  // 聚焦 input 自动打开（但避开从面板点选项回填导致的 focus 再打开）
  input.addEventListener('focus', () => {
    if (menu.hidden) openProdMenu();
  });

  // 输入时实时筛选（显式按输入值过滤）
  input.addEventListener('input', () => {
    renderProdMenu(PROD_OPTIONS, input.value);
    const menu = $('prodMenu');
    if (menu.hidden) {
      menu.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    }
  });

  // 键盘：↑↓ 导航，Enter 选中，Esc 关闭
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (menu.hidden) openProdMenu();
      moveProdHighlight(1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (menu.hidden) openProdMenu();
      moveProdHighlight(-1);
    } else if (e.key === 'Enter') {
      const act = menu.querySelector('li.active');
      if (act && !act.classList.contains('empty')) {
        e.preventDefault();
        pickProdOption(act.dataset.value);
      } else {
        closeProdMenu();   // 没高亮时按回车只收下输入值、关闭面板，不发起查询
      }
    } else if (e.key === 'Escape') {
      closeProdMenu();
    }
  });

  // 点面板外关闭
  document.addEventListener('mousedown', e => {
    if (!$('prodCombo').contains(e.target)) closeProdMenu();
  });
}

/* 产品号候选的提示文字。 */
function updateProdHint(n, discovering) {
  const el = $('prodHint');
  if (!el) return;
  if (n > 1) {
    el.textContent = `候选 ${n} 个产品号，也能自己输入别的`
      + (discovering ? '（仍在向 MI 探测更多…）' : '');
  } else {
    el.textContent = discovering
      ? '正在向 MI 探测在产的产品号…也能自己直接输入'
      : '可以自己输入任意产品号';
  }
}

/* 轮询补全产品号候选。

   为什么要轮询：后端**故意**把"问 MI 最近半年有哪些产品号"放到后台线程
   （否则模块导入就要卡 90 秒，服务起不来 —— 档案里记过这个坑）。那次探测
   要几十秒，而前端初始化是页面一打开就跑的，所以第一次只能拿到默认值。
   这里过一会儿再取几次，把后台探测到的补进 datalist。

   退避式间隔，总共约 2 分钟内取完；数量不再增长就提前停。 */
async function pollProducts() {
  const delays = [5000, 10000, 15000, 25000, 30000, 40000];
  let have = new Set(PROD_OPTIONS);
  for (const d of delays) {
    await new Promise(r => setTimeout(r, d));
    let opts;
    try { opts = await api('/api/options'); } catch (e) { continue; }
    const got = (opts.products || []).filter(Boolean);
    const fresh = got.filter(p => !have.has(p));
    if (fresh.length) {
      fresh.forEach(p => {
        PROD_OPTIONS.push(p);
        have.add(p);
      });
      renderProdMenu(PROD_OPTIONS);
      updateProdHint(have.size, true);
    }
    // 探测完成的判据：后端产品号数量已经和本地一致，且不止默认那一个
    if (got.length === have.size && have.size > 1) break;
  }
  updateProdHint(have.size, false);
}

/* 供应商下拉。
   名字的权威来源是 MI 的 UnitProductStructure 报表的 Mfg. Unit 列。实测
   628 台装配明细确认「子件序列号前 3 位 == Mfg.Unit 代码」，所以后端直接
   按序列号前缀查表得到公司名，不必再请求那个报表。
   产地不计入供应商：春兴的苏州厂和深圳厂合并成一个 Chunxing 选项。
   实测滤波器 Grentech / Chunxing 两家，天线 Comba / AMPHENOL 两家。

   ★ /api/suppliers 默认不统计台数（统计要拉全部装配结构，约 9 分钟），
     这时 n_parents / n_children 是 null —— 不能直接拼进文案显示成 "null 台"，
     只在真有统计（with_counts）时才显示台数。 */
async function loadSuppliers() {
  const fill = (id, items, hintId, label, counted) => {
    const sel = $(id);
    sel.innerHTML = '';
    const all = document.createElement('option');
    all.value = ''; all.textContent = '全部';
    sel.appendChild(all);
    items.forEach(it => {
      const o = document.createElement('option');
      o.value = it.supplier;
      // 有台数统计才显示 "(N 台 / M 个件)"，没有就只显示供应商名
      o.textContent = (counted && it.n_parents != null)
        ? `${it.supplier}（${it.n_parents} 台 / ${it.n_children} 个件）`
        : it.supplier;
      sel.appendChild(o);
    });
    $(hintId).textContent = items.length
      ? `${label}共 ${items.length} 家供应商（同一家的多个产地已合并）`
      : `暂无${label}供应商选项`;
  };
  try {
    const d = await api('/api/suppliers');
    fill('fAntSup', d.antenna || [], 'antSupHint', '天线', d.counted);
    fill('fFuSup', d.filter_unit || [], 'fuSupHint', '滤波器', d.counted);
  } catch (e) {
    fill('fAntSup', [], 'antSupHint', '天线', false);
    fill('fFuSup', [], 'fuSupHint', '滤波器', false);
  }
}

/* 把 Date 格式化成后端要的 "YYYY-MM-DD HH:MM"（本地时区）。 */
function fmtLocal(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* 今天往前推 n 天的 00:00（起点取整到当天开头，省得差几小时漏数据）。 */
function daysAgo(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  d.setHours(0, 0, 0, 0);
  return d;
}

/* ── 日期控件 ──────────────────────────────────
   <input type="datetime-local"> 的值格式是 "2026-09-14T08:00"（带 T），
   而后端要的是 "2026-09-14 08:00"（空格）。所以读写都要转一次。 */

function dtVal(id) {
  const v = $(id).value;
  return v ? v.replace('T', ' ') : '';
}

function setDt(id, v) {
  // 接受 "2026-09-14 08:00:00" 或 "2026-09-14T08:00"，统一成控件要的格式
  if (!v) { $(id).value = ''; return; }
  $(id).value = String(v).replace(' ', 'T').slice(0, 16);
}

/* ── 良率查询 ─────────────────────────────────── */

// 当前查询方式：'cond'（按产品/时间/供应商）或 'sn'（按序列号列表）。
// 两种并列但二选一 —— 见界面上的"查询方式"tab。
let QUERY_MODE = 'cond';

function currentParams() {
  const byCat = readCheckedValues('fCat');
  if (QUERY_MODE === 'sn') {
    // 按序列号查：只认序列号 + 类别。产品/时间/供应商一律不传，
    // 让后端就按这批序列号取（它们本来就唯一确定了机器，不需要别的条件）。
    return {
      product: '',
      from_date: '',
      to_date: '',
      serial_numbers: parseList($('fSns').value).join(','),
      test_category: byCat,
    };
  }
  // 按条件查：产品 + 时间 + 供应商。序列号留空 —— 后端会按 (产品,日期)
  // 实时列出全部在产整机，不再塞固定清单（那样会漏掉产线新机）。
  return {
    product: $('fProduct').value,
    from_date: dtVal('fFrom'),
    to_date: dtVal('fTo'),
    serial_numbers: '',
    test_category: byCat,
  };
}

/* 条件改了但还没查 —— 提示用户点查询。

   为什么需要这个：以前改个供应商下拉会自动重查，所以"改完立刻生效"是
   不言自明的。现在一次查询要 60~120 秒，自动重查会把整页锁死一两分钟，
   所以改成手动。
   ★ 原来还配一句"查询条件已修改，点「查询」后生效"的文字，用户反馈那句
   没用（按钮光圈已经说明问题），所以只留光圈。 */
let DIRTY = false;
let QUERY_RUN_ID = 0;     // 查询版本号：每次 doQuery 递增；改条件也递增 ——
                          // 查询完成后对比版本号，不一致就不渲染（相当于中断）
let QUERY_IN_FLIGHT = false;  // 当前有查询在跑？

function markDirty() {
  // 条件改了：如果有查询在跑，立刻把 UI 恢复可点状态（视觉上"中断"查询）。
  // 后端那次请求继续跑完没关系 —— 结果会被版本号比对丢弃。
  // 这是"渐进中断" —— 用户立刻能发起新查询，不用等 60 秒。
  if (QUERY_IN_FLIGHT) {
    QUERY_RUN_ID++;              // 旧查询渲染时版本号不匹配，自动忽略
    QUERY_IN_FLIGHT = false;
    const btn = $('btnQuery');
    if (btn) { btn.disabled = false; btn.textContent = 'Search'; }
    progressDone(false);
  }
  if (DIRTY) return;
  DIRTY = true;
  $('btnQuery').classList.add('btn-dirty');
}

function clearDirty() {
  DIRTY = false;
  $('btnQuery').classList.remove('btn-dirty');
}

/* ── 查询进度 ────────────────────────────────────
   一次查询 60~120 秒，只把按钮变灰不够：用户不知道要等多久、也分不清
   "在跑"和"卡死了"。

   ⚠️ 诚实说明：后端是**一个同步请求**，不上报进度，所以这里的百分比是
   按经验耗时**估算**出来的，不是真实进度。两条自律：
     1. 封顶 90% —— 没真拿到结果绝不画满，不假装精确
     2. 预估值用**上一次同类查询的真实耗时**（存在 localStorage），
        首次没有历史就用 90 秒；超过预估就改说"比平时久"
*/
let QP_TIMER = null;

function estimateQuerySeconds() {
  const v = parseFloat(localStorage.getItem('pim_last_query_s') || '');
  // 用上次真实耗时（夹在 20~300 秒，避免异常值把进度条拖坏）
  return (isFinite(v) && v > 0) ? Math.min(300, Math.max(20, v)) : 90;
}

function progressStart() {
  const box = $('qProgress');
  if (!box) return;
  const est = estimateQuerySeconds();
  const t0 = Date.now();
  box.classList.add('on');
  $('qStage').textContent = '正在实时查 MI';
  $('qNote').textContent = `预计约 ${Math.round(est)} 秒`
    + (localStorage.getItem('pim_last_query_s') ? '（按上次耗时估）' : '（首次按经验估）');

  if (QP_TIMER) clearInterval(QP_TIMER);
  QP_TIMER = setInterval(() => {
    const sec = (Date.now() - t0) / 1000;
    // 估算百分比，封顶 90%：真完成才满格
    const pct = Math.min(90, (sec / est) * 90);
    $('qFill').style.width = pct.toFixed(1) + '%';
    $('qElapsed').textContent = `已用 ${Math.round(sec)} 秒`;
    if (sec > est * 1.2) {
      $('qStage').textContent = '仍在查询';
      $('qNote').textContent = '比平时久 —— 范围大或产线数据多时会这样，再等等';
    }
  }, 500);
}

function progressDone(ok) {
  const box = $('qProgress');
  if (!box) return;
  if (QP_TIMER) { clearInterval(QP_TIMER); QP_TIMER = null; }
  $('qFill').style.width = '100%';
  const txt = $('qElapsed').textContent || '';
  // 把这次真实耗时记下来，下次的预估就更准。
  // ★ 只记"真正去问了 MI"的那种耗时（>10 秒）。命中 60 秒内存缓存的查询
  //   可能只要 0.01 秒，拿它当预估基准会让下次的进度条瞬间冲到 90% 再干等。
  const m = txt.match(/(\d+)/);
  if (ok && m && parseInt(m[1], 10) >= 10) {
    localStorage.setItem('pim_last_query_s', m[1]);
  }
  $('qStage').textContent = ok ? '查询完成' : '查询结束（有问题，见下方提示）';
  setTimeout(() => {
    box.classList.remove('on');
    $('qFill').style.width = '0';
  }, 1200);
}

async function doQuery() {
  const btn = $('btnQuery');
  const p = currentParams();

  // 按查询方式各自校验必填项，别发注定失败的请求
  if (QUERY_MODE === 'sn' && !p.serial_numbers) {
    notice('按序列号查，得先在序列号列表里填至少一台整机。', 'warn');
    return;
  }
  if (QUERY_MODE === 'cond' && !p.product) {
    notice('按条件查，产品号必填（时间默认最近 30 天，供应商可留"全部"）。', 'warn');
    return;
  }

  btn.disabled = true; btn.textContent = '查询中';
  clearDirty();
  hideNotice();
  progressStart();

  // ★ 版本号机制：这次查询的身份。查完后如果 QUERY_RUN_ID 已经被 markDirty
  //   或下一次 doQuery 递增，就不渲染这次结果 —— 相当于"中断了"。
  const myRunId = ++QUERY_RUN_ID;
  QUERY_IN_FLIGHT = true;

  // 现在结果区只剩「良率与趋势」。良率/趋势和场次明细都用测试历史
  // （带时间戳，任意日期都能查）。场次数据（RUNS）不单独成表了，只用来
  // 在良率表展开时列出每段的 DUT 明细、以及点「查看」送到 RTD 曲线。
  //
  // ★ 条件都从 currentParams(p) 来，它已经按查询方式二选一了：
  //   按序列号模式 -> p.serial_numbers 有值、产品/时间为空、供应商不传；
  //   按条件模式   -> 反之。所以这里不要再直接读 #fSns / #fAntSup。
  const bySn = QUERY_MODE === 'sn';
  const histParams = {
    from_date: p.from_date, to_date: p.to_date,
    product: p.product,
    sn: p.serial_numbers,
    category: p.test_category,
    ant_supplier: bySn ? '' : $('fAntSup').value,
    fu_supplier: bySn ? '' : $('fFuSup').value,
  };

  try {
    RUNS_LOADING = true;        // 展开时能区分"正在取"和"还没查"
    BRANCHES_STATE = 'idle';
    const [trendOk, runsOk] = await Promise.all([doTrend(histParams), doRuns(histParams)]);
    // ★ 版本号对比：如果中间用户改了条件（markDirty 递增了 ID）或
    //   又点了一次查询（新 doQuery 递增了 ID），本次结果作废。
    if (myRunId !== QUERY_RUN_ID) return;
    refreshOpenDutRows();       // 场次数据到了，已展开的明细自动填上
    progressDone(trendOk);      // 只有查成了才把耗时记为下次的预估基准
    // 下层件索引：这次查询的 UPS 响应已经进 _ups_cache，可以从里面构建
    // 反查索引。不 await —— tilePart 异步出现，不卡用户。
    // ★ 和当前查询条件联动：只用这次查询对应的 UPS 响应，不混入历史查询。
    //   sn 模式下 product/from_date/to_date 都是空，loadParts 会调
    //   /api/parts 不带参数（累积模式）—— 实际上 sn 模式下 cache 里
    //   没对应 key，返回会是空，tilePart 自然就隐藏。
    if (QUERY_MODE === 'cond') {
      loadParts({
        product: p.product,
        from_date: p.from_date,
        to_date: p.to_date,
      });
    } else {
      // sn 模式下不加载下层件（没有产品/时间范围概念）
      PARTS = null;
      PARTS_CTX = null;
      $('tilePart').classList.add('hidden');
    }
    if (trendOk && runsOk) {
      notice('FPY 良率与趋势已更新。展开某个时间段可看该段的 DUT 明细，'
             + '点某台的「查看」看它的 RTD Knock 曲线。', 'ok');
    } else if (trendOk && !runsOk) {
      // 趋势出来了但场次没取到 —— DUT 明细会展不开，提前说清原因
      notice('良率趋势已更新，但 DUT 明细没取到。'
             + (RUNS_ERR ? '原因：' + RUNS_ERR : ''), 'warn');
    } else {
      notice('这个条件下没查到测试记录。换个区间或条件试试。', 'warn');
    }
  } catch (e) {
    if (myRunId !== QUERY_RUN_ID) return;    // 已被中断就不显示错误
    progressDone(false);
    notice('查询失败', 'err', String(e.message || e));
  } finally {
    if (myRunId === QUERY_RUN_ID) {
      QUERY_IN_FLIGHT = false;
      btn.disabled = false; btn.textContent = 'Search';
    }
    // 被中断的那次：markDirty 已经恢复了 UI，这里不要再碰
  }
}

/* ── 时间趋势：测试历史带时间戳，所以任意日期都能查 ───────── */

async function doTrend(hp) {
  try {
    const by = $('fTrendBy').value;
    const [y, t] = await Promise.all([
      api('/api/history/yield', hp),
      api('/api/history/trend', Object.assign({}, hp, {by: by})),
    ]);
    TREND = {y: y, t: t, by: by};
    renderTrend(y, t, by);
    return true;
  } catch (e) {
    $('tileTrend').classList.add('hidden');
    return false;
  }
}

function renderTrend(y, t, by) {
  const tile = $('tileTrend');
  if (!y || !y.n_runs) {
    tile.classList.remove('hidden');
    $('runKpiRow').innerHTML = '';
    $('trendNote').textContent = '这个时间范围内没有测试记录。换一个区间试试。';
    $('tblTrend').querySelector('tbody').innerHTML = '';
    const cv = $('trendChart');
    cv.getContext('2d').clearRect(0, 0, cv.width, cv.height);
    return;
  }

  const kpis = [
    ['首次测试场次', fmtNum(y.n_runs), null],
    ['通过', fmtNum(y.n_passed), null],
    ['失败', fmtNum(y.n_failed), y.n_failed > 0 ? 'minor' : null],
    ['FPY 良率', y.yield_pct.toFixed(2) + '%',
     y.yield_pct < 90 ? 'major' : y.yield_pct < 99 ? 'minor' : null],
    ['涉及整机', fmtNum(y.n_serials), null],
  ];
  $('runKpiRow').innerHTML = kpis.map(([label, val, kind]) => `
    <div class="kpi">
      <div class="kpi-label">${label}</div>
      <div class="kpi-value">${val}</div>
      ${kind ? `<div class="kpi-tag"><span class="tag tag-${kind==='major'?'orange':'yellow'}">
        <i>◐</i>${kind === 'major' ? '需关注' : '少量失败'}</span></div>` : ''}
    </div>`).join('');

  // 原来这里有一段 FPY 口径说明（"★ 这是本项目唯一的良率..." 加数据范围）。
  // 用户反馈文字太长没必要（KPI 已经显示失败/场次数了），已删。
  $('trendNote').textContent = '';

  // 表头已经写在 HTML 里了，这里只填 tbody。
  // 注意不能用 table()——它要求 cols 是带 label/get 的对象数组，传 null 会崩。
  const tb = $('tblTrend').querySelector('tbody');
  tb.innerHTML = '';
  t.rows.forEach((r, i) => {
    const tr = document.createElement('tr');

    // 展开钮：点开看这个时间段里具体是哪些机器
    const tdEx = document.createElement('td');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-icon';
    btn.textContent = '▸';
    btn.title = '展开这个时间段的 DUT 明细';
    tdEx.appendChild(btn);
    tr.appendChild(tdEx);

    const cells = [
      [r.bucket, ''],
      [fmtNum(r.n_runs), 'num'],
      [fmtNum(r.n_passed), 'num'],
      [fmtNum(r.n_failed), 'num'],
      [r.yield_pct === null ? '—' : r.yield_pct.toFixed(2) + '%', 'num'],
      [fmtNum(r.n_serials), 'num'],
    ];
    cells.forEach(([v, cls]) => {
      const td = document.createElement('td');
      td.textContent = v;
      if (cls) td.className = cls;
      tr.appendChild(td);
    });
    // 失败的时间段整行标出来，扫一眼就能找到问题区间
    if (r.n_failed > 0) tr.classList.add('row-warn');
    tb.appendChild(tr);

    // DUT 明细行：先插一个空的，点展开时才填（避免一次渲染几百行）
    const detail = document.createElement('tr');
    detail.style.display = 'none';
    const dtd = document.createElement('td');
    dtd.colSpan = 7;
    dtd.className = 'dut-cell';
    detail.appendChild(dtd);
    tb.appendChild(detail);

    btn.addEventListener('click', () => {
      const open = detail.style.display !== 'none';
      detail.style.display = open ? 'none' : '';
      btn.textContent = open ? '▸' : '▾';
      if (open) {
        // 收起：去掉标记，后台数据到了就不用管这一格了
        delete dtd.dataset.bucket;
        delete dtd.dataset.by;
      } else {
        // 展开：记下这一格对应哪个时间桶，这样场次/分支数据后台到达时
        // refreshOpenDutRows() 能找到它并自动填上（不用用户再收起展开一次）
        dtd.dataset.bucket = r.bucket;
        dtd.dataset.by = by;
        renderDutRows(dtd, r.bucket, by);
      }
    });
  });
  drawTrendChart(t.rows);
  tile.classList.remove('hidden');
}

/* 和后端 test_history._bucket_of 一一对应：把一条场次的 started 算成它所属
   时间桶的 key。★ 必须和后端完全一致，否则 DUT 明细按桶归组会对不上。
   踩过：原来用 started.startsWith(bucket) 做前缀匹配，按天/按月能对上
   （bucket 是 started 的前缀），但：
     周   bucket 是 "2026-W40"（ISO 周号），根本不是 started 的前缀 -> 0 行
     小时 bucket 是 "2026-09-28 00:00"，而 started 是 "...00:01:46"，
          分钟不同 -> startsWith 失败 -> 0 行
   所以改成按同一套规则算 key 再相等比较。 */
function bucketKey(started, by) {
  const s = String(started || '');
  if (!s) return '';
  if (by === 'day') return s.slice(0, 10);              // YYYY-MM-DD
  if (by === 'month') return s.slice(0, 7);             // YYYY-MM
  if (by === 'hour') return s.slice(0, 13) + ':00';     // YYYY-MM-DD HH:00
  if (by === 'week') {
    // ISO 周号，和 Python datetime.isocalendar() 对齐
    const d = new Date(s.replace(' ', 'T') + 'Z');
    const dt = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
    const day = dt.getUTCDay() || 7;            // 周一=1..周日=7
    dt.setUTCDate(dt.getUTCDate() + 4 - day);   // 挪到本周四
    const yearStart = new Date(Date.UTC(dt.getUTCFullYear(), 0, 1));
    const week = Math.ceil(((dt - yearStart) / 86400000 + 1) / 7);
    return `${dt.getUTCFullYear()}-W${String(week).padStart(2, '0')}`;
  }
  return s.slice(0, 10);
}

/* 某个时间段里具体是哪些机器。
   数据来自 RUNS（/api/history/runs 的结果，和良率同一批场次），
   所以展开不发新请求 —— 按时间段在本地归组就行。 */
function renderDutRows(container, bucket, by) {
  container.innerHTML = '';
  if (!RUNS || !RUNS.rows) {
    if (RUNS_LOADING) {
      // ★ trend 比 runs 快，表画出来时 runs 常还在路上。这时展开不能说
      //   "还没查询"（看着像没点查询），要说清在等什么，取到会自动填上。
      container.textContent = '正在取这段的场次数据…取到后会自动显示在这里。';
    } else if (RUNS_ERR) {
      container.textContent = '场次数据没取到：' + RUNS_ERR;
    } else {
      container.textContent =
        '还没有场次数据。先在上面点「查询」，再展开这里看明细。';
    }
    return;
  }
  // ★ 按"桶 key 相等"归组，和后端一致；不要用字符串前缀（周/小时会对不上）
  const key = String(bucket).trim();
  let mine = RUNS.rows.filter(x => bucketKey(x.started, by) === key);

  // 「只看第 1 次测试」：纯本地筛选（test_count 已在数据里），不重新查询。
  // MI 的 TestCount 就是"这台的第几次测试"，=1 即首次 —— 良率(FPY)用的
  // 正是这一场，所以勾上后明细口径和上面的良率一致。
  const firstOnly = DUT_FILTER.firstOnly === 'first';
  const nAll = mine.length;
  if (firstOnly) mine = mine.filter(x => Number(x.test_count) === 1);

  // 明细筛选条：SN 包含 / 天线供应商 / 滤波器供应商。都是本地筛选，
  // 不重查后端。ant_suppliers / fu_suppliers 是公司名**列表**，可能一台
  // 整机装过多家（天线换件记录），所以用 includes 判断。
  const snFilter = (DUT_FILTER.sn || '').trim().toUpperCase();
  const antFilter = DUT_FILTER.ant || '';
  const fuFilter = DUT_FILTER.fu || '';
  if (snFilter) {
    mine = mine.filter(x =>
      String(x.serial_number || '').toUpperCase().includes(snFilter));
  }
  if (antFilter) {
    mine = mine.filter(x => (x.ant_suppliers || []).includes(antFilter));
  }
  if (fuFilter) {
    mine = mine.filter(x => (x.fu_suppliers || []).includes(fuFilter));
  }

  if (!mine.length) {
    container.textContent = firstOnly && nAll
      ? `这个时间段里没有第 1 次测试的场次（${nAll} 条全是复测）。去掉"只看第 1 次测试"可以看到它们。`
      : `这个时间段的 DUT 明细不在已取回的场次里` +
        `（最多取 ${fmtNum(RUNS.returned || 0)} 行，共 ${fmtNum(RUNS.total || 0)} 次测试；` +
        `超出的话缩小时间范围再查）。`;
    return;
  }

  // 显式按测试开始时间升序排（不依赖后端顺序）。同一时刻的按序列号稳定排。
  mine = mine.slice().sort((a, b) =>
    String(a.started || '').localeCompare(String(b.started || ''))
    || String(a.serial_number || '').localeCompare(String(b.serial_number || '')));

  const info = document.createElement('p');
  info.className = 'caption';
  info.textContent = firstOnly
    ? `这一段共 ${nAll} 次测试，其中第 1 次测试 ${mine.length} 场（已按测试时间排序）。`
    : `这一段共 ${mine.length} 次测试，含复测；按测试时间排序。`;
  container.appendChild(info);

  // ★ 失败分支/测试点后台查询失败时，把具体错误原因显示出来。原来只在
  //   每一行单元格里显示"查询失败"4 个字，用户和开发都不知道真实错因。
  if (BRANCHES_STATE === 'error' && BRANCHES_ERR) {
    const err = document.createElement('p');
    err.className = 'caption';
    err.style.color = 'var(--eds-color-fail, #c23)';
    err.textContent = '失败分支/测试点查询失败：' + BRANCHES_ERR
      + '（展开行里的"查询失败"就是这个原因；下次「查询」会重试。）';
    container.appendChild(err);
  }

  const tbl = document.createElement('table');
  tbl.className = 'tbl tbl-sub';
  // 表头两行：第 1 行列名，第 2 行筛选器。筛选器的值从全局 DUT_FILTER 读
  // （所有展开桶共享），改任一值触发 refreshOpenDutRows() 重画所有展开桶。
  const antOpts = collectDutSuppliers('ant_suppliers');
  const fuOpts = collectDutSuppliers('fu_suppliers');
  const optTag = (val, label, selected) =>
    `<option value="${val}"${val === selected ? ' selected' : ''}>${label}</option>`;
  tbl.innerHTML =
    '<thead>' +
      '<tr>' +
        '<th>测试时间</th><th class="num">第几次</th>' +
        '<th>产品号</th><th>整机 SN</th><th>天线供应商</th>' +
        '<th>滤波器供应商</th><th class="num">测试类别</th><th>结果</th>' +
        '<th>失败分支</th><th>失败测试点</th><th></th>' +
      '</tr>' +
      '<tr class="filter-row">' +
        '<th></th>' +   // 测试时间 — 不筛
        `<th class="num"><select class="col-filter" data-k="firstOnly">` +
          optTag('', '全部', DUT_FILTER.firstOnly) +
          optTag('first', '只第 1 次', DUT_FILTER.firstOnly) +
        '</select></th>' +
        '<th></th>' +   // 产品号 — 不筛
        '<th><input class="col-filter" data-k="sn" type="text" ' +
          `placeholder="包含…" value="${(DUT_FILTER.sn || '').replace(/"/g, '&quot;')}"></th>` +
        `<th><select class="col-filter" data-k="ant">` +
          optTag('', '全部', DUT_FILTER.ant) +
          antOpts.map(s => optTag(s, s, DUT_FILTER.ant)).join('') +
        '</select></th>' +
        `<th><select class="col-filter" data-k="fu">` +
          optTag('', '全部', DUT_FILTER.fu) +
          fuOpts.map(s => optTag(s, s, DUT_FILTER.fu)).join('') +
        '</select></th>' +
        '<th></th><th></th><th></th><th></th><th></th>' +   // 测试类别/结果/分支/测试点/查看 — 不筛
      '</tr>' +
    '</thead>';
  // 筛选控件：change 事件写回 DUT_FILTER + 重画所有展开桶
  tbl.querySelectorAll('.col-filter').forEach(ctl => {
    const ev = ctl.tagName === 'INPUT' ? 'input' : 'change';
    ctl.addEventListener(ev, e => {
      DUT_FILTER[ctl.dataset.k] = ctl.value;
      refreshOpenDutRows();
    });
  });
  const body = document.createElement('tbody');

  mine.forEach(x => {
    const tr = document.createElement('tr');
    if (!x.passed) tr.classList.add('row-warn');

    const add = (node) => {
      const td = document.createElement('td');
      if (typeof node === 'string') td.textContent = node;
      else td.appendChild(node);
      tr.appendChild(td);
      return td;
    };

    add(x.started || '—');
    // 第几次测试：=1 用蓝色标签点出"首次"，复测用灰字，一眼能分清
    const tc = Number(x.test_count);
    if (tc === 1) {
      add(tag('第 1 次', 'blue', '①'));
    } else {
      const span = document.createElement('span');
      span.className = 'hint';
      span.textContent = isFinite(tc) ? `第 ${tc} 次（复测）` : '—';
      add(span);
    }
    add(x.product || '—');
    add(x.serial_number || '—');
    add((x.ant_suppliers || []).join(' / ') || '—');
    add((x.fu_suppliers || []).join(' / ') || '—');
    // 测试类别:MI 的 cat 字段,整数值。界面只显示数字,不加业务说明
    // (用户定的,见 project-context 的"控件清单"节)。类别决定了后面
    // "失败分支/测试点"是否有数据(非 PIM 类别那两列通常是空),所以
    // 放在它们前面,语义上更连贯。
    add(String(x.category ?? '—'));
    add(x.passed ? tag('通过', 'green', '✓') : tag('失败', 'orange', '◐'));
    // 用户明确要求：只有 fail 才显示分支，pass 不加。
    // 失败但分支为空，要按后台加载状态分清是"还在查"还是"查了没有"
    // ★ 失败分支/测试点：优先用**行级**状态（_branches_status），因为
    //   现在是**分批查**，不同的 SN 可能处于不同状态（已查完 / 还没轮到 / 失败）。
    //   行级状态由 loadBranchesAsync 填：'done' / 'error' / undefined(=还没轮到)
    let brText;
    if (x.passed) {
      brText = '—';
    } else if ((x.failed_branches || []).length) {
      brText = (x.failed_branches || []).map(b => 'BR' + b).join(' ');
    } else if (x._branches_status === 'done') {
      // 这台查过了，确实没有分支级记录
      brText = '无测试点级记录';
    } else if (x._branches_status === 'error') {
      brText = '查询失败';
    } else if (BRANCHES_STATE === 'loading') {
      // 还没轮到这台（分批查中）
      brText = BRANCHES_PROGRESS
        ? `查询中（${BRANCHES_PROGRESS.done}/${BRANCHES_PROGRESS.total}）…`
        : '查询中…';
    } else {
      brText = '—';
    }
    add(brText);

    // ★ 失败测试点列（比分支级更细：BR 几号的哪个频率失败）。和失败分支
    //   一样是异步加载。显示紧凑：每个失败点一行 "BR28 @ 1896 MHz"，
    //   最多显示 3 个，超出显示 "+N 个"；完整名称放 title 悬停看。
    const tpCell = document.createElement('span');
    const tps = x.failed_testpoints || [];
    if (x.passed) {
      tpCell.textContent = '—';
    } else if (tps.length) {
      const MAX = 3;
      const lines = tps.slice(0, MAX).map(tp => {
        const f = tp.freq_mhz != null ? tp.freq_mhz + ' MHz' : '?';
        return `BR${tp.branch} @ ${f}`;
      });
      if (tps.length > MAX) lines.push(`+${tps.length - MAX} 个`);
      tpCell.innerHTML = lines.map(s =>
        s.replace(/</g, '&lt;')).join('<br>');
      // 悬停显示每个失败点的完整名称 + 测量值
      tpCell.title = tps.map(tp => {
        const parts = [`BR${tp.branch}`];
        if (tp.freq_mhz != null) parts.push(tp.freq_mhz + ' MHz');
        if (tp.name) parts.push(tp.name);
        if (tp.measured != null) parts.push('实测 ' + tp.measured);
        if (tp.fault_code) parts.push('fault ' + tp.fault_code);
        return parts.join(' / ');
      }).join('\n');
    } else if (x._branches_status === 'done') {
      tpCell.textContent = '无';
      tpCell.className = 'muted';
    } else if (x._branches_status === 'error') {
      tpCell.textContent = '查询失败';
      tpCell.className = 'muted';
      tpCell.title = x._branches_err || '';
    } else if (BRANCHES_STATE === 'loading') {
      tpCell.textContent = BRANCHES_PROGRESS
        ? `查询中（${BRANCHES_PROGRESS.done}/${BRANCHES_PROGRESS.total}）…`
        : '查询中…';
      tpCell.className = 'muted';
    } else {
      tpCell.textContent = '—';
      tpCell.className = 'muted';
    }
    add(tpCell);

    const view = document.createElement('button');
    view.type = 'button';
    view.className = 'btn btn-secondary btn-sm';
    view.textContent = '查看';
    view.title = '把这台送到下面的「单台详情」';
    view.addEventListener('click', () => sendToSingle(x.serial_number, x));
    // ★ 只给 PIM 测试行显示「查看」按钮：MI 的 category=28 (RADIO_PIM) 和
    //   212 (RADIO_PIM after GBI) 这两个才有 Knock 曲线。其他类别（26 烧机前、
    //   38 辅助测试、29 空气泄漏、32 老化）在 RTD 侧频率列表就是空的，
    //   点了只会看到"没有 knock 曲线"的提示 —— 干脆不让点，省得困惑。
    const PIM_CATS = new Set(['28', '212']);
    const cat = String(x.category || '').trim();
    if (PIM_CATS.has(cat)) {
      add(view);
    } else {
      // 占位，保持列对齐，用灰字告诉用户这类测试没有曲线可看
      const dash = document.createElement('span');
      dash.className = 'muted';
      dash.textContent = '—';
      dash.title = `${x.category ? '类别 ' + x.category : '非 PIM 测试'}`
        + ' 没有 Knock 曲线';
      add(dash);
    }

    body.appendChild(tr);
  });
  tbl.appendChild(body);
  container.appendChild(tbl);
}

/* 从良率表 DUT 明细点「查看」：把那台整机送到「测试曲线（RTD Knock）」块。
   row 带 serial_number / product / started（MI 的测试开始时间）。

   做法：用那台的序列号 + 测试当天的日期去 RTD 列会话 —— RTD 会话是按
   (device_id≈序列号, 日期) 索引的，不能用 MI 的秒级时间精确拼，所以按天找。
   列出后自动选中、填好频率，用户再点「取曲线」（或我们自动取第一条）。 */
async function sendToSingle(sn, row) {
  if (!sn) return;
  const started = (row && row.started) || '';
  const date = started.slice(0, 10);   // "2026-09-20 14:27:32" -> "2026-09-20"
  const product = (row && row.product) || '';
  // ★ 用户是从良率表某一行点查看进来的 —— 那一行就是"哪一次测试"：
  //   (test_count, category) 两边一一对应（MI 的 TestCount 和类别数字
  //   与 RTD 的 test_count 和 testcat "(28)"括号里的数字相同）。
  //   拿到后在卡片里按这两个字段精确定位，避免让用户再从多场里选。
  const want = {
    test_count: Number(row && row.test_count) || null,
    category: String((row && row.category) || '').trim() || null,
    started: started,
  };

  const tile = $('tileSession');
  tile.classList.remove('hidden');
  tile.scrollIntoView({behavior: 'smooth', block: 'start'});

  if (!date) {
    $('sessListHint').textContent =
      `这台（${sn}）没有测试开始时间，无法定位 RTD 会话当天的日期。`;
    return;
  }
  // 总是建卡片。凭据过期的处理交给卡片内部（cardListSessions 会自动触发刷新），
  // 这样用户点一下就行，不用先去点 ↻。
  await addCurveCard(sn, date, product, want);
}

/* 手绘趋势图：柱子 = 场次数，折线 = 良率。
   不引第三方图表库，省一个依赖，也避免服务器要出网下载。 */
function drawTrendChart(rows) {
  const cv = $('trendChart');
  const ctx = cv.getContext('2d');
  const W = cv.clientWidth || 900;
  const dpr = window.devicePixelRatio || 1;
  cv.width = W * dpr; cv.height = 210 * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, 210);
  if (!rows.length) return;

  const padL = 44, padR = 44, padT = 14, padB = 34;
  const w = W - padL - padR, h = 210 - padT - padB;
  const maxRuns = Math.max(...rows.map(r => r.n_runs), 1);
  const bw = Math.max(2, Math.min(38, w / rows.length * 0.62));
  const step = w / rows.length;

  // 横向参考线 + 右轴良率刻度（0~100%）
  ctx.strokeStyle = EDS.borderSubtle; ctx.fillStyle = EDS.onSecondary;
  ctx.font = '12px ' + EDS.font; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const yy = padT + h - (h * i / 4);
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + w, yy); ctx.stroke();
    ctx.textAlign = 'right';
    ctx.fillText(Math.round(maxRuns * i / 4), padL - 6, yy + 4);
    ctx.textAlign = 'left';
    ctx.fillText((i * 25) + '%', padL + w + 6, yy + 4);
  }

  // 柱子：场次数
  rows.forEach((r, i) => {
    const x = padL + step * i + (step - bw) / 2;
    const bh = h * r.n_runs / maxRuns;
    ctx.fillStyle = EDS.dv1;
    ctx.fillRect(x, padT + h - bh, bw, bh);
    // 失败部分用告警色叠在上面，一眼能看出哪天失败多
    if (r.n_failed) {
      const fh = h * r.n_failed / maxRuns;
      ctx.fillStyle = EDS.critical;
      ctx.fillRect(x, padT + h - fh, bw, fh);
    }
  });

  // 折线：良率
  ctx.strokeStyle = EDS.primary; ctx.lineWidth = 2;
  ctx.beginPath();
  let started = false;
  rows.forEach((r, i) => {
    if (r.yield_pct === null) return;
    const x = padL + step * i + step / 2;
    const yy = padT + h - h * (r.yield_pct / 100);
    if (!started) { ctx.moveTo(x, yy); started = true; } else { ctx.lineTo(x, yy); }
  });
  ctx.stroke();
  // 折线上的点
  ctx.fillStyle = EDS.primary;
  rows.forEach((r, i) => {
    if (r.yield_pct === null) return;
    const x = padL + step * i + step / 2;
    const yy = padT + h - h * (r.yield_pct / 100);
    ctx.beginPath(); ctx.arc(x, yy, 3, 0, Math.PI * 2); ctx.fill();
  });

  // X 轴标签：太密就隔几个显示一个
  ctx.fillStyle = EDS.onSecondary; ctx.font = '12px ' + EDS.font;
  ctx.textAlign = 'center';
  const every = Math.ceil(rows.length / Math.max(1, Math.floor(w / 78)));
  rows.forEach((r, i) => {
    if (i % every) return;
    const x = padL + step * i + step / 2;
    ctx.fillText(r.bucket.slice(5), x, 210 - 12);
  });
}

function exportTrend() {
  if (!TREND) return;
  const head = ['时间', '首次测试场次', '通过', '失败', 'FPY良率%', '涉及整机'];
  const lines = [head.join(',')].concat(TREND.t.rows.map(r => [
    r.bucket, r.n_runs, r.n_passed, r.n_failed,
    r.yield_pct === null ? '' : r.yield_pct.toFixed(4), r.n_serials,
  ].join(',')));
  const blob = new Blob(['\ufeff' + lines.join('\n')],
                        {type: 'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `FPY良率趋势_${TREND.by}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ── 测试结果列表 ───────────────────────────────
   参照 RTD Drilldown 的会话列表：一行一次测试，列的语义一一对应
       RTD  device_id / start_time / factory_name / test_station_id
            / prod_slash / rstate / Testcat / test_count / Status
       MI   serial_number / started / factory / station
            / product / revision / category / test_count / passed
   额外加了天线和滤波器供应商两列——这是 RTD 没有的，用来看失败
   是不是集中在某个供应商。 */

let RUNS = null;
let RUNS_ERR = null;   // doRuns 失败的原因，展开 DUT 明细时显示
let RUNS_LOADING = false;      // 场次数据正在取（展开时显示"正在加载"而不是"还没查询"）
let BRANCHES_STATE = 'idle';   // idle | loading | done | error：失败分支的后台加载状态
let BRANCHES_ERR = null;
let BRANCHES_PROGRESS = null;  // null | {done:N, total:M}：分批查询的进度

async function doRuns(hp) {
  try {
    const d = await api('/api/history/runs', Object.assign({}, hp, {
      // ★ 这一步固定**不查**失败分支，保证测试结果先快速出来。
      //   分支要逐台问 MI（每台约 30 秒），放到后台 loadBranchesAsync() 去补。
      with_branches: false,
      limit: 500}));
    RUNS = d;
    RUNS_ERR = null;
    RUNS_LOADING = false;
    // 不再渲染独立的"测试结果列表"块（已删）。RUNS 只用于：
    //   1. 良率表展开时列每段的 DUT 明细（renderDutRows 读 RUNS.rows）
    //   2. 点 DUT 行「查看」把那台送到 RTD 曲线
    loadBranchesAsync();      // 不 await：失败分支后台慢慢补
    return true;
  } catch (e) {
    // ★ 不静默吞掉：记下原因，展开 DUT 明细时显示出来。
    RUNS = null;
    RUNS_ERR = String(e.message || e);
    RUNS_LOADING = false;
    return false;
  }
}

/* 筛选状态：所有展开的 DUT 明细桶共享这一份。改任一字段后调
   refreshOpenDutRows() 重画所有展开的桶。 */
const DUT_FILTER = {
  firstOnly: '',   // '' | 'first'（"全部"/"只第 1 次"）
  sn: '',          // SN 包含文本
  ant: '',         // 天线供应商名（空=全部）
  fu: '',          // 滤波器供应商名（空=全部）
};

/* 从当前 RUNS.rows 收集某种供应商（去重排序）。
   这样表头的下拉选项反映用户查询范围里真实出现的供应商，
   不会出现"选了但没匹配项"的困惑。 */
function collectDutSuppliers(field) {
  if (!RUNS || !RUNS.rows) return [];
  const vals = new Set();
  RUNS.rows.forEach(r => (r[field] || []).forEach(s => s && vals.add(s)));
  return [...vals].sort();
}

/* 后台补「失败分支」。测试结果已经显示了，这一步回来再把分支填进去。
   只问失败的整机（通过的查了是白等几十秒）。 */
async function loadBranchesAsync() {
  if (!RUNS || !RUNS.rows) return;
  const failed = [...new Set(RUNS.rows
    .filter(r => !r.passed && r.serial_number)
    .map(r => r.serial_number))];
  if (!failed.length) { BRANCHES_STATE = 'done'; refreshOpenDutRows(); return; }

  // ★ 分批串行查，不要一次性查所有失败整机：
  //   - 实测 MI 的"8 路并行"其实没加速（SSPI 认证不好并发），6 台要 27 秒，
  //     用户 10+ 台会撞浏览器超时 → 显示"查询失败"但不知道为啥
  //   - 分批 3 台 × 串行：每批独立，不会撞超时；
  //     一批成功就立刻填进 DUT 明细，用户看到进度；
  //     某批失败不影响其他 —— 另外几批照样显示数据
  const BATCH = 3;
  const batches = [];
  for (let i = 0; i < failed.length; i += BATCH) {
    batches.push(failed.slice(i, i + BATCH));
  }
  BRANCHES_STATE = 'loading';
  BRANCHES_ERR = null;
  BRANCHES_PROGRESS = {done: 0, total: failed.length};
  refreshOpenDutRows();     // 显示"查询中（0/N）…"

  let lastErr = null;
  for (let i = 0; i < batches.length; i++) {
    const batch = batches[i];
    try {
      const d = await api('/api/history/branches', {sns: batch.join(',')});
      const br = d.branches || {};
      const tp = d.testpoints || {};
      RUNS.rows.forEach(r => {
        if (r.passed || !r.serial_number) return;
        if (!batch.includes(r.serial_number)) return;   // 只填这一批的
        // branches 对应整台（跨所有场次聚合）
        r.failed_branches = br[r.serial_number] || [];
        // testpoints 按 (serial_number, started) 匹配到行
        const bySt = tp[r.serial_number] || {};
        r.failed_testpoints = bySt[r.started] || [];
        // 行级状态：这台已经查完了（成功）
        r._branches_status = 'done';
      });
    } catch (e) {
      lastErr = String(e.message || e);
      // 这一批失败：把涉及的 SN 标成 error，但其他批继续
      RUNS.rows.forEach(r => {
        if (batch.includes(r.serial_number)) {
          r._branches_status = 'error';
          r._branches_err = lastErr;
        }
      });
    }
    BRANCHES_PROGRESS.done = Math.min((i + 1) * BATCH, failed.length);
    refreshOpenDutRows();   // 每批完成就刷新：已查完的行立刻填数据
  }
  // 整体状态：全部成功 → done；任何一批失败 → error（但其他批照样显示数据）
  BRANCHES_STATE = lastErr ? 'error' : 'done';
  BRANCHES_ERR = lastErr;
  BRANCHES_PROGRESS = null;
  refreshOpenDutRows();
}

/* 把当前已经展开的 DUT 明细重新渲染一遍（分支数据到了要体现出来）。
   只动已展开的行，不去打扰用户没展开的。 */
function refreshOpenDutRows() {
  document.querySelectorAll('#tblTrend .dut-cell[data-bucket]').forEach(cell => {
    renderDutRows(cell, cell.dataset.bucket, cell.dataset.by);
  });
}



/* ── 下层件追踪 ───────────────────────────────── */

let PARTS = null;
// ★ 下层件追踪**和前面查询条件联动**。
// 保存最近一次成功 doQuery 的条件，供 /api/parts 和 /api/parts/{sn} 使用，
// 保证"下拉里能选的子件 == 当前查询条件下 UPS 响应里的子件"。
// sn 模式下这里是 null（下层件追踪不显示）。
let PARTS_CTX = null;

async function loadParts(ctx) {
  // ctx: { product, from_date, to_date } —— 来自最近一次 doQuery。
  // 不传 ctx（累积模式）:后端会遍历所有已缓存 UPS 响应合并，用于向后兼容。
  // sn 模式下不传 ctx 也可（但 doQuery 现在根本不叫 loadParts）。
  try {
    PARTS_CTX = ctx || null;
    PARTS = await api('/api/parts', ctx || {});
    const kinds = ['antenna', 'filter_unit'];
    const total = kinds.reduce(
      (n, k) => n + ((PARTS && PARTS[k]) ? PARTS[k].length : 0), 0);
    if (total === 0) {
      // 当前条件下没有装配记录（比如 sn 模式、或该范围没产线数据）
      // → 不显示 tilePart，避免给用户一个空的下拉
      $('tilePart').classList.add('hidden');
      return;
    }
    fillPartSelect();
    fillParentSelect();            // 新加:"按整机反查"的整机 SN 下拉
    fillParentChildSelect();       // 级联:选中第一台整机的装过的子件
    $('tilePart').classList.remove('hidden');
  } catch (e) { /* 没有结构数据就不显示这个 tile */ }
}

function fillPartSelect() {
  const kind = $('fPartKind').value;
  const sel = $('fPartSn'); sel.innerHTML = '';
  (PARTS[kind] || []).forEach(p => {
    const o = document.createElement('option');
    o.value = p.child_sn;
    o.textContent = `${p.child_sn}（装过 ${p.n_parents} 台）`;
    sel.appendChild(o);
  });
}

/* "按整机反查"的整机 SN 下拉，用 PARTS.by_parent 的 key。
   显示 "parent_sn (天线 X / 滤波 Y)" —— 让用户一眼看到这台装过多少件。 */
function fillParentSelect() {
  const sel = $('fParentSn'); sel.innerHTML = '';
  const bp = (PARTS && PARTS.by_parent) || {};
  // 按 parent_sn 字母排序，稳定且可预测
  const parents = Object.keys(bp).sort();
  parents.forEach(parent => {
    const d = bp[parent] || {};
    const nAnt = (d.antenna || []).length;
    const nFu = (d.filter_unit || []).length;
    const o = document.createElement('option');
    o.value = parent;
    o.textContent = `${parent}（天线 ${nAnt} / 滤波 ${nFu}）`;
    sel.appendChild(o);
  });
}

/* 级联：根据当前选中的整机 + 子件类型，填"装过的子件 SN"下拉。 */
function fillParentChildSelect() {
  const sel = $('fParentChildSn'); sel.innerHTML = '';
  const parent = $('fParentSn').value;
  const kind = $('fParentChildKind').value;
  if (!parent) return;
  const bp = (PARTS && PARTS.by_parent) || {};
  const children = (bp[parent] && bp[parent][kind]) || [];
  if (!children.length) {
    const o = document.createElement('option');
    o.value = ''; o.disabled = true;
    o.textContent = (kind === 'antenna' ? '这台没装过天线' : '这台没装过滤波器');
    sel.appendChild(o);
    return;
  }
  children.forEach(c => {
    const o = document.createElement('option');
    o.value = c.child_sn;
    o.textContent = c.child_sn + (c.child_product ? `（${c.child_product}）` : '');
    sel.appendChild(o);
  });
}

/* "按整机反查" → "查看测试结果"：
   复用 openPartCurve 的核心逻辑 —— 现查该整机最新 PIM 测试,然后展开 RTD
   Knock 曲线。子件 SN 虽然界面上让用户选了,但 RTD 曲线是按整机(device_id)
   加载的,子件信息只是让用户确认"是这台的哪个件"。*/
async function viewByParent() {
  const parent = $('fParentSn').value;
  const slot = $('parentResult');
  const btn = $('btnViewByParent');
  if (!parent) { slot.innerHTML = '<p class="caption" style="padding:8px 12px">请先选整机 SN</p>'; return; }
  // 按钮立刻灰 + 显示加载态（防重复点击）
  btn.disabled = true;
  slot.innerHTML = '<p class="caption" style="padding:8px 12px">'
    + `查这台 ${parent} 的最新 PIM 测试…</p>`;
  try {
    const r = await api('/api/history/runs?' + new URLSearchParams({
      sn: parent, limit: '500',
    }));
    const pimRows = (r.rows || [])
      .filter(x => x.category === '28' || x.category === '212')
      .filter(x => x.started);
    if (!pimRows.length) {
      slot.innerHTML = '<p class="caption" style="padding:8px 12px">'
        + `${parent} 在 MI 里没有 PIM 测试（类别 28/212）记录，看不了曲线。</p>`;
      btn.disabled = false;  // 没数据让用户能换 SN 重试
      return;
    }
    pimRows.sort((a, b) => (a.started < b.started ? 1 : -1));
    const row = pimRows[0];
    slot.innerHTML = '';
    await addCurveCard(parent, row.started.slice(0, 10), row.product,
                       { test_count: Number(row.test_count) || null,
                         category: String(row.category || '').trim() || null,
                         started: row.started },
                       slot);
    // 展开成功 —— 按钮保持 disabled（灰），等用户换 SN 再复原
  } catch (e) {
    slot.innerHTML = '<p class="caption" style="padding:8px 12px;color:#c62828">'
      + '查这台的测试历史失败：' + (e.message || e) + '</p>';
    btn.disabled = false;   // 失败也放开按钮，让用户能重试
  }
}

/* 换下拉时：复原按钮 + 清空旧卡片。
   让"重新选了其他 SN"立刻能再点一次查看。 */
function resetParentView() {
  const btn = $('btnViewByParent');
  if (btn) btn.disabled = false;
  const slot = $('parentResult');
  if (slot) {
    slot.querySelectorAll('.curve-card').forEach(c => {
      const p = c.querySelector('.cardPlot');
      if (p && window.Plotly) Plotly.purge(p);
    });
    slot.innerHTML = '';
  }
}

async function doPart() {
  const sn = $('fPartSn').value;
  const box = $('partResult');
  box.innerHTML = '<span class="spin"></span>';
  try {
    // 和列表接口口径保持一致：只在当前查询条件对应的 UPS 响应里找这个子件
    const d = await api('/api/parts/' + encodeURIComponent(sn),
                        PARTS_CTX || {});
    box.innerHTML = '';

    const head = document.createElement('div');
    head.className = 'tl-br'; head.style.marginTop = '16px';
    head.appendChild(tag(`装过 ${d.n_parents} 台整机`, 'blue', 'ⓘ'));
    if (d.repeated_branches.length) {
      head.appendChild(tag(
        `跨机重复分支 ${d.repeated_branches.map(b => 'BR' + b).join('、')}`,
        'orange', '◐'));
    } else {
      head.appendChild(tag('没有跨机重复的分支', 'green', '✓'));
    }
    box.appendChild(head);

    const concl = document.createElement('p'); concl.className = 'caption';
    concl.innerHTML = d.repeated_branches.length
      ? `有 <strong>${d.repeated_branches.length}</strong> 个分支在多台整机上反复出现，`
        + '这些分支的问题可能跟着这个件走；其余分支每次都不同，更像跟着整机走。'
      : '所有失败分支每次装配都不同，<strong>支持 NFF（未发现故障）判定</strong>'
        + '——问题跟着整机走，不跟着这个件走。';
    box.appendChild(concl);

    const ul = document.createElement('ul'); ul.className = 'timeline';
    d.spans.forEach((s, i) => {
      const li = document.createElement('li');
      // 曲线卡片插入点（在时间轴项内部，这样展开后布局不乱）
      const slot = document.createElement('div');
      slot.className = 'tl-curve-slot';
      const h = document.createElement('div'); h.className = 'tl-head';
      const n = document.createElement('span'); n.className = 'tl-sn';
      n.textContent = `第 ${i + 1} 次 · ${s.parent_sn}`;
      h.appendChild(n);
      const tm = document.createElement('span'); tm.className = 'tl-time';
      tm.textContent = `${s.installed_at || '?'} → ${s.removed_at || '仍在机器上'}`;
      h.appendChild(tm);
      // ★ 「查看」按钮：就地展开该 parent 整机的 RTD Knock 曲线。
      //   快路径（UPS）没时间字段，所以不能像良率表那样传 (date, test_count)
      //   过去——点击时现查 /api/history/runs?sn=parent_sn 拿最新 PIM 测试。
      const viewBtn = document.createElement('button');
      viewBtn.type = 'button';
      viewBtn.className = 'btn btn-primary btn-sm tl-view';
      viewBtn.textContent = '查看';
      viewBtn.title = '展开该整机的 RTD Knock 曲线';
      viewBtn.addEventListener('click', () => openPartCurve(s.parent_sn, slot, viewBtn));
      h.appendChild(viewBtn);
      li.appendChild(h);
      const brs = Object.entries(s.failed_branches);
      const bd = document.createElement('div'); bd.className = 'tl-br';
      if (brs.length) {
        brs.sort((a,b) => Number(a[0]) - Number(b[0])).forEach(([b, v]) => {
          const rep = d.repeated_branches.includes(b);
          bd.appendChild(tag('BR' + b + (rep ? ' 重复' : ''),
                             rep ? 'orange' : 'yellow', '◐'));
        });
      } else {
        bd.appendChild(tag('该整机无 PIM 失败记录（或未拉逐台明细）', 'gray', '○'));
      }
      li.appendChild(bd);
      li.appendChild(slot);
      ul.appendChild(li);
    });
    box.appendChild(ul);
  } catch (e) {
    box.innerHTML = '';
    const n = document.createElement('div'); n.className = 'notice err';
    const i = document.createElement('i'); i.className = 'ic'; i.textContent = '✕';
    n.appendChild(i);
    const t = document.createElement('div'); t.textContent = String(e.message || e);
    n.appendChild(t); box.appendChild(n);
  }
}

/* 下层件追踪的时间轴某一项「查看」：就地展开该 parent 整机的 RTD Knock 曲线。
 *
 * ★ 关键取舍：
 *   下层件追踪走快路径（UnitProductStructure），没有装入/测试的时间字段，
 *   所以**不知道**该 parent 整机是哪一天测的。要列 RTD 会话必须有日期。
 *
 *   解决：点击时现查 /api/history/runs?sn=parent_sn 拿这台的测试历史，
 *   从里面挑**最新一次 PIM 测试**（cat=28 或 212），拿它的 date/product/
 *   test_count/category 做 `want`，然后调 addCurveCard 展开曲线。
 *
 *   /api/history/runs 单传 sn（不传产品/日期）只查这一台的历史，实测
 *   单台 1-5 秒，不卡。
 */
async function openPartCurve(parentSn, slot, btn) {
  if (!parentSn || !slot) return;
  // 已经展开过 → 再点一下就折叠
  if (slot.dataset.open === '1') {
    slot.querySelectorAll('.curve-card').forEach(c => {
      Plotly.purge(c.querySelector('.cardPlot'));
    });
    slot.innerHTML = '';
    slot.dataset.open = '';
    if (btn) btn.textContent = '查看';
    return;
  }
  slot.innerHTML = '<p class="caption" style="padding:8px 12px">'
    + `查这台 ${parentSn} 的最新 PIM 测试…</p>`;
  slot.dataset.open = '1';
  if (btn) btn.textContent = '关闭';
  try {
    // 单传 sn 让后端只查这一台（all_test_runs(serials=[sn])，不走慢路径）
    const r = await api('/api/history/runs?' + new URLSearchParams({
      sn: parentSn, limit: '500',
    }));
    // 挑最新一次 PIM 场次（cat 28 或 212），按 started 降序
    const pimRows = (r.rows || [])
      .filter(x => x.category === '28' || x.category === '212')
      .filter(x => x.started);
    if (!pimRows.length) {
      slot.innerHTML = '<p class="caption" style="padding:8px 12px">'
        + `${parentSn} 在 MI 里没有 PIM 测试（类别 28/212）记录，看不了曲线。</p>`;
      return;
    }
    pimRows.sort((a, b) => (a.started < b.started ? 1 : -1));
    const row = pimRows[0];  // 最新一次
    // 清掉"查这台..."的占位
    slot.innerHTML = '';
    slot.dataset.open = '1';
    // 复用 addCurveCard，容器指向这里的 slot，而不是全局 #sessCards
    await addCurveCard(parentSn, row.started.slice(0, 10), row.product,
                       { test_count: Number(row.test_count) || null,
                         category: String(row.category || '').trim() || null,
                         started: row.started },
                       slot);
  } catch (e) {
    slot.innerHTML = '<p class="caption" style="padding:8px 12px;color:#c62828">'
      + '查这台的测试历史失败：' + (e.message || e) + '</p>';
  }
}

/* ── 导出 ─────────────────────────────────────── */

/* ── 事件绑定 ─────────────────────────────────── */

$('btnQuery').addEventListener('click', doQuery);
$('btnPart').addEventListener('click', doPart);
$('fPartKind').addEventListener('change', fillPartSelect);

// 按整机反查:
//   - 整机下拉 change → 刷新子件下拉(级联) + 复原查看按钮 + 清空结果
//   - 子件类型下拉 change → 刷新子件下拉 + 复原
//   - 子件 SN 下拉 change → 只复原(整机还是同一台,子件下拉不需要重填)
//   - 查看按钮 → 展开 RTD 曲线、按钮灰掉
$('fParentSn').addEventListener('change', () => {
  fillParentChildSelect();
  resetParentView();
});
$('fParentChildKind').addEventListener('change', () => {
  fillParentChildSelect();
  resetParentView();
});
$('fParentChildSn').addEventListener('change', resetParentView);
$('btnViewByParent').addEventListener('click', viewByParent);

// ★ 筛选条件改了**不自动重查**，只提示"点查询生效"。
//   原来这几个控件是 change -> doQuery 的（注释写着"改完还要手点一次很
//   反直觉"）。那是快照时代的判断：当时一次查询 0.2 秒，自动触发确实顺手。
//   现在一次查询 60~120 秒（全部实时问 MI），自动触发的代价变了：
//     · 下拉一选，整页锁死一两分钟
//     · 想同时设天线+滤波器两个供应商，第一次改动那 60 秒完全白等
//     · 用户以为自己只是在"挑选条件"，结果已经发起了一次昂贵查询
//   和曲线那一块同样的取舍（那边也从自动加载改成了手动按钮）。
['fAntSup', 'fFuSup', 'fCat', 'fFrom', 'fTo', 'fProduct', 'fSns'].forEach(id => {
  const el = $(id);
  if (el) el.addEventListener('change', markDirty);
});

// ── 查询方式二选一（按条件 / 按序列号）──────────────
// 选中哪个，另一组控件就 disabled 变灰。<fieldset disabled> 会把里面
// 所有输入一次性禁用，比逐个控件设省事，也是浏览器原生的"整组禁用"。
function setMode(mode) {
  QUERY_MODE = mode;
  document.querySelectorAll('.mode-tab').forEach(b => {
    b.classList.toggle('is-active', b.dataset.mode === mode);
  });
  $('groupCond').disabled = (mode !== 'cond');
  $('groupSn').disabled = (mode !== 'sn');
  markDirty();   // 换了查询方式等于换了条件，提示点查询
}
document.querySelectorAll('.mode-tab').forEach(b => {
  b.addEventListener('click', () => setMode(b.dataset.mode));
});

// 趋势粒度切换：只重查趋势，不用重查整个良率分解
$('fTrendBy').addEventListener('change', () => {
  if (TREND) doTrend({
    from_date: dtVal('fFrom'), to_date: dtVal('fTo'),
    product: $('fProduct').value,
    sn: parseList($('fSns').value).join(','),
    category: readCheckedValues('fCat'),
    ant_supplier: $('fAntSup').value,
    fu_supplier: $('fFuSup').value,
  });
});
$('btnExportTrend').addEventListener('click', exportTrend);

// 「只看第 1 次测试」、SN 包含、天线/滤波器供应商 —— 这些筛选器现在
// 都移到**每个展开桶的 DUT 明细表头**，不在上方全局了。
// 控件在 renderDutRows 里渲染，事件把值写回 DUT_FILTER 并刷新所有展开桶。

// ★ 这里原来绑了「日期/产品框里按回车直接查」。已去掉，原因：
//   日期时间控件在键盘输入或用日历选择时，按回车是很自然的确认动作，
//   一按就发起一次 60~120 秒的查询 —— 用户以为自己只是在"改条件"，
//   结果整页锁住。这和档案里「改查询条件绝不能自动重查」是同一条原则：
//   昂贵操作只能由明确的点击发起，不能被顺手的键盘动作触发。
//   （fCat 现在是复选框组的 <div> 容器，本来也接不到回车。）

// RTD 那张图的网格线是 #dce9f5。本项目不能直接抄外部色值，
// 用 EDS 的 fill-selected(#d3e3f7) 做网格线，色相接近且在规范内。
// （原来是多台同屏曲线块的常量，那个块已删除，这里留给「单次测试明细」用）
const CURVE_GRID = '#d3e3f7';


window.addEventListener('resize', () => {
  if (TREND && TREND.t.rows.length) drawTrendChart(TREND.t.rows);
  // RTD 曲线卡片都是 Plotly responsive:true，窗口变化会自己缩放，不用手动重画
});
// ════════════════════════════════════════════════════════════════════
// 第 3 块：测试曲线（RTD Knock）—— 多卡片，每台一张图
//
// 来自良率表 DUT 明细的「查看」：每点一台就 addCurveCard() 往下加一张
// 独立卡片，卡片自带 会话/频率/分支 下拉和一张图，互不干扰，可单独关闭。
// 底层取数走 /api/sessions、/api/session/options、/api/session/curves
// （RTD，实测打通），和原来单图版一样，只是状态从全局挪到每张卡片自己身上。
//
// RTD 凭据约 11 小时过期，所以「不可用」是常态。initSession 探一次状态：
// 可用就显示剩余小时，不可用就提示去刷新（整块仍显示，别藏掉让人以为没功能）。
// ════════════════════════════════════════════════════════════════════

let RTD_OK = false;        // 最近一次探到的 RTD 凭据是否可用

async function initSession() {
  let h;
  try {
    h = await api('/api/health');
  } catch (e) { return; }
  const r = h.rtd || {};
  const box = $('rtdStatus');
  $('tileSession').classList.remove('hidden');
  RTD_OK = !!r.available;

  // 日期/产品默认跟随上方查询条件（点「查看」时会被那条记录覆盖）
  const to = dtVal('fTo');
  $('fSessDate').value = to ? to.slice(0, 10)
                            : new Date().toISOString().slice(0, 10);
  $('fSessProd').value = $('fProduct').value || '';

  // 刷新按钮要在本机起浏览器，缺 playwright 时后端报 enabled=false，
  // 先问一下再决定显不显示，别给个点了报错的按钮。
  let canRefresh = false;
  try {
    const st = await api('/api/rtd/refresh');
    canRefresh = !!st.enabled;
  } catch (e) { canRefresh = false; }
  if (canRefresh) {
    const btn = $('btnRtdRefresh');
    btn.style.display = '';
    if (!btn.dataset.wired) {
      btn.addEventListener('click', refreshRtdCookies);
      btn.dataset.wired = '1';
    }
  }

  if (!r.available) {
    box.innerHTML =
      '<span class="tag red"><i class="ic">✕</i>RTD 凭据不可用</span>';
    $('sessListHint').textContent = (r.error || '凭据已过期') +
      (r.cookie_hours_left != null
        ? `（已过期 ${Math.abs(r.cookie_hours_left).toFixed(1)} 小时）` : '') +
      '。' + (canRefresh
        ? '点右上角的 ↻ 刷新（多数情况不用管弹出的窗口，它会自己跑完；'
          + '只有连登录态也失效时才要在窗口里登录）。'
        : '在 Windows 上跑 refresh_cookies.py 在浏览器里登录一次。');
    return;
  }

  const left = r.cookie_hours_left;
  const warn = left !== null && left < 2;
  box.innerHTML = '<span class="tag ' + (warn ? 'yellow' : 'green') + '">' +
    '<i class="ic">' + (warn ? '◐' : '✓') + '</i>' +
    'RTD 凭据还剩 ' + (left === null ? '?' : left.toFixed(1)) + ' 小时</span>';
}

// 刷新 RTD 凭据的全局锁：多张卡片同时发现凭据过期时，只跑一次刷新，
// 其它都 await 同一个 Promise。★ 两个浏览器抢同一个 profile 会互相破坏，
// 所以绝不能并发刷新（档案里踩过）。
let RTD_REFRESH_PROMISE = null;

/* 确保 RTD 凭据可用。可用直接返回 true；不可用就刷新（带锁防重复），
   刷完返回是否可用。onProgress(文本) 用来把进度显示在调用方（卡片）上。 */
async function ensureRtdReady(onProgress) {
  // 先查当前状态
  try {
    const h = await api('/api/health');
    if (h.rtd && h.rtd.available) { RTD_OK = true; return true; }
  } catch (e) { /* 查不到就往下尝试刷新 */ }

  // 已有刷新在跑 -> 复用它，不另起（防止并发抢 profile）
  if (RTD_REFRESH_PROMISE) {
    if (onProgress) onProgress('凭据过期，正在刷新（已有一个刷新在进行，等它完成）…');
    return RTD_REFRESH_PROMISE;
  }
  RTD_REFRESH_PROMISE = _runRtdRefresh(onProgress);
  try {
    return await RTD_REFRESH_PROMISE;
  } finally {
    RTD_REFRESH_PROMISE = null;
  }
}

/* 真正执行一次刷新并轮询到结束。返回凭据是否可用。 */
async function _runRtdRefresh(onProgress) {
  const box = $('rtdStatus');
  const btn = $('btnRtdRefresh');
  if (btn) btn.disabled = true;
  box.innerHTML = '<span class="tag blue"><i class="ic">◐</i>正在刷新凭据...</span>';
  const prog = (t) => { if (onProgress) onProgress(t); };
  prog('凭据过期，正在启动刷新。若弹出浏览器窗口要求登录，请在窗口里完成单点登录。');
  try {
    await api('/api/rtd/refresh', null, 'POST');
  } catch (e) {
    if (btn) btn.disabled = false;
    box.innerHTML = '<span class="tag red"><i class="ic">✕</i>刷新启动失败</span>';
    prog('刷新启动失败：' + (e.message || e));
    return false;
  }
  const t0 = Date.now();
  while (Date.now() - t0 < 360000) {       // 最多等 6 分钟
    await new Promise(r => setTimeout(r, 2000));
    let st;
    try { st = await api('/api/rtd/refresh'); } catch (e) { continue; }
    const last = (st.log || []).slice(-1)[0] || '';
    if (st.state === 'running') {
      const sec = Math.round((Date.now() - t0) / 1000);
      box.innerHTML = '<span class="tag blue"><i class="ic">◐</i>刷新中 ' + sec + ' 秒</span>';
      prog('凭据刷新中（' + sec + ' 秒）…' + (last ? ' ' + last : ''));
      continue;
    }
    // 跑完了
    if (btn) btn.disabled = false;
    await initSession();        // 重新探一次状态，更新右上角标签
    const h = await api('/api/health').catch(() => ({}));
    const ok = !!(h.rtd && h.rtd.available);
    const left = h.rtd && h.rtd.cookie_hours_left;
    if (st.ok && ok) {
      notice('凭据已刷新，还剩 ' + (left == null ? '?' : left.toFixed(1)) + ' 小时', 'ok');
    } else {
      notice('刷新未成功（退出码 ' + st.returncode + '）'
             + (ok ? '。但凭据现在可用。' : '。凭据仍不可用，可能要在弹出的窗口里登录。'),
             'warn', (st.log || []).slice(-6).join('\n'));
    }
    RTD_OK = ok;
    return ok;
  }
  if (btn) btn.disabled = false;
  box.innerHTML = '<span class="tag yellow"><i class="ic">◐</i>刷新超时</span>';
  prog('刷新等了 6 分钟还没结束。看一眼有没有浏览器窗口在等你登录。');
  return false;
}

/* ↻ 按钮的手动入口：就是走同一套 ensureRtdReady。 */
async function refreshRtdCookies() {
  await ensureRtdReady((t) => { $('sessListHint').textContent = t; });
}

/* 新增一张曲线卡片并加载。sn/date/product 来自良率表 DUT 明细那条记录。
   want = {test_count, category, started}（都可以是 null）：从良率表点查看
   过来时带着"用户点的是哪一次测试"，列完会话后精确定位到那场。 */
async function addCurveCard(sn, date, product, want, container) {
  // container：卡片插到哪里。默认 '#sessCards'（测试曲线主区）；
  // 下层件追踪里就地展开曲线时传入该整机那条时间轴项下的 slot。
  const containerEl = container || $('sessCards');
  const tpl = $('curveCardTpl');
  const node = tpl.content.firstElementChild.cloneNode(true);
  const q = sel => node.querySelector(sel);
  // 卡片自带状态（不再用全局变量，多张卡片才能各管各的）
  const card = {
    node, sn, date, product,
    want: want || null,
    sessions: null, data: null,
    selSess: q('.selSess'), selFreq: q('.selFreq'), selBranch: q('.selBranch'),
    title: q('.curve-card-title'), hint: q('.cardHint'),
    kpi: q('.cardKpi'), plot: q('.cardPlot'),
  };
  // 卡片标题多一个"对应那次测试"的说明，让用户确信是他点的那次
  const w = want || {};
  const whichTest = (w.test_count && w.category)
    ? `　第 ${w.test_count} 次（类别 ${w.category}）` : '';
  card.title.textContent = `${sn}　${date || '(无日期)'}` +
    (product ? `　${product}` : '') + whichTest;
  q('.btnClose').addEventListener('click', () => {
    Plotly.purge(card.plot);
    node.remove();
  });
  // ★ 重试按钮：出错或图没出来时点一下，清掉当前状态再重走一遍整个取数流程
  //   （列会话 → 查选项 → 取曲线）。如果凭据过期，cardListSessions 开头的
  //   ensureRtdReady 会自动刷新；网络瞬时错误走 rtd_client 的 3 次重试。
  q('.btnRetry').addEventListener('click', () => {
    // 清状态：图、KPI、下拉选项、缓存的数据
    Plotly.purge(card.plot);
    card.plot.innerHTML = '';
    card.kpi.innerHTML = '';
    card.selSess.innerHTML = '';
    card.selFreq.innerHTML = '';
    card.sessions = null;
    card.data = null;
    cardListSessions(card);
  });
  card.selSess.addEventListener('change', () => cardPickSession(card));
  card.selFreq.addEventListener('change', () => cardLoadCurves(card));
  card.selBranch.addEventListener('change', () => cardLoadCurves(card));

  // 新卡片插在最上面，最近看的在最前（容器可能是 #sessCards，也可能是
  // 下层件追踪时间轴里某项下方的 slot）
  containerEl.prepend(node);
  node.scrollIntoView({behavior: 'smooth', block: 'center'});

  await cardListSessions(card);
}

/* 列出这台当天的 RTD 会话。凭据过期会自动触发刷新（带全局锁防重复）。 */
async function cardListSessions(card) {
  if (!card.date) {
    card.hint.textContent = '这台没有测试开始时间，无法定位当天的 RTD 会话。';
    return;
  }
  // ★ 自动刷新：凭据过期就先刷新再列会话，用户不用先去点 ↻。
  //   多张卡片同时触发时，ensureRtdReady 内部有锁，只会刷一次。
  const ready = await ensureRtdReady((t) => { card.hint.textContent = t; });
  if (!ready) {
    card.hint.textContent =
      'RTD 凭据刷新没成功，看不了 Knock 曲线。' +
      '可能需要在弹出的浏览器窗口里完成微软登录，然后重新点「查看」。';
    return;
  }

  card.hint.textContent = '正在列出当天的测试会话（约 6 秒）...';
  try {
    const d = await api('/api/sessions', {
      date: card.date,
      product: card.product || undefined,
      // 只列这台整机的会话（RTD 的 device_id 约等于序列号）
      sn: card.sn || undefined,
    });
    card.sessions = d.sessions;
    const sel = card.selSess;
    sel.innerHTML = '';
    if (!d.n_sessions) {
      // ★ 这台这天 RTD 真的没录到 —— 不是查询出错，是**两边数据缺口**：
      //   RTD 和 MI 是两套独立系统，RTD 测试台有时漏录或入库延迟，这天其他
      //   机器可能都在、偏偏缺这台。实测遇到过。消息要让用户知道系统**查过了**，
      //   并给出可操作的下一步。
      const whichTest = (card.want && card.want.test_count && card.want.category)
        ? `第 ${card.want.test_count} 次（类别 ${card.want.category}）`
        : '这次测试';
      card.hint.textContent =
        `RTD 里没有 ${card.sn} 在 ${card.date} 的 ${whichTest}。` +
        'MI 和 RTD 是两套独立系统，RTD 测试台偶尔会漏录或入库延迟 ——' +
        '这台这天在 RTD 侧没数据，看不了曲线。' +
        '换到良率表里这台**其他日期**的 PIM 测试再试试。';
      return;
    }
    d.sessions.forEach((s, i) => {
      const o = document.createElement('option');
      o.value = String(i);
      // ★ 下拉里把 PIM 场次用 ★ 标出来，让用户一眼看出哪场有曲线。
      //   同一台机器一天会跑好几场测试（PRE_UNIT/BOARD/PIM 等），
      //   只有 PIM 那场才有 Knock 曲线。标记基于 testcat 的字符串匹配，
      //   比等所有会话 options 都查完要快得多。
      const isPim = /PIM/i.test(String(s.testcat || ''));
      o.textContent = (isPim ? '★ ' : '　 ')
        + `${s.start_time_short || s.start_time}（${s.testcat}）`;
      if (isPim) o.dataset.pim = '1';
      sel.appendChild(o);
    });

    // ★ 定位用户点"查看"进来的那一次测试（如果良率表传了 want 过来）。
    //   匹配键 = (test_count, 类别数字) —— MI 和 RTD 两边完全一致：
    //     MI  cat="28"  test_count=2
    //     RTD testcat="RADIO_PIM (28)"  test_count=2
    //   秒级时间也对应（但 MI 是北京时间、RTD 是 UTC，差 8 小时，不好直接比字符串）。
    //   test_count+类别是整数，是最可靠的匹配键。
    const want = card.want || {};
    const wantTC = Number(want.test_count) || null;
    const wantCat = want.category ? String(want.category).trim() : null;
    const catOf = (ss) => {
      const m = String(ss.testcat || '').match(/\((\d+)\)/);
      return m ? m[1] : '';
    };
    let picked = -1;
    if (wantTC != null && wantCat) {
      picked = d.sessions.findIndex(
        ss => Number(ss.test_count) === wantTC && catOf(ss) === wantCat);
      if (picked >= 0) {
        // 在那条选项前加✓标记，让用户确信是他点的那次
        const opt = sel.options[picked];
        opt.textContent = '✓ ' + opt.textContent.replace(/^(★ |　 )/, '');
        opt.selected = true;
      }
    }
    // 退回：没精确匹配到就选第一个 PIM 场次
    if (picked < 0) {
      picked = d.sessions.findIndex(
        ss => /PIM/i.test(String(ss.testcat || '')));
    }
    sel.selectedIndex = picked >= 0 ? picked : 0;

    const nPim = d.sessions.filter(
      ss => /PIM/i.test(String(ss.testcat || ''))).length;
    if (nPim === 0) {
      card.hint.textContent =
        `这天 ${card.sn} 共 ${d.n_sessions} 场测试，但**没有 PIM 场次**，`
        + '所以没有 Knock 曲线可画。';
    } else if (wantTC != null && wantCat && picked < 0) {
      // 用户点的那次在 RTD 里找不到（时间偏差 >24 小时、测试没进 RTD 等）
      card.hint.textContent =
        `没找到 "第 ${wantTC} 次（类别 ${wantCat}）" 对应的 RTD 会话，`
        + `已退回选中第一个 PIM 场次。`;
    }
    await cardPickSession(card);
  } catch (e) {
    card.hint.textContent = '列会话失败：' + (e.message || e);
  }
}

function cardSession(card) {
  if (!card.sessions) return null;
  return card.sessions[parseInt(card.selSess.value, 10)] || null;
}

/* 选中某次会话后查它的可选频率，并自动取一次曲线。 */
async function cardPickSession(card) {
  const s = cardSession(card);
  if (!s) return;
  const fs = card.selFreq;
  fs.innerHTML = '<option>查可选频率...</option>';
  try {
    // ★ 频率必须按会话单独查，沿用别的会话的频率会静默返回 0 条曲线。
    const o = await api('/api/session/options', {
      date: card.date, device_id: s.device_id, start_time: s.start_time,
    });
    fs.innerHTML = '';
    o.freqs_mhz.forEach(f => {
      const el = document.createElement('option');
      el.value = String(f); el.textContent = String(f);
      fs.appendChild(el);
    });
    if (!o.freqs_mhz.length) {
      Plotly.purge(card.plot);
      card.kpi.innerHTML = '';
      const isPim = /PIM/i.test(String(s.testcat || ''));
      const allSessions = card.sessions || [];
      const otherPim = allSessions.filter((x, i) =>
        i !== card.selSess.selectedIndex && /PIM/i.test(String(x.testcat || '')));
      // ★ 根据当前场次是不是 PIM，提示语完全不同。原来统一说"通常不是 PIM
      //   敲击测试"，但如果用户选的就是 cat=28 RADIO_PIM，这句话自相矛盾。
      if (isPim) {
        // 用户选的是 PIM 场次但频率是空的 —— RTD 侧真的没录到频率数据
        // （测试台漏录、测试跑一半停了，或 RTD 没识别出产品，prod_slash=None）
        card.hint.textContent =
          `${s.device_id} ${s.start_time}（${s.testcat}）：这场**是** PIM 测试，`
          + '但 RTD 侧频率列表是空的 —— 说明这次测试没录到有效的 PIM 频率数据。'
          + '原因可能是：测试台采集故障、测试跑到一半中断、或 RTD 没识别出产品。'
          + (otherPim.length > 0
              ? ` 下拉里还有 ${otherPim.length} 场 PIM —— 可以试试，但如果都是空，就是这台这天 RTD 侧真的没数据。`
              : ' 这台当天其他场次也不是 PIM，没别的可试了。');
      } else {
        // 用户选的是非 PIM 场次（PRE_UNIT/BURN-IN 之类）—— 这类本来就没曲线
        card.hint.textContent =
          `${s.device_id} ${s.start_time}（${s.testcat}）这次测试没有 knock 曲线：`
          + '它不是 PIM 敲击测试（PRE_UNIT/BURN-IN/ADDITIONAL 等类别没有敲击流程）。'
          + (otherPim.length > 0
              ? ` 下拉里带 ★ 的 ${otherPim.length} 场是 PIM，换一个试试。`
              : ' 这台当天没有 PIM 场次，没曲线可画。');
      }
      return;
    }
    const want = o.freqs_mhz.indexOf(1896);
    fs.selectedIndex = want >= 0 ? want : 0;
    card.hint.textContent =
      `${s.device_id} ${s.start_time}（${s.testcat}，测试台 ${s.station}）：`
      + `${o.freqs_mhz.length} 个频率、${o.branches.length} 个分支可选。`;
    // 自动取一次（用户点「查看」就是想直接看图）
    await cardLoadCurves(card);
  } catch (e) {
    fs.innerHTML = '';
    card.hint.textContent = '查可选频率失败：' + (e.message || e);
  }
}

/* 按当前频率/分支取曲线并画到这张卡片。 */
async function cardLoadCurves(card) {
  const s = cardSession(card);
  if (!s) return;
  const freq = card.selFreq.value;
  if (!freq || isNaN(parseFloat(freq))) return;   // 频率还没加载完
  card.hint.textContent = '正在取曲线...';
  try {
    const t0 = performance.now();
    const d = await api('/api/session/curves', {
      date: card.date, device_id: s.device_id, start_time: s.start_time,
      freq_mhz: freq,
      branches: card.selBranch.value || undefined,
    });
    d._fetchMs = performance.now() - t0;
    card.data = d;
    drawCardCurves(card);
  } catch (e) {
    card.hint.textContent = '取曲线失败：' + (e.message || e);
  }
}

/* 把一张卡片的曲线画出来（32 分支合并成 1 条 trace，最省内存）。 */
function drawCardCurves(card) {
  const d = card.data;
  if (!d) return;
  if (!d.series || !d.series.length) {
    card.hint.textContent =
      '这个组合没有曲线数据，换个频率试试（频率填错时接口返回空而不报错）。';
    return;
  }
  const x = d.x;
  const t0 = performance.now();
  // 所有分支拼成一条 trace，段间插 null 断开（connectgaps:false）
  const xs = [], ys = [];
  d.series.forEach(se => {
    for (let i = 0; i < x.length; i++) { xs.push(x[i]); ys.push(se.y[i]); }
    xs.push(null); ys.push(null);
  });
  const traces = [{
    x: xs, y: ys, type: 'scatter', mode: 'lines',
    name: d.series.length + ' 个分支',
    line: {color: EDS.dv1, width: 1}, opacity: 0.65,
    hoverinfo: 'skip', connectgaps: false,
  }];

  let lo = Infinity, hi = -Infinity;
  d.series.forEach(se => se.y.forEach(v => {
    if (v === null || v === undefined) return;
    if (v < lo) lo = v; if (v > hi) hi = v;
  }));
  const pad = (hi - lo) * 0.08 || 0.1;
  const yrange = isFinite(lo) ? [lo - pad, hi + pad] : undefined;

  Plotly.react(card.plot, traces, {
    height: 360,
    margin: {l: 62, r: 16, t: 12, b: 44},
    paper_bgcolor: '#ffffff', plot_bgcolor: '#ffffff',
    font: {family: EDS.font, size: 12, color: EDS.on},
    // ★ 横轴标题照搬 RTD 原文，不自己翻译成「第 N 次迭代」（含义未确认）
    xaxis: {title: {text: d.x_title || '', font: {size: 12}},
            gridcolor: CURVE_GRID, zeroline: false},
    yaxis: {title: {text: d.y_title || '', font: {size: 12}},
            gridcolor: CURVE_GRID, zeroline: false, range: yrange},
    hovermode: 'closest', showlegend: false,
  }, {
    displaylogo: false, responsive: true,
    modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'],
  });
  const drawMs = performance.now() - t0;

  card.kpi.innerHTML = [
    ['分支数', fmtNum(d.n_series)],
    ['每条点数', fmtNum(x.length)],
    ['总点数', fmtNum(d.n_points)],
    ['测试结果', d.status || '—'],
  ].map(kv => '<div class="kpi">' +
      '<div class="kpi-label">' + kv[0] + '</div>' +
      '<div class="kpi-value">' + kv[1] + '</div></div>').join('');

  card.hint.textContent =
    `${d.device_id} ${d.start_time}，${d.freq_mhz} MHz，一条线一个分支。` +
    ` 取数 ${(d._fetchMs || 0).toFixed(0)} ms、画图 ${drawMs.toFixed(0)} ms。` +
    ` 横轴是 RTD 的 ${d.x_title || '?'}（取值 0~61 的浮点，不是迭代序号）。`;
}

// ★ initSession 必须等 init() 完成。它要读 #fProduct 和 #fTo 的值，
//   而那两个是 init() 里异步填上的 —— 并行启动会读到空字符串。
init().then(initSession);
