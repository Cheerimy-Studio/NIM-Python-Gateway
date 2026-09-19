/* NVIDIA Gateway 控制台脚本 */
(() => {
'use strict';

const B = window.NGW.base, CSRF = window.NGW.csrf;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

// 渲染「客户端可控」内容（请求/响应原文、模型名等）时必须先转义，否则一段带
// <script> 的会话文本会在管理员浏览器里执行（存储型 XSS → 偷走会话/CSRF 令牌）。
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};
const fmtTime = ts => !ts ? '-' : new Date(ts * 1000).toLocaleString('zh-CN', {hour12: false});
const fmtAgo = ts => {
  if (!ts) return '从未';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h';
  return Math.floor(s / 86400) + 'd';
};
const statusText = st => st === 0 ? '网络' : String(st);

const BAN_NAMES = {
  fail_ladder: '失败封禁', auth_fail: '鉴权失败', invalid_key: '密钥失效',
  daily_cap: '当日额度',
};
const keyState = k => {
  const now = Date.now() / 1000;
  if (!k.enabled) return {t: '已停用', c: 'secondary'};
  if ((k.banned_until || 0) > now) {
    const left = Math.ceil(k.banned_until - now);
    const name = BAN_NAMES[k.ban_reason] || '封禁';
    const leftTxt = left >= 3600 ? Math.ceil(left / 3600) + 'h' : left >= 60 ? Math.ceil(left / 60) + 'm' : left + 's';
    return {t: name + ' ' + leftTxt, c: k.ban_reason === 'invalid_key' || k.ban_reason === 'daily_cap' ? 'danger' : 'warning'};
  }
  if (k.status === 'invalid') return {t: '密钥失效', c: 'danger'};
  return {t: '可用', c: 'success'};
};

let toastBox = null;
const toast = (msg, type = 'success') => {
  if (!toastBox) { toastBox = el('div', 'toast-container position-fixed bottom-0 end-0 p-3'); document.body.appendChild(toastBox); }
  const t = el('div', 'toast align-items-center text-bg-' + type + ' border-0');
  const wrap = el('div', 'd-flex');
  const body = el('div', 'toast-body'); body.textContent = msg;
  const btn = el('button', 'btn-close btn-close-white me-2 m-auto');
  btn.onclick = () => t.remove();
  wrap.append(body, btn); t.appendChild(wrap);
  toastBox.appendChild(t);
  new bootstrap.Toast(t, {delay: 2200}).show();
  t.addEventListener('hidden.bs.toast', () => t.remove());
};

async function api(path, opts = {}) {
  const init = {method: opts.method || 'GET', headers: {'X-CSRF': CSRF}};
  if (opts.json !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json); }
  if (opts.form) init.body = opts.form;
  const res = await fetch(B + 'api/' + path, init);
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty */ }
  if (!res.ok) throw new Error((data && data.error && data.error.message) || ('HTTP ' + res.status));
  return data;
}

/* run: 立即执行并捕获错误（数据加载用） */
const run = async fn => { try { return await fn(); } catch (e) { toast(e.message, 'danger'); } };
/* guard: 包装为事件处理器（切勿直接当函数调用） */
const guard = fn => async (...args) => { try { return await fn(...args); } catch (e) { toast(e.message, 'danger'); } };

/* ================= 自定义弹窗（替代 confirm/prompt） ================= */
function uiDialog({ title = '确认', body = '', input = null, danger = false, okText = '确定' }) {
  return new Promise(resolve => {
    const mask = el('div', 'ui-mask');
    const box = el('div', 'ui-dialog');
    box.append(el('div', 'ui-title', title));
    const bodyEl = el('div', 'ui-body');
    if (body) bodyEl.appendChild(el('div', 'ui-msg', body));
    let inputEl = null;
    if (input !== null) {
      inputEl = el('input', 'form-control form-control-sm mt-2');
      inputEl.value = input.value || '';
      inputEl.placeholder = input.placeholder || '';
      bodyEl.appendChild(inputEl);
    }
    box.appendChild(bodyEl);
    const foot = el('div', 'ui-foot');
    const cancel = el('button', 'btn btn-sm btn-outline-secondary', '取消');
    const ok = el('button', 'btn btn-sm ' + (danger ? 'btn-danger' : 'btn-primary'), okText);
    foot.append(cancel, ok);
    box.appendChild(foot);
    mask.appendChild(box);
    document.body.appendChild(mask);
    const close = val => { mask.remove(); document.removeEventListener('keydown', onKey); resolve(val); };
    const okFn = () => close(inputEl ? inputEl.value.trim() : true);
    cancel.onclick = () => close(input !== null ? null : false);
    ok.onclick = okFn;
    mask.onclick = e => { if (e.target === mask) close(input !== null ? null : false); };
    const onKey = e => {
      if (e.key === 'Escape') close(input !== null ? null : false);
      if (e.key === 'Enter') okFn();
    };
    document.addEventListener('keydown', onKey);
    if (inputEl) { inputEl.focus(); inputEl.select(); } else { ok.focus(); }
  });
}
const uiConfirm = (msg, opts = {}) => uiDialog({ title: opts.title || '确认操作', body: msg, danger: !!opts.danger, okText: opts.okText || '确定' });
const uiPrompt = (msg, val = '', opts = {}) => uiDialog({ title: opts.title || '输入', body: msg, input: { value: val, placeholder: opts.placeholder || '' }, okText: opts.okText || '确定' });

/* ================= 导航 ================= */
const LOADERS = {
  dash: () => loadOverview(),
  keys: () => loadKeys(),
  upstreams: () => loadUpstreams(),
  logs: () => loadLogs(),
  queue: () => loadQueue(),
  sessions: () => loadSessions(),
  settings: () => loadSettings(),
  docs: () => fillDocs(),
};
function activate(name) {
  $$('.sidebar nav a').forEach(a => a.classList.toggle('active', a.dataset.pane === name));
  $$('.pane').forEach(p => p.classList.toggle('active', p.id === 'pane-' + name));
  if (LOADERS[name]) LOADERS[name]();
}

