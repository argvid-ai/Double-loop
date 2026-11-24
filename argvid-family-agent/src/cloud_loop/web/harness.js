'use strict';
const $ = id => document.getElementById(id),
  el = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
let savedSelection = {};
try {
  savedSelection = JSON.parse(localStorage.getItem('muse-workbench-selection') || '{}');
} catch {}
const state = {
  side: 'cloud',
  conversation: savedSelection.side === 'cloud' && savedSelection.uiVersion === 3 ? savedSelection
    .conversation || null : null,
  newWork: false,
  snapshots: {},
  events: [],
  cursor: null,
  historyLoaded: false,
  loadingOlder: false,
  trace: null,
  source: null,
  busy: false,
  refreshing: false,
  signatures: {},
  connections: {
    edge: {
      status: 'connecting'
    },
    cloud: {
      status: 'connecting'
    }
  },
  drafts: {},
  pending: {}
};
try {
  const saved = JSON.parse(sessionStorage.getItem('muse-drafts') || '{}');
  state.drafts = saved.drafts || {};
  state.pending = saved.pending || {};
} catch {}
const persistDrafts = () => {
  try {
    sessionStorage.setItem('muse-drafts', JSON.stringify({
      drafts: state.drafts,
      pending: state.pending
    }));
  } catch {}
};
const labels = {
  idle: '等待输入',
  ready: '待处理',
  waiting: '等待条件',
  waiting_peer: '等待对侧',
  running: '处理中',
  paused: '已暂停',
  cancelled: '已取消',
  expired: '已过期',
  completed: '已完成',
  failed: '失败',
  queued: '排队',
  done: '已回复',
  superseded: '已被新状态取代'
};
const taskLabel = t => t?.status === 'ready' && t.result?.review_status === 'pending' ? '等待成片复看' :
  t?.status === 'ready' && t.result?.review_status === 'render_failed' ? '制作失败，等待修正' :
  t?.status === 'ready' && t.schedule ? (t.schedule.enabled ? '等待计划执行' :
  '计划已停用') : (labels[t?.status] || t?.status || '准备中');
const sideName = s => s === 'edge' ? '观察服务' : '主助理';
const stamp = v => v ? new Date(typeof v === 'number' ? v * 1000 : v).toLocaleString('zh-CN', {
  hour12: false
}) : '—';
const path = (side, route) => '/workbench/' + side + '/' + route;
const selected = () => state.snapshots[state.side];
const mediaSide = () => state.cameraSide || state.side;
const mediaSnapshot = () => state.snapshots[mediaSide()];
const processingReady = s => Boolean(s?.workers?.length) && s.workers.every(w => !w.done);
const online = side => state.connections[side]?.status === 'online';
const connectionLabel = (connection, snap) => connection.status === 'connecting' ? '连接中' :
  connection.status === 'online' ? (processingReady(snap) ? '在线' : '服务异常') :
  '状态暂未取得';
const writable = () => online(state.side) && processingReady(selected());
const contextKey = () => state.side + ':' + (state.newWork ? 'new' : state.conversation || 'main');
const shortTitle = t => String(t || '新任务').split(/[：，,。\n]/)[0].slice(0, 26);
const taskTitle = t => t?.kind === 'compose' ? '制作' + (t.constraints?.duration_ms ? t.constraints
  .duration_ms / 1000 + '秒' : '') + '短片' : t?.kind === 'capture' ? '采集' + (t.constraints
  ?.duration_ms ? t.constraints.duration_ms / 1000 + '秒' : '') + '片段' : shortTitle(t?.goal);
