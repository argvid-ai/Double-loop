'use strict';
const observationUI = {
  view: null,
  source: null,
  previewSource: null,
  selectedRange: null,
  request: 0,
  formSignature: null,
  inflight: null,
  task: null
};

function observationPlayer() {
  return $('observationVideo').hidden && !$('observationAudio').hidden ? $('observationAudio') : $(
    'observationVideo');
}
const observationToolNames = {
  mark_media: '保留片段',
  read_media: '回看素材',
  get_edge_state: '查询观察状态',
  move_camera: '调整机位',
  stop_motion: '停止云台'
};

function selectOptions(id, options) {
  const select = $(id),
    old = select.value;
  select.replaceChildren();
  for (const [value, label] of options) {
    const option = el('option', label);
    option.value = value;
    select.append(option);
  }
  if (options.some(([value]) => value === old)) select.value = old;
}

function observationError(error) {
  return error.status === 409 ? '任务状态或配置已变化，请刷新后重试。' : error.message;
}

function namedExecutor() {
  return observationUI.view?.catalogue?.executors?.find(e => e.id === $('observationExecutor')
    .value);
}

function samplingLabel(s) {
  const interval = (s.frame_interval_ms || 1000 / s.frames_per_second) / 1000;
  return `${s.id} · 每${interval}秒1帧 · ${s.window_seconds}秒一批`;
}

function renderExecutorSampling() {
  const e = namedExecutor();
  selectOptions('observationSampling', (e?.sampling || []).map(s => [s.id, samplingLabel(s)]));
  renderObservationModel();
}

function observationStatus(task) {
  if (task.status === 'running') {
    const visual = (task.health || []).find(h => h.component === 'visual_worker');
    if (visual?.code === 'paused') return '录像继续，判断已暂停';
    if (['analysis_recovering', 'restarting'].includes(visual?.code)) return '录像继续，判断恢复中';
    if (visual?.active) return '录像继续，判断异常';
    return task.inference_simulated ? '模拟处理运行中' : '观察运行中';
  }
  if (task.status === 'starting') return '正在准备观察…';
  if (task.status === 'accepted') return '已配置，尚未启动';
  if (task.status === 'paused') return task.reason ? '观察异常，已暂停' : '已暂停';
  if (task.status === 'stopped') return task.shutdown_confirmed ? '已停止' : '停止中，等待确认';
  return '状态待确认';
}

function renderObservationModel() {
  const view = observationUI.view,
    executor = view?.catalogue?.executors?.find(e => e.id === $('observationExecutor')?.value);
  const model = view?.models?.find(m => m.id === (executor?.model_ref || $('observationModel')
    .value));
  const labels = {
    not_started: '已配置，尚未加载',
    starting: '正在启动程序',
    starting_server: '正在启动程序',
    server_ready: '程序已就绪，模型尚未确认',
    loading_model: '正在加载影音模型',
    preparing_context: '正在准备观察上下文',
    ready: '模型与观察上下文已就绪',
    context_invalid: '观察上下文需恢复',
    failed: '模型初始化失败',
    stopping: '正在停止',
    stopped: '已停止，模型已释放',
    exited: '模型程序已退出',
    unreachable: '模型程序暂时无响应',
    changing: '模型正在切换，等待确认',
    recovery_pending: '上次运行待恢复确认',
    unconfirmed: '模型状态暂时无法确认'
  };
  let text = !view ? '端侧无法连接，模型状态未知。' : !view.installed ? '尚未配置观察模型。' : !model ? '当前模型状态未提供。' :
    labels[model.state] || labels.unconfirmed;
  if (model?.backend) text += ' · ' + (model.backend.includes('qwen') ? '本地CPU视觉判别' : model
    .backend === 'semantic_media_http' ? '已绑定影音Provider' : model.backend);
  if (model?.initialization_failed) text += '；上次初始化未成功。';
  if (model?.state === 'ready') text += '；这不代表识别质量或速度已通过验收。';
  if (model?.inference_simulated) text = '页面联调模拟状态 · ' + text;
  $('observationModelStatus').textContent = text;
  if ($('observationExecutorStatus')) $('observationExecutorStatus').textContent = text;
}

