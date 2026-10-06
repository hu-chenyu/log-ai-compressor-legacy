/* 日志AI压缩器 · 前端逻辑
   零依赖、零构建、零 CDN —— 直接改完刷新即可生效（完全离线可用）。
   后端契约见 log_ai_compressor/web/server.py。 */
'use strict';

/* ============================ 工具 ============================ */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** 转义后再插入 innerHTML，防止日志内容里的尖括号破坏页面结构 */
function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

let toastTimer = null;
function toast(msg, kind = '') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === 'err' ? 6000 : 3000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_) { /* 非 JSON 响应，沿用状态码 */ }
    throw new Error(detail);
  }
  const type = res.headers.get('content-type') || '';
  return type.includes('json') ? res.json() : res.text();
}

const fmtInt = (n) => (n ?? 0).toLocaleString('en-US');
const LEVEL_COLOR = {
  ERROR: 'var(--error)', FAIL: 'var(--fail)', WARN: 'var(--warn)',
  INFO: 'var(--info)', DEBUG: 'var(--debug)',
};
const ANOMALY_LABEL = {
  burst: '集中爆发', periodic: '周期发作', novel: '新型错误', rare: '罕见异常',
};
const ANOMALY_CLASS = {
  burst: 'tag-burst', periodic: 'tag-periodic',
  novel: 'tag-novel', rare: 'tag-rare',
};

/* ============================ 全局状态 ============================ */
const State = {
  mode: 'file',
  jobId: null,
  result: null,        // 序列化后的分析结果
  compare: null,       // 对比结果
  clusters: [],
  filtered: [],
  selected: -1,
  running: false,
  es: null,            // SSE 连接
  health: null,
  comparePaths: [],
  exportFormats: new Set(['md']),
};

/* ============================ 主题 ============================ */
function applyTheme(theme) {
  document.body.dataset.theme = theme;
  const btn = $('#btn-theme');
  btn.textContent = theme === 'dark' ? '🌙 暗色' : '☀ 亮色';
  try { localStorage.setItem('lac.theme', theme); } catch (_) {}
}
$('#btn-theme').addEventListener('click', () => {
  applyTheme(document.body.dataset.theme === 'dark' ? 'light' : 'dark');
});

/* ============================ Tab ============================ */
$$('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    $$('.tab').forEach((t) => t.classList.remove('active'));
    $$('.tab-pane').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    State.mode = tab.dataset.tab;
    $(`.tab-pane[data-pane="${State.mode}"]`).classList.add('active');
  });
});

/* ============================ 级别复选 ============================ */
const LEVELS = ['ERROR', 'FAIL', 'WARN', 'INFO', 'DEBUG'];
const LEVEL_HELP = {
  ERROR: '错误：程序运行中的异常，可能导致功能异常但仍可继续',
  FAIL: '失败：操作或测试未成功完成',
  WARN: '警告：可能有问题但不影响正常运行，需要关注',
  INFO: '信息：正常运行的一般性记录',
  DEBUG: '调试：开发调试用，生产通常不显示',
};
function buildLevelChips() {
  const box = $('#level-checks');
  box.innerHTML = '';
  LEVELS.forEach((lv) => {
    const chip = document.createElement('span');
    chip.className = 'chip on';
    chip.dataset.v = lv;
    chip.textContent = lv;
    chip.title = LEVEL_HELP[lv];
    chip.addEventListener('click', () => chip.classList.toggle('on'));
    box.appendChild(chip);
  });
}
const selectedLevels = () =>
  $$('#level-checks .chip.on').map((c) => c.dataset.v);

/* ============================ 配置持久化 ============================ */
const CFG_KEYS = ['cfg-context', 'cfg-similarity', 'cfg-analysis', 'cfg-rule',
  'cfg-maxlines', 'cfg-encoding', 'cfg-include', 'cfg-exclude', 'cfg-regex',
  'cfg-redact', 'search'];
function saveCfg() {
  const out = {};
  CFG_KEYS.forEach((id) => {
    const el = $('#' + id);
    if (!el) return;
    out[id] = el.type === 'checkbox' ? el.checked : el.value;
  });
  try { localStorage.setItem('lac.cfg', JSON.stringify(out)); } catch (_) {}
}
function loadCfg() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('lac.cfg') || '{}'); } catch (_) {}
  CFG_KEYS.forEach((id) => {
    const el = $('#' + id);
    if (!el || !(id in saved)) return;
    if (el.type === 'checkbox') el.checked = !!saved[id];
    else el.value = saved[id];
  });
}
$$('#cfg-context, #cfg-similarity, #cfg-analysis, #cfg-rule, #cfg-maxlines, #cfg-encoding, #cfg-include, #cfg-exclude, #cfg-regex, #cfg-redact')
  .forEach((el) => el.addEventListener('change', saveCfg));

