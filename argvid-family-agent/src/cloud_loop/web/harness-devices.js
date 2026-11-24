'use strict';

// Connection, progress and permission to control are different facts.
function deviceOverviewRows(data, now = Date.now() / 1000) {
  const rows = [];
  const add = (name, status, detail, tone = 'unknown') => rows.push({name, status, detail, tone});
  const unknown = '状态暂未取得';
  const fresh = value => Number.isFinite(value) && now - value >= -5 && now - value <= 30;
  const discovery = data.discovery;
  const devices = recordingSettingsModel(null, null, discovery).devices;
  const cameras = [...new Set(devices.filter(d => d.kind === 'video').map(d => d.name))];
  const microphones = devices.filter(d => d.kind === 'audio');
  add('摄像头', !discovery?.scan_supported ? unknown : cameras.length ? '已发现设备' : '未发现 USB 摄像头',
    cameras.length ? cameras.join('、') + '；可提供共享录像，实际进度见下方。' :
      '此处只读识别设备，不会另开一路相机。', cameras.length ? 'ok' : 'unknown');
  const v = data.voice;
  const listening = v?.installed && fresh(v.service_tick) &&
    v.input?.state === 'listening' && fresh(v.input.last_audio_at);
  add('麦克风', listening ? '正在收音' : v?.input?.error ? '收音异常' :
    microphones.length ? '已发现设备 · 收音待确认' : unknown,
    listening ? '读取录像中的共享音轨 · 最近音频 ' + stamp(v.input.last_audio_at) :
      (microphones.length ? `发现 ${microphones.length} 个录音入口；` : '') +
      '语音读取服务未确认新音频，不代表设备被拔出。', listening ? 'ok' : 'warning');
  const out = v?.outputs || [];
  const selected = out.find(d => d.name === v?.configuration?.output_device) ||
    (out.length === 1 && !v?.configuration?.output_device ? out[0] : null);
  const outputReady = v?.installed && !v.output_error && fresh(v.service_tick) &&
    v.output_state === 'ready' && selected;
  add('音响', !v || v.output_error ? unknown : outputReady ? (v.playing ? '播报中' : '输出就绪') :
    !fresh(v.service_tick) ? '设备状态待刷新' : out.length ? '等待选择输出' : '未发现可用输出',
    (selected?.label || out.map(d => d.label).join('、') || '尚未确认输出设备') +
      '；用于语音答复，设备就绪不等于现场已经听到声音。', outputReady ? 'ok' : 'warning');
  add('机械臂', discovery?.ptz?.real_driver_installed === false ? '控制尚未接入' :
    discovery?.ptz?.real_driver_installed === true ? '已安装驱动 · 待核实连接' : unknown,
    '目前不能据此确认机械臂能运动；本页不发送电机指令。', 'warning');
  for (const side of ['cloud', 'edge']) {
    const s = data[side];
    const issues = s?.runtime_status?.items?.filter(i => i.component.startsWith('worker_')) || [];
    const ready = processingReady(s);
    add(sideName(side), !s ? unknown : ready ? (issues.length ? '在线 · 有异常' : '在线') : '后台未就绪',
      issues.length ? issues.map(i => i.title).join('；') :
        side === 'cloud' ? '接收主对话、安排任务、调用模型；在线不等于模型请求必定成功。' :
          '管理共享录像和画面初筛，独立于云端工作。', !s ? 'unknown' : ready && !issues.length ? 'ok' : 'warning');
  }
  for (const [side, key, name, detail] of [
    ['edge', 'recording', '默认录像', '一份常驻录像供各任务共读；不会因打开本页而启停。'],
    ['edge', 'analysis', '视觉判断', '显示初筛进度；识别人或动物等具体目标，以观察任务配置为准。'],
    ['cloud', 'cloud_model', '云端模型', '显示最近一次实际请求结果，不会为刷新页面另行调用模型。'],
  ]) {
    const item = data[side]?.runtime_status?.items?.find(i => i.component === key);
    const time = item?.last_success || item?.observed_at;
    add(name, !data[side] ? unknown : item?.title || '暂无运行记录',
      detail + (time ? ' 最近记录 ' + stamp(time) : ''), item?.severity || 'unknown');
  }
  const voiceProblem = v && (v.configuration_error || v.input?.error ||
    Object.keys(v.errors || {}).length || !fresh(v.service_tick));
  add('语音交互', !v ? unknown : !v.installed ? '未配置' :
    !v.configuration?.enabled ? '已关闭' : voiceProblem ? '已启用 · 有异常' :
    listening ? (outputReady ? '收音与输出就绪' : '正在收音 · 等待音响') : '已启用 · 等待收音',
    '唤醒后进入原主对话；现场唤醒、识别和可听答复仍需整体验证。',
    listening && outputReady && !voiceProblem ? 'ok' : 'warning');
  const r = data.reader;
  add('R1 看图', r?.state === 'active' ? (r.observe_only ? '服务运行 · 只读画面' : '服务运行 · 模式待核实') :
    ({inactive: '已停止', failed: '服务故障', not_installed: '未安装', activating: '启动中'}[r?.state] || unknown),
    '读取同一份录像；这里只确认进程与模式，不代表电机控制或最近一次看图成功。',
    r?.state === 'active' && r.observe_only ? 'ok' : 'warning');
  // Guard reports describe the whole installation, not the node serving the snapshot.
  const guard = [...new Set([data.cloud, data.edge].flatMap(s =>
    s?.runtime_status?.items?.filter(i => i.component === 'service_guard').map(i => i.title) || []))];
  add('保护告警', guard.length ? '有告警' : data.cloud && data.edge ? '本次未报告告警' : unknown,
    guard.length ? guard.join('；') : '此处只汇总服务保护告警；没有告警不等于所有故障恢复都已验收。',
    guard.length ? 'warning' : 'unknown');
  return rows;
}