function videoTitle(title, created, fallback = '视频片段') {
  const clean = String(title || fallback).replace(/(?:task-|record-|media-job-|received-)[\w-]+/g, '')
    .split(/[。；;\n]/)[0].trim().slice(0, 24) || fallback;
  if (created == null) return clean + ' · 日期未记录';
  const date = new Date(typeof created === 'number' ? created * 1000 : created);
  if (!Number.isFinite(date.getTime())) return clean + ' · 日期未记录';
  const pad = value => String(value).padStart(2, '0');
  return clean + ' · ' + date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate());
}
function eventAsset(event) {
  const result = event.reply?.reviewed_result || (event.kind === 'peer.result' && event.body.peer_message?.payload?.result);
  return result?.verified && result.media_manifest &&
    (!result.review_required || result.review_status === 'approved') ? result : null;
}
function messageGroups(events) {
  const groups = [];
  for (const event of events) {
    const previous = groups.at(-1), first = previous?.[0];
    // Only fold adjacent identical completed messages, never hide a film or
    // deduplicate separate user inputs. Every original event stays expandable.
    const same = first && event.status === 'done' && first.status === 'done' &&
      !eventAsset(event) && !eventAsset(first) && event.kind === first.kind;
    const repeatedNotice = same && event.kind === 'task.notification' &&
      event.reply?.text === first.reply?.text &&
      event.body.origin_event_id && event.body.origin_event_id === first.body.origin_event_id;
    const batchInput = same && event.kind === 'user' && event.task_id && first.task_id &&
      event.body.text === first.body.text && Math.abs(event.created - first.created) <= 2 &&
      previous.every(e => e.task_id !== event.task_id);
    if (repeatedNotice || batchInput) previous.push(event);
    else groups.push([event]);
  }
  return groups;
}
const saveDraft = () => {
  state.drafts[contextKey()] = $('prompt').value;
  persistDrafts();
};

function closeDirectory() {
  $('directory').classList.remove('open');
  $('directoryShade').hidden = true;
  $('toggleDirectory').setAttribute('aria-expanded', 'false');
}

function panel(id) {
  pauseMedia();
  closeDirectory();
  $(id).showModal();
  if (id === 'taskDialog') renderTaskDetails();
}


const processingNotice = '后台处理程序未运行；请恢复服务后再发送。已有记录仍可查看，暂停与取消仍可使用。';
const selectedConversation = () => selected()?.conversations.find(c => c.id === state.conversation);
const selectedTask = () => selected()?.tasks.find(t => t.id === selectedConversation()?.task_id);

function notice(text) {
  $('notice').textContent = text;
  $('notice').hidden = !text;
}
async function api(side, route, body, method = 'POST') {
  let response;
  try {
    response = await fetch(path(side, route), {
      ...(body === undefined ? {} : {
        method,
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(body)
      }),
      signal: AbortSignal.timeout(35000)
    });
  } catch {
    throw new Error('连接未成功，已保留输入。请检查服务或稍后重试。');
  }
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.error === 'string' ? data.error : typeof data.detail ===
      'string' ? data.detail : typeof data.detail?.message === 'string' ? data.detail.message :
      '操作未成功，请稍后重试。');
    error.status = response.status;
    throw error;
  }
  return data;
}

function raw(title, data) {
  const d = el('details');
  d.append(el('summary', title), el('pre', JSON.stringify(data, null, 2)));
  return d;
}

function changed(key, data) {
  const s = JSON.stringify(data);
  if (state.signatures[key] === s) return false;
  state.signatures[key] = s;
  return true;
}

function record(title, data) {
  $('dialogTitle').textContent = title;
  $('dialogContent').textContent = JSON.stringify(data, null, 2);
  $('recordDialog').showModal();
}
async function choose(side, id, newWork = false) {
  saveDraft();
  notice('');
  localStorage.setItem('muse-workbench-selection', JSON.stringify({
    side,
    conversation: id,
    uiVersion: 3
  }));
  state.side = side;
  state.conversation = id;
  state.newWork = newWork;
  state.events = [];
  state.cursor = null;
  state.historyLoaded = false;
  state.source = null;
  state.trace = null;
  state.signatures = {};
  $('prompt').value = state.drafts[contextKey()] || '';
  closeDirectory();
  renderDirectory();
  renderHeading();
  renderMessages();
  renderTaskAssets();
  await refreshConversation();
}