/* ============================ 拖放 ============================ */
const dropZone = $('#drop-hint');
['dragenter', 'dragover'].forEach((ev) =>
  document.body.addEventListener(ev, (e) => {
    e.preventDefault();
    dropZone.classList.add('over');
  }));
['dragleave', 'drop'].forEach((ev) =>
  document.body.addEventListener(ev, (e) => {
    e.preventDefault();
    if (ev === 'dragleave' && e.relatedTarget) return;
    dropZone.classList.remove('over');
  }));
document.body.addEventListener('drop', (e) => {
  e.preventDefault();
  const files = Array.from(e.dataTransfer?.files || []);
  if (!files.length) return;
  // 浏览器拿到的 File 拿不到真实路径（安全限制），只有 name；
  // 这里如实告知，而不是假装拿到了可用路径。
  const names = files.map((f) => f.name);
  toast(`已接收 ${names.length} 个文件：${names.slice(0, 3).join('、')}`
        + `${names.length > 3 ? '…' : ''}。`
        + '浏览器出于安全限制拿不到绝对路径，请点「浏览…」选择，或把路径粘进输入框。',
        'err');
});

/* ============================ 文件选择器 ============================ */
const fsDialog = $('#fs-dialog');
let fsSel = [];       // 已选路径（多选）
let fsCur = '';

async function openFs(startPath) {
  fsSel = [];
  fsCur = startPath || State.health?.home || '';
  $('#fs-filter').value = '';
  fsDialog.showModal();
  await loadFs(fsCur);
}

