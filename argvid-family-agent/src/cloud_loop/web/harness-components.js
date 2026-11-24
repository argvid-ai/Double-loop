'use strict';
const configUI = {
    side: 'cloud',
    data: null
  },
  libraryUI = {
    items: [],
    request: 0,
    cache: {}
  };

function reportIn(id, text) {
  $(id).textContent = text;
}

function removeComponentButton(kind, id) {
  const button = el('button', '移除已停用项');
  button.onclick = async () => {
    try {
      await api(configUI.side, 'components/remove', {
        kind,
        id
      });
      await loadComponents();
    } catch (e) {
      reportIn('componentStatus', e.message);
    }
  };
  return button;
}
async function loadComponents() {
  configUI.side = $('componentSide').value;
  configUI.data = null;
  $('applyComponents').disabled = true;
  const side = configUI.side;
  reportIn('componentStatus', '正在读取实际配置…');
  try {
    const data = await api(side, 'components');
    if (side !== configUI.side) return;
    configUI.data = data;
    reportIn('componentStatus', sideName(side) + ' · 当前生效版本 ' + data.revision);
    $('applyComponents').disabled = false;
    $('builtinTools').replaceChildren();
    for (const tool of data.builtins) {
      const row = el('label', undefined, 'tool-row'),
        box = el('input');
      box.type = 'checkbox';
      box.checked = tool.enabled;
      box.disabled = tool.required;
      box.dataset.tool = tool.name;
      row.append(box, el('span', tool.name + (tool.required ? ' · 必需' : '')));
      const guide = el('div');
      guide.append(el('p', tool.description, 'muted'), raw('调用参数', tool.parameters));
      $('builtinTools').append(row, guide);
    }
    $('skillList').replaceChildren();
    for (const skill of data.skills) {
      const row = el('label', undefined, 'tool-row'),
        box = el('input');
      box.type = 'checkbox';
      box.checked = skill.enabled;
      box.disabled = skill.missing.length > 0;
      box.dataset.skill = skill.id;
      const content = el('span');
      content.append(el('b', skill.title), el('p', skill.description, 'muted'));
      if (skill.missing.length) content.append(el('p', '缺少依赖：' + skill.missing.join('、'),
        'error-text'));
      if (skill.origin === 'imported' && skill.scripts.length) content.append(el('p',
        '包含脚本资源；尚未自动接入脚本执行器。', 'muted'));
      row.append(box, content);
      $('skillList').append(row);
      if (skill.origin === 'imported' && !skill.enabled) $('skillList').append(
        removeComponentButton('skill', skill.id));
    }
    $('mcpSources').replaceChildren();
    for (const source of data.sources) {
      const card = el('section', undefined, 'source-card');
      card.append(el('h3', source.title), el('p', source.url, 'muted'), el('p', '上次检查：' + stamp(
        source.checked_at), 'muted'));
      const active = data.active.external.find(s => s.spec.id === source.id);
      for (const tool of source.tools) {
        const row = el('div', undefined, 'tool-row'),
          text = el('div');
        text.append(el('b', tool.name), el('p', tool.description, 'muted'));
        const select = el('select');
        select.setAttribute('aria-label', source.title + ' / ' + tool.name + ' 权限');
        select.dataset.source = source.id;
        select.dataset.external = tool.name;
        for (const [value, label] of [
            ['off', '不启用'],
            ['read_only', '只读查询'],
            ['task_action', '已授权任务内操作']
          ]) {
          const o = el('option', label);
          o.value = value;
          select.append(o);
        }
        select.value = active?.tools.find(t => t.name === tool.name)?.mode || 'off';
        row.append(text, select);
        card.append(row, raw('调用说明', {
          name: tool.registered_name,
          parameters: tool.schema
        }));
      }
      card.append(el('p', '权限由你按工具实际作用选择；服务的只读提示不自动等于授权。', 'muted'));
      if (!active) card.append(removeComponentButton('source', source.id));
      $('mcpSources').append(card);
    }
  } catch (e) {
    reportIn('componentStatus', e.message);
  }
}
async function loadLibrary() {
  return loadGallery();
}