function renderObservation() {
  const view = observationUI.view,
    catalogue = view?.catalogue,
    configured = !!catalogue;
  renderManualGimbals();
  $('observationNotice').textContent = !view ? '端侧暂时无法连接，不能据此判断观察已停止。' : !view.installed ?
    '端侧尚未配置持续观察。' : !catalogue ? '观察服务已安装，来源配置尚不可用。' : catalogue.execution_mode === 'disabled' ?
    '已登记回放素材；观察模型尚未启用。' : '录像与判断状态分别展示；这里播放已保存片段，不是实时直播，也不会启动新录像。';
  $('observationSource').disabled = !view?.sources?.length;
  const sourceSignature = JSON.stringify(view?.sources || []);
  if (observationUI.sourceSignature !== sourceSignature) {
    observationUI.sourceSignature = sourceSignature;
    selectOptions('observationSource', (view?.sources || []).map(s => [s.id, s.title]));
    if (!observationUI.source && view?.tasks?.[0]) $('observationSource').value = view.tasks[0]
      .source_ref;
    showObservationSource();
  }
  const sourceTasks = (view?.tasks || []).filter(t => t.source_ref === $('observationSource')
  .value);
  selectOptions('observationTask', sourceTasks.map((t, i) => [t.task_id, (i === 0 ? '最近任务 · ' :
    '') + t.goal.slice(0, 54)
  ]));
  observationUI.task = $('observationTask').value;
  if (changed('observation-task-list', [view?.tasks, catalogue?.execution_mode, observationUI
    .task])) {
    $('observationTasks').replaceChildren();
    for (const task of (view?.tasks || []).filter(t => t.task_id === observationUI.task)) {
      const card = el('article', undefined, 'observation-task'),
        head = el('div');
      head.append(el('h3', String(task.goal).slice(0, 48)), el('p', observationStatus(task),
        'observation-status'));
      card.append(head);
      if (task.unprocessed_seconds > 0) card.append(el('p',
        `还有 ${task.unprocessed_seconds.toFixed(1)} 秒素材待处理`, 'muted'));
      if (task.gap_count) card.append(el('p', '有未处理的时间段，不能视为完整观察。', 'muted'));
      if (task.inference_simulated) card.append(el('p', '处理与云台动作使用模拟结果，不代表真实模型或硬件能力。', 'muted'));
      for (const issue of task.health || []) {
        const recording = task.status === 'running' ? '录像仍在运行。'
          : task.status === 'starting' ? '录像正在启动。' : '当前录像没有运行。';
        const labels = {speech: '语音交互', visual_worker: '视觉分析', visual_analysis: '视觉积压', recording_retention: '录像空间管理', default_task: '默认任务', observation_task: '观察任务'};
        const description = issue.code === 'paused' ? '已单独暂停。' + recording
          : ['restarting', 'analysis_recovering'].includes(issue.code) ? '正在本机恢复判断。' + recording
          : issue.code === 'analysis_failed' ? '判断失败，正在等待本机恢复。' + recording
          : ['analysis_recovery_blocked', 'analysis_recovery_waiting'].includes(issue.code) ? '正在等待旧判断结束后恢复。' + recording
          : '异常：' + issue.code + '。已记录；其他能力以各自状态为准。';
        card.append(el('p', (labels[issue.component] || '运行能力') + description, issue.active ? 'error-text' : 'muted'));
      }
      const errors = task.retention?.errors || [];
      if (errors.length) card.append(el('p', '部分片段还未保存，请检查空间或片段完整性。', 'error-text'));
      const actions = el('div', undefined, 'observation-actions');
      const choices = task.status === 'accepted' ? ['start', 'stop'] : task.status === 'paused' ? [
        'resume', 'stop'
      ] : ['starting', 'running'].includes(task.status) ? ['pause', 'stop'] : [];
      for (const action of choices) {
        const button = el('button', ({
          start: '启动观察实验',
          resume: '继续观察',
          pause: '暂停',
          stop: '停止'
        })[action], action === 'stop' ? 'danger' : '');
        button.disabled = ['start', 'resume'].includes(action) && catalogue?.execution_mode !==
          'candidate_probe';
        if (button.disabled) button.title = '模型尚未通过验证，实验开关也未启用';
        button.onclick = async () => {
          button.disabled = true;
          const key = 'observation:' + task.task_id + ':' + action;
          const payload = state.pending[key]?.body || {
            request_id: crypto.randomUUID(),
            task_id: task.task_id,
            version: task.version,
            generation: task.generation,
            action
          };
          state.pending[key] = {
            body: payload
          };
          persistDrafts();
          try {
            await api('edge', 'observation/control', payload);
            delete state.pending[key];
            persistDrafts();
            await loadObservation(true);
          } catch (error) {
            if (error.status >= 400 && error.status < 500) {
              delete state.pending[key];
              persistDrafts();
            }
            $('observationNotice').textContent = observationError(error);
            button.disabled = false;
          }
        };
        actions.append(button);
      }
      card.append(actions);
      $('observationTasks').append(card);
    }
    if (view?.installed && !view.tasks.length) $('observationTasks').append(el('p',
      '还没有观察任务。可以在对话中交代目标，也可以先保存一份观察配置。', 'empty'));
  }
  const current = (view?.tasks || []).find(t => t.task_id === observationUI.task && t
    .preview_position_ms != null);
  $('followObservation').hidden = !current || current.status === 'stopped';
  $('playObservation').disabled = !view?.sources?.some(s => s.id === $('observationSource').value && s.url);
  $('observationProgress').textContent = current ?
    `最近记录到 ${(current.preview_position_ms/1000).toFixed(1)} 秒 · ${observationStatus(current)}` :
    '预览播放不会启动观察，也不会改变端侧处理进度。';
  $('observationConfigStatus').textContent = !configured ? '尚未登记观察组件，请先在端侧加载观察配置文件。' : catalogue
    .execution_mode === 'disabled' ? '组件已登记。模型仍未验证，保存任务不会启动模型。' : '候选模型实验已允许；启动不代表识别质量或速度通过。';
  const named = !!catalogue?.executors?.length;
  $('observationNamedFields').hidden = !named;
  $('observationLegacyFields').hidden = named;
  for (const field of $('observationLegacyFields').querySelectorAll('select,input')) field
    .disabled = named;
  $('observationExecutor').required = named;
  $('observationExecutor').disabled = !named;
  $('saveObservation').textContent = named ? '交给家庭助理启动' : '新建观察任务';
  if (named) {
    $('observationConfigStatus').textContent = '来源、模型与采样已经配置。判断按采样进度推进，是否正在录像请看“默认录像”。';
  }
  $('saveObservation').disabled = !configured;
  if (configured && observationUI.formSignature !== catalogue.digest) {
    observationUI.formSignature = catalogue.digest;
    selectOptions('observationExecutor', (catalogue.executors || []).map(e => [e.id, e.title]));
    renderExecutorSampling();
    selectOptions('observationConfigSource', catalogue.sources.map(s => [s.id, s.title]));
    selectOptions('observationModel', catalogue.models.map(m => [m.id, m.title]));
    selectOptions('observationStorage', Object.entries(catalogue.storage).map(([id, s]) => [id, s
      .project_id + ' · 视频文件夹'
    ]));
    selectOptions('observationSave', Object.entries(catalogue.saves).map(([id, s]) => [id,
      `片段前 ${s.pre_roll_ms/1000} 秒 / 后 ${s.post_roll_ms/1000} 秒`
    ]));
    selectOptions('observationBatch', Object.entries(catalogue.batches).map(([id, b]) => [id,
      `每 ${b.window_ms/1000} 秒一轮 · ${1000/b.frame_interval_ms} 帧/秒`
    ]));
    selectOptions('observationGimbal', [
      ['', '不使用云台'], ...catalogue.gimbals.map(id => [id, id + '（模拟）'])
    ]);
    $('observationToolChoices').replaceChildren();
    for (const tool of catalogue.tools) {
      const row = el('label', undefined, 'tool-row'),
        box = el('input');
      box.type = 'checkbox';
      box.dataset.observationTool = tool;
      box.checked = !['move_camera', 'stop_motion'].includes(tool);
      box.disabled = tool === 'mark_media';
      row.append(box, el('span', observationToolNames[tool] || tool));
      $('observationToolChoices').append(row);
    }
    $('observationSkillChoices').replaceChildren();
    for (const skill of catalogue.skills) {
      const row = el('label', undefined, 'tool-row'),
        box = el('input');
      box.type = 'checkbox';
      box.dataset.observationSkill = skill;
      row.append(box, el('span', skill));
      $('observationSkillChoices').append(row);
    }
  }
  renderObservationModel();
  if (changed('observation-saved-videos', view?.media || [])) {
    const box = $('observationSavedVideos');
    pauseMedia(box);
    box.replaceChildren();
    for (const item of view?.media || []) {
      const id = item.asset_id || item.id,
        card = el('article', undefined, 'asset'),
        video = el('video');
      const url = path('edge', 'observation/media/' + encodeURIComponent(id));
      browserVideo(video, url);
      const link = el('a', '打开原视频 ↗');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener';
      card.append(el('h3', '已保留视频'), video, el('p', '来自真实录像回放 · 原音轨保留', 'muted'), link);
      box.append(card);
    }
    if (!view?.media?.length) box.append(el('p', view?.tasks?.some(t => t.output_mode ===
      'markers') ? '本配置只标记时间段，尚未剪辑导出独立视频。' : '还没有正式保存的视频。待判断或受保护的素材不会显示成已保存。', 'muted'));
  }
}

