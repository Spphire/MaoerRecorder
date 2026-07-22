const token = document.querySelector('meta[name="maoer-token"]').content;
const stateLabels = {
  starting: '启动中', monitoring: '等待开播', recording: '录制中',
  finalizing: '正在合并', stopping: '停止中', restarting: '重启中',
  degraded: '单路运行', unresponsive: '无响应', error: '异常', stopped: '已停止'
};

let currentData = { tasks: [], history: [], summary: {} };
let currentFilter = 'all';
let currentLogRoom = null;
let pollTimer = null;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[char]);
}

async function api(path, options = {}) {
  const method = options.method || 'GET';
  const headers = { ...(options.headers || {}) };
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    headers['X-Maoer-Token'] = token;
  }
  const response = await fetch(path, { ...options, method, headers });
  const payload = await response.json().catch(() => ({ ok: false, error: '服务响应格式错误' }));
  if (!response.ok || !payload.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return payload;
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size >= 100 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds || 0)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours) return `${hours} 小时 ${minutes} 分`;
  return `${minutes} 分钟`;
}

function formatDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
  }).format(date);
}

function toast(message, kind = 'normal') {
  const element = document.createElement('div');
  element.className = `toast ${kind === 'error' ? 'error' : ''}`;
  element.textContent = message;
  $('#toast-region').appendChild(element);
  setTimeout(() => element.remove(), 3600);
}

function taskMatchesFilter(task) {
  if (currentFilter === 'active') return task.alive;
  if (currentFilter === 'attention') return ['error', 'stopped', 'degraded', 'unresponsive'].includes(task.status);
  return true;
}

function taskActions(task) {
  const room = task.room_id;
  const buttons = [
    `<button class="icon-button" data-action="logs" data-room="${room}" title="查看日志" aria-label="查看房间 ${room} 的日志"><i data-lucide="scroll-text"></i></button>`,
    `<button class="icon-button" data-action="folder" data-room="${room}" title="打开录制目录" aria-label="打开房间 ${room} 的录制目录"><i data-lucide="folder-open"></i></button>`
  ];
  if (task.alive) {
    if (['stopping', 'finalizing'].includes(task.status)) {
      buttons.push(`<button class="icon-button danger" data-action="force-stop" data-room="${room}" title="强制结束" aria-label="强制结束房间 ${room}"><i data-lucide="octagon-x"></i></button>`);
    } else {
      buttons.push(`<button class="icon-button" data-action="restart" data-room="${room}" title="重启进程" aria-label="重启房间 ${room}"><i data-lucide="rotate-cw"></i></button>`);
      buttons.push(`<button class="icon-button danger" data-action="stop" data-room="${room}" title="停止并合并" aria-label="停止房间 ${room}"><i data-lucide="square"></i></button>`);
    }
  } else {
    buttons.push(`<button class="icon-button" data-action="start" data-room="${room}" title="重新启动" aria-label="启动房间 ${room}"><i data-lucide="play"></i></button>`);
    buttons.push(`<button class="icon-button danger" data-action="remove" data-room="${room}" title="从列表移除" aria-label="移除房间 ${room}"><i data-lucide="trash-2"></i></button>`);
  }
  return buttons.join('');
}

function renderTasks() {
  const tasks = currentData.tasks.filter(taskMatchesFilter);
  $('#task-count').textContent = currentData.tasks.length;
  $('#process-empty').hidden = tasks.length !== 0;
  $('#process-rows').innerHTML = tasks.map((task) => {
    const stats = task.recordings || {};
    const name = stats.creator || `房间 ${task.room_id}`;
    const status = stateLabels[task.status] || task.status;
    const runtime = task.runtime || {};
    const processText = task.pid ? `PID ${task.pid}` : '无运行进程';
    const errorText = task.error ? `<div class="secondary-text">${escapeHtml(task.error)}</div>` : '';
    return `<tr>
      <td><div class="room-name">${escapeHtml(name)}</div><div class="room-id">${task.room_id}</div></td>
      <td><span class="state-label state-${escapeHtml(task.status)}"><span class="status-dot"></span>${escapeHtml(status)}</span>${errorText}</td>
      <td><span class="numeric">${escapeHtml(processText)}</span><div class="secondary-text">${task.alive && runtime.lanes ? `通道 ${runtime.lanes_alive || 0}/${runtime.lanes}` : `重启 ${task.restart_count || 0} 次`}</div></td>
      <td><span class="numeric">${stats.sessions || 0} 场 · ${formatBytes(stats.bytes)}</span><div class="secondary-text">完成 ${stats.finalized || 0} 场</div></td>
      <td><span class="numeric">${formatDate(task.started_at)}</span><div class="secondary-text">${task.alive ? '进程运行中' : formatDate(task.stopped_at)}</div></td>
      <td><div class="row-actions">${taskActions(task)}</div></td>
    </tr>`;
  }).join('');
}