async function loadCameras() {
  state.cameraSide = $('cameraSide').value;
  state.cameraId = null;
  state.cameraKind = null;
  $('sourceVideo').pause();
  $('replayPreview').hidden = true;
  $('liveCameraPreview').hidden = true;
  const side = state.cameraSide;
  renderMedia();
  try {
    const [data, sources] = await Promise.all([api(side, 'cameras'), api(side, 'sources')]);
    if (side !== state.cameraSide) return;
    $('cameraStatus').textContent = data.discovery.devices.length ? '发现' + data.discovery.devices
      .length + '个USB视频设备；添加后再测试预览。' : '尚未发现USB视频设备，可添加网络流或文件回放。';
    $('cameraFile').replaceChildren();
    for (const s of sources.sources) {
      const o = el('option', s.title);
      o.value = s.source_id;
      $('cameraFile').append(o);
    }
    $('cameraDevice').replaceChildren();
    for (const d of data.discovery.devices) {
      const o = el('option', d.title + ' · ' + d.device);
      o.value = d.device;
      $('cameraDevice').append(o);
    }
    $('cameraList').replaceChildren();
    for (const source of data.sources) {
      const card = el('article', undefined, 'source-card'),
        status = source.last_probe;
      card.append(el('h3', source.title), el('p', ({
        file_replay: '文件回放 · 模拟来源',
        usb: 'USB摄像头',
        rtsp: '网络视频流'
      })[source.kind], 'muted'), el('p', !source.enabled ? '已停用' : status ? (status
          .preview_available ? '上次预览成功' : '上次预览未成功') + ' · ' + stamp(status.checked_at) :
        '尚未测试预览', 'muted'));
      card.append(el('p', '录像：' + (source.recording_available ? '文件回放采集已装配' : '未接入') +
        ' · 视觉理解：未接入', 'muted'));
      const preview = el('button', '测试 / 刷新预览');
      preview.disabled = !source.enabled;
      preview.onclick = async () => {
        preview.disabled = true;
        reportIn('cameraStatus', '正在读取一帧画面…');
        try {
          const result = await api(side, 'cameras/' + source.id + '/probe', {});
          if (side !== state.cameraSide) return;
          state.cameraId = source.id;
          state.cameraKind = source.kind;
          reportIn('cameraStatus', result.preview_available ? '已取得预览帧；视觉理解未启用。' :
            '未取得画面，请检查地址、驱动、网络或设备占用。');
          if (result.preview_available) {
            if (source.kind === 'file_replay') {
              state.snapshots[side] = await api(side, 'snapshot');
              if (side !== state.cameraSide) return;
              state.source = source.source_id;
              state.signatures.sources = null;
              renderMedia();
              $('replayPreview').hidden = false;
              $('liveCameraPreview').hidden = true;
            } else {
              $('sourceVideo').pause();
              $('replayPreview').hidden = true;
              $('liveCameraPreview').hidden = false;
              $('liveCameraTitle').textContent = source.title;
              $('liveCameraImage').src = path(side, 'cameras/' + source.id + '/frame') +
                '?v=' + Date.now();
              $('liveCameraFacts').textContent = '单帧预览 · ' + stamp(result.checked_at) +
                ' · 录像未接入，视觉理解未启用';
            }
          } else {
            $('replayPreview').hidden = true;
            $('liveCameraPreview').hidden = true;
          }
        } catch (e) {
          reportIn('cameraStatus', e.message);
        } finally {
          preview.disabled = false;
        }
      };
      const toggle = el('button', source.enabled ? '停用' : '启用');
      toggle.onclick = async () => {
        try {
          const {
            last_probe,
            simulated,
            vision_available,
            recording_available,
            ...spec
          } = source;
          await api(side, 'cameras', {
            ...spec,
            enabled: !source.enabled
          });
          await loadCameras();
        } catch (e) {
          reportIn('cameraStatus', e.message);
        }
      };
      card.append(preview, toggle);
      $('cameraList').append(card);
    }
  } catch (e) {
    reportIn('cameraStatus', e.message);
  }
}
async function bootComponentUI() {
  const version = document.documentElement.dataset.assetVersion;
  const response = await fetch('/harness-static/harness-components.html' +
    (version ? '?v=' + encodeURIComponent(version) : ''));
  if (!response.ok) throw new Error('component UI unavailable');
  const template = document.createElement('template');
  template.innerHTML = await response.text();
  document.body.append(template.content);
  $('settingsDialog').insertBefore($('componentManager'), $('components'));
  const advanced = el('details');
  advanced.append(el('summary', '实际运行信息'), $('components'));
  $('settingsDialog').append(advanced);
  $('hardwareSettings').insertBefore($('cameraManager'), $('hardwareSettings').querySelector(
    '.media-panel'));
  $('hardwareSettings').addEventListener('toggle', () => {
    if ($('hardwareSettings').open) loadCameras();
  });
  const replay = $('sourceVideo').parentElement;
  replay.id = 'replayPreview';
  replay.hidden = true;
  $('sourceSelect').hidden = true;
  document.querySelector('label[for="sourceSelect"]').hidden = true;
  const lib = el('button', '视频');
  lib.id = 'openLibrary';
  lib.onclick = () => {
    closeDirectory();
    panel('libraryDialog');
  };
  document.querySelector('.sidebar-bottom').append(lib);
  $('openLibraryCompact').onclick = lib.onclick;
  const previousPanel = panel;
  panel = function(id) {
    previousPanel(id);
    if (id === 'settingsDialog') {
      $('componentSide').value = state.side;
      loadComponents();
    }
    if (id === 'libraryDialog') loadLibrary();
  };
  for (const b of document.querySelectorAll('#libraryDialog [data-close]')) b.onclick = () => $(b
    .dataset.close).close();
  $('componentSide').onchange = $('reloadComponents').onclick = loadComponents;
  $('mcpForm').onsubmit = async e => {
    e.preventDefault();
    const side = configUI.side;
    reportIn('componentStatus', '正在检查工具来源…');
    try {
      await api(side, 'components/probe', {
        id: $('mcpId').value,
        title: $('mcpTitle').value,
        transport: $('mcpTransport').value,
        url: $('mcpUrl').value,
        headers_file: $('mcpHeaders').value || null
      });
      if (side !== configUI.side) return;
      await loadComponents();
      reportIn('componentStatus', '检查成功。请选择工具权限，再应用配置。');
    } catch (e) {
      if (side === configUI.side) reportIn('componentStatus', e.message);
    }
  };
  $('skillForm').onsubmit = async e => {
    e.preventDefault();
    const side = configUI.side;
    try {
      const file = $('skillZip').files[0];
      let body = {
        path: $('skillPath').value || null
      };
      if (file) {
        if (file.size > 1_400_000) throw new Error('ZIP过大；请使用节点本地目录导入。');
        const bytes = new Uint8Array(await file.arrayBuffer());
        let binary = '';
        for (const c of bytes) binary += String.fromCharCode(c);
        body = {
          zip_base64: btoa(binary)
        };
      }
      await api(side, 'components/skills/import', body);
      if (side !== configUI.side) return;
      await loadComponents();
      reportIn('componentStatus', '已导入。检查依赖后勾选启用，再应用配置。');
    } catch (e) {
      if (side === configUI.side) reportIn('componentStatus', e.message);
    }
  };
  $('applyComponents').onclick = async () => {
    if (!configUI.data) return;
    const side = configUI.side;
    const external = {};
    for (const select of document.querySelectorAll('[data-external]'))
      if (select.value !== 'off') {
        external[select.dataset.source] ??= {};
        external[select.dataset.source][select.dataset.external] = select.value;
      } const body = {
      expected_revision: configUI.data.revision,
      disabled_tools: [...document.querySelectorAll('[data-tool]')].filter(x => !x.checked)
        .map(x => x.dataset.tool),
      enabled_skills: [...document.querySelectorAll('[data-skill]')].filter(x => x.checked)
        .map(x => x.dataset.skill),
      external_tools: external
    };
    try {
      $('applyComponents').disabled = true;
      await api(side, 'components/apply', body);
      if (side !== configUI.side) return;
      await loadComponents();
      await refresh();
      reportIn('componentStatus', '配置已实际应用，后续模型调用使用此工具与Skill列表。');
    } catch (e) {
      if (side === configUI.side) reportIn('componentStatus', e.message);
    } finally {
      $('applyComponents').disabled = false;
    }
  };
  $('cameraSide').onchange = $('refreshCameras').onclick = loadCameras;
  $('cameraKind').onchange = () => {
    const kind = $('cameraKind').value;
    $('cameraFileLabel').hidden = kind !== 'file_replay';
    $('cameraDeviceLabel').hidden = kind !== 'usb';
    $('cameraUrlLabel').hidden = kind !== 'rtsp';
  };
  $('cameraForm').onsubmit = async e => {
    e.preventDefault();
    const kind = $('cameraKind').value;
    try {
      await api(state.cameraSide, 'cameras', {
        id: 'camera-' + crypto.randomUUID(),
        title: $('cameraTitle').value,
        kind,
        source_id: kind === 'file_replay' ? $('cameraFile').value : null,
        device: kind === 'usb' ? $('cameraDevice').value : null,
        url: kind === 'rtsp' ? $('cameraUrl').value : null,
        enabled: true
      });
      await loadCameras();
      reportIn('cameraStatus', '来源已保存，点击测试预览确认可用性。');
    } catch (e) {
      reportIn('cameraStatus', e.message);
    }
  };
}
bootComponentUI().catch(() => notice('组件管理界面未加载，请刷新页面。'));