function showObservationSource(force = false) {
  if (force) observationUI.selectedRange = null;
  const candidate = observationUI.view?.sources?.find(s => s.id === $('observationSource').value),
    source = candidate?.url ? candidate : null;
  const audio = source?.modalities?.length === 1 && source.modalities[0] === 'audio';
  $('observationVideo').hidden = !!audio || !source;
  $('observationAudio').hidden = !audio || !source;
  const video = audio ? $('observationAudio') : $('observationVideo');
  const identity = source ? JSON.stringify([source.id, source.media_ref, source.start_ms || 0,
    source.end_ms ?? null
  ]) : null;
  // A growing recording changes on each status poll. Keep the chosen playback
  // range until it finishes or the user explicitly requests a newer range.
  if (!force && source && observationUI.previewSource?.id === source.id &&
      (observationUI.selectedRange || video.dataset.checkingFrames === 'true' || !video.paused)) return;
  if (observationUI.source !== identity) {
    pauseMedia($('sceneDialog'));
    observationUI.source = identity;
    observationUI.previewSource = source ? {...source} : null;
    if (source) {
      rangeVideo(video, source);
      $('observationPreviewStatus').textContent = '点击播放；首次准备兼容预览可能需要稍等。';
    } else {
      video.removeAttribute('src');
      video.removeAttribute('poster');
      video.load();
    }
  }
  video.hidden = !source;
  $('observationPreviewEmpty').hidden = !!source;
  if (!source && candidate?.unavailable_reason === 'waiting_for_sealed_media')
    $('observationPreviewEmpty').textContent = '等待第一段录像录完；正在采集的文件暂不提供网页回放。';
  $('observationSourceBadge').hidden = !source;
  $('observationSourceBadge').textContent = audio ? '音频输入' : source?.managed_input ?
    '最近已录完片段 · 非实时直播' : source?.simulated ? '文件回放 · 非现场摄像头' : '摄像头';
}