/* ================= 仪表盘 ================= */
async function loadOverview() {
  const o = await run(() => api('overview'));
  if (!o) return;
  const cards = [
    ['账号', o.keys.enabled + ' / ' + o.keys.total, o.keys.banned ? '封禁 ' + o.keys.banned : '全部可用', 'accent'],
    ['今日请求', o.today.total, '成功 ' + o.today.success + ' / 失败 ' + o.today.fail, 'green'],
    ['今日成功率', o.today.rate == null ? '—' : o.today.rate + '%', '账号计 ' + o.daily.requests + ' 次', 'accent'],
    ['当前 RPM', o.rpm + ' / ' + (o.rpm_limit_total === -1 ? '不限' : o.rpm_limit_total), '今日 tokens ' + o.daily.tokens, 'amber'],
    ['排队中', o.queue, o.queue ? '等待可用账号' : '无等待', o.queue ? 'red' : 'accent'],
  ];
  const box = $('#dash-cards'); box.innerHTML = '';
  for (const [label, val, sub, tone] of cards) {
    const col = el('div', 'col-6 col-md-4 col-xl');
    const stat = el('div', 'card stat h-100 ' + tone);
    stat.append(el('div', 'lbl', label), el('div', 'num', String(val)), el('div', 'sub', sub || ' '));
    col.appendChild(stat); box.appendChild(col);
  }

  const tb = $('#dash-models'); tb.innerHTML = '';
  if (!o.models.length) { const tr = el('tr'); const td = el('td', 'text-muted text-center', '—'); td.colSpan = 2; tr.appendChild(td); tb.appendChild(tr); }
  for (const m of o.models) {
    const tr = el('tr');
    const td1 = el('td', 'text-truncate', m.model); td1.style.maxWidth = '180px'; td1.title = m.model;
    tr.append(td1, el('td', 'text-end', String(m.count)));
    tb.appendChild(tr);
  }

  const te = $('#dash-errors'); te.innerHTML = '';
  if (!o.recent_errors.length) { const tr = el('tr'); const td = el('td', 'text-muted text-center', '—'); td.colSpan = 4; tr.appendChild(td); te.appendChild(tr); }
  for (const r of o.recent_errors) {
    const tr = el('tr');
    tr.append(el('td', 'small', fmtTime(r[0])), el('td', 'small text-truncate', r[2] || '-'),
      el('td', 'small', statusText(r[4])), el('td', 'small text-danger err-cell text-truncate', r[6] || '-'));
    te.appendChild(tr);
  }

  const tk = $('#dash-risky'); tk.innerHTML = '';
  if (!o.risky.length) { const tr = el('tr'); const td = el('td', 'text-muted text-center', '—'); td.colSpan = 4; tr.appendChild(td); tk.appendChild(tr); }
  for (const k of o.risky) {
    const tr = el('tr');
    tr.append(el('td', 'small', k.email), el('td', 'text-end small' + (k.consecutive ? ' text-danger fw-bold' : ''), String(k.consecutive)),
      el('td', 'text-end small', k.fail_ratio + '%'), el('td', 'small text-danger err-cell text-truncate', k.last_error || '-'));
    tk.appendChild(tr);
  }
}

/* ================= 账号池 ================= */
const keysState = {page: 1};
let revealSet = new Set();
const batchSel = new Set();

function updateBatchBar(rows) {
  const bar = $('#batch-bar');
  const n = batchSel.size;
  bar.classList.toggle('d-none', n === 0);
  $('#batch-count').textContent = String(n);
  const all = rows && rows.length && rows.every(k => batchSel.has(k.id));
  $('#keys-all').checked = !!all;
}

async function runBatch(op) {
  const ids = [...batchSel];
  if (!ids.length) return;
  if (op === 'delete') {
    const ok = await uiConfirm(`删除选中的 ${ids.length} 个账号？此操作不可恢复。`, {danger: true, okText: '删除'});
    if (!ok) return;
  }
  if (op === 'reset') {
    const ok = await uiConfirm(`重置选中的 ${ids.length} 个账号的统计与封禁？`);
    if (!ok) return;
  }
  await guard(async () => {
    const payload = {op, ids};
    if (op === 'move') payload.upstream_id = $('#batch-up').value;
    const r = await api('keys/batch', {method: 'POST', json: payload});
    if (op === 'test') {
      const okN = Object.values(r.results).filter(t => t && t.ok).length;
      toast(`测试完成：${okN}/${Object.keys(r.results).length} 可用`);
    } else if (op === 'move') {
      toast(`已转移 ${r.moved} 个账号`);
    } else {
      toast('批量操作完成');
    }
    loadKeys(); loadOverview();
  });
}

function bindBatch() {
  $('#keys-all').onchange = e => {
    $$('#keys-rows tr.krow').forEach(tr => {
      const cb = tr.querySelector('.row-check');
      if (cb) { cb.checked = e.target.checked; e.target.checked ? batchSel.add(tr.dataset.id) : batchSel.delete(tr.dataset.id); }
    });
    updateBatchBar();
  };
  $('#batch-clear').onclick = () => { batchSel.clear(); loadKeys(); };
  $('#batch-enable').onclick = () => runBatch('enable');
  $('#batch-disable').onclick = () => runBatch('disable');
  $('#batch-test').onclick = () => runBatch('test');
  $('#batch-reset').onclick = () => runBatch('reset');
  $('#batch-delete').onclick = () => runBatch('delete');
  $('#batch-move').onclick = () => runBatch('move');
}

