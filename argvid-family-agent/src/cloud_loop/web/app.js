/* The page renders server facts; it owns no task state or execution outcomes. */
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;'
} [c]));
const state = {
  profile: null,
  catalog: [],
  binding: null,
  scenario: null,
  task: null,
  taskId: null,
  events: new Map(),
  stream: null,
  status: null
};
const labels = {
  queued: '排队中',
  running: '执行中',
  waiting: '等待条件',
  completed: '已完成',
  unsupported: '缺少能力',
  failed: '执行失败',
  needs_input: '需要处理',
  cancelled: '已取消',
  expired: '已过期'
};
const titles = {
  'task.created': '收到用户任务',
  'task.accepted': '端侧接受任务',
  'agent.started': '恢复 Agent 会话',
  'agent.finished': '本轮结束',
  'model.request': '调用真实模型',
  'tool.started': '调用工具',
  'tool.completed': '工具返回',
  'tool.failed': '工具失败',
  'bridge.invoke': '发送补采请求',
  'bridge.accepted': '端侧已接受',
  'bridge.completed': '收到完成事件',
  'bridge.final': '当次返回最终结果',
  'asset.verified': '素材校验通过',
  'media.rendered': '视频已生成',
  'task.outcome': '登记本轮结果',
  'tool.move': '模拟观察位移动',
  'tool.capture': '回放素材已产生',
  'bridge.sent': '回执已发送',
  'bridge.send_failed': '发送失败，等待重试',
  'input.transcript': '收到模拟语音文本',
  'task.paused': '端侧已暂停',
  'task.resumed': '端侧已恢复',
  'control.queued': '输入已排队',
  'control.result': '端侧输入确认'
};

async function api(url, options = {}) {
  const r = await fetch(url, {
    ...options,
    headers: {
      ...(options.body ? {
        'Content-Type': 'application/json'
      } : {}),
      ...options.headers
    }
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || (typeof data.detail === 'string' ? data.detail : JSON
    .stringify(data.detail)) || `HTTP ${r.status}`);
  return data;
}
let noticeTimer;

function notice(message, error = false) {
  clearTimeout(noticeTimer);
  $('notice').hidden = false;
  $('notice').className = error ? 'error' : '';
  $('notice').textContent = message;
  if (!error) noticeTimer = setTimeout(() => $('notice').hidden = true, 6000);
}

function safeURL(value) {
  try {
    const u = new URL(value);
    return ['http:', 'https:'].includes(u.protocol) ? u.href : '#';
  } catch {
    return '#';
  }
}

function tab(name) {
  document.querySelectorAll('[data-tab]').forEach(b => b.setAttribute('aria-selected', String(b
    .dataset.tab === name)));
  document.querySelectorAll('.panel').forEach(p => p.hidden = p.id !== `panel-${name}`);
}
document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => tab(b.dataset
  .tab)));

async function status() {
  state.status = await api('/api/status');
  const s = state.status;
  $('connection').textContent = s.node.online ? (s.node.hostname && s.node.hostname === s.cloud
    .hostname ? '已连接 · 同机联调' : '两端已连接') : '端侧未连接';
  $('connection').className = `signal ${s.node.online?'online':''}`;
  $('profileVersion').textContent = `装配 v${s.revision}`;
  $('cloudModel').textContent = state.task?.models?.cloud || s.cloud.model;
  $('edgeModel').textContent = state.task?.models?.edge || state.profile?.edge.model || '等待装配';
  if (s.scene) {
    $('sceneState').textContent = `${s.scene.stage} · 事实版本 ${s.scene.revision} · 设备回放`;
    document.querySelectorAll('[data-stage]').forEach(b => b.classList.toggle('active', b.dataset
      .stage === s.scene.stage));
  }
  $('startTask').disabled = !s.node.online || !s.prepared || s.node.profile_digest !== s.cloud
    .profile_digest;
  const inputReady = s.node.online && (s.node.capabilities || []).includes('inputs');
  $('mockPause').disabled = !inputReady || !state.taskId;
  $('resumeEdge').disabled = !inputReady || !state.taskId;
  $('motionFault').disabled = !inputReady;
  document.querySelectorAll('[data-stage]').forEach(b => b.disabled = !s.node.online);
  const controls = await api('/api/edge/controls');
  if (controls.length) {
    const c = controls[0];
    $('controlStatus').textContent =
      `${c.id.slice(0,8)} · ${c.result?(c.result.status==='applied'?'端侧已应用':'端侧拒绝，查看协议记录'):'等待端侧确认'}`;
  }
  $('updatedAt').textContent = `状态更新 ${new Date().toLocaleTimeString('zh-CN')}`;
}
async function refreshTasks() {
  const tasks = await api('/tasks');
  const current = state.taskId;
  $('taskSelect').innerHTML = '<option value="">选择任务</option>' + tasks.map(t =>
    `<option value="${esc(t.id)}">${esc(labels[t.status]||t.status)} · ${esc(t.prompt.slice(0,22))} · ${esc(t.id.slice(0,8))}</option>`
    ).join('');
  if (current) $('taskSelect').value = current;
  return tasks;
}
async function selectTask(id) {
  if (state.stream) state.stream.close();
  state.stream = null;
  state.events.clear();
  state.taskId = id || null;
  state.task = null;
  if (!id) {
    renderTask();
    return;
  }
  await loadTask();
  const items = await api(`/tasks/${encodeURIComponent(id)}/events`);
  items.forEach(e => state.events.set(e.seq, e));
  renderTimeline();
  const cursor = items.length ? items[items.length - 1].seq : 0;
  const source = new EventSource(`/tasks/${encodeURIComponent(id)}/events?after=${cursor}`);
  state.stream = source;
  source.onmessage = e => {
    if (state.taskId !== id) return;
    const row = JSON.parse(e.data);
    state.events.set(row.seq, row);
    renderTimeline();
  };
  source.onerror = () => {
    $('updatedAt').textContent = '事件连接重试中；任务状态仍由服务端保存';
  };
  sessionStorage.setItem('cix-selected-task', id);
}
async function loadTask() {
  if (!state.taskId) return;
  state.task = await api(`/tasks/${encodeURIComponent(state.taskId)}`);
  renderTask();
  renderAssets();
  renderProtocol();
}

