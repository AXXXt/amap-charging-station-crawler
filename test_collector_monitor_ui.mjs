// Run: node --test test_collector_monitor_ui.mjs (Node.js 20+; no external dependencies).
// Set MONITOR_JS_PATH to an older script to verify the regression fails there.
// The minimal DOM covers controls/table rendering; it does not replace browser/layout acceptance.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { webcrypto } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import test from 'node:test';

const scriptPath = process.env.MONITOR_JS_PATH || fileURLToPath(new URL('./collector-monitor/monitor.js', import.meta.url));
const source = readFileSync(scriptPath, 'utf8');
const html = readFileSync(new URL('./collector-monitor/index.html', import.meta.url), 'utf8');
const deviceId = 'offline-test-client';
const testMonitorKey = 'offline-test-monitor-key';

// Execute the complete shipped script with DOM events and a strictly offline API.
// Do not replace render(), request(), or any command/state management function.
function createHarness({ firstStop = 'success', platform = 'BAIDU', unclassifiedSnapshots = null } = {}) {
  let document;
  class Element {
    constructor(tag = 'div') {
      this.tagName = tag.toLowerCase(); this.children = []; this.dataset = {};
      this.listeners = new Map(); this.attributes = {}; this.className = '';
      this.disabled = false; this.hidden = false; this.value = ''; this._text = '';
    }
    set textContent(value) { this._text = String(value ?? ''); this.replaceChildren(); }
    get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
    append(...nodes) { for (const node of nodes) { node.parentElement = this; this.children.push(node); } }
    replaceChildren(...nodes) { for (const node of this.children) node.parentElement = null; this.children = []; this.append(...nodes); }
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(node => node !== this); this.parentElement = null; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener(name, listener) { if (!this.listeners.has(name)) this.listeners.set(name, []); this.listeners.get(name).push(listener); }
    async dispatch(name) {
      if (name === 'click' && this.disabled) throw new Error(`Clicked a disabled control: ${this.textContent}`);
      await Promise.all((this.listeners.get(name) ?? []).map(listener => listener({ preventDefault() {}, target: this })));
    }
    matches(selector) {
      if (selector.endsWith(':not(:disabled)')) return !this.disabled && this.matches(selector.slice(0, -':not(:disabled)'.length));
      if (selector.startsWith('.')) return this.className.split(/\s+/).includes(selector.slice(1));
      const match = selector.match(/^([a-z]+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/);
      if (!match || match[1] && match[1] !== this.tagName) return false;
      if (!match[2]) return true;
      const name = match[2];
      const value = name.startsWith('data-') ? this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())]
        : name === 'open' ? this.open ? '' : undefined : this.attributes[name];
      return value !== undefined && (match[3] === undefined || value === match[3]);
    }
    querySelectorAll(selector) { return this.children.flatMap(node => [...(node.matches(selector) ? [node] : []), ...node.querySelectorAll(selector)]); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
    closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector); }
    focus() { document.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; for (const listener of this.listeners.get('close') ?? []) listener({}); }
    get tBodies() { return this.children.filter(node => node.tagName === 'tbody'); }
    get rows() { return this.children.filter(node => node.tagName === 'tr'); }
    get cells() { return this.children.filter(node => ['td', 'th'].includes(node.tagName)); }
    createTBody() { const body = new Element('tbody'); this.append(body); return body; }
    insertRow() { const row = new Element('tr'); this.append(row); return row; }
    insertCell() { const cell = new Element('td'); this.append(cell); return cell; }
  }
  const root = new Element('body');
  const ids = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => {
    const node = new Element(id === 'device-table' ? 'table' : 'div'); root.append(node); return [id, node];
  }));
  document = { hidden: false, activeElement: null, fonts: { ready: Promise.resolve() },
    getElementById: id => { assert.ok(ids.has(id), `Missing real page element ${id}`); return ids.get(id); },
    createElement: tag => new Element(tag), querySelectorAll: selector => root.querySelectorAll(selector) };
  ids.get('days-filter').value = '1';
  for (const interval of [10, 30, 60]) { const button = new Element('button'); button.dataset.window = String(interval); ids.get('window-picker').append(button); }
  const posts = [], gets = [];
  let command = null;
  const snapshot = () => ({ ingestion: unclassifiedSnapshots == null ? null : {
    cutoff: Math.floor(Date.now() / 1000), total: {},
    devices: { [deviceId]: { unclassified: { 30: { snapshots: unclassifiedSnapshots } } } },
  }, deviceView: null, operations: {
    checkedAt: Math.floor(Date.now() / 1000), recovery: null,
    fleet: { generation: 0, releaseComplete: true, allowedDevices: 1, pending: 0, acknowledged: 0 },
    devices: [{ id: deviceId, name: 'Offline phone', platform, appVersion: '0.1.75',
      heartbeatAt: new Date().toISOString(), status: 'IDLE', remoteControlSupported: true,
      stopRequired: command?.action === 'STOP', stopAcknowledged: command?.status === 'APPLIED',
      command: command ? { ...command } : null }],
  } });
  const response = (status, body) => ({ ok: status >= 200 && status < 300, status, async json() { return structuredClone(body); } });
  const fetch = async (url, options) => {
    const target = new URL(url);
    assert.equal(target.origin, 'https://offline.invalid');
    assert.equal(options.headers['X-Collector-Monitor-Key'], testMonitorKey);
    if (options.method === 'GET') {
      assert.equal(target.pathname, '/api/v1/collector-monitor'); gets.push(target.href); return response(200, snapshot());
    }
    assert.equal(options.method, 'POST');
    assert.equal(target.pathname, `/api/v1/collector-monitor/devices/${deviceId}/command`);
    const body = JSON.parse(options.body); posts.push(body);
    if (command && !command.releaseComplete) {
      assert.equal(body.requestId, command.requestId, 'Partial STOP must be retried with the durable original ID');
      assert.equal(body.action, 'STOP'); command.releaseComplete = true;
      return response(200, { command });
    }
    command = { commandId: posts.length, ...body, status: 'QUEUED', releaseComplete: true,
      mode: 'DEVICE_DISPATCH', acknowledgedAt: '', error: '' };
    if (body.action === 'STOP' && posts.length === 1 && firstStop !== 'success') {
      command.releaseComplete = firstStop === 'completed-before-refresh';
      return response(503, { detail: 'COMMAND_RESULT_UNKNOWN_RETRY_SAME_REQUEST' });
    }
    return response(200, { command });
  };
  const context = vm.createContext({ document, location: { search: '', origin: 'https://offline.invalid' },
    window: { devicePixelRatio: 1 }, URL, URLSearchParams, Intl, Date, crypto: webcrypto, fetch,
    AbortSignal: { timeout() { return {}; } }, Option: class extends Element { constructor(text, value) { super('option'); this.textContent = text; this.value = value; } },
    ResizeObserver: class { observe() {} }, setInterval() {}, console });
  vm.runInContext(source, context, { filename: scriptPath });
  return {
    posts, gets, ids,
    async login() { ids.get('monitor-key').value = testMonitorKey; await ids.get('auth-form').dispatch('submit'); },
    row() { return ids.get('device-table').tBodies[0].rows.find(row => row.dataset.device === deviceId); },
    button(action) { return this.row().querySelector(`[data-action="${action}"]`); },
    async click(action) { await this.button(action).dispatch('click'); },
    async refresh() { await ids.get('refresh').dispatch('click'); },
    acknowledge() { command.status = 'APPLIED'; command.acknowledgedAt = new Date().toISOString(); },
  };
}