function renderDirectory() {
  if (typeof renderRuntimeStatus === 'function') renderRuntimeStatus();
  const key = {
    side: state.side,
    id: state.conversation,
    newWork: state.newWork,
    nodes: Object.fromEntries(Object.entries(state.snapshots).map(([side, v]) => [side, {
      caps: v.capabilities,
      conversations: v.conversations,
      tasks: v.tasks,
      observations: v.observation_tasks,
      workers: v.workers
    }])),
    connections: Object.fromEntries(Object.entries(state.connections).map(([side, v]) => [side, v
      .status
    ]))
  };
  if (!changed('directory', key)) return;
  $('nodes').replaceChildren();
  $('connectionSummary').replaceChildren();
  for (const side of ['edge', 'cloud']) {
    const snap = state.snapshots[side],
      main = snap?.conversations.find(c => c.kind === 'main'),
      c = state.connections[side],
      status = connectionLabel(c, snap);
    const chip = el('span', sideName(side) + ' ' + status, 'connection-dot' + (status === '在线' ?
      '' : ' offline'));
    $('connectionSummary').append(chip);
    if (side !== 'cloud') continue;
    const button = el('button', undefined, 'node' + (state.side === side && state.conversation ===
      main?.id && !state.newWork ? ' active' : ''));
    button.append(el('span', '主对话'), el('span', status, 'connection'));
    button.disabled = !main;
    button.onclick = () => choose(side, main.id);
    $('nodes').append(button);
  }
  const works = ['cloud'].flatMap(side => (state.snapshots[side]?.conversations || []).filter(c => c
    .kind === 'work' && (c.task_id || c.entry_kind === 'user_work' || c.id === state
      .conversation)).map(c => ({
    ...c,
    side
  })));
  const observations = state.snapshots.edge?.observation_tasks || [];
  const total = works.length + observations.length;
  $('taskCount').textContent = total ? String(total) : Object.values(state.connections).every(c => c
    .status === 'online') ? '0' : '—';
  $('tasks').replaceChildren();
  if (!total) $('tasks').append(el('p', Object.values(state.connections).every(c => c.status ===
    'online') ? '还没有任务' : '暂时无法读取任务', 'empty'));
  for (const c of works) {
    const t = state.snapshots[c.side].tasks.find(t => t.id === c.task_id),
      button = el('button', undefined, 'task' + (c.id === state.conversation && c.side === state
        .side ? ' active' : ''));
    button.append(el('b', t ? taskTitle(t) : shortTitle(c.entry_text || '准备中的任务')), el('small',
      sideName(c.side) + ' · ' + (online(c.side) ? (t ? taskLabel(t) : labels[c.status] || c
        .status) : '上次记录')));
    button.title = t?.goal || '新任务';
    button.onclick = () => choose(c.side, c.id);
    $('tasks').append(button);
  }
  for (const task of observations) {
    const button = el('button', undefined, 'task');
    button.append(el('b', String(task.goal).slice(0, 28)), el('small', '持续观察 · ' + ({
      accepted: '未启动',
      starting: '准备中',
      running: '进行中',
      paused: '已暂停',
      stopped: '已停止'
    } [task.status] || '待确认')));
    button.onclick = () => openObservationTask(task.id);
    $('tasks').append(button);
  }
  $('components').replaceChildren();
  for (const side of ['edge', 'cloud']) {
    const snap = state.snapshots[side],
      box = el('section', undefined, 'component-side');
    box.append(el('h3', sideName(side)));
    if (snap) {
      const models = side === 'cloud' ? [snap.capabilities.model] : (snap.capabilities.observation
        ?.catalogue?.models || []).map(m => m.title);
      box.append(el('p', '模型：' + (models.join('、') || '尚未配置观察模型')), el('p', side === 'cloud' ?
        '负责对话、任务安排和整理。' : '负责影音观察、机位控制和片段保留。'));
    } else box.append(el('p', '尚未读取到连接信息'));
    $('components').append(box);
  }
}

function renderHeading() {
  const c = selectedConversation(),
    task = selectedTask(),
    link = state.connections[state.side];
  $('send').disabled = state.busy || !writable();
  $('newWork').disabled = !writable();
  $('sideLabel').textContent = 'MUSE';
  $('conversationTitle').textContent = state.newWork ? '新任务' : c?.kind === 'main' ? '主对话' : task ?
    taskTitle(task) : '对话';
  $('conversationMeta').textContent = state.newWork ? '交代目标、素材和想要的结果' : task ? taskLabel(task) : c ?
    '随时提问，也可以交代一件工作' : '正在读取对话';
  $('inputLabel').textContent = '向' + sideName(state.side) + (state.newWork ? '新任务' : c?.kind ===
    'main' ? '对话' : '当前任务') + '发送';
  $('inputHint').textContent = state.busy ? '正在发送…' : state.pending[contextKey()] ?
    '未收到发送确认；重试使用同一编号' : 'Enter 换行';
  $('send').textContent = state.busy ? '发送中…' : state.pending[contextKey()] ? '重试发送' : '发送 ↑';
  $('connectionNotice').hidden = online(state.side) && processingReady(selected());
  $('connectionText').textContent = link.status === 'connecting' ? '正在连接…' : !online(state.side) ? (
    selected() ? '连接中断，显示上次同步的记录。' : '暂时无法连接服务，输入内容会保留。') : '服务已连接，但处理程序异常；暂不能发送新工作。';
  const preparing = c?.kind === 'work' && !task && state.events.some(e => ['queued', 'running']
    .includes(e.status) && ['user', 'voice'].includes(e.kind));
  $('taskActions').hidden = !task && !preparing;
  $('taskDetail').hidden = !task;
  $('taskState').textContent = task ? taskLabel(task) : preparing ? '正在准备任务…' : '';
  const terminal = task && ['completed', 'cancelled', 'expired'].includes(task.status);
  if (terminal) $('taskActions').hidden = true;
  $('pauseTask').disabled = !task || terminal || !online(state.side) || (task.status === 'paused' &&
    !writable());
  $('cancelTask').disabled = (!task && !preparing) || terminal || !online(state.side);
  $('pauseTask').textContent = task?.status === 'paused' ? '继续' : '暂停';
}