async function loadFs(path) {
  const list = $('#fs-list');
  list.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const data = await api('/api/fs/list?path=' + encodeURIComponent(path));
    fsCur = data.path;
    $('#fs-up').disabled = !data.can_go_up;
    list.innerHTML = '';
    const filter = $('#fs-filter').value.trim().toLowerCase();
    const entries = data.entries.filter(
      (e) => !filter || e.name.toLowerCase().includes(filter));
    if (!entries.length) {
      list.innerHTML = '<div class="empty">没有匹配项</div>';
      return;
    }
    entries.forEach((e) => {
      const row = document.createElement('div');
      row.className = 'fs-entry' + (fsSel.includes(e.path) ? ' sel' : '');
      row.innerHTML =
        `<span class="ic">${e.is_dir ? '📁' : '📄'}</span>` +
        `<span>${esc(e.name)}</span><span class="sz">${esc(e.size_text)}</span>`;
      row.addEventListener('click', () => {
        if (e.is_dir) { loadFs(e.path); return; }
        const i = fsSel.indexOf(e.path);
        if (i >= 0) fsSel.splice(i, 1); else fsSel.push(e.path);
        $$('.fs-entry', list).forEach((r) => r.classList.remove('sel'));
        loadFs(fsCur);
      });
      list.appendChild(row);
    });
    $('#fs-status').textContent = `已选 ${fsSel.length} 个`;
  } catch (err) {
    list.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

$('#btn-browse').addEventListener('click', () => openFs());
$('#fs-up').addEventListener('click', () => loadFs(fsCur + '/..'));
$('#fs-filter').addEventListener('input', () => loadFs(fsCur));
$('#fs-cancel').addEventListener('click', () => fsDialog.close());
$('#fs-ok').addEventListener('click', () => {
  if (!fsSel.length) { toast('没有选择文件', 'err'); return; }
  if (State.mode === 'compare') {
    fsSel.forEach(addCompareRow);
  } else {
    $('#file-path').value = fsSel.join(';');
    hintFileSize();
  }
  fsDialog.close();
});
$('#btn-clear-file').addEventListener('click', () => {
  $('#file-path').value = '';
  $('#file-hint').textContent = '';
});

/** 输入路径后异步查文件大小，大文件提前警告（避免用户干等） */
let hintTimer = null;
function hintFileSize() {
  clearTimeout(hintTimer);
  const path = $('#file-path').value.split(';')[0].trim();
  const el = $('#file-hint');
  if (!path) { el.textContent = ''; return; }
  hintTimer = setTimeout(async () => {
    try {
      const info = await api('/api/fs/file?path=' + encodeURIComponent(path));
      el.innerHTML = `${esc(info.path)} · ${esc(info.size_text)}` +
        (info.big ? ' · <b style="color:var(--warn)">大文件，首次分析可能需要几秒</b>' : '');
    } catch (err) {
      el.innerHTML = `<span style="color:var(--error)">${esc(err.message)}</span>`;
    }
  }, 350);
}
$('#file-path').addEventListener('input', hintFileSize);

/* ============================ 对比文件行 ============================ */
function addCompareRow(path) {
  const list = $('#compare-list');
  const row = document.createElement('div');
  row.className = 'compare-item';
  const tag = list.children.length === 0 ? '基准' : '对比';
  row.innerHTML = `<span class="tag">${tag}</span>` +
    `<input class="input grow" value="${esc(path || '')}" placeholder="日志文件绝对路径">` +
    `<button class="btn btn-ghost btn-sm" title="移除">✕</button>`;
  row.querySelector('input').addEventListener('input', saveComparePaths);
  row.querySelector('button').addEventListener('click', () => {
    row.remove(); relabelCompare(); saveComparePaths();
  });
  list.appendChild(row);
  relabelCompare();
  saveComparePaths();
}
function relabelCompare() {
  $$('#compare-list .compare-item').forEach((row, i) => {
    row.querySelector('.tag').textContent = i === 0 ? '基准' : '对比';
  });
}
function saveComparePaths() {
  State.comparePaths = $$('#compare-list input').map((i) => i.value.trim());
}
$('#btn-add-compare').addEventListener('click', () => addCompareRow(''));
$('#btn-add-compare').addEventListener('dblclick', () => openFs());

/* ============================ 分析 ============================ */
function collectParams() {
  return {
    levels: selectedLevels(),
    context_lines: parseInt($('#cfg-context').value || '50', 10),
    similarity: $('#cfg-similarity').value,
    analysis_mode: $('#cfg-analysis').value,
    rule: $('#cfg-rule').value,
    maxlines: $('#cfg-maxlines').value,
    encoding: $('#cfg-encoding').value,
    include: $('#cfg-include').value,
    exclude: $('#cfg-exclude').value,
    use_regex: $('#cfg-regex').checked,
  };
}

function collectRequest() {
  if (State.mode === 'text') {
    return { mode: 'text', text: $('#paste-text').value, params: collectParams() };
  }
  if (State.mode === 'compare') {
    saveComparePaths();
    return { mode: 'compare', paths: State.comparePaths, params: collectParams() };
  }
  return {
    mode: 'file',
    paths: $('#file-path').value.split(';').map((s) => s.trim()).filter(Boolean),
    params: collectParams(),
  };
}

function setRunning(on) {
  State.running = on;
  $('#btn-analyze').disabled = on;
  $('#btn-cancel').hidden = !on;
  $('#progress-wrap').hidden = !on;
  const bar = $('#progress-bar');
  if (on) {
    bar.classList.add('indeterminate');
    bar.style.width = '35%';
    $('#progress-text').textContent = '解析中…';
  } else {
    bar.classList.remove('indeterminate');
    bar.style.width = '0';
  }
}

$('#btn-analyze').addEventListener('click', startAnalysis);
$('#btn-cancel').addEventListener('click', async () => {
  if (!State.jobId) return;
  try { await api(`/api/jobs/${State.jobId}/cancel`, { method: 'POST' }); }
  catch (_) {}
  toast('已请求取消');
  if (State.es) State.es.close();
  setRunning(false);
});

async function startAnalysis() {
  if (State.running) return;
  const req = collectRequest();
  setRunning(true);
  $('#result-panel').hidden = true;
  $('#compare-panel').hidden = true;
  try {
    const res = await api('/api/analyze', {
      method: 'POST', body: JSON.stringify(req),
    });
    State.jobId = res.job_id;
    listenJob(res.job_id, req.mode);
  } catch (err) {
    setRunning(false);
    toast(err.message, 'err');
  }
}

function listenJob(jobId, mode) {
  if (State.es) State.es.close();
  const es = new EventSource(`/api/jobs/${jobId}/stream`);
  State.es = es;

  es.addEventListener('progress', (ev) => {
    const d = JSON.parse(ev.data);
    const pct = Math.round((d.progress ?? 0) * 100);
    const bar = $('#progress-bar');
    bar.classList.remove('indeterminate');
    bar.style.width = Math.min(100, pct) + '%';
    $('#progress-text').textContent =
      `已处理 ${fmtInt(d.lines)} 行 · ${fmtInt(d.errors)} 错误 · ${pct}%`;
  });

  es.addEventListener('done', (ev) => {
    const d = JSON.parse(ev.data);
    es.close();
    setRunning(false);
    if (mode === 'compare') renderCompare(d.result);
    else renderResult(d.result);
  });

  es.addEventListener('error', (ev) => {
    let msg = '分析失败';
    try { msg = JSON.parse(ev.data).message || msg; } catch (_) {}
    es.close();
    setRunning(false);
    toast(msg, 'err');
  });

  es.onerror = () => {
    // EventSource 会在正常收流后也触发 onerror，用 running 标志区分
    if (State.running) { es.close(); setRunning(false); toast('连接中断', 'err'); }
  };
}

/* ============================ 渲染：概览 ============================ */
const VERDICT_LABEL = {
  CONFIRMED: '已定位根因（Caused-by 因果链直连）',
  LIKELY: '可能原因 —— 统计推断，非因果证明',
  INSUFFICIENT: '无法判定根因',
};

/** 渲染证据充分性卡片：判定 + 缺口清单 + 实际用到的证据 */
function renderVerdict(data) {
  const card = $('#verdict-card');
  const ev = data.evidence;
  if (!ev || !ev.verdict) { card.hidden = true; return; }
  const v = ev.verdict;
  const gaps = (ev.gaps || []).map((g) => `
    <li class="gap-item">
      <div class="gap-title"><span class="w">权重 ${g.weight}</span>${esc(g.title)}</div>
      <div class="gap-row"><b>为什么重要：</b>${esc(g.why)}</div>
      <div class="gap-row"><b>现在缺：</b>${esc(g.missing)}</div>
    </li>`).join('');
  const used = (ev.inputs_used || []).length
    ? `<div class="used-inputs">本次实际用到的证据：${esc(ev.inputs_used.join('、'))}</div>`
    : '';
  card.className = `verdict-card v-${v}`;
  card.innerHTML = `
    <div class="verdict-head">
      <span class="verdict-badge">${esc(VERDICT_LABEL[v] || v)}</span>
      ${ev.can_conclude
        ? '<span class="pill pill-ok">可直接作为根因输出</span>'
        : '<span class="pill pill-warn">不可作为根因输出</span>'}
    </div>
    <div class="verdict-text">${esc(ev.headline || '')}</div>
    ${v === 'CONFIRMED' ? '' :
      `<div class="verdict-note">补齐下列证据才能定论（按重要性排序）：</div>
       <ul class="gap-list">${gaps}</ul>`}
    ${used}`;
  card.hidden = false;
}

function renderResult(data) {
  State.result = data;
  State.clusters = data.clusters || [];
  State.selected = -1;
  $('#compare-panel').hidden = true;
  $('#result-panel').hidden = false;
  renderVerdict(data);

  const s = data.stats;
  const roots = (data.root_causes || []).length;
  const stats = [
    ['总行数', fmtInt(s.total_lines), ''],
    ['错误行', fmtInt(s.error_lines), 'hot'],
    ['错误种类', fmtInt(data.total_clusters), ''],
    ['根因候选', String(roots), 'root'],
    ['耗时', s.duration + 's', ''],
    ['速度', fmtInt(s.lines_per_second) + ' 行/秒', ''],
  ];
  if (s.truncated || s.limit_hit) {
    stats.push(['状态', '已达行数上限', 'hot']);
  }
  $('#overview').innerHTML = stats.map(([k, v, cls]) =>
    `<div class="stat ${cls}"><div class="k">${esc(k)}</div>` +
    `<div class="v">${esc(v)}</div></div>`).join('')
    + `<div class="stat" style="min-width:auto"><div class="k">编码 / 规则</div>` +
      `<div class="v" style="font-size:13px">${esc(s.encoding)} / ${esc(s.rule_name)}</div></div>`
    + (s.time_range_text
      ? `<div class="stat" style="min-width:auto"><div class="k">时间范围</div>` +
        `<div class="v" style="font-size:13px">${esc(s.time_range_text)}</div></div>` : '');

  renderCharts(data);
  applyFilter();
  toast(`分析完成：${fmtInt(s.error_lines)} 行错误，去重后 ${data.total_clusters} 种`, 'ok');
}

function renderCharts(data) {
  $('#charts-row').hidden = false;
  // 时间分布（后端已把直方图转成 [{t,n}]，burst 段标红）
  const hist = data.global_hist || {};
  const series = hist.series || [];
  const burstSet = new Set((hist.burst || []).map((b) => b.t));
  const peak = Math.max(1, ...series.map((b) => b.n));
  const box = $('#chart-hist');
  if (!series.length) {
    box.innerHTML = '<div class="empty">日志无时间戳，无法绘制时间分布</div>';
    $('#chart-hist-meta').textContent = '';
  } else {
    const shown = series.slice(-120);
    box.innerHTML = shown.map((b) => {
      const isPeak = burstSet.has(b.t) || b.n >= peak * 0.8;
      return `<div class="bar${isPeak ? ' peak' : ''}" ` +
        `style="height:${Math.max(3, b.n / peak * 100)}%" ` +
        `title="${esc(b.t ? new Date(b.t * 1000).toLocaleString() : '')} · ${fmtInt(b.n)}"></div>`;
    }).join('');
    $('#chart-hist-meta').textContent =
      `峰值 ${fmtInt(peak)} / 共 ${fmtInt(hist.total || 0)} 次` +
      (hist.burst?.length ? ` · 红色为爆发段 ${hist.burst.length} 段` : '');
  }

  // 级别构成
  const lv = Object.entries(data.stats.level_counts || {})
    .sort((a, b) => b[1] - a[1]);
  const lvMax = Math.max(1, ...lv.map((x) => x[1]));
  $('#chart-level').innerHTML = lv.length ? lv.map(([k, v]) =>
    `<div class="bar-row"><span class="name">${esc(k)}</span>` +
    `<span class="track"><span class="fill" style="width:${v / lvMax * 100}%;` +
    `background:${LEVEL_COLOR[k] || 'var(--accent)'}"></span></span>` +
    `<span class="num">${fmtInt(v)}</span></div>`).join('')
    : '<div class="empty">无</div>';

  // 模块分布
  const mods = {};
  State.clusters.forEach((c) => {
    const m = c.module || '(无模块)';
    mods[m] = (mods[m] || 0) + c.count;
  });
  const top = Object.entries(mods).sort((a, b) => b[1] - a[1]).slice(0, 10);
  const modMax = Math.max(1, ...top.map((x) => x[1]));
  $('#chart-module').innerHTML = top.length ? top.map(([k, v]) =>
    `<div class="bar-row"><span class="name" title="${esc(k)}">${esc(k)}</span>` +
    `<span class="track"><span class="fill" style="width:${v / modMax * 100}%;` +
    `background:var(--accent)"></span></span>` +
    `<span class="num">${fmtInt(v)}</span></div>`).join('')
    : '<div class="empty">无</div>';
}

/* ============================ 搜索过滤 ============================ */
const BOOL_RE = /\b(?:and|or|not)\b/i;

/** 与旧 GUI 的 _kw_match 同语义：含布尔算子时按表达式求值，否则子串匹配 */
function kwMatch(hay, expr) {
  if (!expr) return true;
  if (!BOOL_RE.test(expr)) return expr.includes(hay);
  const clauses = expr.split(/\bor\b/i);
  for (const clause of clauses) {
    let ok = true;
    for (let term of clause.split(/\band\b/i)) {
      term = term.trim();
      if (!term) continue;
      let neg = false;
      const m = /^not\b\s*/i.exec(term);
      if (m) { neg = true; term = term.slice(m[0].length).trim(); }
      let hit = !!term && hay.includes(term);
      if (neg) hit = !hit;
      if (!hit) { ok = false; break; }
    }
    if (ok) return true;
  }
  return false;
}

let filterTimer = null;
$('#search').addEventListener('input', () => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(applyFilter, 180);
});