test('partial STOP: 503, GET of unfinished release, same-ID retry, real ACK, then fresh operation', async () => {
  const app = createHarness({ firstStop: 'partial' });
  await app.login(); await app.click('STOP');
  assert.equal(app.posts.length, 1); assert.equal(app.gets.length, 2);
  const originalId = app.posts[0].requestId;
  assert.equal(app.button('STOP').textContent, '重试停止');
  assert.equal(app.button('STOP').disabled, false);
  assert.equal(app.button('START').disabled, true, 'Opposite action must stay blocked while the STOP outcome is unknown');
  await app.refresh();
  assert.equal(app.button('STOP').textContent, '重试停止', 'Repeated polling must retain the pending request');
  await app.click('STOP');
  assert.equal(app.posts[1].requestId, originalId);
  assert.equal(app.button('STOP').textContent, '停止');
  assert.equal(app.button('START').disabled, false);
  assert.match(app.row().textContent, /停止指令已发送，等待设备执行/);
  app.acknowledge(); await app.refresh();
  assert.match(app.row().textContent, /停止已执行/);
  await app.click('STOP');
  assert.notEqual(app.posts[2].requestId, originalId, 'A new operation after success must use a new ID');
});

test('lost STOP response whose release already completed: GET clears retry state normally', async () => {
  const app = createHarness({ firstStop: 'completed-before-refresh' });
  await app.login(); await app.click('STOP');
  assert.equal(app.button('STOP').textContent, '停止');
  assert.equal(app.button('START').disabled, false);
  app.acknowledge(); await app.refresh();
  assert.match(app.row().textContent, /停止已执行/);
  await app.click('STOP');
  assert.notEqual(app.posts[1].requestId, app.posts[0].requestId);
});

test('normal START success and later APPLIED receipt retain normal cleanup and new request IDs', async () => {
  const app = createHarness();
  await app.login(); await app.click('START');
  assert.equal(app.button('START').textContent, '开始');
  assert.equal(app.button('STOP').disabled, false);
  assert.match(app.row().textContent, /启动指令已发送，等待设备执行/);
  app.acknowledge(); await app.refresh();
  assert.match(app.row().textContent, /启动已执行/);
  await app.click('START');
  assert.notEqual(app.posts[1].requestId, app.posts[0].requestId);
});

for (const platform of [null, 'LEGACY_DIAGNOSTIC']) {
  test(`historical platform ${JSON.stringify(platform)} renders as unclassified with disabled remote controls`, async () => {
    const app = createHarness({ platform, unclassifiedSnapshots: 7 });
    await app.login();
    assert.equal(app.ids.get('auth-error').textContent, '', 'Login rendering must not throw');
    assert.match(app.row().textContent, /待核实/);
    assert.match(app.row().textContent, /模式待核实/);
    assert.ok(app.row().querySelector('.unclassified'), 'Unknown platform must use the neutral unclassified marker');
    assert.equal(app.button('START').disabled, true);
    assert.equal(app.button('STOP').disabled, true);
    assert.equal(app.row().cells[3].textContent, '14站次/小时7 条快照', 'Unclassified speed must use its real nonzero receipts');
    await app.refresh();
    assert.notEqual(app.ids.get('sync-state').textContent, '刷新失败');
    assert.ok(app.row(), 'Historical registration must remain visible after polling');
    assert.equal(app.posts.length, 0, 'Rendering and polling must not send device commands');
  });
}