function renderTaskDetails() {
  const task = selectedTask();
  $('taskSummary').replaceChildren();
  if (!task) return;
  $('taskSummary').append(el('p', task.goal), el('p', '状态：' + taskLabel(task)), el('p', '执行侧：' +
    sideName(state.side)));
  renderRules();
}

function message(role, text, event, cls) {
  const n = el('article', undefined, 'message ' + cls),
    h = el('header');
  h.append(el('span', role));
  h.append(el('time', stamp(cls === 'user' ? event.created : event.finished || event.created)));
  n.dataset.eventId = event.id;
  n.append(h);
  if (event.task_id) {
    const task = selected()?.tasks.find(t => t.id === event.task_id);
    if (task) {
      const context = el('button', task.result?.media_manifest?.title || shortTitle(task.goal), 'message-task');
      context.title = task.goal;
      context.onclick = () => {
        const conversation = selected()?.conversations.find(c => c.task_id === task.id);
        if (conversation) choose(state.side, conversation.id);
      };
      n.append(context);
    }
  }
  const technical = /(?:task-|media-job-|received-)[a-z0-9-]{10,}/i.test(String(text || '')),
    askedTechnical = /编号|哈希|hash|调试|messages|\bID\b/i.test((event.body.text || '') + ' ' + (event
      .body.original_question || ''));
  if (cls !== 'user' && (String(text || '').length > 420 || (technical && !askedTechnical))) {
    const d = el('details');
    d.append(el('summary', '展开完整回复'), richText(text));
    let lead = String(text || '').split(/\n\s*\n/)[0];
    if (/(?:task-|media-job-|received-)[a-z0-9-]{10,}/i.test(lead)) lead = '处理结果已返回。';
    n.append(richText(lead.length > 180 ? lead.slice(0, 180) + '…' : lead), d);
  } else n.append(richText(text));
  return n;
}