function applyFilter() {
  const expr = $('#search').value.trim().toLowerCase();
  State.filtered = State.clusters.filter((c) => {
    if (!expr) return true;
    const hay = [c.summary, c.module, c.level, c.template]
      .filter(Boolean).join(' ').toLowerCase();
    return kwMatch(hay, expr);
  });
  $('#filter-count').textContent = expr
    ? `${State.filtered.length} / ${State.clusters.length} 簇`
    : `${State.clusters.length} 簇`;
  renderClusterList();
}

function renderClusterList() {
  const box = $('#cluster-list');
  if (!State.filtered.length) {
    box.innerHTML = '<div class="empty">没有匹配的错误簇</div>';
    return;
  }
  box.innerHTML = State.filtered.map((c, i) => {
    const tags = [];
    if (c.is_root_cause) tags.push('<span class="tag tag-root">▲ 根因</span>');
    if (c.anomaly) {
      tags.push(`<span class="tag ${ANOMALY_CLASS[c.anomaly] || ''}">` +
        `${ANOMALY_LABEL[c.anomaly] || c.anomaly}</span>`);
    }
    if (c.related_clusters?.length) {
      tags.push(`<span class="tag">关联 ${c.related_clusters.length}</span>`);
    }
    return `<div class="cluster-item${i === State.selected ? ' sel' : ''}"
                 data-i="${i}" style="--level-color:${LEVEL_COLOR[c.level] || 'var(--border)'}">
      <div class="cluster-head">
        <span class="lv lv-${esc(c.level)}">${esc(c.level)}</span>
        <span class="cnt">×${fmtInt(c.count)}</span>
        <span class="prio">P${esc(c.priority)}</span>
        ${c.root_cause_confidence === 'CONFIRMED'
          ? '<span class="prio" style="color:var(--ok)">已证实</span>'
          : c.root_cause_confidence === 'LIKELY'
          ? '<span class="prio" style="color:var(--warn)">未证实</span>' : ''}
        <span class="prio">行 ${fmtInt(c.first_line)}~${fmtInt(c.last_line)}</span>
      </div>
      <div class="cluster-summary">${esc(c.summary)}</div>
      ${tags.length ? `<div class="tags">${tags.join('')}</div>` : ''}
    </div>`;
  }).join('');
  $$('.cluster-item', box).forEach((el) => {
    el.addEventListener('click', () => selectCluster(+el.dataset.i));
  });
}