async function loadKeys() {
  const q = $('#keys-q').value.trim();
  const status = $('#keys-filter').value;
  const d = await run(() => api(`keys?q=${encodeURIComponent(q)}&status=${status}&page=${keysState.page}`));
  if (!d) return;
  const tb = $('#keys-rows'); tb.innerHTML = '';
  if (!d.rows.length) {
    const tr = el('tr'); const td = el('td', 'text-muted text-center py-4', '—');
    td.colSpan = 15; tr.appendChild(td); tb.appendChild(tr);
  }
  // 按渠道分组渲染
  const groups = new Map();
  for (const k of d.rows) {
    const g = k.upstream_name || '-';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(k);
  }
  for (const [gName, rows] of groups) {
    const grpTr = el('tr', 'grp-row');
    const grpTd = el('td', '', '');
    grpTd.colSpan = 15;
    grpTd.append(
      el('i', 'bi bi-hdd-network me-1 text-muted'),
      document.createTextNode(gName + ' '),
      el('span', 'text-muted fw-normal', `（${rows.filter(k => k.enabled).length}/${rows.length} 可用 · 今日 ${rows.reduce((s, k) => s + (k.today.requests || 0), 0)} 次 · ${rows.reduce((s, k) => s + (k.today.tokens || 0), 0)} tk）`),
    );
    grpTr.appendChild(grpTd);
    tb.appendChild(grpTr);
  }
  for (const k of d.rows) {
    const tr = el('tr', 'krow');
    tr.dataset.id = k.id;
    const st = keyState(k);
    if (!k.enabled || st.c === 'danger') tr.classList.add('table-light');
    else if (st.c === 'warning') tr.classList.add('table-warning');

    const chevTd = el('td', 'text-center');
    const rowCb = el('input', 'form-check-input row-check');
    rowCb.type = 'checkbox';
    rowCb.checked = batchSel.has(k.id);
    rowCb.style.verticalAlign = 'middle';
    rowCb.onclick = e => e.stopPropagation();
    rowCb.onchange = () => { rowCb.checked ? batchSel.add(k.id) : batchSel.delete(k.id); updateBatchBar(); };
    const chev = el('i', 'bi bi-chevron-right chev ms-1');
    chevTd.append(rowCb, chev);
    chevTd.title = '选择 / 展开详情';

    const tdEmail = el('td', 'small', k.email);
    const tdKey = el('td');
    const code = el('code', 'key-mono');
    code.textContent = revealSet.has(k.id) ? k.apikey : k.apikey.slice(0, 11) + '…' + k.apikey.slice(-4);
    code.style.cursor = 'pointer'; code.title = '显示/隐藏';
    code.onclick = () => { revealSet.has(k.id) ? revealSet.delete(k.id) : revealSet.add(k.id); loadKeys(); };
    const copy = el('button', 'btn btn-sm btn-link py-0 ps-1', '复制');
    copy.onclick = () => navigator.clipboard.writeText(k.apikey).then(() => toast('已复制'));
    tdKey.append(code, copy);

    tr.append(
      chevTd,
      tdEmail, tdKey,
      el('td', 'small text-muted', k.upstream_name || '-'),
      (() => { const td = el('td'); td.appendChild(el('span', 'badge text-bg-' + st.c, st.t));
        if (k.last_error) { const e = el('div', 'small text-danger text-truncate', k.last_error); e.style.maxWidth = '170px'; e.title = k.last_error; td.appendChild(e); }
        return td; })(),
      (() => { const td = el('td', 'text-end small', `${k.today.requests}/${d.daily_cap} · ${k.today.tokens}`); td.title = '今日请求/上限 · tokens'; return td; })(),
      el('td', 'text-end', String(k.total_requests)),
      el('td', 'text-end text-success', String(k.total_success)),
      el('td', 'text-end' + (k.total_fail ? ' text-danger' : ''), String(k.total_fail)),
      el('td', 'text-end' + (k.consecutive_failures ? ' text-danger fw-bold' : ''), String(k.consecutive_failures)),
      el('td', 'text-end', `${k.rpm_used}/${d.rate_limit}`),
      el('td', 'text-end small', `${k.prompt_tokens}/${k.completion_tokens}`),
      el('td', 'small text-muted', fmtAgo(k.last_used_at)),
    );

    const tdOp = el('td');
    const grp = el('div', 'btn-group btn-group-sm');
    const mk = (label, cls, fn) => {
      const b = el('button', 'btn btn-sm ' + cls, label);
      b.onclick = fn; grp.appendChild(b);
    };
    mk(k.enabled ? '停用' : '启用', 'btn-outline-' + (k.enabled ? 'secondary' : 'success'),
      guard(async () => { await api('keys/op', {method: 'POST', json: {op: k.enabled ? 'disable' : 'enable', id: k.id}}); loadKeys(); }));
    mk('测试', 'btn-outline-primary', guard(async () => {
      const t = (await api('keys/op', {method: 'POST', json: {op: 'test', id: k.id}})).test;
      t.ok ? toast(`可用 · ${t.models} 模型 · ${t.ms}ms`) : toast(`失败：${t.error || 'HTTP ' + t.status}`, 'danger');
    }));
    mk('重置', 'btn-outline-secondary', guard(async () => {
      if (await uiConfirm('重置该账号统计与封禁？')) { await api('keys/op', {method: 'POST', json: {op: 'reset', id: k.id}}); loadKeys(); }
    }));
    mk('删除', 'btn-outline-danger', guard(async () => {
      if (await uiConfirm(`删除 ${k.email}？`, {danger: true, okText: '删除'})) { await api('keys/op', {method: 'POST', json: {op: 'delete', id: k.id}}); loadKeys(); }
    }));
    tdOp.appendChild(grp);
    tr.appendChild(tdOp);
    tb.appendChild(tr);
    // 行点击展开/收起详情看板（复选框与按钮区域除外）
    tr.addEventListener('click', e => {
      if (e.target.closest('button') || e.target.closest('a') || e.target.closest('code') || e.target.closest('input')) return;
      toggleKeyDetail(k.id, tr);
    });
    if (keysState2.openId === k.id) toggleKeyDetail(k.id, tr, true);
  }
  updateBatchBar(d.rows);

  const pg = $('#keys-pages'); pg.innerHTML = '';
  const addPage = (label, page, active, disabled) => {
    const li = el('li', 'page-item' + (active ? ' active' : '') + (disabled ? ' disabled' : ''));
    const a = el('a', 'page-link', label); a.href = '#';
    a.onclick = e => { e.preventDefault(); if (!active && !disabled) { keysState.page = page; loadKeys(); } };
    li.appendChild(a); pg.appendChild(li);
  };
  addPage('«', Math.max(1, keysState.page - 1), false, keysState.page <= 1);
  for (let p = 1; p <= d.pages; p++) {
    if (p > 1 && p < d.pages && Math.abs(p - keysState.page) > 2) {
      if (p === 2 || p === d.pages - 1) addPage('…', p, false, true);
      continue;
    }
    addPage(String(p), p, p === keysState.page, false);
  }
  addPage('»', Math.min(d.pages, keysState.page + 1), false, keysState.page >= d.pages);
}

/* ================= 账号行内看板 ================= */
const keysState2 = {openId: null};

function toggleKeyDetail(id, tr, force) {
  const existed = tr.nextElementSibling;
  const isOpen = existed && existed.classList.contains('detail-row');
  if (isOpen) {
    if (force === true) return;
    existed.remove();
    tr.classList.remove('open');
    if (keysState2.openId === id) keysState2.openId = null;
    return;
  }
  // 收起其它已展开的行
  $$('#keys-rows tr.detail-row').forEach(r => r.remove());
  $$('#keys-rows tr.krow.open').forEach(r => r.classList.remove('open'));
  keysState2.openId = id;
  tr.classList.add('open');
  const dtr = el('tr', 'detail-row');
  const td = el('td');
  td.colSpan = tr.children.length;
  const panel = el('div', 'kpanel');
  panel.textContent = '加载中…';
  td.appendChild(panel);
  dtr.appendChild(td);
  tr.after(dtr);
  refreshKeyPanel(id, panel);
}