function renderTask() {
  const t = state.task;
  $('taskStatus').textContent = t ? (labels[t.status] || t.status) : '尚未启动';
  $('taskStatus').className = `status-label ${t?.status||''}`;
  if (t?.models) {
    $('cloudModel').textContent = t.models.cloud;
    $('edgeModel').textContent = t.models.edge;
  }
  const answer = t?.outcome?.summary || t?.answer || '';
  $('taskSummary').textContent = t ? (t.status === 'completed' ?
    '视频已生成，前往「素材与产物」播放或下载。实际来源与检查结果一同保留。' : answer.slice(0, 160) ||
    `任务 ${t.id.slice(0,8)} · 等待实际执行回执`) : '提交任务后，这里展示两侧实际产生的事件。';
  $('taskReport').hidden = !answer;
  $('taskAnswer').textContent = answer;
  $('cancelTask').disabled = !t || ['completed', 'unsupported', 'cancelled', 'expired'].includes(t
    .status);
}

function renderTimeline() {
  const selected = [...state.events.values()];
  if (!selected.length) return;
  const visible = new Set(['task.created', 'task.accepted', 'agent.started', 'agent.finished',
    'bridge.invoke', 'bridge.accepted', 'bridge.completed', 'bridge.final', 'asset.verified',
    'media.rendered', 'task.outcome', 'tool.move', 'tool.capture', 'tool.failed',
    'bridge.send_failed', 'input.transcript', 'task.paused', 'task.resumed', 'control.queued',
    'control.result'
  ]);
  const stages = [
    ['受理', selected.some(e => e.kind === 'task.created')],
    ['委托', selected.some(e => e.kind === 'bridge.invoke')],
    ['现场判断', selected.some(e => e.kind === 'edge.agent.started')],
    ['执行', selected.some(e => e.kind === 'edge.tool.capture')],
    ['回传', selected.some(e => ['bridge.completed', 'bridge.final'].includes(e.kind))],
    ['成片', selected.some(e => e.kind === 'media.rendered')]
  ];
  $('flowSummary').innerHTML = stages.map(([label, done], i) =>
    `<span class="${done?'observed':''}"><small>${String(i+1).padStart(2,'0')}</small>${label}</span>`
    ).join('');
  const lanes = {
    cloud: [],
    edge: []
  };
  const open = new Set([...$('timeline').querySelectorAll('details[open]')].map(x => x.dataset
  .seq));
  const scrolls = [...$('timeline').querySelectorAll('.event-lane')].map(x => ({
    top: x.scrollTop,
    end: x.scrollHeight - x.scrollTop - x.clientHeight < 35
  }));
  selected.forEach(e => {
    const edge = e.kind.startsWith('edge.');
    const x = edge ? e.payload : e;
    const kind = edge ? x.kind : e.kind;
    const p = edge ? x.payload : e.payload;
    if (!visible.has(kind)) return;
    const detail = p?.name || p?.summary || p?.status || p?.model || p?.error_type || '';
    const time = new Date((x.created || e.created) * 1000).toLocaleTimeString('zh-CN');
    lanes[edge ? 'edge' : 'cloud'].push(
      `<article class="event ${edge?'edge':'cloud'}"><div class="event-top"><b>${esc(titles[kind]||kind)}</b><time>${esc(time)}</time></div><p>${esc(detail)}</p><details data-seq="${e.seq}" ${open.has(String(e.seq))?'open':''}><summary>查看回执</summary><pre>${esc(JSON.stringify(p,null,2))}</pre></details></article>`
      );
  });
  $('timeline').innerHTML = ['cloud', 'edge'].map(side =>
      `<div class="event-lane">${lanes[side].join('')||'<p class="muted">尚无这一侧的任务事件</p>'}</div>`)
    .join('');
  [...$('timeline').querySelectorAll('.event-lane')].forEach((x, i) => {
    x.scrollTop = !scrolls[i] || scrolls[i].end ? x.scrollHeight : scrolls[i].top;
  });
}
$('taskForm').addEventListener('submit', async e => {
  e.preventDefault();
  $('startTask').disabled = true;
  try {
    const t = await api('/tasks', {
      method: 'POST',
      body: JSON.stringify({
        ...state.scenario,
        prompt: $('prompt').value,
        completion_mode: $('mode').value
      })
    });
    notice('任务已提交，由云端 Agent 开始判断。');
    await refreshTasks();
    await selectTask(t.id);
  } catch (error) {
    notice(error.message, true);
  } finally {
    await status().catch(() => {});
  }
});
$('taskSelect').addEventListener('change', () => selectTask($('taskSelect').value).catch(e =>
  notice(e.message, true)));