function selectCluster(i) {
  State.selected = i;
  renderClusterList();
  renderDetail(State.filtered[i]);
}

/* ============================ 渲染：详情 ============================ */
function renderDetail(c) {
  if (!c) {
    $('#detail').innerHTML = '<p class="empty">← 从左侧选择一个错误簇查看详情</p>';
    return;
  }
  const s = c.sample || {};
  const entry = s.entry || {};
  const parts = [];

  const kv = [
    ['簇号 / 级别', `[${c.id}] ${c.level}`],
    ['模块', c.module || '—'],
    ['出现次数', fmtInt(c.count)],
    ['优先级', `${c.priority}（${c.priority_detail || '—'}）`],
    ['行范围', `${fmtInt(c.first_line)} ~ ${fmtInt(c.last_line)}`],
    ['时间范围', `${c.first_seen_text || '—'} ~ ${c.last_seen_text || '—'}`],
  ];
  if (c.is_root_cause) kv.push(['根因判定', c.root_cause_reason || '是']);
  if (c.anomaly) kv.push(['异常标记', ANOMALY_LABEL[c.anomaly] || c.anomaly]);
  if (c.related_clusters?.length) kv.push(['相关簇', c.related_clusters.join(', ')]);
  parts.push(`<div class="kv">${kv.map(([k, v]) =>
    `<div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>`).join('')}</div>`);

  if (entry.raw) {
    parts.push('<h5>典型样例</h5>');
    if (s.before?.length) {
      parts.push(`<div class="hint">前文 ${s.before.length} 行</div>`);
      parts.push(`<div class="logbox">${s.before.slice(-12)
        .map((l) => `<span class="dim">${esc(l)}</span>`).join('\n')}</div>`);
    }
    parts.push(`<div class="logbox">${esc(entry.raw)}` +
      (entry.message_extra || []).map((l) => '\n' + esc(l)).join('') + '</div>');
    if (s.after?.length) {
      parts.push(`<div class="hint">后文 ${s.after.length} 行</div>`);
      parts.push(`<div class="logbox">${s.after.slice(0, 12)
        .map((l) => `<span class="dim">${esc(l)}</span>`).join('\n')}</div>`);
    }
  }

  const ss = s.stack_simplified;
  if (ss?.lines?.length) {
    parts.push(`<h5>降噪堆栈（业务帧 ${ss.business_count} 行，已折叠噪声 ${ss.noise_count} 行）</h5>`);
    parts.push(`<div class="logbox">${ss.lines.map((l) =>
      l.includes('已折叠')
        ? `<span class="fold">${esc(l)}</span>`
        : `<span class="bstack">${esc(l)}</span>`).join('\n')}</div>`);
  }

  $('#detail').innerHTML = parts.join('');
}