function renderMessages() {
  $('older').hidden = !state.cursor;
  if (!changed('messages', [state.conversation, state.newWork, state.events,
      selected()?.assets.map(a => [a.id, a.info.title, a.availability])])) return;
  const box = $('messages'),
    scroll = $('chatScroll'),
    bottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 90;
  pauseMedia(box);
  box.replaceChildren();
  if (!state.events.length) {
    const empty = el('div', undefined, 'welcome');
    empty.append(el('h2', state.newWork ? '从一件想做的事开始' : '你好，我是 Muse'), el('p', state.newWork ?
      '比如：用这段素材做一个简短视频。' : '直接提问，或告诉我想拍什么、做什么。'));
    box.append(empty);
  }
  for (const group of messageGroups(state.events)) {
    let target = box;
    if (group.length > 1) {
      const fold = el('details', undefined, 'message-group');
      fold.append(el('summary', group[0].kind === 'user' ?
        `相同指令 · ${group.length}个不同任务：${group[0].body.text}` :
        `同一事项的${group.length}次通知：${group[0].reply.text}`));
      box.append(fold);
      target = fold;
    }
    for (const e of group) {
    const user = ['user', 'voice'].includes(e.kind);
    if (user) target.append(message('你', e.body.text, e, 'user'));
    if (e.reply && e.status !== 'superseded') {
      const delivered = eventAsset(e);
      if (delivered) {
        const asset = selected()?.assets.find(a => a.id === delivered.asset_id);
        const facts = delivered.media_manifest.asset || {},
          card = message('视频交付', (facts.duration_ms ? facts.duration_ms / 1000 + '秒视频' : '视频') +
            '已完成。' + (delivered.simulated ? '使用文件素材制作，未进行现场拍摄。' : ''), e, 'assistant');
        card.insertBefore(el('h3', videoTitle(asset?.info?.title || delivered.media_manifest.title || (/^#{1,6}\s+([^\n]+)/m.exec(e.reply.text || "")?.[1] || /^\*\*\[([^\]\n]+)\]\(cloud-asset:\/\/[^)]+\)\*\*/.exec(e.reply.text || "")?.[1]),
          asset?.created || e.finished || e.created, '剪辑短片')), card.children[1]);
        const detail = el('details');
        detail.append(el('summary', '制作说明'), richText(e.reply.text));
        card.append(detail);
        if (assetUnavailable(asset)) card.append(el('p', assetAvailabilityText(asset), 'muted'));
        else {
          const video = el('video');
          browserVideo(video, path(delivered.asset_side || state.side, 'assets/' + encodeURIComponent(delivered.asset_id)));
          card.append(video);
        }
        target.append(card);
      } else target.append(message(e.reply.model_invoked === false ? '任务更新' : 'Muse', e.reply.text, e,
        'assistant'));
    } else if (user && ['queued', 'running', 'failed'].includes(e.status)) target.append(el('p', ({
      queued: '已收到，正在等待处理…',
      running: '正在处理…',
      failed: '这次处理未完成，请稍后重试。'
    })[e.status], 'progress-note'));
    }
  }
  if (bottom) scroll.scrollTop = scroll.scrollHeight;
  $('older').hidden = !state.cursor;
}

function renderTaskAssets() {
  const task = selectedTask(),
    result = task?.result,
    refs = [];
  if (task) {
    for (const asset of selected()?.assets || [])
      if (asset.business_task_id === task.id || asset.task_id === task.id || asset.id === result
        ?.asset_id) refs.push({
        side: state.side,
        asset
      });
    if (result?.asset_id && result.asset_side && result.asset_side !== state.side && !refs.length) {
      refs.push({
        side: result.asset_side,
        asset: {
          id: result.asset_id,
          info: result.media_manifest || {},
          kind: 'remote'
        }
      });
    }
  }
  if (!changed('taskAssets', [state.side, state.conversation, refs])) return;
  const box = $('taskAssets');
  pauseMedia(box);
  box.replaceChildren();
  box.hidden = !refs.length;
  const history = el('details'),
    historyLabel = el('summary', '此前成果与素材');
  history.append(historyLabel);
  let olderCount = 0;
  for (const {
      side,
      asset
    }
    of refs) {
    const card = el('article', undefined, 'asset'),
      facts = asset.info.asset || {};
    const url = path(side, 'assets/' + encodeURIComponent(asset.id));
    const title = assetAvailabilityText(asset) || (asset.job_version != null && asset.job_version !== task?.version ? '此前版本的视频' :
      asset.kind === 'capture' && task?.kind === 'compose' ? '已采集素材' :
      asset.id === result?.asset_id && result?.review_required && result?.review_status !== 'approved' ? '制作完成，等待复看' : '视频已就绪');
    card.append(el('h3', videoTitle(asset.info.title, asset.created, asset.kind === 'observation_input' ? '观察候选片段' : '剪辑短片')), el('p', title));
    if (!assetUnavailable(asset)) {
      const v = el('video'), link = el('a', '打开原视频 ↗');
      browserVideo(v, url);
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener';
      card.append(v, el('p', (facts.duration_ms ? facts.duration_ms / 1000 + ' 秒 · ' : '') +
        (asset.info.simulated ? '文件回放制作' : '已核验文件')), link);
    }
    card.append(raw('视频来源与规格', asset.info));
    const older = asset.job_version != null && asset.job_version !== task?.version,
      intermediate = asset.kind === 'capture' && task?.result?.operation === 'compose';
    if (older || intermediate) {
      olderCount++;
      history.append(card);
    } else box.append(card);
  }
  if (olderCount) {
    historyLabel.textContent = '此前成果与素材（' + olderCount + '）';
    box.append(history);
  }
}

async function refreshConversation() {
  if (!state.conversation) {
    renderMessages();
    return;
  }
  const side = state.side,
    id = state.conversation;
  try {
    const page = await api(side, 'conversations/' + encodeURIComponent(id) + '?limit=30');
    if (side !== state.side || id !== state.conversation) return;
    const key = contextKey(),
      pending = state.pending[key];
    if (pending && page.events.some(e => e.id === pending.body.event_id && e.body.text === pending
        .body.text)) {
      delete state.pending[key];
      if ($('prompt').value === pending.body.text) {
        $('prompt').value = '';
        delete state.drafts[key];
      }
      persistDrafts();
      notice('');
      renderHeading();
    }
    const merged = new Map(state.events.map(e => [e.id, e]));
    for (const e of page.events) merged.set(e.id, e);
    state.events = [...merged.values()].sort((a, b) => a.sequence - b.sequence);
    if (!state.historyLoaded) {
      state.cursor = page.next_before_seq;
      state.historyLoaded = true;
    }
    renderMessages();
  } catch (e) {
    notice(e.message);
  }
}

function renderMedia() {
  const s = mediaSnapshot();
  if (!s || !$('sceneDialog').open) return;
  const sourceKey = [mediaSide(), state.source, s.sources.map(x => [x.source_id, x.facts.sha256])];
  if (state.cameraId && state.cameraKind === 'file_replay' && changed('sources', sourceKey)) {
    $('sourceSelect').replaceChildren(el('option', '选择源文件'));
    $('sourceSelect').firstChild.value = '';
    for (const source of s.sources) {
      const option = el('option', source.title);
      option.value = source.source_id;
      $('sourceSelect').append(option);
    }
    $('sourceSelect').value = state.source || '';
    showSource();
  }
  if (changed('scene', s.scene)) {
    $('scene').replaceChildren();
    if (!s.scene) $('scene').append(el('p', '此节点没有安装现场输入组件。'));
    else {
      for (const [label, value] of [
          ['用户在场', s.scene.user_present],
          ['主体可见', s.scene.subject_visible],
          ['设备许可', s.scene.safe_to_execute],
          ['机位', s.scene.viewpoint],
          ['当前允许执行', s.scene.conditions_currently_allow]
        ]) $('scene').append(el('p', label + '：' + (typeof value === 'boolean' ? (value ? '是' :
        '否') : value ?? '未知') + (label !== '当前允许执行' && s.scene.observation_status?.[(['设备许可',
        '机位'
      ].includes(label) ? 'robot' : 'scene')] === 'expired' ? '（观测已过期）' : '')));
      $('scene').append(raw('观测来源、时间与有效期', s.scene));
    }
  }

}

function showSource() {
  state.mediaUpdating = true;
  const source = mediaSnapshot()?.sources.find(s => s.source_id === state.source),
    video = $('sourceVideo');
  video.dataset.side = mediaSide();
  video.dataset.source = state.source || '';
  video.hidden = !source;
  $('frameImage').hidden = true;
  $('frameInfo').textContent = '';
  if (!source) {
    video.removeAttribute('src');
    $('sourceInfo').textContent = '登记来源后可预览、跳转和取帧。';
    return;
  }
  state.previewTarget = {
    side: mediaSide(),
    id: source.source_id,
    position: source.playback.position_ms,
    playing: source.playback.playing,
    received: performance.now(),
    duration: source.facts.duration_ms
  };
  browserVideo(video, path(mediaSide(), 'sources/' + encodeURIComponent(source.source_id) +
    '/video'));
  $('sourceInfo').textContent = source.source_kind + ' · ' + source.license + ' · ' + (source.facts
    .duration_ms / 1000).toFixed(2) + ' 秒。' + (source.author || '') + '；文件回放，未进行视觉判断。';
  $('position').value = (source.playback.position_ms / 1000).toFixed(3);
}

function renderRules() {
  const s = selected(),
    task = selectedTask();
  if (!s) return;
  const rules = s.rules.filter(r => r.task_id === task?.id);
  $('rules').replaceChildren();
  if (!rules.length) $('rules').append(el('p', '没有定时计划。', 'muted'));
  for (const rule of rules) {
    const task = s.tasks.find(t => t.id === rule.task_id),
      n = el('article', undefined, 'rule');
    n.append(el('b', task?.goal || rule.task_id), el('p', rule.enabled ? '下次执行：' + stamp(rule
      .next_due) + ' · ' + rule.spec.timezone : '计划已停用，不会自动执行'), el('p', '最近一次：' + (rule
      .last_occurrence ? ({
        done: '已执行',
        queued: '等待执行',
        running: '执行中',
        skipped: '已跳过',
        failed: '未完成'
      })[rule.last_occurrence.status] || '已有记录' : '尚未到期')));
    for (const action of [rule.enabled ? 'disable' : 'enable', 'delete']) {
      const b = el('button', ({
        disable: '停用',
        enable: '启用',
        delete: '删除规则'
      })[action]);
      b.onclick = async () => {
        try {
          await api(state.side, 'rules/control', {
            task_id: rule.task_id,
            action,
            expected_revision: rule.revision
          });
          await refresh();
        } catch (e) {
          notice(e.message);
        }
      };
      n.append(b);
    }
    n.append(raw('规则与最近执行', rule));
    $('rules').append(n);
  }
}
async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    await Promise.all(['edge', 'cloud'].map(async side => {
      try {
        const snap = await api(side, 'snapshot');
        state.snapshots[side] = snap;
        state.connections[side] = {
          status: 'online',
          lastSeen: Date.now()
        };
      } catch (e) {
        state.connections[side] = {
          ...state.connections[side],
          status: 'unknown',
          error: e.message
        };
      }
    }));
    if (state.conversation && online(state.side) && selected() && !selected().conversations.some(
        c => c.id === state.conversation)) {
      state.conversation = null;
      state.events = [];
      state.historyLoaded = false;
      state.cursor = null;
    }
    if (!state.conversation && !state.newWork) {
      state.conversation = selected()?.conversations.find(c => c.kind === 'main')?.id || null;
      if (state.conversation) {
        localStorage.setItem('muse-workbench-selection', JSON.stringify({
          side: state.side,
          conversation: state.conversation,
          uiVersion: 3
        }));
        if (!$('prompt').value) $('prompt').value = state.drafts[contextKey()] || state.drafts[
          state.side + ':main'] || '';
      }
    }
    renderDirectory();
    renderHeading();
    renderTaskAssets();
    if ($('taskDialog').open) renderTaskDetails();
    if (selected() && online(state.side)) await refreshConversation();
    else renderMessages();
  } finally {
    state.refreshing = false;
  }
}
async function send(text) {
  if (state.busy || !writable()) return;
  saveDraft();
  const key = contextKey(),
    side = state.side,
    conversation = state.conversation,
    newWork = state.newWork,
    prior = state.pending[key];
  if (prior && prior.body.text !== text) {
    notice('上一条消息尚未确认。请先重试原消息，确认后再发送新的内容。');
    return;
  }
  const submission = prior || {
    route: newWork ? 'conversations' : 'input',
    body: {
      event_id: crypto.randomUUID(),
      text,
      ...(newWork ? {} : {
        conversation_id: conversation
      })
    }
  };
  state.pending[key] = submission;
  persistDrafts();
  state.busy = true;
  renderHeading();
  notice('');
  try {
    const result = await api(side, submission.route, submission.body);
    delete state.pending[key];
    delete state.drafts[key];
    persistDrafts();
    if (contextKey() === key) {
      $('prompt').value = '';
      if (newWork) {
        state.newWork = false;
        state.conversation = result.conversation_id;
        localStorage.setItem('muse-workbench-selection', JSON.stringify({
          side,
          conversation: state.conversation,
          uiVersion: 3
        }));
      }
    }
    await refresh();
  } catch (e) {
    if (e.status >= 400 && e.status < 500) {
      delete state.pending[key];
      persistDrafts();
      notice(e.message);
    } else notice('未收到发送确认，已保留原消息。点击“重试发送”不会重复创建任务。');
  } finally {
    state.busy = false;
    renderHeading();
  }
}
$('composer').onsubmit = e => {
  e.preventDefault();
  if ($('prompt').value.trim()) send($('prompt').value.trim());
};
$('prompt').oninput = saveDraft;
$('newWork').onclick = () => choose('cloud', null, true);
$('retryConnection').onclick = refresh;
$('older').onclick = async () => {
  if (!state.cursor || state.loadingOlder) return;
  state.loadingOlder = true;
  $('older').disabled = true;
  const side = state.side,
    id = state.conversation, cursor = state.cursor;
  try {
    const page = await api(side, 'conversations/' + encodeURIComponent(id) +
      '?limit=30&before_seq=' + cursor);
    if (side !== state.side || id !== state.conversation) return;
    const m = new Map([...page.events, ...state.events].map(e => [e.id, e]));
    state.events = [...m.values()].sort((a, b) => a.sequence - b.sequence);
    state.cursor = page.next_before_seq;
    renderMessages();
  } catch (e) {
    notice(e.message);
  } finally {
    state.loadingOlder = false;
    $('older').disabled = false;
  }
};
$('taskDetail').onclick = () => panel('taskDialog');
$('openScene').onclick = $('openSceneCompact').onclick = () => panel('sceneDialog');
$('openSettings').onclick = () => panel('settingsDialog');
for (const b of document.querySelectorAll('[data-close]')) b.onclick = () => $(b.dataset.close)
  .close();