async function refreshKeyPanel(id, panel) {
  const d = await run(() => api('keydetail?id=' + encodeURIComponent(id)));
  if (!d || !panel.isConnected) return;
  panel.innerHTML = '';
  const k = d.key;
  const st = keyState(k);

  const chips = el('div', 'mini');
  const chip = (label, val) => {
    const c = el('div', 'chip');
    c.append(el('span', '', label), el('b', '', String(val)));
    return c;
  };
  chips.append(
    (() => { const c = chip('状态', st.t); c.appendChild(el('span', 'badge text-bg-' + st.c, ' ')); return c; })(),
    chip('今日', `${k.today.requests} 次 / ${k.today.tokens} tk`),
    chip('RPM', `${k.rpm_used}/${k.rate_limit || '-'}`),
    chip('tokens', `${k.prompt_tokens} / ${k.completion_tokens}`),
    chip('连败', String(k.consecutive_failures)),
    chip('上游', k.upstream_name || '-'),
  );
  panel.appendChild(chips);

  const strip = el('div', 'd-flex align-items-center mb-2 flex-wrap');
  strip.append(el('span', 'small text-muted me-2', '最近 10 次'));
  const recent = d.recent || [];
  for (let i = 0; i < 10; i++) {
    const r = recent[i];
    const dot = el('span', 'dot ' + (r ? ((r[3] >= 200 && r[3] < 400) ? 'ok' : 'fail') : 'empty'));
    if (r) {
      dot.title = `${fmtTime(r[0])} · ${r[2] || r[1]} · HTTP ${statusText(r[3])} · ${r[4]}ms${r[5] ? ' · ' + r[5] : ''}`;
      dot.style.cursor = 'help';
    }
    strip.appendChild(dot);
  }
  const refreshBtn = el('button', 'btn btn-sm btn-link py-0 ms-2', '刷新');
  refreshBtn.onclick = () => { panel.textContent = '加载中…'; refreshKeyPanel(id, panel); };
  strip.appendChild(refreshBtn);
  panel.appendChild(strip);

  const tbl = el('table', 'table table-sm table-bordered mb-0');
  const thead = el('thead', 'table-light');
  const htr = el('tr');
  ['时间', '接口', '模型', '状态', '耗时', '错误'].forEach(h => htr.appendChild(el('th', '', h)));
  thead.appendChild(htr);
  tbl.appendChild(thead);
  const tbody = el('tbody');
  if (!recent.length) {
    const tr = el('tr'); const td = el('td', 'text-muted text-center', '暂无请求'); td.colSpan = 6; tr.appendChild(td); tbody.appendChild(tr);
  }
  for (const r of recent) {
    const tr = el('tr');
    const ok = r[3] >= 200 && r[3] < 400;
    tr.append(
      el('td', 'small', fmtTime(r[0])),
      el('td', 'small', EP_NAMES[r[1]] || r[1]),
      el('td', 'small text-truncate', r[2] || '-'),
      el('td', 'small ' + (ok ? 'text-success' : 'text-danger fw-bold'), statusText(r[3])),
      el('td', 'text-end small', r[4] + 'ms'),
      el('td', 'small text-danger err-cell', r[5] || '-'),
    );
    tbody.appendChild(tr);
  }
  tbl.appendChild(tbody);
  panel.appendChild(tbl);
}

/* ================= 上游 ================= */
async function loadUpstreams() {
  const d = await run(() => api('upstreams'));
  if (!d) return;
  const tb = $('#up-rows'); tb.innerHTML = '';
  if (!d.rows.length) {
    const tr = el('tr'); const td = el('td', 'text-muted text-center py-4', '—');
    td.colSpan = 13; tr.appendChild(td); tb.appendChild(tr);
  }
  for (const u of d.rows) {
    const tr = el('tr');
    if (!u.enabled) tr.classList.add('table-light');
    const modelsTxt = (u.models && u.models.length) ? u.models.length + ' 个' : '全部';
    const modelsCell = el('td', 'small text-muted', modelsTxt + (u.model_map_count ? ' · ' + u.model_map_count + ' 映射' : ''));
    if (u.models && u.models.length) {
      modelsCell.title = '模型：\n' + u.models.join('\n');
      modelsCell.style.cursor = 'help';
    }
    const scoreCls = u.feasibility >= 80 ? 'score-hi' : u.feasibility >= 60 ? 'score-mid' : 'score-lo';
    const flags = [];
    if ((u.hide_errors || 0) === 1 || ((u.hide_errors || 0) === 0 && u.hide_errors_global)) flags.push('隐错');
    if ((u.hide_mapped || 0) === 1 || ((u.hide_mapped || 0) === 0 && u.hide_mapped_global)) flags.push('隐原名');
    if (u.param_overrides && Object.keys(u.param_overrides).length) flags.push('固定参数×' + Object.keys(u.param_overrides).length);
    if (u.thinking_defaults && u.thinking_defaults.trim()) flags.push('思考默认');
    tr.append(
      el('td', 'fw-semibold', u.name),
      (() => { const td = el('td'); const c = el('code', 'key-mono small', u.base); td.appendChild(c); return td; })(),
      (() => { const td = el('td'); td.appendChild(el('span', scoreCls, u.feasibility + '%'));
        td.title = '近 20 次成功率（平滑）' + (u.recent ? `，样本 ${u.recent}` : '，暂无样本'); return td; })(),
      el('td', 'text-end', String(u.weight)),
      el('td', 'text-end', u.rpm_cap > 0 ? String(u.rpm_cap) : '不限'),
      el('td', 'text-end', u.daily_cap > 0 ? String(u.daily_cap) : '不限'),
      modelsCell,
      (() => { const td = el('td', 'small text-muted', flags.length ? flags.join(' · ') : '—'); return td; })(),
      el('td', 'text-end', `${u.enabled_keys}/${u.keys}`),
      el('td', 'text-end', String(u.today_requests)),
      el('td', 'text-end', String(u.minute_used)),
      (() => { const td = el('td'); td.appendChild(el('span', 'badge text-bg-' + (u.enabled ? 'success' : 'secondary'), u.enabled ? '启用' : '停用')); return td; })(),
    );
    const tdOp = el('td');
    const grp = el('div', 'btn-group btn-group-sm');
    const mk = (label, cls, fn) => {
      const b = el('button', 'btn btn-sm ' + cls, label);
      b.onclick = fn; grp.appendChild(b);
    };
    mk('编辑', 'btn-outline-primary', () => {
      $('#up-id').value = u.id;
      $('#up-name').value = u.name;
      $('#up-base').value = u.base;
      $('#up-weight').value = u.weight;
      $('#up-rpm').value = u.rpm_cap;
      $('#up-daily').value = u.daily_cap;
      $('#up-models').value = (u.models || []).join('\n');
      $('#up-map').value = Object.entries(u.model_map || {}).map(([k, v]) => `${k}=${v}`).join('\n');
      for (const [id, key] of UP_FIELDS) $('#' + id).value = u[key] || 0;
      $('#up-herr').value = String(u.hide_errors || 0);
      $('#up-hname').value = String(u.hide_mapped || 0);
      tdefLoad(u.thinking_defaults || '');
      poverLoad(u.param_overrides || {});
      $('#up-enabled').checked = !!u.enabled;
      $('#up-form-title').textContent = '编辑：' + u.name;
      $('#up-cancel').classList.remove('d-none');
      $('#up-form-title').scrollIntoView({block: 'center'});
    });
    mk(u.enabled ? '停用' : '启用', 'btn-outline-secondary', guard(async () => {
      // 只回传必要字段。整行回传（{...u}）会把 models（数组）与 model_map（字典）
      // 原样送回去，一旦后端按文本解析就会把它们改写坏 —— 曾经就是这样把渠道白名单
      // 写成了 ["['deepseek-v4-flash']"]，那条渠道从此对任何请求都判「模型不匹配」。
      await api('upstreams', {
        method: 'POST',
        json: {id: u.id, name: u.name, base: u.base, enabled: !u.enabled},
      });
      loadUpstreams();
    }));
    mk('删除', 'btn-outline-danger', guard(async () => {
      if (!(await uiConfirm(`删除上游 ${u.name}？`, {danger: true, okText: '删除'}))) return;
      await api('upstreams/delete', {method: 'POST', json: {id: u.id}});
      loadUpstreams();
    }));
    tdOp.appendChild(grp);
    tr.appendChild(tdOp);
    tb.appendChild(tr);
  }
  // 导入页下拉同步
  const sel = $('#import-up');
  const cur = sel.value;
  sel.innerHTML = '';
  for (const u of d.rows.filter(x => x.enabled)) {
    const opt = el('option', '', u.name);
    opt.value = u.id;
    sel.appendChild(opt);
  }
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
}

