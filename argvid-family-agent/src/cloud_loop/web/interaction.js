const $ = s => document.querySelector(s),
  base = '/interaction/edge';
const welcomeTemplate = $('.welcome').cloneNode(true);
let cid = null,
  source = null,
  lastSeq = 0,
  assetSignature = '',
  sends = new Map(),
  refreshPending = false;
const statuses = {
  idle: '无任务',
  ready: '待执行',
  waiting: '等待',
  capturing: '拍摄任务已接受',
  asset_ready: '素材已就绪',
  cloud_pending: '等待云端',
  cloud_ready: '成片已就绪',
  completed: '已完成',
  cancelled: '已取消',
  unsupported: '缺少能力'
};

function notice(text) {
  $('#notice').textContent = text;
  $('#notice').hidden = !text;
}
async function api(path, method = 'GET', body) {
  const r = await fetch(base + path, {
    method,
    headers: body ? {
      'Content-Type': 'application/json'
    } : {},
    body: body ? JSON.stringify(body) : undefined
  });
  const type = r.headers.get('content-type') || '';
  const data = type.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw Error(data.error === 'edge_unavailable' ? '端侧暂时未连接，请等待服务恢复后刷新。' : data.error ||
    data.detail || `请求失败 ${r.status}`);
  return data;
}

function bubble(role, id, text = '') {
  let node = document.getElementById('bubble-' + id);
  if (!node) {
    $('#messages .welcome')?.remove();
    node = document.createElement('div');
    node.id = 'bubble-' + id;
    node.className = 'bubble ' + role;
    const label = document.createElement('div');
    label.className = 'role';
    label.textContent = role === 'user' ? '你' : role === 'assistant' ? 'O6N / NANOBOT' : '运行状态';
    const content = document.createElement('div');
    content.className = 'content';
    node.append(label, content);
    $('#messages').append(node);
  }
  if (text) node.querySelector('.content').textContent = text;
  return node;
}

function clearAcks(revision) {
  document.querySelectorAll('[data-ack-revision]').forEach(n => {
    if (Number(n.dataset.ackRevision) <= revision) {
      sends.delete(n.dataset.eventId);
      n.remove();
    }
  });
}

function scroll() {
  const m = $('#messages');
  m.scrollTop = m.scrollHeight;
}