/* ============================ 分栏拖动 ============================ */
(function initSplitter() {
  const grip = $('#split-grip');
  const left = $('.split-left');
  let dragging = false;
  const onMove = (e) => {
    if (!dragging) return;
    const box = $('#split').getBoundingClientRect();
    const x = (e.touches ? e.touches[0].clientX : e.clientX) - box.left;
    const pct = Math.min(75, Math.max(20, (x / box.width) * 100));
    left.style.width = pct + '%';
  };
  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.userSelect = '';
    try {
      localStorage.setItem('lac.split', $('.split-left').style.width);
    } catch (_) {}
  };
  const start = (e) => { e.preventDefault(); dragging = true;
    document.body.style.userSelect = 'none'; };
  grip.addEventListener('mousedown', start);
  grip.addEventListener('touchstart', start, { passive: false });
  document.addEventListener('mousemove', onMove);
  document.addEventListener('touchmove', onMove, { passive: false });
  document.addEventListener('mouseup', onUp);
  document.addEventListener('touchend', onUp);
  const saved = localStorage.getItem('lac.split');
  if (saved) left.style.width = saved;
})();

/* ============================ 导出 / 复制 ============================ */
const FORMATS = [
  ['md', 'Markdown（投喂大模型）'], ['txt', '纯文本'],
  ['json', 'JSON（标准）'], ['json_full', 'JSON（含全部实例）'],
  ['html', 'HTML 报告'], ['summary', '精简摘要'],
];
(function buildExportChips() {
  const box = $('#export-formats');
  FORMATS.forEach(([key, label], i) => {
    const chip = document.createElement('span');
    chip.className = 'chip' + (i === 0 ? ' on' : '');
    chip.textContent = label;
    chip.dataset.fmt = key;
    chip.addEventListener('click', () => {
      if (State.exportFormats.has(key)) State.exportFormats.delete(key);
      else State.exportFormats.add(key);
      chip.classList.toggle('on', State.exportFormats.has(key));
    });
    box.appendChild(chip);
  });
})();

$('#btn-export').addEventListener('click', () => {
  if (!State.jobId) { toast('请先分析', 'err'); return; }
  $('#export-dialog').showModal();
});
$('#export-cancel').addEventListener('click', () => $('#export-dialog').close());
$('#export-go').addEventListener('click', () => {
  const fmts = Array.from(State.exportFormats);
  if (!fmts.length) { toast('至少选一种格式', 'err'); return; }
  $('#export-dialog').close();
  const redact = $('#cfg-redact').checked;
  fmts.forEach((fmt, i) => setTimeout(() => download(fmt, redact), i * 350));
});