/* 渠道覆盖字段（0=继承全局） */
const UP_FIELDS = [
  ['up-rpm-key', 'rpm'], ['up-tpm-key', 'tpm'],
  ['up-coolcd', 'account_cooldown_ms'], ['up-hrl', 'hourly_request_limit'],
  ['up-accon', 'acct_concurrency'], ['up-chcon', 'total_concurrency'],
  ['up-dcap-key', 'daily_request_cap'], ['up-dtok-key', 'daily_token_limit'],
  ['up-retries', 'max_retries'], ['up-bb-base', 'retry_backoff_base_ms'], ['up-bb-max', 'retry_backoff_max_ms'],
  ['up-minwait', 'retry_min_wait_ms'],
  ['up-timeout', 'request_timeout'], ['up-ctimeout', 'connect_timeout'],
  ['up-bstep', 'ban_step_seconds'], ['up-bmax', 'ban_max_seconds'],
  ['up-hfban', 'hard_fail_ban_seconds'], ['up-hfcnt', 'hard_fail_disable_count'],
  ['up-c429k', 'cool_429_seconds'], ['up-c5xxk', 'cool_5xx_seconds'],
  ['up-ctok', 'cool_timeout_seconds'], ['up-ccnk', 'cool_conn_seconds'],
  ['up-bthk', 'breaker_threshold'], ['up-bseck', 'breaker_seconds'],
];

/* ================= 参数覆写可视化编辑器 ================= */
let poverData = {};  // {scope: {param: value}}
let tdefData = {};   // {model: effort}

function poverLoad(obj) {
  poverData = {};
  for (const [scope, params] of Object.entries(obj || {})) {
    poverData[scope] = {};
    for (const [k, v] of Object.entries(params || {})) {
      poverData[scope][k] = v;
    }
  }
  poverRender();
}

function poverClear() {
  poverData = {};
  poverRender();
}

function poverDump() {
  const out = {};
  for (const [scope, params] of Object.entries(poverData)) {
    if (Object.keys(params).length) out[scope] = params;
  }
  return out;
}

function poverRender() {
  const list = $('#pover-list');
  if (!list) return;
  list.innerHTML = '';
  const entries = Object.entries(poverData);
  if (!entries.length) {
    list.innerHTML = '<span class="text-muted small">暂无覆写规则</span>';
    return;
  }
  for (const [scope, params] of entries) {
    for (const [k, v] of Object.entries(params)) {
      const item = el('span', 'pover-item');
      const scopeSpan = el('span', 'pover-scope', scope === '*' ? '全部' : scope);
      const kvSpan = el('span', 'pover-kv', `${k}=${v}`);
      const del = el('span', 'pover-del', '×');
      del.title = '删除';
      del.onclick = () => { delete poverData[scope][k]; if (!Object.keys(poverData[scope]).length) delete poverData[scope]; poverRender(); };
      item.append(scopeSpan, kvSpan, del);
      list.appendChild(item);
    }
  }
}

function tdefLoad(raw) {
  tdefData = {};
  for (const line of String(raw || '').split('\n')) {
    const line2 = line.trim();
    if (!line2 || !line2.includes('=')) continue;
    const [k, ...rest] = line2.split('=');
    const v = rest.join('=').trim();
    if (k.trim() && v) tdefData[k.trim()] = v;
  }
  tdefRender();
}

function tdefDump() {
  return Object.entries(tdefData).map(([k, v]) => `${k}=${v}`).join('\n');
}

function tdefClear() {
  tdefData = {};
  tdefRender();
}

function tdefRender() {
  const list = $('#tdef-list');
  if (!list) return;
  list.innerHTML = '';
  const entries = Object.entries(tdefData);
  if (!entries.length) {
    list.innerHTML = '<span class="text-muted small">暂无默认强度</span>';
    return;
  }
  for (const [model, effort] of entries) {
    const item = el('span', 'pover-item');
    const scopeSpan = el('span', 'pover-scope', model);
    const kvSpan = el('span', 'pover-kv', effort);
    const del = el('span', 'pover-del', '×');
    del.title = '删除';
    del.onclick = () => { delete tdefData[model]; tdefRender(); };
    item.append(scopeSpan, kvSpan, del);
    list.appendChild(item);
  }
}

function poverAdd() {
  const scope = $('#pover-model').value.trim() || '*';
  const key = $('#pover-key').value.trim();
  const val = $('#pover-val').value.trim();
  if (!key || !val) { toast('参数名和值不能为空', 'warning'); return; }
  if (!poverData[scope]) poverData[scope] = {};
  poverData[scope][key] = val;
  $('#pover-key').value = '';
  $('#pover-val').value = '';
  poverRender();
}

function tdefAdd() {
  const model = $('#tdef-model').value.trim();
  const effort = $('#tdef-effort').value;
  if (!model) { toast('请选择模型', 'warning'); return; }
  tdefData[model] = effort;
  tdefRender();
  toast(`已添加 ${model}=${effort}`, 'success');
}
async function fillModelSelects() {
  const sel1 = $('#pover-model');
  const sel2 = $('#tdef-model');
  if (!sel1 && !sel2) return;
  const models = new Set();
  // 从后台拉取所有渠道的可用模型 + 映射源名
  try {
    const d = await api('upstreams');
    for (const u of (d.rows || [])) {
      for (const m of (u.models || [])) models.add(m);
      for (const src of Object.keys(u.model_map || {})) models.add(src);
    }
  } catch (e) { /* 静默 */ }
  // 补充当前表单里的模型/映射
  const upModels = $('#up-models');
  if (upModels && upModels.value.trim()) {
    upModels.value.split(/[\n,]/).forEach(m => { m = m.trim(); if (m) models.add(m); });
  }
  const upMap = $('#up-map');
  if (upMap && upMap.value.trim()) {
    upMap.value.split('\n').forEach(line => {
      const parts = line.split('=');
      if (parts[0] && parts[0].trim()) models.add(parts[0].trim());
    });
  }
  for (const sel of [sel1, sel2]) {
    if (!sel) continue;
    const cur = sel.value;
    sel.innerHTML = '<option value="">选择模型...</option>';
    for (const m of [...models].sort()) {
      const opt = el('option', '', m);
      opt.value = m;
      sel.appendChild(opt);
    }
    if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
  }
}