(() => {
  const dialog = $('devicesDialog');
  const cells = new Map();
  for (const [title, names] of [
    ['硬件接入 · Muse 可以使用什么', ['摄像头', '麦克风', '音响', '机械臂']],
    ['运行服务 · 现在正在做什么', ['主助理', '观察服务', '默认录像', '视觉判断', '云端模型', '语音交互', 'R1 看图', '保护告警']],
  ]) {
    const group = document.createElement('section');
    const heading = document.createElement('h3');
    heading.textContent = title;
    const list = document.createElement('dl');
    for (const name of names) {
      const label = document.createElement('dt');
      label.textContent = name;
      const value = document.createElement('dd');
      const status = document.createElement('strong');
      const detail = document.createElement('small');
      value.append(status, detail);
      cells.set(name, {value, status, detail});
      list.append(label, value);
    }
    group.append(heading, list);
    $('deviceRows').append(group);
  }
  let timer;
  async function load() {
    if ($('refreshDevices').disabled) return;
    clearTimeout(timer);
    $('refreshDevices').disabled = true;
    $('devicesChecked').textContent = '正在读取板端状态…';
    for (const {value, status, detail} of cells.values()) {
      value.dataset.tone = 'unknown';
      status.textContent = '读取中…';
      detail.textContent = '';
    }
    const data = {};
    let failures = 0;
    await Promise.all([
      ['discovery', 'edge', 'input-media/devices'],
      ['reader', 'cloud', 'components/r1'],
      ['voice', 'cloud', 'voice/device'],
      ['edge', 'edge', 'snapshot'],
      ['cloud', 'cloud', 'snapshot'],
    ].map(async ([key, side, route]) => {
      try { data[key] = await api(side, route); }
      catch { failures++; }
      for (const row of deviceOverviewRows(data)) {
        const {value, status, detail} = cells.get(row.name);
        value.dataset.tone = row.tone;
        status.textContent = row.status;
        detail.textContent = row.detail;
      }
    }));
    $('devicesChecked').textContent = (failures ? '部分状态未取得 · ' : '已更新 · ') +
      new Date().toLocaleTimeString('zh-CN') + ' · 打开时每 15 秒刷新';
    $('refreshDevices').disabled = false;
    if (dialog.open && !document.hidden) timer = setTimeout(load, 15000);
  }
  $('openDevices').onclick = () => { panel('devicesDialog'); load(); };
  $('refreshDevices').onclick = load;
  dialog.addEventListener('close', () => clearTimeout(timer));
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (dialog.open && !document.hidden) load();
  });
})();