async function download(fmt, redact) {
  try {
    const res = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_id: State.jobId, format: fmt, redact,
        custom_rules: [], top_n: null,
      }),
    });
    if (!res.ok) {
      const b = await res.json().catch(() => ({}));
      throw new Error(b.detail || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `log-report.${fmt === 'json_full' ? 'json' : fmt}`;
    a.click();
    URL.revokeObjectURL(url);
    toast(`已导出 ${a.download}`, 'ok');
  } catch (err) {
    toast('导出失败：' + err.message, 'err');
  }
}

$('#btn-copy').addEventListener('click', async () => {
  if (!State.jobId) { toast('请先分析', 'err'); return; }
  try {
    const r = await api('/api/export/copy', {
      method: 'POST',
      body: JSON.stringify({
        job_id: State.jobId, format: 'summary',
        redact: $('#cfg-redact').checked, custom_rules: [],
      }),
    });
    await navigator.clipboard.writeText(r.text);
    toast(`已复制 ${fmtInt(r.length)} 字符的摘要，可直接粘给 AI`, 'ok');
  } catch (err) {
    toast('复制失败：' + err.message, 'err');
  }
});

/* ============================ AI ============================ */
async function loadAiStatus() {
  const pill = $('#ai-status');
  try {
    const st = await api('/api/ai/status');
    State.health = State.health || {};
    State.health.ai = st;
    if (st.available) {
      const c = st.config || {};
      pill.className = 'pill pill-ok';
      pill.textContent = `AI 就绪 · ${c.model || '?'}`;
      pill.title = `${c.provider} · ${c.base_url}\nAI 解读会把压缩后的证据摘要发往该地址`;
    } else {
      pill.className = 'pill pill-muted';
      pill.textContent = 'AI 未启用';
      pill.title = st.reason || '';
    }
  } catch (_) {
    pill.className = 'pill pill-muted';
    pill.textContent = 'AI 状态未知';
  }
}

$('#btn-ai').addEventListener('click', () => {
  if (!State.jobId) { toast('请先分析', 'err'); return; }
  $('#ai-dialog-out').showModal();
  if (State.selected >= 0) $('#ai-scope').value = 'cluster';
});
$('#ai-out-close').addEventListener('click', () => $('#ai-dialog-out').close());
$('#ai-copy').addEventListener('click', () => {
  const txt = $('#ai-output').innerText;
  if (!txt.trim()) return;
  navigator.clipboard.writeText(txt).then(
    () => toast('已复制', 'ok'), (e) => toast('复制失败：' + e, 'err'));
});

$('#ai-run').addEventListener('click', async () => {
  const scope = $('#ai-scope').value;
  if (scope === 'cluster' && State.selected < 0) {
    toast('请先在左侧选中一个错误簇', 'err');
    return;
  }
  const out = $('#ai-output');
  out.innerHTML = '<p class="empty">AI 思考中…（压缩后上下文很小，通常几秒）</p>';
  $('#ai-run').disabled = true;
  try {
    const res = await api('/api/ai/explain', {
      method: 'POST',
      body: JSON.stringify({
        job_id: State.jobId,
        cluster_id: scope === 'cluster' ? State.filtered[State.selected].id : null,
        question: $('#ai-question').value,
        top_n: 5,
      }),
    });
    out.innerHTML = renderMarkdown(res.text);
  } catch (err) {
    out.innerHTML = `<p style="color:var(--error)">${esc(err.message)}</p>`;
  } finally {
    $('#ai-run').disabled = false;
  }
});

