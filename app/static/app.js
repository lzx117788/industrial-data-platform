const $ = (selector) => document.querySelector(selector);
const fmt = (date) => date ? new Date(date).toLocaleString('zh-CN', { hour12: false }) : '尚未同步';
const node = (tag, text, cls) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; };
const states = { succeeded: ['完成', 'ok'], running: ['运行中', 'wait'], failed: ['失败', 'error'], abandoned: ['已中断', 'error'] };
let syncing = false;
async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) { const body = await res.json().catch(() => ({})); throw new Error(typeof body.detail === 'string' ? body.detail : `请求失败：${res.status}`); }
  return res.json();
}
function toast(text) { $('#toast').textContent = text; $('#toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('#toast').hidden = true, 5000); }
function error(err) { $('#error-banner').textContent = err.message; $('#error-banner').hidden = false; }
function empty(tbody, cols, text) { const tr = node('tr'); const td = node('td', text, 'empty'); td.colSpan = cols; tr.append(td); tbody.append(tr); }
function status(value) { const [label, cls] = states[value] || ['未运行', '']; return node('span', label, `badge ${cls}`); }
function cell(row, value, cls) { const td = node('td', value, cls); row.append(td); return td; }
function button(text, action) { const el = node('button', text, 'link-button'); el.type = 'button'; el.addEventListener('click', action); el.disabled = syncing; return el; }
function busy(value) { syncing = value; $('#sync-all').disabled = value; document.querySelectorAll('.link-button').forEach(b => b.disabled = value); $('#sync-all').textContent = value ? '正在同步…' : '同步示例数据 ↗'; }
async function syncSource(id, full = false) {
  if (syncing) return;
  busy(true); $('#error-banner').hidden = true;
  try { const run = await api(`/api/sources/${id}/sync`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ full }) }); toast(`同步完成：新增 ${run.inserted_count}，更新 ${run.updated_count}，拒绝 ${run.rejected_count}`); }
  catch (err) { error(err); }
  finally { busy(false); await refresh(); }
}
async function syncAll() {
  if (syncing) return;
  busy(true); $('#error-banner').hidden = true;
  try { const data = await api('/api/demo/run', { method: 'POST' }); const sum = data.items.reduce((s, r) => s + r.inserted_count, 0); toast(`三个数据源同步完成，新增 ${sum} 条记录。异常数据已隔离。`); }
  catch (err) { error(err); }
  finally { busy(false); await refresh(); }
}
function renderSources(data) {
  const body = $('#source-rows'); body.replaceChildren(); $('#sources-count').textContent = `${data.items.length} 个数据源`;
  if (!data.items.length) empty(body, 5, '暂无数据源，可通过 API 文档创建。');
  for (const s of data.items) {
    const row = node('tr'); const title = cell(row); title.append(node('strong', s.name), node('small', s.code, 'mono'));
    cell(row).append(node('span', s.kind.toUpperCase(), 'badge'));
    const cursor = cell(row); cursor.append(node('span', fmt(s.cursor.updated_at)), node('small', s.cursor.external_id || '同步后自动记录', 'mono'));
    cell(row).append(status(s.last_run?.status));
    cell(row, undefined, 'right').append(button('增量同步', () => syncSource(s.id)), button('全量校验', () => syncSource(s.id, true)));
    body.append(row);
  }
}
function renderOverview(data) {
  for (const [id, key] of [['devices','devices'], ['alarms','alarms'], ['maintenance','maintenance'], ['quality','quality_issues']]) $(`#count-${id}`).textContent = data.counts[key];
  $('#sync-all').hidden = !data.demo_mode;
  const bars = $('#quality-bars'); bars.replaceChildren();
  for (const [label, key] of [['必填字段完整率','required_field_completeness_pct'], ['批次记录唯一率','batch_uniqueness_pct'], ['设备关联成功率','association_success_pct']]) {
    const value = data.quality[key], row = node('div', undefined, 'quality-row'), caption = node('div', undefined, 'quality-caption');
    caption.append(node('span', label), node('strong', value === null ? '暂无样本' : `${value.toFixed(1)}%`));
    const bar = node('div', undefined, 'bar'), fill = node('i'); fill.style.width = `${value ?? 0}%`; bar.append(fill); row.append(caption, bar); bars.append(row);
  }
  const freshness = $('#freshness'); freshness.replaceChildren();
  for (const f of data.freshness) { const row = node('div', undefined, 'fresh-row'); row.append(node('strong', f.source_code, 'mono'), node('span', fmt(f.latest_accepted_update))); freshness.append(row); }
}
function renderDevices(data) {
  const body = $('#device-rows'); body.replaceChildren();
  if (!data.items.length) empty(body, 6, '暂无设备。先同步示例数据，或调整搜索条件。');
  for (const d of data.items) {
    const row = node('tr', undefined, 'clickable'); row.tabIndex = 0; row.setAttribute('aria-label', `查看${d.name}详情`);
    cell(row, d.device_key, 'mono'); cell(row, d.name); cell(row, d.category); cell(row, d.location);
    cell(row).append(node('span', { active: '运行中', inactive: '停用', maintenance: '维护中' }[d.status] || d.status, 'badge ok'));
    cell(row, fmt(d.source_updated_at)); row.addEventListener('click', () => showDevice(d.device_key)); row.addEventListener('keydown', e => { if (e.key === 'Enter') showDevice(d.device_key); }); body.append(row);
  }
}
async function showDevice(key) {
  try {
    const [d, t] = await Promise.all([api(`/api/devices/${encodeURIComponent(key)}`), api(`/api/devices/${encodeURIComponent(key)}/timeline`)]);
    const panel = $('#device-detail'); panel.replaceChildren(); panel.hidden = false;
    const head = node('div', undefined, 'device-header'); head.append(node('h2', `${d.device_key} · ${d.name}`), node('p', `${d.location} · ${d.alarm_count} 条告警 · ${d.maintenance_count} 条维修记录`));
    const aliases = node('div', undefined, 'aliases'); for (const a of d.aliases) aliases.append(node('span', `${a.source_code} / ${a.external_device_id}`, 'badge')); head.append(aliases); panel.append(head);
    const list = node('div', undefined, 'timeline');
    for (const event of t.items) { const el = node('article', undefined, 'event'); el.append(node('strong', `${event.title} · ${event.external_id}`), node('p', event.description), node('small', `${fmt(event.at)} · ${event.source_code}${event.cost ? ` · ¥${event.cost}` : ''}`)); list.append(el); }
    if (!t.items.length) list.append(node('p', '暂无关联事件', 'empty')); panel.append(list); panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) { error(err); }
}
function renderIssues(data) {
  const body = $('#issue-rows'); body.replaceChildren();
  if (!data.issues.length) empty(body, 5, '暂无质量问题。同步后在这里查看检查结果。');
  for (const i of data.issues) { const row = node('tr'); cell(row, i.source_code, 'mono'); cell(row, i.external_id || '—', 'mono'); cell(row).append(node('span', i.code, 'badge error')); cell(row, i.message); cell(row, fmt(i.created_at)); body.append(row); }
}
function renderRuns(data) {
  const body = $('#run-rows'); body.replaceChildren();
  if (!data.items.length) empty(body, 7, '暂无运行记录。点击同步示例数据开始。');
  for (const r of data.items) {
    const row = node('tr'), title = cell(row); title.append(node('strong', r.source_code, 'mono'), node('small', fmt(r.started_at)), node('small', r.full_sync ? '全量校验' : '增量同步'));
    if (r.error) title.append(node('small', r.error));
    cell(row).append(status(r.status)); cell(row, r.read_count); cell(row, `${r.inserted_count} / ${r.updated_count}`); cell(row, r.unchanged_count); cell(row, `${r.rejected_count} / ${r.duplicate_count}`); cell(row, r.duration_ms === null ? '—' : `${r.duration_ms.toFixed(1)} ms`); body.append(row);
  }
}
function renderMetrics(data) {
  const list = $('#api-metrics'); list.replaceChildren();
  for (const m of data.routes) { const row = node('div', undefined, 'metric-item'); row.append(node('strong', m.route, 'mono'), node('span', `${m.samples} 次 · P50 ${m.p50_ms} ms · P95 ${m.p95_ms} ms`)); list.append(row); }
  if (!data.routes.length) list.append(node('p', '暂无请求样本', 'empty'));
}
async function refresh() {
  try {
    const [overview, sources, devices, quality, runs, metrics] = await Promise.all([api('/api/overview'), api('/api/sources'), api(`/api/devices?q=${encodeURIComponent($('#search').value)}`), api('/api/quality'), api('/api/runs'), api('/api/metrics')]);
    renderOverview(overview); renderSources(sources); renderDevices(devices); renderIssues(quality); renderRuns(runs); renderMetrics(metrics);
    $('#connection').textContent = '本地服务运行中'; $('#updated-at').textContent = `更新于 ${new Date().toLocaleTimeString('zh-CN')}`;
  } catch (err) { $('#connection').textContent = '服务连接异常'; error(err); }
}
const pages = { overview: ['让设备数据，关联起来。', '汇集设备档案、告警与维修记录，为运维应用提供一致的数据视图。'], devices: ['一台设备，一份完整视图。', '关联设备在不同系统中的编号，汇总告警与维修历史。'], quality: ['每个异常，都有据可查。', '检查输入数据，隔离问题记录，保留来源和失败原因。'], runs: ['每次同步，都能追溯。', '记录已提交的批次与水位，让重试和中断恢复有迹可循。'] };
document.querySelectorAll('.nav').forEach(b => b.addEventListener('click', () => { document.querySelectorAll('.nav').forEach(n => n.classList.toggle('active', n === b)); document.querySelectorAll('.view').forEach(v => v.hidden = v.id !== b.dataset.section); [$('#page-title').textContent, $('#page-subtitle').textContent] = pages[b.dataset.section]; }));
$('#sync-all').addEventListener('click', syncAll); $('#refresh').addEventListener('click', refresh);
let searchTimer; $('#search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(async () => { try { renderDevices(await api(`/api/devices?q=${encodeURIComponent($('#search').value)}`)); } catch (err) { error(err); } }, 250); });
refresh();
