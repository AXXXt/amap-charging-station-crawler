'use strict';

const $ = id => document.getElementById(id);
const endpoint = '/api/v1/collector-monitor';
const pageParams = new URLSearchParams(location.search);
const previewMode = pageParams.get('preview') === '1';
const demoMode = previewMode && pageParams.get('demo') === '1';
const platformNames = { BAIDU: '百度', AMAP: '高德' };
const outcomeNames = {
  REQUESTED: '等待结果', NO_ACTION: '未执行恢复', REJECTED: '校验未通过',
  RESUMED: '恢复续采', NEEDS_REVIEW: '恢复未完成', CANCELLED: '已取消',
};
const stageNames = {
  OPEN_MAP: '打开地图', SEARCH_INPUT: '打开搜索输入框', SEARCH: '搜索站点',
  OPEN_DETAIL: '打开详情', READ_DETAIL: '读取详情', PRICE: '读取电价',
};
const causeNames = { POPUP: '检测到弹窗', UNEXPECTED_PAGE: '页面异常', STAGE_TIMEOUT: '阶段超时' };
const actionNames = { PAUSE_AND_BACK: '返回上一页', PAUSE_AND_CLOSE_OVERLAY: '关闭遮挡弹窗', CLICK_NODE: '点击关闭节点', NONE: '无需操作' };
const modelNames = {
  PENDING: '等待响应', DECISION: '收到可解析建议', HTTP_ERROR: '接口返回错误', NETWORK_ERROR: '网络请求失败',
  INVALID_RESPONSE: '建议格式或内容校验失败', TIMEOUT: '模型请求超时', CANCELLED: '模型请求已取消',
};
const digitFormat = new Intl.NumberFormat('zh-CN');
const timeZone = 'Asia/Shanghai';
const clock = new Intl.DateTimeFormat('zh-CN', { timeZone, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
const minutes = new Intl.DateTimeFormat('zh-CN', { timeZone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const date = new Intl.DateTimeFormat('zh-CN', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' });
const roundTime = new Intl.DateTimeFormat('zh-CN', { timeZone, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const digits = value => value == null ? '—' : digitFormat.format(value);
const at = (value, formatter = clock) => value ? formatter.format(value * 1000) : '—';
const element = (tag, text, className = '') => {
  const item = document.createElement(tag);
  if (text != null) item.textContent = text;
  if (className) item.className = className;
  return item;
};
const setText = (id, value) => { $(id).textContent = value; };
const measure = (record, window) => record?.windows?.[window];
const ratePerHour = (snapshots, durationMinutes) => snapshots == null ? null : snapshots * (60 / durationMinutes);
const trendBinMinutes = 5;
const trendSeries = [
  { key: 'total', color: '--ink', width: 2.6, dash: [] },
  { key: 'BAIDU', color: '--accent', width: 2, dash: [] },
  { key: 'AMAP', color: '--secondary', width: 2, dash: [5, 4] },
];
const modelErrors = new Set(['HTTP_ERROR', 'NETWORK_ERROR', 'INVALID_RESPONSE', 'TIMEOUT']);

let key = '';
let snapshot = null;
let connected = false;
let intervalMinutes = 30;
let fetchNumber = 0;
let olderEvents = [];
let nextBefore = null;
let stopRequestId = null;
let sendingStop = false;
let chartIndex = null;
let chartPlot = null;
let failure = false;
let interactionNotice = '';
const deviceRequests = new Map();
const deviceFeedback = new Map();
const modes = { BAIDU: '设备调度', AMAP: '重点站点详情采集' };
const newRequestId = () => `web-${Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('')}`;

function message(id, value) {
  setText(id, value || '');
  $(id).hidden = !value;
}

async function request(path, { method = 'GET', body, providedKey = key } = {}) {
  const headers = providedKey ? { 'X-Collector-Monitor-Key': providedKey } : {};
  if (body) headers['Content-Type'] = 'application/json';
  const url = new URL(endpoint + path, location.origin);
  if (demoMode && method === 'GET') url.searchParams.set('demo', '1');
  const response = await fetch(url, {
    method, headers, body: body ? JSON.stringify(body) : undefined, cache: 'no-store',
    signal: AbortSignal.timeout(method === 'POST' ? 22000 : 17000),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(result.detail), { status: response.status });
  return result;
}

function eventParams(before = null) {
  const params = new URLSearchParams();
  params.set('log_days', $('days-filter').value);
  if ($('outcome-filter').value) params.set('outcome', $('outcome-filter').value);
  if ($('device-filter').value) params.set('device_id', $('device-filter').value);
  if (before != null) params.set('before', before);
  return `?${params}`;
}

function showAuth() {
  message('auth-error', '');
  if (!$('auth-dialog').open) $('auth-dialog').showModal();
  $('monitor-key').focus();
}

function apply(result) {
  const lastCheck = snapshot?.operations?.checkedAt;
  const currentCheck = result.operations?.checkedAt;
  if ($('days-filter').value === '1' && at(lastCheck, date) !== at(currentCheck, date)) olderEvents = [];
  snapshot = result;
  connected = true;
  failure = false;
  render();
}

async function refresh({ resetLogs = false, force = false } = {}) {
  if (!connected || ($('refresh').disabled && !force)) return;
  const number = ++fetchNumber;
  $('refresh').disabled = true;
  if (resetLogs) olderEvents = [];
  try {
    const result = await request(eventParams());
    if (number === fetchNumber) apply(result);
  } catch (error) {
    if (number !== fetchNumber) return;
    snapshot = null;
    failure = true;
    if (error.status === 401) {
      connected = false;
      key = '';
      showAuth();
    }
    render();
  }
}

function drawTrend(ingestion) {
  const data = ingestion?.trend;
  const chart = $('trend');
  const available = Boolean(data?.length);
  $('trend-legend').hidden = !available;
  $('trend-empty').hidden = available;
  setText('trend-empty', connected ? '采集速度趋势暂不可用' : '登录后查看采集速度趋势');
  chart.hidden = !available;
  $('chart-readout').hidden = !available;
  chartPlot = null;
  if (!available) {
    chartIndex = null;
    return;
  }

  const binSeconds = trendBinMinutes * 60;
  const rates = data.map(bin => ({
    total: ratePerHour(bin.BAIDU + bin.AMAP, trendBinMinutes),
    BAIDU: ratePerHour(bin.BAIDU, trendBinMinutes),
    AMAP: ratePerHour(bin.AMAP, trendBinMinutes),
  }));
  if (chartIndex != null) chartIndex = Math.min(chartIndex, data.length - 1);
  const index = chartIndex ?? data.length - 1;
  setText('chart-time', `${at(data[index].endAt - binSeconds, minutes)}–${at(data[index].endAt, minutes)}`);
  setText('chart-total', digits(rates[index].total));
  setText('chart-baidu', digits(rates[index].BAIDU));
  setText('chart-amap', digits(rates[index].AMAP));

  const ratio = window.devicePixelRatio || 1;
  const { width, height } = chart.getBoundingClientRect();
  if (!width || !height) return;
  chart.width = Math.round(width * ratio);
  chart.height = Math.round(height * ratio);
  const ctx = chart.getContext('2d');
  ctx.scale(ratio, ratio);
  ctx.font = '12px "Public Sans", "Segoe UI", sans-serif';

  const colors = getComputedStyle(chart);
  const axis = colors.getPropertyValue('--muted');
  const maximum = Math.max(...rates.map(bin => bin.total), 12);
  const targetStep = maximum / 4;
  const magnitude = 10 ** Math.floor(Math.log10(targetStep));
  const step = [1, 2, 2.5, 5, 10].map(value => value * magnitude).find(value => value >= targetStep);
  const start = Math.ceil(ctx.measureText(digits(step * 4)).width) + 10;
  const end = width - 9;
  const top = 11;
  const bottom = height - 27;
  const span = (end - start) / data.length;
  chartPlot = { start, span };
  const x = i => start + (i + .5) * span;
  const y = value => bottom - (value / (step * 4)) * (bottom - top);

  ctx.lineWidth = 1;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  ctx.fillStyle = axis;
  ctx.strokeStyle = colors.getPropertyValue('--border');
  for (let i = 0; i <= 4; i++) {
    const lineY = Math.round(y(i * step)) + .5;
    ctx.beginPath(); ctx.moveTo(start, lineY); ctx.lineTo(end, lineY); ctx.stroke();
    ctx.fillText(digits(i * step), start - 7, lineY);
  }
  const tickCount = width < 450 ? 3 : 6;
  const trendStart = data[0].endAt - binSeconds;
  for (let i = 0; i <= tickCount; i++) {
    ctx.textAlign = i === 0 ? 'left' : i === tickCount ? 'right' : 'center';
    const offset = (i / tickCount) * data.length;
    ctx.fillText(at(trendStart + offset * binSeconds, minutes), start + offset * span, height - 11);
  }

  for (const series of trendSeries) {
    ctx.strokeStyle = colors.getPropertyValue(series.color);
    ctx.lineWidth = series.width;
    ctx.setLineDash(series.dash);
    ctx.beginPath();
    ctx.moveTo(start, y(rates[0][series.key]));
    rates.forEach((bin, i) => ctx.lineTo(x(i), y(bin[series.key])));
    ctx.lineTo(end, y(rates.at(-1)[series.key]));
    ctx.stroke();
  }
  if (chartIndex != null) {
    ctx.setLineDash([]);
    ctx.strokeStyle = axis;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x(index), top); ctx.lineTo(x(index), bottom); ctx.stroke();
    for (const series of trendSeries) {
      ctx.fillStyle = colors.getPropertyValue(series.color);
      ctx.beginPath(); ctx.arc(x(index), y(rates[index][series.key]), 3, 0, Math.PI * 2); ctx.fill();
    }
  }
}

function renderCoverage(ingestion) {
  const section = $('coverage');
  section.replaceChildren();
  const coverage = ingestion?.roundCoverage;
  const batches = ingestion?.batches;
  setText('batch-note', batches && Object.values(batches).every(ids => ids.length === 1) ? `正式批次 ${batches.BAIDU[0]} / ${batches.AMAP[0]}` : '');
  const total = coverage?.total;
  if (total) {
    const row = element('div', null, 'coverage-figures');
    const covered = element('div');
    covered.append(element('strong', digits(total.stations), 'digits'), element('span', ` / ${digits(total.stationsInScope)} 站`));
    const remaining = element('div');
    remaining.append(element('strong', digits(total.outstanding), 'outstanding digits'), element('span', ' 站本轮未入库'));
    row.append(covered, remaining);
    section.append(row);
  }
  for (const [platform, name] of Object.entries(platformNames)) {
    const stats = coverage?.platforms?.[platform];
    const row = element('div', null, 'coverage-row');
    const progress = element('progress', null, platform.toLowerCase());
    progress.setAttribute('aria-label', `${name}本轮覆盖`);
    progress.max = Math.max(1, stats?.stationsInScope ?? 0);
    progress.value = stats?.stations ?? 0;
    if (!stats) progress.hidden = true;
    const figures = element('div', null, 'figures digits');
    figures.append(element('strong', digits(stats?.stations)), element('span', ` / ${digits(stats?.stationsInScope)}`));
    if (stats) figures.title = `本轮 ${digits(stats.outstanding)} 站未入库，累计 ${digits(stats.snapshots)} 条主任务快照`;
    row.append(element('span', name), progress, figures);
    section.append(row);
  }
  const percentage = total?.stationsInScope ? `${(100 * total.stations / total.stationsInScope).toFixed(1)}%` : '—';
  section.append(element('p', total ? `覆盖率 ${percentage} · 本轮累计 ${digits(total.snapshots)} 条主任务快照` : '等待核实活动轮次和入库记录', 'coverage-sub'));
  const starts = Object.entries(platformNames)
    .map(([platform, name]) => `${name} ${at(coverage?.platforms[platform]?.startedAt, roundTime)}`);
  setText('round-start', coverage && Object.values(coverage.platforms).some(Boolean)
    ? `本轮开始（北京时间） · ${starts.join(' · ')}` : '等待轮次数据');
}

function heartbeatSeconds(value) {
  const stamp = value ? Date.parse(value) / 1000 : NaN;
  return Number.isFinite(stamp) ? stamp : null;
}

function isOnline(device) {
  const heartbeat = heartbeatSeconds(device?.heartbeatAt);
  return heartbeat != null && Date.now() / 1000 - heartbeat <= 90;
}

function statusOf(device) {
  if (!device) return ['无设备登记', 'neutral'];
  if (!isOnline(device)) return ['心跳超时', 'warn'];
  if (device.stopRequired) return device.stopAcknowledged ? ['已停止', 'neutral'] : ['等待停采回执', 'warn'];
  if (device.runRequested === false) return ['已停止', 'neutral'];
  if (device.currentTaskId) return ['正在执行任务', 'good'];
  if (device.runRequested === true) return ['采集中 · 等待任务', 'good'];
  return ({ RUNNING: ['采集中', 'good'], IDLE: ['空闲', 'neutral'], PAUSED: ['已暂停', 'warn'],
    STOPPING: ['停止中', 'warn'], STOPPED: ['已停止', 'neutral'], ERROR: ['异常', 'danger'] })[device.status] || ['心跳在线', 'good'];
}

function shortDeviceId(id = '') {
  return id.length > 16 ? id.slice(0, 12) + '…' : id;
}

function commandDescription(command) {
  if (!command || command.status === 'CANCELLED') return '';
  const action = command.action === 'START' ? '启动' : '停止';
  return ({ QUEUED: `${action}指令已发送，等待设备执行`, APPLIED: `${action}已执行 · ${at(heartbeatSeconds(command.acknowledgedAt))}`,
    FAILED: `${action}失败：${command.error || '请查看手机端日志'}` })[command.status] || '';
}

function updateHeartbeatAges() {
  for (const node of document.querySelectorAll('[data-heartbeat]')) {
    const age = Math.max(0, Math.floor(Date.now() / 1000 - Number(node.dataset.heartbeat)));
    node.textContent = age < 60 ? `${age} 秒前` : age < 3600 ? `${Math.floor(age / 60)} 分 ${age % 60} 秒前` : `${Math.floor(age / 3600)} 小时 ${Math.floor(age % 3600 / 60)} 分前`;
    node.className = `heartbeat-age state ${age <= 90 ? 'good' : 'warn'}`;
  }
}

function renderDevices(view, ingestion, operations) {
  const table = $('device-table');
  const lanes = [...(view?.phones ?? []).flatMap(phone => Object.values(phone.apps)), ...(view?.unassigned ?? [])];
  const visibleIds = new Set(lanes.flatMap(lane => lane.registrationIds));
  const registered = new Map((operations?.devices ?? []).filter(device => !view || visibleIds.has(device.id)).map(device => [device.id, device]));
  const focused = document.activeElement?.closest('tr[data-device]');
  const focusId = focused?.dataset.device;
  const focusAction = focused ? document.activeElement.dataset.action : null;
  const identities = new Map();
  const missing = [];
  const history = [];
  for (const phone of view?.phones ?? []) {
    for (const lane of Object.values(phone.apps)) {
      for (const id of lane.registrationIds) identities.set(id, phone);
      for (const id of lane.clientIds.filter(id => !registered.has(id))) history.push({ id, platform: lane.platform, phone });
      if (lane.expected && !lane.clientIds.length) missing.push({ platform: lane.platform, phone });
    }
  }
  for (const lane of view?.unassigned ?? []) {
    for (const id of lane.clientIds.filter(id => !registered.has(id))) history.push({ id, platform: lane.platform, phone: null });
  }
  const devices = [...registered.values()].sort((a, b) => Number(isOnline(b)) - Number(isOnline(a)) || a.name.localeCompare(b.name));
  const online = devices.filter(isOnline).length;
  const pending = devices.filter(d => d.command?.status === 'QUEUED').length;
  setText('device-summary', operations ? `${devices.length} 个采集客户端 · ${online} 个心跳在线 · ${pending} 个指令待回执` : '等待设备数据');
  setText('device-window', `近 ${intervalMinutes} 分钟速度`);
  setText('device-foot', '百度开始设备调度，高德开始重点站点详情采集。心跳超过 90 秒标记超时；速度按正式站点的实际入库快照折算（站次/小时）。'
    + (view?.inventoryError ? ' 手机名单读取失败。' : ''));
  for (const body of [...table.tBodies]) body.remove();
  const body = table.createTBody();

  function addRow(id, platform, phone, device = null) {
    if (!Object.hasOwn(platformNames, platform)) platform = 'UNCLASSIFIED';
    const row = body.insertRow();
    row.dataset.appRow = '';
    if (device) row.dataset.device = id;
    const name = element('th', null, 'device-name'); name.scope = 'row';
    name.append(element('strong', phone?.name || device?.name || '手机归属待核实'));
    const code = element('span', device?.deviceCode || shortDeviceId(id) || '尚未登记', 'cell-note digits');
    code.title = [id, phone?.id].filter(Boolean).join(' · '); name.append(code); row.append(name);
    const app = row.insertCell();
    const label = element('span', null, 'device-app-label');
    label.append(element('i', null, `platform-dot ${platform.toLowerCase()}`), element('strong', platformNames[platform] || '待核实'));
    app.append(label, element('span', modes[platform] || '模式待核实', 'cell-note'));
    if (device?.appVersion) app.append(element('span', `v${device.appVersion}`, 'cell-note digits'));

    const state = row.insertCell();
    const [text, tone] = statusOf(device);
    state.append(element('span', device ? text : id ? '仅有入库记录' : text, `state ${tone}`));
    if (device?.currentTaskId) {
      const task = element('span', shortDeviceId(device.currentTaskId), 'cell-note digits');
      task.title = `在途任务：${device.currentTaskId}`; state.append(task);
    }
    if (device?.accessibilityConnected === false) state.append(element('span', '请在手机上开启无障碍', 'cell-note state warn'));
    const reading = ingestion?.devices?.[id];
    const windows = platform === 'UNCLASSIFIED' ? reading?.unclassified : reading?.windows?.[platform];
    const count = ingestion?.total ? windows?.[intervalMinutes]?.snapshots ?? 0 : null;
    const quantity = row.insertCell(); quantity.className = 'number digits';
    quantity.append(element('strong', digits(ratePerHour(count, intervalMinutes))));
    quantity.append(element('span', '站次/小时', 'cell-note'));
    quantity.append(element('span', count == null ? '入库数据不可用' : `${digits(count)} 条快照`, 'cell-note'));
    const heartbeat = row.insertCell(); heartbeat.className = 'digits heartbeat';
    const stamp = heartbeatSeconds(device?.heartbeatAt);
    if (stamp != null) {
      const age = element('span', '', 'heartbeat-age'); age.dataset.heartbeat = stamp;
      heartbeat.append(age, element('span', at(stamp), 'cell-note'));
      heartbeat.title = `${at(stamp, date)} ${at(stamp)}（北京时间）`;
    } else heartbeat.append(element('span', device ? '尚无心跳' : '无登记心跳', 'state neutral'));
    const last = reading?.latestByPlatform?.[platform];
    const latest = row.insertCell(); latest.className = 'digits';
    latest.append(element('span', last ? at(last) : ingestion ? '未查到' : '—'));
    if (last && at(last, date) !== at(ingestion?.cutoff, date)) latest.append(element('span', at(last, date), 'cell-note'));
    const controls = row.insertCell(); controls.className = 'device-controls';
    const request = deviceRequests.get(id);
    if (request && device?.command?.requestId === request.requestId && device.command.releaseComplete !== false && !request.sending) {
      deviceRequests.delete(id); deviceFeedback.delete(id);
    }
    const current = deviceRequests.get(id);
    const actions = element('div', null, 'device-actions');
    const unsupported = !device?.remoteControlSupported || !modes[platform];
    for (const action of ['START', 'STOP']) {
      const retry = current?.unknown && current.action === action;
      const button = element('button', retry ? `重试${action === 'START' ? '开始' : '停止'}` : action === 'START' ? '开始' : '停止', action === 'STOP' ? 'device-stop' : 'device-start');
      button.type = 'button'; button.dataset.action = action;
      button.disabled = !connected || previewMode || snapshot?.stopDisabled || unsupported || sendingStop || Boolean(current?.sending)
        || Boolean(current?.unknown && current.action !== action) || snapshot?.operations?.fleet?.releaseComplete === false;
      button.setAttribute('aria-label', `${action === 'START' ? '开始' : '停止'}${phone?.name || device?.name || ''}的${platformNames[platform] || ''}采集`);
      button.title = previewMode ? '预览模式，不能控制设备' : unsupported ? '请先升级手机端 App' : action === 'START' ? `开始${modes[platform]}${isOnline(device) ? '' : '，等待设备联网后执行'}` : '仅停止此采集客户端并释放它的在途任务';
      button.addEventListener('click', () => sendDeviceCommand(id, action));
      actions.append(button);
    }
    controls.append(actions);
    const feedback = current?.sending ? '正在提交指令…' : current?.unknown ? '提交结果待确认，请重试同一指令'
      : deviceFeedback.get(id) || commandDescription(device?.command) || (unsupported ? (device ? '升级 App 后可远程启停' : '无可控制的客户端') : !isOnline(device) ? '设备离线，指令会等待接收' : '');
    for (const [index, cell] of [...row.cells].entries()) cell.dataset.label = ['设备 / 客户端', '采集模式', '运行状态', `近 ${intervalMinutes} 分钟速度`, '设备心跳', '最近入库', '设备操作'][index];
    if (feedback) controls.append(element('span', feedback, `command-note cell-note ${device?.command?.status === 'FAILED' || current?.unknown ? 'state warn' : ''}`));
  }
  for (const device of devices) addRow(device.id, device.platform, identities.get(device.id), device);
  const seen = new Set();
  for (const entry of history) {
    const key = entry.id + '/' + entry.platform;
    if (seen.has(key)) continue;
    seen.add(key); addRow(entry.id, entry.platform, entry.phone);
  }
  for (const entry of missing) addRow('', entry.platform, entry.phone);
  if (!body.rows.length) {
    const empty = body.insertRow().insertCell(); empty.colSpan = 7; empty.className = 'table-empty';
    empty.textContent = connected ? '暂无设备登记，开启手机端无障碍后自动上报' : '登录后查看设备列表';
  }
  updateHeartbeatAges();
  if (focusId && focusAction) {
    const focusedRow = [...body.rows].find(row => row.dataset.device === focusId);
    focusedRow?.querySelector(`[data-action="${focusAction}"]:not(:disabled)`)?.focus({ preventScroll: true });
  }
}

async function sendDeviceCommand(id, action) {
  if (previewMode || !connected || snapshot?.stopDisabled) return;
  const old = deviceRequests.get(id);
  if (old?.sending || old?.unknown && old.action !== action) return;
  const pending = { requestId: old?.requestId || newRequestId(), action, sending: true, unknown: false };
  deviceRequests.set(id, pending); deviceFeedback.delete(id);
  renderDevices(snapshot?.deviceView, snapshot?.ingestion, snapshot?.operations);
  try {
    await request(`/devices/${encodeURIComponent(id)}/command`, { method: 'POST', body: { requestId: pending.requestId, action } });
    deviceRequests.delete(id);
    deviceFeedback.delete(id);
  } catch (error) {
    pending.sending = false;
    if (!error.status || error.status >= 500) pending.unknown = true;
    else {
      deviceRequests.delete(id);
      const errors = { DEVICE_APP_UPGRADE_REQUIRED: '请先升级手机端 App', STOP_RELEASE_IN_PROGRESS: '正在释放全停任务，请稍后重试',
        DEVICE_RELEASE_IN_PROGRESS_RETRY_PREVIOUS_REQUEST: '上一条停采指令仍在释放任务，请重试停止', REQUEST_ID_CONFLICT: '指令编号冲突，请刷新',
        UNKNOWN_DEVICE_PLATFORM: '设备平台尚未识别，暂不能启停' };
      deviceFeedback.set(id, error.status === 401 ? '登录已失效，请重新登录' : errors[error.message] || '指令提交失败，请刷新后重试');
    }
  }
  await refresh({ force: true });
}

function renderRecovery(operations, { appended = false } = {}) {
  const recovery = operations?.recovery;
  const supported = operations?.devices.filter(d => d.recoveryTelemetry) ?? [];
  const visible = Boolean(recovery && (recovery.since || supported.length));
  setText('ai-calls', digits(visible ? recovery.todayCalls : null));
  setText('ai-resumed', digits(visible ? recovery.todayOutcomes.RESUMED ?? 0 : null));
  setText('ai-review', digits(visible ? recovery.todayAttention : null));
  setText('ai-note', !connected ? 'deepseek-v4-flash · 百度采集'
    : !recovery ? '恢复日志服务暂不可用'
      : !visible ? '百度客户端尚未接入遥测 · 高德未接入模型'
        : `${recovery.model} · ${supported.length} 个百度客户端支持上报 · ${supported.filter(d => d.recoveryEnabled === true).length} 个已启用`);
  const filter = $('device-filter');
  const selected = filter.value;
  const candidates = operations?.devices.filter(d => d.platform === 'BAIDU') ?? [];
  const phoneLabels = new Map();
  for (const phone of snapshot?.deviceView?.phones ?? []) {
    for (const id of phone.apps.BAIDU?.registrationIds ?? []) {
      phoneLabels.set(id, `${phone.name} · ${shortDeviceId(phone.id)}`);
    }
  }
  filter.replaceChildren(new Option('全部客户端', ''),
    ...candidates.map(d => new Option(phoneLabels.get(d.id) || d.name || d.id, d.id)));
  if (candidates.some(d => d.id === selected)) filter.value = selected;
  filter.disabled = !visible;
  $('outcome-filter').disabled = !visible;
  $('days-filter').disabled = !visible;
  const logs = $('logs');
  const open = new Set([...logs.querySelectorAll('details[open]')].map(node => node.dataset.event));
  const preservedScroll = logs.scrollTop;
  logs.replaceChildren();
  const rows = [...new Map([...olderEvents, ...(recovery?.events ?? [])].map(event => [event.event_id, event])).values()].sort((a, b) => b.event_id - a.event_id);
  for (const event of rows) {
    const details = element('details');
    details.dataset.event = event.event_id;
    details.open = open.has(String(event.event_id));
    const summary = element('summary');
    const callFailed = event.outcome === 'NO_ACTION' && modelErrors.has(event.model_status);
    const outcome = callFailed ? modelNames[event.model_status] : outcomeNames[event.outcome] || '未知结果';
    const tone = event.outcome === 'RESUMED' ? 'good' : (callFailed || ['NEEDS_REVIEW', 'REJECTED'].includes(event.outcome)) ? 'warn' : 'neutral';
    const stamp = $('days-filter').value === '7' ? `${at(event.received_at, date).slice(5)} ${at(event.received_at, minutes)}` : at(event.received_at);
    summary.append(element('time', stamp, 'log-time digits'),
      element('span', `${phoneLabels.get(event.device_id) || event.device_name || event.device_id} · ${causeNames[event.cause] || '恢复触发'}`, 'log-main'),
      element('span', outcome, `state ${tone}`));
    const detail = element('div', null, 'log-detail');
    const http = event.http_status == null ? '' : ` · HTTP ${event.http_status}`;
    const elapsed = event.model_ms == null ? '' : ` · ${digits(event.model_ms)} 毫秒`;
    detail.append(element('p', `${stageNames[event.stage] || event.stage} · ${modelNames[event.model_status] || event.model_status}${http}${elapsed}`),
      element('p', `建议动作 ${actionNames[event.action] || '—'} · 已执行 ${digits(event.applied_actions)} 步`));
    if (event.outcome === 'RESUMED') detail.append(element('p', '已确认回到百度地图首页，采集继续。'));
    if (event.outcome === 'NEEDS_REVIEW') detail.append(element('p', '未确认回到百度地图首页，等待设备端处理。'));
    details.append(summary, detail);
    logs.append(details);
  }
  if (!rows.length) logs.append(element('div', visible ? '所选时间内没有符合条件的上报日志' : '等待客户端上报恢复事件', 'empty small'));
  logs.scrollTop = preservedScroll;
  if (!appended && !olderEvents.length) nextBefore = recovery?.nextBefore ?? null;
  $('more-wrap').hidden = nextBefore == null || !rows.length;
}

function stopReady(fleet) {
  return Boolean(fleet && (!fleet.releaseComplete || stopRequestId || fleet.generation === 0 || fleet.allowedDevices > 0));
}

function render() {
  const ingestion = snapshot?.ingestion;
  const operations = snapshot?.operations;
  const fleet = operations?.fleet;
  const preview = snapshot?.preview;
  $('preview-banner').hidden = !preview;
  setText('preview-note', preview || '');
  message('notice', interactionNotice || (failure ? '刷新失败，当前数据暂不可用。'
    : connected && !ingestion ? '暂未取得真实入库数据，速度和覆盖率显示为 —。'
      : connected && !operations ? '暂未取得设备与恢复日志，停采按钮暂不可用。' : ''));
  if (failure) setText('sync-state', '刷新失败');
  else if (ingestion) setText('sync-state', `数据截止 ${at(ingestion.cutoff)} · 北京时间`);
  else setText('sync-state', connected ? '等待读取入库' : '等待登录');
  $('sync-state').dataset.tone = failure || (connected && (!ingestion || !operations)) ? 'warn' : ingestion ? 'good' : 'neutral';
  $('operator').textContent = preview ? '本机预览' : connected ? '退出监控' : '监控登录';
  $('operator').disabled = Boolean(preview);
  $('refresh').disabled = !connected;
  if (fleet?.releaseComplete && stopRequestId === fleet.requestId) stopRequestId = null;
  if (fleet?.generation > 0) {
    message('fleet', fleet.releaseComplete
      ? `停采指令 #${fleet.generation} · 已处理 ${digits(fleet.releasedTasks)} 个在途任务 · ${digits(fleet.acknowledged)} 个客户端已回执 / ${digits(fleet.pending)} 个待回执`
      : `停采指令 #${fleet.generation} · 已暂停派发，等待服务端处理在途任务`);
    if (fleet.retryRequestId) stopRequestId = fleet.retryRequestId;
  } else message('fleet', '');
  $('stop').disabled = !stopReady(fleet) || sendingStop || Boolean(snapshot?.stopDisabled);
  $('stop').title = snapshot?.stopDisabled ? '本机只读预览' : '';
  setText('stop-label', sendingStop ? '发送中…' : stopRequestId ? '重试停采指令' : '停止全部采集');
  const metricValues = { total: ingestion?.speed?.total, ...ingestion?.speed?.platforms };
  setText('speed-window', ingestion
    ? `近 ${intervalMinutes} 分钟 · ${at(ingestion.cutoff - intervalMinutes * 60)}–${at(ingestion.cutoff)}`
    : `近 ${intervalMinutes} 分钟 · 实际采集端`);
  for (const el of document.querySelectorAll('[data-metric]')) {
    const period = measure(metricValues[el.dataset.metric], intervalMinutes);
    const last = metricValues[el.dataset.metric]?.latestAt;
    el.querySelector('.rate').textContent = digits(ratePerHour(period?.snapshots, intervalMinutes));
    el.querySelector('.metric-detail').textContent = period
      ? period.snapshots === 0 && el.dataset.metric !== 'total'
        ? `最近入库 ${last ? `${at(last, date)} ${at(last)}` : '未查到'}`
        : `${digits(period.snapshots)} 条快照 · 涉及 ${digits(period.stations)} 站`
      : ingestion?.speed ? '该时段数据暂不可用' : ingestion ? '正式名单待核实' : '等待入库数据';
  }
  const roundTotal = ingestion?.roundCoverage?.total;
  setText('finding', roundTotal ? `当前轮次还有 ${digits(roundTotal.outstanding)} 个正式站点未入库。` : '百度与高德采集的入库、覆盖和恢复情况。');
  renderCoverage(ingestion);
  drawTrend(ingestion);
  renderDevices(snapshot?.deviceView, ingestion, operations);
  if (!operations) olderEvents = [];
  renderRecovery(operations);
  let note = '速度按执行采集的 App 统计，同站多次入库计入站次。本轮覆盖按正式名单和主任务入库统计。';
  const cross = ingestion?.supplement;
  if (cross) note += ` 今日补采入库 · 百度 → 高德 ${digits(cross.BAIDU.today.stations)} 站 · 高德 → 百度 ${digits(cross.AMAP.today.stations)} 站。`;
  const unclassified = measure(ingestion?.speed?.unclassified, intervalMinutes);
  if (unclassified?.snapshots) note += ` 另有 ${digits(unclassified.snapshots)} 条正式站点快照的采集端待核实。`;
  const outside = measure(ingestion?.outside, intervalMinutes);
  if (outside) {
    note += ` 名单外快照近 ${intervalMinutes} 分钟 ${digits(outside.snapshots)} 条 / ${digits(outside.stations)} 站，今日 ${digits(ingestion.outside.today.snapshots)} 条 / ${digits(ingestion.outside.today.stations)} 站。`;
  }
  if (ingestion?.unassignedStations) note += ` ${digits(ingestion.unassignedStations)} 个站点归属待核实，今日收到 ${digits(ingestion.unassigned.today.snapshots)} 条快照。`;
  if (ingestion && !ingestion.total) note += ' 未能核实两个平台均有唯一活动批次。';
  setText('data-note', note);
}

$('auth-form').addEventListener('submit', async event => {
  event.preventDefault();
  const providedKey = $('monitor-key').value.trim();
  $('auth-submit').disabled = true;
  message('auth-error', '');
  try {
    const result = await request('', { providedKey });
    key = providedKey;
    $('auth-dialog').close();
    apply(result);
  } catch (error) {
    message('auth-error', error.message === 'COLLECTOR_MONITOR_KEY_NOT_CONFIGURED' ? '采集服务尚未配置登录密码。'
      : error.status === 401 ? '登录密码不正确。' : '连接失败，请稍后重试。');
  } finally {
    $('auth-submit').disabled = false;
  }
});
$('auth-dialog').addEventListener('close', () => { $('monitor-key').value = ''; });
$('auth-cancel').addEventListener('click', () => $('auth-dialog').close());
$('operator').addEventListener('click', () => {
  if (!connected) return showAuth();
  fetchNumber++;
  key = '';
  snapshot = null;
  olderEvents = [];
  connected = false;
  failure = false;
  interactionNotice = '';
  stopRequestId = null;
  render();
});
$('refresh').addEventListener('click', () => refresh());
for (const control of $('window-picker').querySelectorAll('button')) {
  control.addEventListener('click', () => {
    intervalMinutes = Number(control.dataset.window);
    for (const button of $('window-picker').querySelectorAll('button')) button.setAttribute('aria-pressed', String(button === control));
    render();
  });
}
for (const id of ['days-filter', 'device-filter', 'outcome-filter']) {
  $(id).addEventListener('change', () => {
    // Invalidate any poll that began before the filter changed.
    fetchNumber++;
    $('refresh').disabled = false;
    refresh({ resetLogs: true });
  });
}
$('load-more').addEventListener('click', async () => {
  const query = eventParams();
  $('load-more').disabled = true;
  try {
    const result = await request(eventParams(nextBefore));
    if (query !== eventParams() || !connected || !result.operations) return;
    olderEvents.push(...result.operations.recovery.events);
    nextBefore = result.operations.recovery.nextBefore;
    renderRecovery(snapshot.operations, { appended: true });
  } catch {
    message('notice', '加载较早的日志失败，请刷新后重试。');
  } finally {
    $('load-more').disabled = false;
  }
});
$('stop').addEventListener('click', () => $('stop-dialog').showModal());
$('stop-cancel').addEventListener('click', () => $('stop-dialog').close());
$('stop-confirm').addEventListener('click', async () => {
  if (sendingStop || !connected || snapshot?.stopDisabled || !stopReady(snapshot?.operations?.fleet)) return;
  $('stop-dialog').close();
  sendingStop = true;
  interactionNotice = '';
  // Preserve the same ID across network retries. The server exposes a pending ID after a partial release.
  stopRequestId ||= `web-${Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('')}`;
  render();
  try {
    const result = await request('/stop-all', { method: 'POST', body: { requestId: stopRequestId } });
    stopRequestId = null;
    await refresh({ force: true });
    if (snapshot?.operations?.fleet?.generation < result.stopGeneration || !snapshot?.operations) {
      interactionNotice = `停采指令 #${result.stopGeneration} 已提交，服务端已处理 ${digits(result.releasedTaskCount)} 个在途任务。请刷新读取设备回执。`;
    }
  } catch (error) {
    await refresh({ force: true });
    if (stopRequestId) {
      interactionNotice = error.status === 401 ? '登录已失效，请重新登录。' : '停采结果待确认。再次提交将沿用同一条指令。';
    }
  } finally {
    sendingStop = false;
    render();
  }
});

const chart = $('trend');
function selectTrend(event) {
  const length = snapshot?.ingestion?.trend?.length ?? 0;
  if (!length || !chartPlot) return;
  const { start, span } = chartPlot;
  const index = Math.max(0, Math.min(length - 1, Math.floor((event.offsetX - start) / span)));
  if (index === chartIndex) return;
  chartIndex = index;
  drawTrend(snapshot.ingestion);
}
function clearTrendSelection() {
  if (chartIndex == null) return;
  chartIndex = null;
  drawTrend(snapshot?.ingestion);
}
chart.addEventListener('pointermove', selectTrend);
chart.addEventListener('pointerdown', selectTrend);
chart.addEventListener('pointerleave', event => { if (event.pointerType !== 'touch') clearTrendSelection(); });
chart.addEventListener('pointercancel', clearTrendSelection);
chart.addEventListener('blur', clearTrendSelection);
chart.addEventListener('keydown', event => {
  if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
  const length = snapshot?.ingestion?.trend?.length ?? 0;
  if (!length) return;
  event.preventDefault();
  chartIndex = Math.max(0, Math.min(length - 1, (chartIndex ?? length - 1) + (event.key === 'ArrowLeft' ? -1 : 1)));
  drawTrend(snapshot.ingestion);
});
new ResizeObserver(() => drawTrend(snapshot?.ingestion)).observe(chart.parentElement);
document.fonts.ready.then(() => drawTrend(snapshot?.ingestion));
render();
if (previewMode) request('').then(apply).catch(() => {
  failure = true;
  render();
});
setInterval(updateHeartbeatAges, 1_000);
setInterval(() => { if (!document.hidden) refresh(); }, 5_000);
if (!previewMode) showAuth();