function bindUpstreams() {
  $('#up-save').onclick = guard(async () => {
    const payload = {
      id: $('#up-id').value || undefined,
      name: $('#up-name').value.trim(),
      base: $('#up-base').value.trim(),
      weight: $('#up-weight').value,
      rpm_cap: $('#up-rpm').value,
      daily_cap: $('#up-daily').value,
      models: $('#up-models').value,
      model_map: $('#up-map').value,
      hide_errors: $('#up-herr').value,
      hide_mapped: $('#up-hname').value,
      thinking_defaults: tdefDump(),
      param_overrides: poverDump(),
      enabled: $('#up-enabled').checked,
    };
    for (const [id, key] of UP_FIELDS) payload[key] = $('#' + id).value;
    await api('upstreams', {method: 'POST', json: payload});
    $('#up-id').value = '';
    $('#up-name').value = '';
    $('#up-base').value = '';
    $('#up-weight').value = '10';
    $('#up-rpm').value = '0';
    $('#up-daily').value = '0';
    $('#up-models').value = '';
    $('#up-map').value = '';
    $('#up-herr').value = '0';
    $('#up-hname').value = '0';
    tdefClear();
    poverClear();
    for (const [id] of UP_FIELDS) $('#' + id).value = '0';
    $('#up-enabled').checked = true;
    $('#up-form-title').textContent = '添加上游';
    $('#up-cancel').classList.add('d-none');
    toast('已保存');
    loadUpstreams(); refreshBatchUps(); loadPresets();
  });
  $('#up-cancel').onclick = () => {
    $('#up-id').value = '';
    $('#up-form-title').textContent = '添加上游';
    $('#up-cancel').classList.add('d-none');
  };
  // 参数覆写 & 思考强度可视化编辑
  const poverBtn = $('#pover-add');
  if (poverBtn) poverBtn.onclick = () => { poverAdd(); };
  const tdefBtn = $('#tdef-add');
  if (tdefBtn) tdefBtn.onclick = () => { tdefAdd(); };
  // 渠道模型/映射变化时刷新模型选择器
  const upModels = $('#up-models');
  if (upModels) upModels.addEventListener('input', fillModelSelects);
  const upMap = $('#up-map');
  if (upMap) upMap.addEventListener('input', fillModelSelects);
}

async function loadPresets() {
  try {
    const r = await fetch(B + 'api/presets');
    const d = await r.json();
    const sel = $('#up-preset');
    sel.innerHTML = '<option value="">— 选择预设 —</option>';
    for (const [id, p] of Object.entries(d.presets || {})) {
      sel.innerHTML += `<option value="${esc(id)}">${esc(p.name)}</option>`;
    }
    sel.onchange = () => {
      const p = d.presets[sel.value];
      if (!p) return;
      for (const [id, key] of UP_FIELDS) { $('#' + id).value = p[key] != null ? p[key] : 0; }
    };
  } catch(e) {}
}

async function refreshBatchUps() {
  const d = await run(() => api('upstreams'));
  if (!d) return;
  const sel = $('#batch-up');
  sel.innerHTML = '';
  for (const u of d.rows.filter(x => x.enabled)) {
    const opt = el('option', '', u.name);
    opt.value = u.id;
    sel.appendChild(opt);
  }
}

/* ================= 导入 ================= */
function bindImport() {
  $('#import-btn').onclick = guard(async () => {
    const text = $('#import-text').value;
    const file = $('#import-file').files[0];
    if (!file && !text.trim()) return toast('无内容', 'warning');
    const fd = new FormData();
    if (file) fd.append('file', file);
    if (text.trim()) fd.append('text', text);
    fd.append('upstream_id', $('#import-up').value);
    const res = await fetch(B + 'api/keys/import', {method: 'POST', headers: {'X-CSRF': CSRF}, body: fd});
    const data = await res.json();
    if (!res.ok) throw new Error((data.error && data.error.message) || '导入失败');
    $('#import-result').textContent = `新增 ${data.added} · 更新 ${data.updated} · 去重 ${data.duplicate} · 无效 ${data.invalid} · 总 ${data.total}`;
    keysState.page = 1;
    loadKeys(); loadOverview();
  });
}

/* ================= 日志 ================= */
const logsState = {filter: 'all', rows: []};
const EP_NAMES = {chat: 'chat/completions', resp: 'responses', cmpl: 'completions', emb: 'embeddings', models: 'models', test: '测试'};
const EP_BADGE = {chat: 'bg-primary-subtle text-primary', resp: 'bg-info-subtle text-info', cmpl: 'bg-primary-subtle text-primary',
                  emb: 'bg-warning-subtle text-warning', models: 'bg-secondary-subtle text-secondary', test: 'bg-secondary-subtle text-secondary'};

async function loadLogs() {
  const d = await run(() => api('logs'));
  if (!d) return;
  logsState.rows = d.rows || [];
  $('#logs-meta').textContent = `${logsState.rows.length} 条`;
  renderLogs();
}