function constrainObservationPreview(reset = false) {
  const source = observationUI.previewSource ||
    observationUI.view?.sources?.find(s => s.id === $('observationSource').value);
  const audio = source?.modalities?.length === 1 && source.modalities[0] === 'audio';
  const video = audio ? $('observationAudio') : $('observationVideo');
  if (!source || !Number.isFinite(video.duration)) return;
  const offset = Number(video.dataset?.rangeOffset || 0);
  const start = Math.max(0, ((source.start_ms || 0) - offset) / 1000),
    end = Math.min(source.end_ms == null ? video.duration : (source.end_ms - offset) / 1000, video
      .duration);
  if (start >= end) {
    video.pause();
    return;
  }
  if (reset || video.currentTime < start) {
    video.currentTime = start;
    return;
  }
  if (source.end_ms != null && video.currentTime >= end) {
    video.pause();
    if (video.currentTime > end) video.currentTime = end;
  }
}
async function loadObservation(force = false) {
  if (observationUI.inflight) {
    if (!force) return observationUI.inflight;
    observationUI.request++;
    await observationUI.inflight;
    return loadObservation();
  }
  const request = ++observationUI.request;
  const pending = (async () => {
    try {
      const view = await api('edge', 'observation/view');
      if (request !== observationUI.request) return;
      observationUI.view = view;
    } catch (error) {
      if (request !== observationUI.request) return;
      observationUI.view = null;
      pauseMedia($('sceneDialog'));
    }
    renderObservation();
  })();
  observationUI.inflight = pending;
  try {
    await pending;
  } finally {
    if (observationUI.inflight === pending) observationUI.inflight = null;
  }
}
$('observationSource').onchange = () => {
  showObservationSource(true);
  renderObservation();
};
$('observationModel').onchange = renderObservationModel;
$('observationExecutor').onchange = renderExecutorSampling;
$('observationAudio').onloadedmetadata = $('observationVideo').onloadedmetadata = () =>
  constrainObservationPreview();