function displayEvent(e) {
  if (e.seq <= lastSeq) return;
  lastSeq = e.seq;
  const p = e.payload;
  if (e.kind === 'conversation.input') bubble('user', p.event_id, p.text);
  if (e.kind === 'conversation.turn.started' && sends.has(p.input_event_id)) bubble('system',
    'ack-' + p.input_event_id, '端侧正在处理这条输入。');
  if (e.kind === 'conversation.delta') {
    const n = bubble('assistant', 'reply-' + p.input_event_id);
    n.querySelector('.content').textContent += p.text;
    if (sends.has(p.input_event_id)) {
      const ms = performance.now() - sends.get(p.input_event_id);
      $('#clientLatency').textContent = (ms / 1000).toFixed(2) + ' s';
      sends.delete(p.input_event_id);
    }
    scroll();
  }
  if (e.kind === 'conversation.turn.finished') {
    sends.delete(p.event_id);
    clearAcks(p.revision);
    document.getElementById('bubble-ack-' + p.event_id)?.remove();
    const n = bubble('assistant', 'reply-' + p.event_id);
    if (p.answer) n.querySelector('.content').textContent = p.answer;
    if (p.status === 'interrupted' || p.status === 'superseded') n.classList.add('interrupted');
    if (p.status === 'failed' && !p.answer) {
      n.className = 'bubble system';
      n.querySelector('.role').textContent = '运行状态';
      n.querySelector('.content').textContent = p.error === 'MalformedFinalText' ?
        '模型回复格式不正确，工具结果和任务状态已保留。' : '本轮未完成，当前状态已保留。请查看轨迹中的错误。';
    }
    if (p.elapsed_seconds != null) $('#turnLatency').textContent = p.elapsed_seconds.toFixed(2) +
      ' s';
    scroll();
  }
  if (e.kind === 'intent.control') {
    clearAcks(p.revision);
    if (p.source_text && p.event_id) bubble('user', p.event_id, p.source_text);
    bubble('system', 'event-' + e.seq,
      `${p.scope==='pending_inputs'?'尚未处理的新指令':'任务'}控制已生效：${{pause:'暂停',resume:'继续',cancel:'取消'}[p.action]||p.action}。`
      );
  }
  if (e.kind === 'capture.accepted') bubble('system', 'event-' + e.seq, '回放近景处理已接受，完成后会通知。');
  if (e.kind === 'cloud.delegation.queued') bubble('system', 'event-' + e.seq,
    '合成委托已在端侧排队，等待云端确认接收。');
  if (e.kind === 'capture.effect.finished') bubble('system', 'event-' + e.seq, p.status ===
    'completed' ? '近景文件已生成并核验。' : p.status === 'superseded' ? '旧任务结果已隔离，未应用到新目标。' :
    '本次拍摄处理未完成，状态与错误已记录。');
  if (e.kind === 'cloud.delegation.updated' && p.status === 'completed') bubble('system', 'event-' +
    e.seq, '云端成片已回传，并在端侧核验。');
  if (!refreshPending) {
    refreshPending = true;
    setTimeout(() => {
      refreshPending = false;
      refreshState().catch(e => notice(e.message));
    }, 120);
  }
}
async function openConversation(id) {
  source?.close();
  cid = id;
  lastSeq = 0;
  assetSignature = '';
  $('#messages').replaceChildren(welcomeTemplate.cloneNode(true));
  localStorage.setItem('cix-interaction-cid', cid);
  source = new EventSource(base + `/conversations/${cid}/events`);
  source.onmessage = e => {
    if (cid !== id) return;
    try {
      displayEvent(JSON.parse(e.data));
    } catch (err) {
      notice(err.message);
    }
  };
  source.onerror = () => {
    $('#connection').textContent = '连接恢复中…';
  };
  source.onopen = () => {
    $('#connection').textContent = '端侧已连接';
  };
  await refreshState();
}
async function listSessions() {
  const rows = await api('/conversations');
  $('#sessions').replaceChildren();
  for (const c of rows) {
    const o = document.createElement('option');
    o.value = c.id;
    o.textContent = (c.intent.goal || '新会话').slice(0, 18) + ' · ' + c.id.slice(0, 6);
    $('#sessions').append(o);
  }
  if (cid) $('#sessions').value = cid;
  return rows;
}
async function newSession() {
  const c = await api('/conversations', 'POST', {});
  await listSessions();
  await openConversation(c.id);
  $('#sessions').value = c.id;
  notice('');
}
async function refreshState() {
  if (!cid) return;
  const active = cid,
    state = await api(`/conversations/${active}`);
  if (cid !== active) return;
  const i = state.intent;
  $('#goal').textContent = i.goal || '还没有保存任务目标。';
  $('#intentStatus').textContent = statuses[i.status] || i.status;
  $('#version').textContent = String(i.version);
  $('#permission').textContent = state.can_capture ? '允许当前拍摄' : '当前不执行';
  $('#blockReason').textContent = state.block_reason || '—';
  $('#cloudStatus').textContent = state.cloud ? ({
    not_requested: '未委托',
    pending: '本地排队，未确认收到',
    accepted: '云端已接受',
    running: '云端处理中',
    completed: '成片已核验',
    failed: '未完成',
    superseded: '旧版本已隔离'
  } [state.cloud.status] || state.cloud.status) : '未委托';
  document.querySelectorAll('[data-stage]').forEach(b => b.classList.toggle('selected', b.dataset
    .stage === state.scene?.stage));
  const assets = await api(`/conversations/${active}/assets`);
  if (cid !== active) return;
  const sig = JSON.stringify(assets.map(a => [a.id, a.current]));
  if (sig !== assetSignature) {
    assetSignature = sig;
    $('#assets').replaceChildren();
    $('#assetCount').textContent = assets.length ? `${assets.length} 个已登记文件` : '等待真实产物';
    for (const a of assets) {
      const card = document.createElement('article');
      card.className = 'asset';
      const video = document.createElement('video');
      video.controls = true;
      video.preload = 'metadata';
      video.src = base + `/conversations/${cid}/assets/${a.id}`;
      const label = document.createElement('p');
      label.textContent = (a.current ? '' : '历史任务 · ') + (a.kind === 'deliverable' ?
        '十二秒成片 · 回放素材' : '六秒近景 · 视频文件回放');
      const link = document.createElement('a');
      link.href = video.src;
      link.textContent = '下载已核验文件 ↗';
      const p = document.createElement('p');
      p.append(link);
      card.append(video, label, p);
      $('#assets').append(card);
    }
    if (!assets.length) {
      const p = document.createElement('p');
      p.className = 'empty';
      p.textContent = '已核验的素材会出现在这里。文字声明不会生成视频。';
      $('#assets').append(p);
    }
  }
}
$('#composer').addEventListener('submit', async e => {
  e.preventDefault();
  const text = $('#message').value.trim();
  if (!text) return;
  $('#send').disabled = true;
  try {
    if (!cid) await newSession();
    const event_id = crypto.randomUUID();
    sends.set(event_id, performance.now());
    bubble('user', event_id, text);
    scroll();
    const result = await api(`/conversations/${cid}/messages`, 'POST', {
      event_id,
      text
    });
    $('#message').value = '';
    if (result.model_invoked === false) sends.delete(event_id);
    else if (sends.has(event_id)) {
      const ack = document.getElementById('bubble-ack-' + event_id) || bubble('system',
        'ack-' + event_id, '已收到这条输入，等待端侧处理。');
      ack.dataset.ackRevision = result.revision;
      ack.dataset.eventId = event_id;
    }
    notice('');
  } catch (error) {
    notice(error.message);
  } finally {
    $('#send').disabled = false;
  }
});
$('#message').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    $('#composer').requestSubmit();
    e.preventDefault();
  }
});
$('#newSession').onclick = () => newSession().catch(e => notice(e.message));
$('#sessions').onchange = e => openConversation(e.target.value).catch(e => notice(e.message));
$('#messages').addEventListener('click', e => {
  if (e.target.closest('#example')) {
    $('#message').value = '做一段十二秒竖屏猫咪短片：前六秒全景，后六秒近景。用户在场才能补拍，不要闪光灯。';
    $('#message').focus();
  }
});
document.querySelectorAll('[data-stage]').forEach(b => b.onclick = () => api('/simulation/stage',
  'POST', {
    stage: b.dataset.stage
  }).then(refreshState).catch(e => notice(e.message)));