$('cancelTask').addEventListener('click', async () => {
  try {
    await api(`/tasks/${state.taskId}/cancel`, {
      method: 'POST'
    });
    await loadTask();
    notice('取消请求已记录，端侧会在执行前复查。');
  } catch (e) {
    notice(e.message, true);
  }
});
async function edgeControl(body) {
  const result = await api('/api/edge/controls', {
    method: 'POST',
    body: JSON.stringify(body)
  });
  $('controlStatus').textContent = `已提交 ${result.id.slice(0,8)}，等待端侧确认。`;
  notice('输入已排队，端侧应用后才会显示为生效。');
}
document.querySelectorAll('[data-stage]').forEach(b => b.addEventListener('click', () =>
  edgeControl({
    kind: 'stage',
    stage: b.dataset.stage
  }).catch(e => notice(e.message, true))));
$('mockPause').addEventListener('click', () => {
  if (!state.taskId) return notice('请先选择已有任务。', true);
  edgeControl({
    kind: 'transcript',
    task_id: state.taskId,
    text: '暂停当前拍摄任务'
  }).catch(e => notice(e.message, true));
});
$('resumeEdge').addEventListener('click', () => {
  if (!state.taskId) return notice('请先选择已有任务。', true);
  edgeControl({
    kind: 'resume',
    task_id: state.taskId
  }).catch(e => notice(e.message, true));
});
$('motionFault').addEventListener('change', () => edgeControl({
  kind: 'motion',
  motion_outcome: $('motionFault').value
}).catch(e => notice(e.message, true)));

function renderComposer() {
  $('composer').innerHTML = ['cloud', 'edge'].map(side => {
    const p = state.profile[side];
    const catalog = state.catalog.filter(x => [side, 'both'].includes(x.side));
    return `<section class="compose-side ${side}"><header><b>${side==='cloud'?'云端':'端侧'}</b><span>${side.toUpperCase()} HARNESS</span></header><label for="model-${side}">生成模型</label><select id="model-${side}">${[...new Set([p.model,"DeepSeek-V4-Pro","DeepSeek-V4-Flash"])].map(m=>`<option ${p.model===m?"selected":""}>${esc(m)}</option>`).join("")}</select><div class="plugin-options">${catalog.map(x=>`<label class="plugin-option ${x.implemented?'':'unavailable'}"><input type="checkbox" data-side="${side}" data-plugin="${esc(x.id)}" ${p.plugins.includes(x.id)?'checked':''} ${x.implemented?'':'disabled'}><span><b>${esc(x.name)}</b><small>${esc(x.provides.join(' · '))}${x.requires.length?`<br>依赖 ${esc(x.requires.join(' · '))}`:''}</small></span><em>${x.implemented?esc(x.slot):'待接入'}</em></label>`).join('')}</div><p class="bound-tools">当前配置工具：${esc(state.binding[side].tools.join(' · '))}</p></section>`;
  }).join('');
  $('validation').className = 'validation';
  $('validation').textContent = '当前配置已通过后端依赖校验；每轮运行还会核对实际加载的工具。';
}