$('observationAudio').onseeking = $('observationVideo').onseeking = () =>
  constrainObservationPreview();
$('observationAudio').ontimeupdate = $('observationVideo').ontimeupdate = () =>
  constrainObservationPreview();
$('observationVideo').onerror = () => {
  $('observationPreviewStatus').textContent = '画面暂时无法播放，请检查连接和浏览器格式支持；这不代表视频已丢失。';
};
$('playObservation').onclick = () => {
  if (observationUI.selectedRange && observationUI.previewSource?.id === $('observationSource').value) {
    return playMediaRange(observationPlayer(), ...observationUI.selectedRange, $(
      'observationPreviewStatus'));
  }
  showObservationSource(true);
  const s = observationUI.previewSource;
  if (s) playMediaRange(observationPlayer(), s.start_ms || 0, s.end_ms, $(
    'observationPreviewStatus'));
};
$('followObservation').onclick = () => {
  showObservationSource(true);
  const source = observationUI.previewSource;
  if (source?.end_ms != null) playMediaRange(observationPlayer(),
    Math.max(source.start_ms || 0, source.end_ms - 5000), source.end_ms,
    $('observationPreviewStatus'));
};
$('observationTask').onchange = () => renderObservation();
async function openObservationTask(taskId) {
  panel('sceneDialog');
  await loadObservation();
  const task = observationUI.view?.tasks.find(t => t.task_id === taskId);
  if (task) {
    $('observationSource').value = task.source_ref;
    showObservationSource();
    renderObservation();
    $('observationTask').value = task.task_id;
    renderObservation();
  }
}