$('toggleDirectory').onclick = () => {
  const opened = $('directory').classList.toggle('open');
  $('directoryShade').hidden = !opened;
  $('toggleDirectory').setAttribute('aria-expanded', String(opened));
};
$('closeDirectory').onclick = $('directoryShade').onclick = closeDirectory;

async function control(text) {
  const t = selectedTask();
  if ((!t && selectedConversation()?.kind !== 'work') || !online(state.side)) return;
  try {
    const result = await api(state.side, 'input', {
      event_id: crypto.randomUUID(),
      text,
      ...(t ? {
        task_id: t.id
      } : {}),
      conversation_id: state.conversation
    });
    notice(result.reply?.text || '控制请求已保存');
    await refresh();
  } catch (e) {
    notice(e.message);
  }
}
$('pauseTask').onclick = () => control(selectedTask()?.status === 'paused' ? '恢复当前任务' : '暂停');
$('cancelTask').onclick = () => control('取消当前任务');
$('closeDialog').onclick = () => $('recordDialog').close();
$('sourceSelect').onchange = () => {
  state.source = $('sourceSelect').value;
  showSource();
};
$('seekForm').onsubmit = async e => {
  e.preventDefault();
  if (!state.source) return;
  const side = mediaSide(),
    source = state.source;
  try {
    const position = Math.round(Number($('position').value) * 1000);
    await api(side, 'sources/playback', {
      source_id: state.source,
      position_ms: position,
      playing: false
    }, 'PUT');
    $('sourceVideo').pause();
    $('sourceVideo').currentTime = position / 1000;
    const response = await fetch(path(side, 'sources/' + encodeURIComponent(source) +
      '/frame?position_ms=' + position));
    if (!response.ok) throw new Error('无法在此位置取帧');
    if (side !== mediaSide() || source !== state.source) return;
    const url = URL.createObjectURL(await response.blob());
    if ($('frameImage').dataset.blob) URL.revokeObjectURL($('frameImage').dataset.blob);
    $('frameImage').dataset.blob = url;
    $('frameImage').src = url;
    $('frameImage').hidden = false;
    $('frameInfo').textContent = '实际帧时间 ' + response.headers.get('X-Frame-Time-Ms') +
      ' ms · 文件回放，未进行视觉识别。';
  } catch (err) {
    notice(err.message);
  }
};
for (const b of document.querySelectorAll('[data-stage]')) b.onclick = async () => {
  try {
    await api('edge', 'scene/control', {
      stage: b.dataset.stage,
      valid_for_ms: 60000
    });
    notice('已向端侧提交模拟现场观测，有效期60秒。');
    await refresh();
  } catch (e) {
    notice(e.message);
  }
};
$('prompt').value = state.drafts[contextKey()] || '';
renderHeading();
refresh();
// Conversation updates stay responsive; do not rebuild both node workbenches for each tick.
setInterval(() => {
  if (!document.hidden && !state.refreshing) refreshConversation();
}, 2500);
setInterval(() => {
  if (!document.hidden) refresh();
}, 30000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh();
});