function renderLibrary() {
  const history = currentData.presets || currentData.history || [];
  $('#library-empty').hidden = history.length !== 0;
  $('#library-rows').innerHTML = history.map((item) => `<tr>
    <td><div class="room-name">${escapeHtml(item.creator || `房间 ${item.room_id}`)}</div><div class="room-id">${item.room_id}</div></td>
    <td class="numeric">${item.sessions || 0}</td>
    <td class="numeric">${item.finalized || 0}</td>
    <td class="numeric">${formatDuration(item.duration_seconds)}</td>
    <td class="numeric">${formatBytes(item.bytes)}</td>
    <td class="numeric">${formatDate(item.last_activity)}</td>
    <td><div class="row-actions"><button class="icon-button" data-action="folder" data-room="${item.room_id}" title="打开录制目录" aria-label="打开房间 ${item.room_id} 的录制目录"><i data-lucide="folder-open"></i></button></div></td>
  </tr>`).join('');
}

function renderPresets() {
  const history = currentData.history || [];
  const field = $('#preset-field');
  const select = $('#room-preset');
  const selected = select.value;
  field.hidden = history.length === 0;
  select.innerHTML = '<option value="">从历史录制中选择</option>' + history.map((item) => {
    const name = item.creator || `房间 ${item.room_id}`;
    return `<option value="${item.room_id}">${escapeHtml(name)} · ${item.room_id} · ${item.sessions || 0} 场</option>`;
  }).join('');
  if (history.some((item) => String(item.room_id) === selected)) {
    select.value = selected;
  } else {
    $('#preset-description').textContent = '';
  }
}

function render() {
  const summary = currentData.summary || {};
  $('#metric-processes').textContent = summary.processes || 0;
  $('#metric-active').textContent = summary.active || 0;
  $('#metric-recording').textContent = summary.recording || 0;
  $('#metric-sessions').textContent = summary.sessions || 0;
  $('#metric-storage').textContent = formatBytes(summary.bytes);
  const diskFree = Number(summary.disk_free || 0);
  const diskTotal = Number(summary.disk_total || 0);
  const lowDisk = diskFree > 0 && (diskFree < 20 * 1024 ** 3 || (diskTotal && diskFree / diskTotal < 0.05));
  $('#metric-disk-free').textContent = diskFree ? `可用 ${formatBytes(diskFree)}` : '';
  $('#metric-disk-free').classList.toggle('warning', lowDisk);
  $('#last-updated').textContent = `更新于 ${formatDate(currentData.updated_at)}`;
  $('#recordings-path').textContent = currentData.recordings_dir || '';
  renderTasks();
  renderLibrary();
  renderPresets();
  if (window.lucide) window.lucide.createIcons();
}

async function loadStatus(silent = false) {
  const refreshIcon = $('#refresh-data svg');
  if (!silent && refreshIcon) refreshIcon.classList.add('spin');
  try {
    currentData = await api('/api/status');
    render();
  } catch (error) {
    if (!silent) toast(error.message, 'error');
  } finally {
    if (refreshIcon) refreshIcon.classList.remove('spin');
  }
}

async function runAction(room, action) {
  const labels = { start: '进程已启动', stop: '正在优雅停止并合并录制', restart: '正在重启', remove: '任务已移除', folder: '已打开录制目录', 'force-stop': '进程已强制结束' };
  if (action === 'stop' && !confirm(`停止房间 ${room}？当前录制会先完成合并。`)) return;
  if (action === 'restart' && !confirm(`重启房间 ${room}？当前录制会先完成合并。`)) return;
  if (action === 'force-stop' && !confirm(`强制结束房间 ${room}？未完成的录制可能不会生成成品音频。`)) return;
  if (action === 'remove' && !confirm(`从进程列表移除房间 ${room}？录制文件不会被删除。`)) return;
  try {
    await api(`/api/tasks/${room}/${action === 'folder' ? 'open-folder' : action}`, { method: 'POST', body: '{}' });
    toast(labels[action] || '操作已完成');
    await loadStatus(true);
  } catch (error) { toast(error.message, 'error'); }
}