function renderLogs() {
  const tb = $('#logs-rows'); tb.innerHTML = '';
  const rows = logsState.rows.filter(r => {
    const st = r[4];
    if (logsState.filter === 'ok') return st >= 200 && st < 400;
    if (logsState.filter === 'err') return st === 0 || st >= 400;
    return true;
  });
  if (!rows.length) {
    const tr = el('tr'); const td = el('td', 'text-muted text-center py-4', '—');
    td.colSpan = 8; tr.appendChild(td); tb.appendChild(tr);
    return;
  }
  for (const r of rows) {
    // 新格式: [t, ep, model, key, st, ms, err, ip, att, up_model, stream, ttfb, in_tok, out_tok]
    const t = r[0], ep = r[1], model = r[2], key = r[3], st = r[4], ms = r[5], err = r[6], ip = r[7], att = r[8];
    const upModel = r[9] || '', isStream = !!r[10], ttfb = r[11] || 0, inTok = r[12] || 0, outTok = r[13] || 0;
    const stCls = st >= 400 || st === 0 ? 'bg-danger-subtle text-danger' : 'bg-success-subtle text-success';
    const tr = el('tr');
    tr.append(
      // 时间 + IP
      el('td', 'small text-muted', fmtTime(t)),
      // 模型 + 映射（两行）
      (() => {
        const td = el('td');
        const div = el('div', 'fw-medium', model || '-');
        td.appendChild(div);
        if (upModel && upModel !== model) {
          td.appendChild(el('div', 'small text-muted', '↳ ' + upModel));
        }
        return td;
      })(),
      // 端点 + 流式标识
      (() => {
        const td = el('td');
        const badge = el('span', 'badge ' + (EP_BADGE[ep] || 'bg-secondary-subtle text-secondary'), EP_NAMES[ep] || ep);
        td.appendChild(badge);
        if (isStream) td.appendChild(el('span', 'badge bg-purple-subtle text-purple ms-1', 'SSE'));
        return td;
      })(),
      // 账号
      el('td', 'small', key || '-'),
      // 状态
      (() => {
        const td = el('td');
        td.appendChild(el('span', 'badge ' + stCls, statusText(st)));
        return td;
      })(),
      // Token
      (() => {
        const td = el('td', 'text-end small text-muted');
        if (inTok || outTok) {
          td.textContent = `${inTok}↑ ${outTok}↓`;
        } else {
          td.textContent = '—';
        }
        return td;
      })(),
      // 延迟 + 耗时
      (() => {
        const td = el('td', 'text-end small');
        if (isStream && ttfb > 0 && ttfb < ms) {
          td.textContent = `${ttfb}ms / ${ms}ms`;
        } else {
          td.textContent = ms + 'ms';
        }
        return td;
      })(),
      // 错误
      el('td', 'small text-danger err-cell', err || '-'),
    );
    tb.appendChild(tr);
  }
}

/* ================= 排队 ================= */
async function loadQueue() {
  const d = await run(() => api('queue'));
  if (!d) return;
  $('#queue-meta').textContent = d.rows.length ? `${d.rows.length} 个等待 · 最长 ${d.max_wait}s` : '空';
  const tb = $('#queue-rows'); tb.innerHTML = '';
  if (!d.rows.length) {
    const tr = el('tr'); const td = el('td', 'text-muted text-center py-4', '—');
    td.colSpan = 6; tr.appendChild(td); tb.appendChild(tr);
    return;
  }
  d.rows.forEach((q, i) => {
    const tr = el('tr');
    tr.append(
      el('td', 'small text-muted', String(i + 1)),
      el('td', 'small', fmtTime(q.t)),
      el('td', 'text-end small' + (q.wait > d.max_wait / 2 ? ' text-warning fw-bold' : ''), q.wait + 's'),
      el('td', 'small', q.ip),
      el('td', 'small', EP_NAMES[q.ep] || q.ep),
      el('td', 'small text-truncate', q.model || '-'),
    );
    tb.appendChild(tr);
  });
}

/* ================= 设置 ================= */
const SET_FIELDS = [
  ['set-rate', 'rate_limit_per_minute'], ['set-tpm', 'tpm_limit'],
  ['set-coolcd', 'account_cooldown_ms'], ['set-hrl', 'hourly_request_limit'],
  ['set-accon', 'acct_concurrency'], ['set-chcon', 'total_concurrency'],
  ['set-prpm', 'pool_rpm_cap'], ['set-pdcap', 'pool_daily_cap'],
  ['set-dcap', 'daily_request_cap'], ['set-dtok', 'daily_token_limit'],
  ['set-warmup', 'warmup_seconds'],
  ['set-retries', 'max_retries'],
  ['set-bb-base', 'retry_backoff_base_ms'], ['set-bb-max', 'retry_backoff_max_ms'],
  ['set-minwait', 'retry_min_wait_ms'],
  ['set-timeout', 'request_timeout'], ['set-ctimeout', 'connect_timeout'],
  ['set-ban-step', 'ban_step_seconds'], ['set-ban-max', 'ban_max_seconds'],
  ['set-hf-ban', 'hard_fail_ban_seconds'], ['set-hf-cnt', 'hard_fail_disable_count'],
  ['set-qwait', 'queue_max_wait'], ['set-qpoll', 'queue_poll_ms'],
  ['set-c429', 'cool_429_seconds'], ['set-c5xx', 'cool_5xx_seconds'],
  ['set-cto', 'cool_timeout_seconds'], ['set-ccn', 'cool_conn_seconds'],
  ['set-bth', 'breaker_threshold'], ['set-bsec', 'breaker_seconds'],
  ['set-ttfb', 'ttfb_timeout'], ['set-sidle', 'sse_idle_timeout'],
  ['set-logmax', 'log_max'], ['set-tz', 'timezone'],
  ['set-user', 'admin_username'], ['set-mwl', 'model_whitelist'], ['set-mbl', 'model_blacklist'],
  ['set-pover', 'param_overrides'],
  ['set-smax', 'session_log_max'],
];

async function loadSettings() {
  const c = await run(() => api('settings'));
  if (!c) return;
  for (const [id, key] of SET_FIELDS) $('#' + id).value = c[key] != null ? c[key] : '';
  $('#set-logen').checked = !!c.log_enabled;
  $('#set-verify').checked = !!c.verify_tls;
  $('#set-queue').checked = !!c.queue_enabled;
  $('#set-breaker').checked = !!c.breaker_enabled;
  $('#set-herr').checked = !!c.hide_upstream_errors;
  $('#set-mhide').checked = !!c.hide_mapped_names;
  $('#set-tokens').value = (c.gateway_tokens || []).map(t => {
    if (typeof t === 'string') return t;
    return t.m && t.m.length ? `${t.t} | ${t.m.join(',')}` : t.t;
  }).join('\n');
}