document.querySelectorAll('[data-control]').forEach(b => b.onclick = () => {
  if (cid) api(`/conversations/${cid}/controls`, 'POST', {
    event_id: crypto.randomUUID(),
    action: b.dataset.control
  }).then(refreshState).catch(e => notice(e.message));
});
$('#sendTranscript').onclick = () => {
  if (cid) api('/simulation/transcript', 'POST', {
    conversation_id: cid,
    event_id: crypto.randomUUID(),
    text: $('#transcript').value
  }).then(() => {
    notice('模拟转写已提交；未调用麦克风或 ASR。');
    return refreshState();
  }).catch(e => notice(e.message));
};
$('#refreshTrace').onclick = async () => {
  if (!cid) return;
  try {
    let all = [],
      after = 0;
    for (;;) {
      const rows = await api(`/conversations/${cid}/trace?after=${after}`);
      all.push(...rows);
      if (rows.length < 500) break;
      after = rows[rows.length - 1].seq;
    }
    $('#trace').textContent = JSON.stringify(all, null, 2);
  } catch (e) {
    notice(e.message);
  }
};
async function init() {
  try {
    const h = await api('/health');
    $('#connection').textContent = '端侧已连接';
    $('#modelName').textContent = h.model;
    $('#contextSize').textContent = h.context_tokens.toLocaleString() + ' tokens';
    const caps = await api('/capabilities');
    for (const name of caps.binding.tools) {
      const li = document.createElement('li');
      li.textContent = name;
      $('#toolList').append(li);
    }
    const rows = await listSessions(),
      saved = localStorage.getItem('cix-interaction-cid');
    if (rows.length) await openConversation(rows.some(x => x.id === saved) ? saved : rows[0].id);
    else await newSession();
  } catch (e) {
    $('#connection').textContent = '端侧未连接';
    notice(e.message);
  }
}
init();
setInterval(() => {
  if (cid) refreshState().catch(() => {});
}, 4000);