function draftProfile() {
  const p = structuredClone(state.profile);
  p.revision++;
  for (const side of ['cloud', 'edge']) {
    p[side].model = $(`model-${side}`).value;
    p[side].plugins = [...document.querySelectorAll(`input[data-side="${side}"]:checked`)].map(x =>
      x.dataset.plugin);
  }
  return p;
}
async function validateDraft() {
  const p = draftProfile();
  const result = await api(`/api/profiles/${encodeURIComponent(p.id)}/validate`, {
    method: 'POST',
    body: JSON.stringify(p)
  });
  $('validation').className = `validation ${result.valid?'':'invalid'}`;
  $('validation').textContent = result.valid ? '装配合法，可以保存。当前运行中的任务不会被改变。' : Object.values(result
    .bindings).flatMap(x => x.errors).join('；');
  return {
    p,
    result
  };
}
$('validateProfile').addEventListener('click', () => validateDraft().catch(e => notice(e.message,
  true)));
$('saveProfile').addEventListener('click', async () => {
  try {
    const {
      p,
      result
    } = await validateDraft();
    if (!result.valid) return;
    await api('/api/profiles', {
      method: 'POST',
      body: JSON.stringify(p)
    });
    await loadProfile();
    notice('配置已保存，正在等待端侧确认；确认前不能启动新任务。');
    await status();
  } catch (e) {
    notice(e.message, true);
  }
});
async function loadProfile() {
  const [p, c] = await Promise.all([api('/api/profiles'), api('/api/plugins')]);
  state.profile = p.active;
  state.catalog = c.catalog;
  state.binding = c.bindings;
  state.schemas = c.tool_schemas;
  renderComposer();
  renderProtocol();
}

function renderAssets() {
  const assets = state.task?.available_assets || state.task?.assets || [];
  const key = JSON.stringify(assets.map(a => [a.id, a.info.asset.sha256]));
  if ($('assets').dataset.renderKey === key) return;
  $('assets').dataset.renderKey = key;
  if (!assets.length) {
    $('assets').innerHTML =
      '<div class="empty-state"><span>▧</span><h3>尚无任务产物</h3><p>端侧产出素材、云端完成合成后，文件会出现在这里。</p></div>';
    return;
  }
  $('assets').innerHTML = assets.map(item => {
    const i = item.info,
      a = i.asset,
      p = i.provenance || {};
    return `<article class="asset"><video controls preload="metadata" playsinline src="/assets/${encodeURIComponent(item.id)}"></video><div class="asset-body"><h3>${item.kind==='deliverable'?'最终成片':item.kind==='wide'?'已有全景':'端侧近景回放'}</h3><div class="asset-meta"><span>${esc(a.width)} × ${esc(a.height)}</span><span>${esc((a.duration_ms/1000).toFixed(2))} s</span><span>${esc(a.fps)} fps</span></div><p>实拍素材回放 · 非本次现场采集</p>${p.capture_operation?`<p>${p.capture_operation==='extract_from_original_video'?'端侧从原片实际截取':'预裁片段回放'}</p>`:''}<p>${esc(p.author||'')} · <a href="${esc(safeURL(p.license_url))}" target="_blank" rel="noreferrer">${esc(p.license||'查看许可')}</a></p><p>校验值 ${esc(a.sha256.slice(0,20))}…</p><div class="asset-links"><a href="/assets/${encodeURIComponent(item.id)}" download>下载视频 ↗</a><a href="/assets/${encodeURIComponent(item.id)}?format=manifest" target="_blank">素材清单 ↗</a><a href="${esc(safeURL(p.source_page))}" target="_blank" rel="noreferrer">原始来源 ↗</a></div></div></article>`;
  }).join('');
}

function renderProtocol() {
  const records = [
    ['装配配置', state.profile],
    ['用户任务', state.task ? {
      prompt: state.task.prompt,
      context: state.task.context
    } : state.scenario],
    ['模型工具定义', state.schemas],
    ['远程任务请求与结果', state.task?.edge_tasks || []],
    ['持久事件与模型运行', state.task ? {
      events: state.task.events,
      runs: state.task.runs
    } : {}]
  ];
  if (!$('protocol').children.length) $('protocol').innerHTML = records.map(([label]) =>
    `<details><summary>${label}</summary><pre></pre></details>`).join('');
  records.forEach(([, value], i) => {
    const pre = $('protocol').children[i].querySelector('pre');
    const text = JSON.stringify(value, null, 2);
    if (pre.textContent !== text) pre.textContent = text;
  });
}
async function boot() {
  await loadProfile();
  state.scenario = await api('/api/scenario');
  $('prompt').value = state.scenario.prompt;
  await status();
  const tasks = await refreshTasks();
  const selected = sessionStorage.getItem('cix-selected-task');
  if (selected && tasks.some(t => t.id === selected)) await selectTask(selected);
  else if (tasks.length) await selectTask(tasks[0].id);
  setInterval(async () => {
    try {
      await status();
      await refreshTasks();
      await loadTask();
    } catch (e) {
      $('connection').textContent = '连接中断';
      $('connection').className = 'signal';
    }
  }, 3000);
}
boot().catch(e => notice(`工作台连接失败：${e.message}`, true));