$('sourceVideo').addEventListener('loadedmetadata', async () => {
  const video = $('sourceVideo'),
    target = state.previewTarget;
  if (!target) return;
  try {
    const elapsed = target.playing ? performance.now() - target.received : 0;
    const pos = Math.min(target.duration - 1, target.position + elapsed) / 1000;
    if (pos > 0) {
      await new Promise(resolve => {
        video.addEventListener('seeked', resolve, {
          once: true
        });
        video.currentTime = pos;
      });
    }
    if (target.playing && mediaVisible(video) && state.previewTarget === target) {
      video.muted = true;
      await video.play();
    }
  } catch (error) {
    notice('自动预览未启动，请点击视频播放按钮。');
  } finally {
    state.mediaUpdating = false;
  }
});
for (const event of ['play', 'pause', 'seeked']) $('sourceVideo').addEventListener(event,
async () => {
    if (state.mediaUpdating || !state.source) return;
    const video = $('sourceVideo');
    try {
      await api(video.dataset.side, 'sources/playback', {
        source_id: video.dataset.source,
        position_ms: Math.min(Math.round(video.currentTime * 1000), state.snapshots[video
            .dataset.side]?.sources.find(s => s.source_id === video.dataset.source)?.facts
          .duration_ms || 0),
        playing: !video.paused
      }, 'PUT');
    } catch (error) {
      notice(error.message);
    }
  });