async function openLogs(room) {
  currentLogRoom = room;
  $('#log-title').textContent = `房间 ${room}`;
  $('#log-output').textContent = '正在读取日志…';
  $('#drawer-backdrop').hidden = false;
  $('#log-drawer').classList.add('open');
  $('#log-drawer').setAttribute('aria-hidden', 'false');
  await refreshLogs();
}

async function refreshLogs() {
  if (!currentLogRoom) return;
  try {
    const data = await api(`/api/tasks/${currentLogRoom}/logs?lines=240`);
    $('#log-path').textContent = data.path;
    $('#log-status').textContent = `${data.lines.length} 行`;
    $('#log-output').textContent = data.lines.join('\n') || '暂无日志';
    $('#log-output').scrollTop = $('#log-output').scrollHeight;
  } catch (error) { $('#log-output').textContent = error.message; }
}

function closeLogs() {
  currentLogRoom = null;
  $('#log-drawer').classList.remove('open');
  $('#log-drawer').setAttribute('aria-hidden', 'true');
  setTimeout(() => { $('#drawer-backdrop').hidden = true; }, 210);
}

$('#add-room-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('#room-id');
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  const roomId = input.value.trim();
  $('#room-error').textContent = '';
  if (!/^[1-9][0-9]{0,14}$/.test(roomId)) {
    $('#room-error').textContent = '请输入 1 到 15 位正整数房间 ID';
    input.focus();
    return;
  }
  submit.disabled = true;
  try {
    await api('/api/tasks', { method: 'POST', body: JSON.stringify({ room_id: roomId }) });
    input.value = '';
    $('#room-preset').value = '';
    $('#preset-description').textContent = '';
    toast(`房间 ${roomId} 的录制进程已创建`);
    await loadStatus(true);
  } catch (error) {
    $('#room-error').textContent = error.message;
  } finally { submit.disabled = false; }
});

document.addEventListener('click', (event) => {
  const actionButton = event.target.closest('[data-action]');
  if (actionButton) {
    const { action, room } = actionButton.dataset;
    if (action === 'logs') openLogs(room); else runAction(room, action);
  }
});

$$('.tab-button').forEach((button) => button.addEventListener('click', () => {
  $$('.tab-button').forEach((item) => { item.classList.toggle('active', item === button); item.setAttribute('aria-selected', item === button ? 'true' : 'false'); });
  $$('.view-panel').forEach((panel) => { const active = panel.id === `${button.dataset.view}-view`; panel.classList.toggle('active', active); panel.hidden = !active; });
}));

$$('.filter-button').forEach((button) => button.addEventListener('click', () => {
  currentFilter = button.dataset.filter;
  $$('.filter-button').forEach((item) => item.classList.toggle('active', item === button));
  renderTasks();
  if (window.lucide) window.lucide.createIcons();
}));

$('#open-recordings').addEventListener('click', async () => {
  try { await api('/api/recordings/open', { method: 'POST', body: '{}' }); toast('已打开录制目录'); }
  catch (error) { toast(error.message, 'error'); }
});
$('#refresh-data').addEventListener('click', () => loadStatus());
$('#empty-add').addEventListener('click', () => $('#room-id').focus());
$('#close-logs').addEventListener('click', closeLogs);
$('#drawer-backdrop').addEventListener('click', closeLogs);
$('#refresh-logs').addEventListener('click', refreshLogs);
$('#room-preset').addEventListener('change', (event) => {
  const roomId = event.currentTarget.value;
  const preset = (currentData.presets || currentData.history || []).find((item) => String(item.room_id) === roomId);
  if (!preset) {
    $('#preset-description').textContent = '';
    return;
  }
  $('#room-id').value = String(preset.room_id);
  $('#room-error').textContent = '';
  $('#preset-description').textContent = `历史录制 ${preset.sessions || 0} 场，最近活动 ${formatDate(preset.last_activity)}`;
  $('#room-id').focus();
});
document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && currentLogRoom) closeLogs(); });

if (window.lucide) window.lucide.createIcons();
loadStatus();
pollTimer = setInterval(() => loadStatus(true), 3000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) loadStatus(true);
});