/** 极简 Markdown 渲染：只处理 AI 回答里常见的结构，输出仍然全部转义 */
function renderMarkdown(md) {
  const lines = String(md || '').split('\n');
  const out = [];
  let inCode = false, inList = false, listTag = 'ul';
  const closeList = () => { if (inList) { out.push(`</${listTag}>`); inList = false; } };
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  lines.forEach((raw) => {
    const line = raw.trimEnd();
    if (/^```/.test(line)) {
      closeList();
      out.push(inCode ? '</pre>' : '<pre>');
      inCode = !inCode;
      return;
    }
    if (inCode) { out.push(esc(raw)); return; }
    if (!line.trim()) { closeList(); return; }

    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) { closeList(); out.push(`<h3>${inline(h[2])}</h3>`); return; }
    if (/^&gt;|^>/.test(line)) {
      closeList(); out.push(`<blockquote>${inline(line.replace(/^>\s?/, ''))}</blockquote>`);
      return;
    }
    const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    const ul = /^\s*[-*+]\s+(.*)$/.exec(line);
    if (ol || ul) {
      const want = ol ? 'ol' : 'ul';
      if (inList && listTag !== want) closeList();
      if (!inList) { out.push(`<${want}>`); inList = true; listTag = want; }
      out.push(`<li>${inline((ol || ul)[1])}</li>`);
      return;
    }
    closeList();
    out.push(`<p>${inline(line)}</p>`);
  });
  if (inCode) out.push('</pre>');
  closeList();
  return out.join('');
}

/* --- AI 设置弹窗 --- */
$('#btn-ai-settings').addEventListener('click', async () => {
  const sel = $('#ai-provider');
  if (!sel.options.length) {
    let st;
    try { st = await api('/api/ai/status'); } catch (_) { st = { providers: [] }; }
    (st.providers || []).forEach((p) => {
      const o = document.createElement('option');
      o.value = p.key; o.textContent = p.label;
      o.dataset.note = p.note || ''; o.dataset.env = p.env_key || '';
      sel.appendChild(o);
    });
  }
  const st = await api('/api/ai/status').catch(() => ({}));
  const cfg = st.config || {};
  sel.value = cfg.provider || 'none';
  $('#ai-base').value = cfg.base_url && cfg.provider !== 'none' ? cfg.base_url : '';
  $('#ai-model').value = cfg.model || '';
  $('#ai-key').value = '';
  $('#ai-key-state').textContent = cfg.has_key
    ? `已保存 Key：${cfg.masked_key}` : '未保存 Key';
  onProviderChange();
  $('#ai-dialog').showModal();
});

function onProviderChange() {
  const opt = $('#ai-provider').selectedOptions[0];
  $('#ai-note').textContent = opt?.dataset.note || '';
  const env = opt?.dataset.env;
  $('#ai-env-hint').textContent = env
    ? `留空则读取环境变量 ${env}`
    : '该服务商不需要 API Key';
  $('#ai-base').placeholder = (State.health?.providers || [])
    .find((p) => p.key === sel.value)?.label || '';
}
$('#ai-provider').addEventListener('change', onProviderChange);
$('#ai-save').addEventListener('click', async () => {
  const provider = $('#ai-provider').value;
  const base = $('#ai-base').value.trim();
  const model = $('#ai-model').value.trim();
  const key = $('#ai-key').value.trim();
  if (!key) {
    // 不填 Key 时也允许保存（例如改用环境变量或只换模型名）
    toast('未填 Key，将沿用已保存的 Key 或环境变量', '');
  }
  try {
    await api('/api/ai/config', {
      method: 'POST',
      body: JSON.stringify({ provider, base_url: base, model, api_key: key }),
    });
    $('#ai-dialog').close();
    await loadAiStatus();
    toast('AI 设置已保存', 'ok');
  } catch (err) {
    toast('保存失败：' + err.message, 'err');
  }
});

/* ============================ 对比渲染 ============================ */
function renderCompare(data) {
  State.compare = data;
  State.result = null;
  State.clusters = [];
  $('#result-panel').hidden = true;
  const panel = $('#compare-panel');
  panel.hidden = false;
  const parts = ['<div class="cmp-legend">' +
    '<span class="add">+ 新增</span><span class="gone">− 消失</span>' +
    '<span class="same">= 共同</span></div>'];
  (data.compare || []).forEach((pair) => {
    parts.push(`<h5 style="margin:14px 0 6px;font-size:13px">` +
      `${esc(pair.base_name)} → ${esc(pair.other_name)}` +
      ` <span class="hint-inline">新增 ${pair.new_count} · ` +
      `消失 ${pair.gone_count} · 共同 ${pair.common_count}</span></h5>`);
    const row = (sign, cls, it) =>
      `<div class="cmp-item"><span class="sign ${cls}">${sign}</span>` +
      `<span class="lv lv-${esc(it.level || 'INFO')}">${esc(it.level || '?')}</span>` +
      `<span>${esc(it.summary)}</span>` +
      (it.count ? `<span class="hint-inline">×${fmtInt(it.count)}</span>` : '') +
      '</div>';
    parts.push('<details open><summary class="hint">展开/收起</summary>');
    pair.new_items.forEach((it) => parts.push(row('+', 'add', it)));
    pair.gone_items.forEach((it) => parts.push(row('−', 'gone', it)));
    pair.common_items.forEach((it) => parts.push(row('=', 'same', it)));
    parts.push('</details>');
  });
  panel.innerHTML = parts.join('');
  toast('对比完成', 'ok');
}

/* ============================ 启动 ============================ */
(async function init() {
  buildLevelChips();
  applyTheme(localStorage.getItem('lac.theme') || 'dark');
  loadCfg();
  try {
    State.health = await api('/api/health');
    // 预置两条对比行，省得用户点
    if (!$('#compare-list').children.length) {
      addCompareRow(''); addCompareRow('');
    }
  } catch (err) {
    toast('无法连接本地服务：' + err.message, 'err');
  }
  await loadAiStatus();
  saveCfg();
})();