function bindSettings() {
  $('#settings-save').onclick = guard(async () => {
    const config = {};
    for (const [id, key] of SET_FIELDS) config[key] = $('#' + id).value;
    config.log_enabled = $('#set-logen').checked;
    config.verify_tls = $('#set-verify').checked;
    config.queue_enabled = $('#set-queue').checked;
    config.breaker_enabled = $('#set-breaker').checked;
    config.hide_upstream_errors = $('#set-herr').checked;
    config.hide_mapped_names = $('#set-mhide').checked;
    config.gateway_tokens = $('#set-tokens').value;
    await api('settings', {method: 'POST', json: {config}});
    toast('已保存'); loadSettings(); fillDocs();
  });

  $('#pwd-btn').onclick = guard(async () => {
    const old = $('#pwd-old').value, nw = $('#pwd-new').value;
    if (!old || !nw) return toast('填写完整', 'warning');
    await api('password', {method: 'POST', json: {old, new: nw}});
    $('#pwd-old').value = ''; $('#pwd-new').value = '';
    toast('密码已修改');
  });

  $('#btn-reset-stats').onclick = guard(async () => {
    if (await uiConfirm('重置全部统计与封禁？', {danger: true})) { await api('stats/reset', {method: 'POST'}); toast('已重置'); loadOverview(); }
  });

  $('#cfg-export').href = B + 'api/config/export';
  $('#cfg-import').onclick = guard(async () => {
    const file = $('#cfg-import-file').files[0];
    if (!file) return toast('请先选择配置 JSON 文件', 'warning');
    const text = await file.text();
    let parsed;
    try { parsed = JSON.parse(text); } catch (e) { return toast('文件不是合法 JSON', 'danger'); }
    const res = await fetch(B + 'api/config/import', {method: 'POST', headers: {'X-CSRF': CSRF, 'Content-Type': 'application/json'}, body: JSON.stringify(parsed)});
    const data = await res.json();
    if (!res.ok) throw new Error((data.error && data.error.message) || '导入失败');
    $('#cfg-result').textContent = `已应用 ${data.applied} 项配置`;
    toast(`配置已导入（${data.applied} 项）`);
    loadSettings(); fillDocs();
  });
  $('#btn-clear-logs').onclick = guard(async () => {
    if (await uiConfirm('清空日志？')) { await api('logs/clear', {method: 'POST'}); toast('已清空'); loadLogs(); }
  });
  $('#btn-clear-keys').onclick = guard(async () => {
    if (!(await uiConfirm('删除账号池中全部账号？', {danger: true, okText: '删除'}))) return;
    if ((await uiPrompt('输入 DELETE 确认：', '', {okText: '删除'})) !== 'DELETE') return;
    const r = await api('keys/clear-all', {method: 'POST', json: {confirm: 'yes'}});
    toast(`已删除 ${r.removed}`); loadKeys(); loadOverview();
  });
}

/* ================= 接入 ================= */
function fillDocs() {
  const base = location.origin + (B === '/' ? '' : B.replace(/\/$/, ''));
  $('#doc-base').textContent = base + '/v1';
  $('#doc-curl1').textContent =
`curl ${base}/v1/chat/completions \\
  -H "Authorization: Bearer <令牌>" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"deepseek-ai/deepseek-v4-flash-0731","messages":[{"role":"user","content":"hi"}],"stream":true}'`;
  $('#doc-curl2').textContent =
`curl ${base}/v1/responses \\
  -H "Authorization: Bearer <令牌>" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"meta/llama-3.3-70b-instruct","input":"hi","stream":true}'`;
  $('#doc-py').textContent =
`from openai import OpenAI

client = OpenAI(api_key="<令牌>", base_url="${base}/v1")

resp = client.chat.completions.create(
    model="meta/llama-3.3-70b-instruct",
    messages=[{"role": "user", "content": "hi"}],
)
print(resp.choices[0].message.content)`;
  $('#doc-anthropic').textContent =
`from anthropic import Anthropic

client = Anthropic(api_key="<令牌>", base_url="${base}/")

msg = client.messages.create(
    model="meta/llama-3.3-70b-instruct",
    max_tokens=1024,
    messages=[{"role": "user", "content": "hi"}],
)
print(msg.content[0].text)`;
}


/* ================= 会话日志 ================= */
async function loadSessions() {
  const d = await run(() => api('sessions'));
  if (!d) return;
  $('#sessions-meta').textContent = d.rows.length ? `${d.rows.length} 条 / 上限 ${d.max}` : '空';
  const box = $('#sessions-list');
  box.innerHTML = '';
  if (!d.rows.length) { box.innerHTML = '<div class="text-muted text-center py-4">—</div>'; return; }
  for (const s of d.rows) {
    const card = document.createElement('div');
    card.className = 'card mb-2';
    const st_cls = s.status >= 200 && s.status < 400 ? 'text-success' : 'text-danger';
    const reqPreview = s.req ? s.req.substring(0, 200) : '';
    const respPreview = s.resp ? s.resp.substring(0, 200) : '';
    card.innerHTML = `
      <div class="card-header py-1 d-flex justify-content-between align-items-center">
        <span class="small">${new Date(s.t * 1000).toLocaleString('zh-CN', {hour12: false})}</span>
        <span class="badge text-bg-${s.status >= 400 ? 'danger' : 'success'}">${s.status}</span>
      </div>
      <div class="card-body py-2">
        <div class="small text-muted mb-1">模型: ${esc(s.model)} · 密钥: ${esc(s.key)}</div>
        <details><summary class="small text-primary" style="cursor:pointer">请求</summary>
          <pre style="font-size:.75em;margin:4px 0">${esc(reqPreview)}</pre></details>
        <details><summary class="small text-primary" style="cursor:pointer">响应</summary>
          <pre style="font-size:.75em;margin:4px 0">${esc(respPreview)}</pre></details>
      </div>`;
    box.appendChild(card);
  }
}
$('#sessions-refresh').onclick = loadSessions;
$('#sessions-clear').onclick = guard(async () => {
  if (await uiConfirm('清空全部会话日志？')) { await api('sessions/clear', {method: 'POST'}); loadSessions(); }
});

/* ================= 初始化 ================= */
document.addEventListener('DOMContentLoaded', () => {
  $$('.sidebar nav a').forEach(a => a.addEventListener('click', e => { e.preventDefault(); activate(a.dataset.pane); }));
  $('#btn-logout').onclick = guard(async () => { await api('logout', {method: 'POST'}); location.href = B + 'admin'; });
  bindImport(); bindSettings(); bindUpstreams(); bindBatch();
  refreshBatchUps();
  fillModelSelects();  // 初始化模型选择器（从后台拉取所有渠道模型）

  let qTimer = null;
  $('#keys-q').addEventListener('input', () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => { keysState.page = 1; loadKeys(); }, 300);
  });
  $('#keys-filter').onchange = () => { keysState.page = 1; loadKeys(); };
  $('#keys-refresh').onclick = loadKeys;
  $('#keys-export').href = B + 'api/keys/export';

  $('#logs-filter').onchange = renderLogs;
  $('#logs-refresh').onclick = loadLogs;
  $('#logs-clear').onclick = guard(async () => {
    if (await uiConfirm('清空日志？')) { await api('logs/clear', {method: 'POST'}); loadLogs(); }
  });
  $('#queue-refresh').onclick = loadQueue;
  $('#queue-clear').onclick = guard(async () => { await api('queue', {method: 'POST'}); loadQueue(); });

  activate('dash');
  setInterval(() => {
    if (document.hidden) return;
    if ($('#pane-dash').classList.contains('active')) loadOverview();
    if ($('#pane-logs').classList.contains('active') && $('#logs-auto').checked) loadLogs();
    if ($('#pane-queue').classList.contains('active') && $('#queue-auto').checked) loadQueue();
    if ($('#pane-keys').classList.contains('active') && keysState2.openId) {
      const panel = $('#keys-rows tr.detail-row .kpanel');
      if (panel) refreshKeyPanel(keysState2.openId, panel);
    }
  }, 10000);
});
})();
