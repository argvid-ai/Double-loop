'use strict';
const manualCameraUI = {
  busy: new Map(),
  messages: new Map()
};
async function controlManualCamera(camera, action, angles = {}, retry = false) {
  const key = 'gimbal:' + camera.id,
    prior = state.pending[key];
  if (manualCameraUI.busy.has(camera.id) && action !== 'stop') return;
  let body = retry && prior ? prior.body : {
    request_id: crypto.randomUUID(),
    device_ref: camera.id,
    action,
    expected_pose_revision: camera.revision,
    ...angles
  };
  const superseded = prior?.body || null;
  state.pending[key] = {
    body,
    superseded
  };
  persistDrafts();
  manualCameraUI.busy.set(camera.id, body.request_id);
  manualCameraUI.messages.delete(camera.id);
  renderManualGimbals();
  try {
    for (let attempt = 0;; attempt++) {
      try {
        await api('edge', 'observation/gimbal', body);
        break;
      } catch (error) {
        if (error.status !== 409 || body.action !== 'stop' || attempt >= 1 || superseded
          ?.action !== 'move' || state.pending[key]?.body.request_id !== body.request_id)
        throw error;
        // A prior move may already have executed while its response is delayed.
        // Retain the stop intent, read authoritative pose, and retry once with a
        // new identity. Never remove the server's revision fence.
        const moved = await api('edge', 'observation/gimbals/' + encodeURIComponent(camera.id) +
          '/receipts/' + encodeURIComponent(superseded.request_id));
        if (moved.operation !== 'move' || !moved.confirmed || !moved.current_state?.manual ||
          moved.state?.revision !== moved.current_state.revision || moved.state.revision !==
          superseded.expected_pose_revision + 1) throw error;
        await loadObservation(true);
        if (state.pending[key]?.body.request_id !== body.request_id) return;
        const fresh = observationUI.view?.gimbals?.find(g => g.id === camera.id);
        if (!fresh || fresh.revision !== moved.state.revision) throw error;
        body = {
          ...body,
          request_id: crypto.randomUUID(),
          expected_pose_revision: fresh.revision
        };
        state.pending[key] = {
          body,
          superseded
        };
        persistDrafts();
        manualCameraUI.busy.set(camera.id, body.request_id);
      }
    }
    if (state.pending[key]?.body.request_id === body.request_id) {
      delete state.pending[key];
      persistDrafts();
      manualCameraUI.messages.set(camera.id, '操作已确认。');
    }
    await loadObservation(true);
  } catch (error) {
    if (state.pending[key]?.body.request_id === body.request_id) {
      if (error.status >= 400 && error.status < 500 && body.action !== 'stop') {
        delete state.pending[key];
        persistDrafts();
      }
      manualCameraUI.messages.set(camera.id, body.action === 'stop' ? '停止尚未确认，请重试停止。' : error
        .status === 409 ? '机位或控制状态已变化，请查看最新状态后重试。' : '未收到操作确认。可重试原操作，或停止云台。');
      await loadObservation(true);
    }
  } finally {
    if (manualCameraUI.busy.get(camera.id) === body.request_id) manualCameraUI.busy.delete(camera
      .id);
    renderManualGimbals();
  }
}

function renderManualGimbals() {
  const root = $('observationGimbals'),
    cameras = observationUI.view?.gimbals || [];
  root.hidden = !cameras.length;
  const current = cameras.map(c => [c, state.pending['gimbal:' + c.id], manualCameraUI.busy.get(c
    .id), manualCameraUI.messages.get(c.id)]);
  if (!changed('manual-gimbals', current)) return;
  root.replaceChildren();
  cameras.forEach((camera, index) => {
    const card = el('section', undefined, 'manual-camera'),
      head = el('div', undefined, 'manual-camera-heading');
    head.append(el('h3', cameras.length > 1 ? `模拟云台 ${index+1}` : '模拟云台'), el('span', camera
      .manual ? '手动控制' : camera.automatic_active ? '自动控制' : '等待观察任务', 'muted'));
    card.append(head, el('p', '仅模拟角度与控制状态，回放画面不会随之转动。', 'muted'));
    const controls = el('div', undefined, 'manual-camera-controls'),
      pad = el('div', undefined, 'camera-pad');
    const key = 'gimbal:' + camera.id,
      pending = state.pending[key],
      busy = manualCameraUI.busy.has(camera.id);
    const step = Math.min(5, camera.spec.max_step);
    for (const [label, dx, dy, position] of [
        ['↑', 0, step, 'up'],
        ['←', -step, 0, 'left'],
        ['↓', 0, -step, 'down'],
        ['→', step, 0, 'right']
      ]) {
      const button = el('button', label, 'camera-direction ' + position),
        pan = camera.pan + dx,
        tilt = camera.tilt + dy;
      button.setAttribute('aria-label', ({
        up: '向上',
        down: '向下',
        left: '向左',
        right: '向右'
      })[position] + `调整 ${step} 度`);
      button.disabled = !camera.manual || busy || !!pending || Math.abs(pan) > camera.spec
        .pan_limit || Math.abs(tilt) > camera.spec.tilt_limit;
      button.onclick = () => controlManualCamera(camera, 'move', {
        pan,
        tilt
      });
      pad.append(button);
    }
    const stop = el('button', '停止', 'camera-stop danger');
    stop.setAttribute('aria-label', '停止云台并保持手动控制');
    stop.disabled = busy && pending?.body.action === 'stop';
    stop.onclick = () => controlManualCamera(camera, 'stop');
    pad.append(stop);
    const mode = el('div', undefined, 'camera-mode');
    mode.append(el('p', `水平 ${camera.pan}° · 俯仰 ${camera.tilt}°`));
    const toggle = el('button', camera.manual ? '交还自动控制' : '手动接管');
    toggle.disabled = busy || !!pending;
    toggle.onclick = () => controlManualCamera(camera, camera.manual ? 'release' : 'takeover');
    mode.append(toggle);
    if (pending && !busy) {
      const retry = el('button', pending.body.action === 'stop' ? '重新发出停止' : '重试未确认操作');
      retry.onclick = () => controlManualCamera(camera, pending.body.action, {}, pending.body
        .action !== 'stop');
      mode.append(retry);
    }
    controls.append(pad, mode);
    card.append(controls);
    if (manualCameraUI.messages.has(camera.id)) {
      const message = el('p', manualCameraUI.messages.get(camera.id), 'muted');
      message.setAttribute('role', 'status');
      card.append(message);
    }
    root.append(card);
  });
}