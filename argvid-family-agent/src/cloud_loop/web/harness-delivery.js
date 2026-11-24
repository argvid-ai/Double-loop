'use strict';
(() => {
  let current, loading = false, dirty = false, request = 0;
  const section = el('details');
  section.id = 'deliverySettings';
  section.innerHTML = `<summary>任务汇报与通知</summary>
    <p class="muted">任务可以持续工作、及时保存成片；什么时候向你汇报，在这里单独设置。工作唤醒时间不在这里修改。</p>
    <label for="deliveryScope">配置范围<select id="deliveryScope"><option value="">通用偏好</option></select></label>
    <p id="deliveryCurrent" class="muted" role="status">打开时读取实际配置。</p>
    <form id="deliveryForm"><div class="settings-pair">
      <label for="deliveryMode">正式成果如何汇报<select id="deliveryMode" disabled>
        <option value="inherit">沿用通用偏好</option><option value="on_result">有正式成果时通知</option>
        <option value="interval">按我设置的周期合并</option><option value="on_request">只保存，我询问时再看</option></select></label>
      <label id="deliveryIntervalField" for="deliveryInterval" hidden>汇总间隔（小时）
        <input id="deliveryInterval" type="number" min="0.0166666667" max="8784" step="any" placeholder="由你设置，例如 24 或 168"></label>
    </div><label class="settings-switch"><input id="deliveryAgent" type="checkbox" disabled> 允许任务自行调整自己的汇报节奏</label>
    <p class="muted">只有勾选才授权任务调整；它不能修改通用偏好或其他任务。确实需要你补充信息、选择或授权的问题会单独提出，不等普通汇总。</p>
    <div class="settings-actions"><button id="saveDelivery" class="primary" disabled>保存汇报设置</button>
      <button id="reloadDelivery" type="button">重新读取，放弃未保存编辑</button></div>
    </form><p id="deliveryFeedback" class="settings-feedback" role="status"></p>`;
  $('voiceSettings').after(section);
  const nav = el('nav'); nav.className = 'settings-shortcuts'; nav.setAttribute('aria-label', '设置分类');
  for (const [id, text] of [['voiceSettings','语音唤醒'], ['deliverySettings','任务汇报'], ['recordingSettings','默认录像']]) {
    const button = el('button', text); button.type = 'button';
    button.onclick = () => { const target = $(id); if (!target) return; if (target.tagName === 'DETAILS') target.open = true; target.scrollIntoView({block:'start'}); };
    nav.append(button);
  }
  const back = el('button', '返回对话'); back.type = 'button';
  back.onclick = () => $('settingsDialog').close(); nav.append(back);
  $('settingsDialog').querySelector('.panel-heading').after(nav);
  function fields() {
    const mode = $('deliveryMode').value;
    $('deliveryIntervalField').hidden = mode !== 'interval';
    $('deliveryInterval').required = mode === 'interval';
    $('deliveryAgent').disabled = loading || !current || mode === 'inherit';
  }
  function description(p) {
    return p.mode === 'interval' ? `每 ${p.interval_seconds / 3600} 小时汇总新成果` :
      p.mode === 'on_request' ? '只保存成果，主动询问时查看' : '有正式成果时通知';
  }
  async function load() {
    const epoch = ++request, task = $('deliveryScope').value;
    loading = true; current = null;
    for (const id of ['deliveryScope','deliveryMode','deliveryAgent','saveDelivery']) $(id).disabled = true;
    try {
      const result = await api('cloud', 'delivery' + (task ? '?task_id=' + encodeURIComponent(task) : ''));
      if (epoch !== request) return;
      current = result;
      const inherited = task && !result.policy;
      $('deliveryMode').querySelector('[value=inherit]').hidden = !task;
      $('deliveryMode').value = inherited ? 'inherit' : result.effective.mode;
      $('deliveryInterval').value = result.effective.interval_seconds == null ? '' : result.effective.interval_seconds / 3600;
      $('deliveryAgent').checked = result.effective.allow_agent_changes;
      $('deliveryCurrent').textContent = (result.source === 'existing_behavior' ?
        '尚未单独配置，保留现有方式：' : result.inherited ? '正在沿用通用偏好：' : '当前生效：') +
        description(result.effective) + `。待通知成果 ${result.pending_results} 项。` +
        (result.next_delivery_at ? ` 下一次汇总：${stamp(result.next_delivery_at)}` : '');
      dirty = false;
    } catch (error) { $('deliveryCurrent').textContent = '未能读取配置：' + error.message; }
    finally {
      loading = false;
      $('deliveryScope').disabled = false;
      for (const id of ['deliveryMode','saveDelivery']) $(id).disabled = !current;
      fields();
    }
  }
  function tasks() {
    const select = $('deliveryScope'), value = select.value;
    const entries = (state.snapshots.cloud?.tasks || []);
    select.replaceChildren(new Option('通用偏好', ''));
    for (const task of entries) select.add(new Option(shortTitle(task.goal), task.id));
    select.value = entries.some(t => t.id === value) ? value : '';
  }
  section.addEventListener('toggle', () => { if (section.open && !dirty && !loading) {tasks(); load();} });
  $('deliveryScope').onchange = () => {
    if (dirty && !window.confirm('切换任务会放弃尚未保存的汇报设置，继续吗？')) {
      $('deliveryScope').value = current?.task_id || ''; return;
    }
    dirty = false; $('deliveryFeedback').textContent = ''; load();
  };
  for (const id of ['deliveryMode','deliveryInterval','deliveryAgent']) $(id).oninput = () => {
    dirty = true; fields(); $('deliveryFeedback').textContent = '尚未保存。';
  };
  $('reloadDelivery').onclick = () => { dirty = false; load(); };
  $('deliveryForm').onsubmit = async event => {
    event.preventDefault(); if (!current || loading) return;
    const mode = $('deliveryMode').value;
    const seconds = Math.round($('deliveryInterval').valueAsNumber * 3600);
    if (mode === 'interval' && (!Number.isFinite(seconds) || seconds < 60 || seconds > 366 * 86400)) {
      $('deliveryFeedback').textContent = '请填写1分钟到366天之间的汇报间隔。'; return;
    }
    const body = {task_id: current.task_id, expected_revision: current.revision,
      policy: mode === 'inherit' ? null : {mode, interval_seconds: mode === 'interval' ? seconds : null,
        allow_agent_changes: $('deliveryAgent').checked}};
    loading = true;
    for (const id of ['saveDelivery','deliveryScope','deliveryMode','deliveryInterval','deliveryAgent','reloadDelivery']) $(id).disabled = true;
    try {
      await api('cloud','delivery',body,'PUT');
      $('deliveryFeedback').textContent = '已保存并生效；不改变录像、工作唤醒或已保存的成片。';
      dirty = false;
    } catch (error) { $('deliveryFeedback').textContent = '保存未确认，编辑已保留。' + error.message; }
    finally {
      loading = false;
      for (const id of ['saveDelivery','deliveryScope','deliveryMode','deliveryInterval','deliveryAgent','reloadDelivery']) $(id).disabled = false;
      fields();
      if (!dirty) await load();
    }
  };
})();