$('setupObservation').onclick = () => {
  $('sceneDialog').close();
  panel('settingsDialog');
  $('observationGoal').focus();
};
$('observationGimbal').onchange = () => {
  const enabled = !!$('observationGimbal').value;
  for (const box of document.querySelectorAll('[data-observation-tool]'))
    if (['move_camera', 'stop_motion'].includes(box.dataset.observationTool)) {
      box.checked = enabled;
      box.disabled = !enabled;
    }
};
$('observationForm').onsubmit = async event => {
  event.preventDefault();
  const c = observationUI.view?.catalogue;
  if (!c) return;
  if (namedExecutor()) {
    const executor = namedExecutor(),
      goal = $('observationGoal').value.trim(),
      sampling = $('observationSampling').value;
    const selected = executor.sampling.find(s => s.id === sampling);
    const text =
      `请使用已安装的“${executor.title}”开始端侧持续观察。目标：${goal}。采样档ID：${sampling}（${samplingLabel(selected)}）。`;
    const key = 'observation:named-user-input',
      prior = state.pending[key];
    if (prior && prior.body.text !== text) {
      $('observationSaved').textContent = '上一项请求还未确认，请先重试原请求。';
      return;
    }
    const submission = prior || {
      body: {
        event_id: crypto.randomUUID(),
        text
      }
    };
    state.pending[key] = submission;
    persistDrafts();
    $('saveObservation').disabled = true;
    try {
      await api('cloud', 'input', submission.body);
      delete state.pending[key];
      persistDrafts();
      $('observationGoal').value = '';
      $('observationSaved').textContent = '目标已送入云端主对话，等待家庭助理下发及端侧实际启动确认。';
      await refresh();
      await loadObservation(true);
    } catch (error) {
      if (error.status >= 400 && error.status < 500) {
        delete state.pending[key];
        persistDrafts();
      }
      $('observationSaved').textContent = observationError(error);
    } finally {
      $('saveObservation').disabled = !observationUI.view?.catalogue;
    }
    return;
  }
  const source = c.sources.find(s => s.id === $('observationConfigSource').value),
    storage = c.storage[$('observationStorage').value];
  const selection = {
    project_id: storage.project_id,
    version: 1,
    device_ref: source.device_ref,
    source_ref: source.id,
    storage_binding_ref: $('observationStorage').value,
    model_preset_ref: $('observationModel').value,
    goal: $('observationGoal').value.trim(),
    batch_policy_ref: $('observationBatch').value,
    cache_policy_ref: c.cache.id,
    save_policy_ref: $('observationSave').value,
    ptz_scope_ref: $('observationGimbal').value || null,
    tools: [...document.querySelectorAll('[data-observation-tool]:checked')].map(x => x
      .dataset.observationTool),
    skills: [...document.querySelectorAll('[data-observation-skill]:checked')].map(x => x
      .dataset.observationSkill)
  };
  const key = 'observation:configure',
    pending = state.pending[key];
  if (pending && JSON.stringify(pending.selection) !== JSON.stringify(selection)) {
    $('observationSaved').textContent = '上一份配置尚未确认，请先重试原配置。';
    return;
  }
  const submission = pending || {
    selection,
    body: {
      request_id: crypto.randomUUID(),
      expected_version: 0,
      config: {
        ...selection,
        task_id: 'observe-' + crypto.randomUUID()
      }
    }
  };
  state.pending[key] = submission;
  persistDrafts();
  $('saveObservation').disabled = true;
  try {
    await api('edge', 'observation/configure', submission.body);
    delete state.pending[key];
    persistDrafts();
    $('observationGoal').value = '';
    $('observationSaved').textContent = '观察任务已保存，尚未启动。到现场页查看或启动。';
    await loadObservation(true);
  } catch (error) {
    if (error.status >= 400 && error.status < 500) {
      delete state.pending[key];
      persistDrafts();
    }
    $('observationSaved').textContent = observationError(error);
  } finally {
    $('saveObservation').disabled = !observationUI.view?.catalogue;
  }
};
const observationPanel = panel;
panel = function(id) {
  observationPanel(id);
  if (id === 'sceneDialog' && $('observationVideo').dataset.reloadMedia === 'true') reloadVideo($(
    'observationVideo'));
  if (['sceneDialog', 'settingsDialog'].includes(id)) loadObservation();
  if (id === 'settingsDialog') loadVoiceSettings();
};
// Settings refresh on open/actions. Visible scene metadata has a low-frequency fallback.
setInterval(() => {
  if (!document.hidden && $('sceneDialog').open) loadObservation();
}, 30000);
$('sceneDialog').addEventListener('close', () => {
  pauseMedia($('sceneDialog'));
});
