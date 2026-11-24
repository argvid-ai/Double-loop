'use strict';
// One settings surface and one main conversation. Preview never submits speech.
const voiceSettings = {epoch: 0, busy: false, installed: false, loaded: false,
  polling: false, dirty: false, saved: null};
const voiceStyle = document.createElement('link');
voiceStyle.rel = 'stylesheet';
voiceStyle.href = '/harness-static/harness-settings.css?v=' + document.documentElement.dataset.assetVersion;
document.head.append(voiceStyle);
$('voiceSettings').open = true;
$('voiceSettings').innerHTML = `
  <summary>语音唤醒</summary>
  <div class="settings-intro"><p class="eyebrow">叫我的名字，接着说你的问题</p>
    <h3>一句话，回到同一个主对话。</h3>
    <p class="muted">唤醒只决定这句话是否交给 Muse，不会新建、关闭或清空聊天。</p></div>
  <div class="settings-current" aria-live="polite">
    <span class="muted">当前生效的唤醒词</span><div id="activeWakeWords" class="wake-chips">正在读取…</div>
    <p id="voiceCurrentExample" class="muted"></p>
  </div>
  <p id="voiceDeviceStatus" class="muted" role="status">正在连接板端语音服务…</p>
  <form id="wakeForm">
    <label class="settings-switch"><input id="voiceEnabled" type="checkbox" disabled> 开启板端语音交互</label>
    <label for="wakeWord">修改唤醒词 <span class="muted">每行一个，最多5个，每个最多32字</span></label>
    <textarea id="wakeWord" rows="3" maxlength="164" required disabled aria-describedby="wakeHelp" placeholder="读取设置后即可修改"></textarea>
    <p id="wakeHelp" class="muted">从句子开头匹配。中文允许同声调的同音转写，例如“小芯”与“小心”；英文只匹配原词，Muse 不会匹配 MISS。问句本身不会被替换。</p>
    <details class="settings-advanced"><summary>收音与音响设置</summary>
      <div class="settings-pair">
        <label for="followupSeconds">回答播完后，免唤醒追问（秒）
          <input id="followupSeconds" type="number" min="5" max="300" step="1" required disabled>
        </label>
        <label for="voiceOutput">板子上的音响
          <select id="voiceOutput" disabled><option value="">只有一个音响时自动选择</option></select>
        </label>
        <label for="voiceVolume">播报音量（%）
          <input id="voiceVolume" type="number" min="0" max="100" step="1" required disabled>
        </label>
      </div>
      <p class="muted">这是当前已有的短时追问机制，不是聊天时限，也不是无限收音。只叫名字时，会短暂等待你补完问题；播报时暂不接新口令。</p>
      <p class="muted">目前读共享录像里的实时声音，不另占麦克风。录像关闭时等待，不会擅自开录；语音开关也不会改变录像。没有音响仍可接话，答复可在主对话查看。</p>
    </details>
    <div class="settings-actions"><button id="saveWake" class="primary" disabled>保存设置</button>
      <button id="reloadWake" type="button">重新读取，放弃未保存编辑</button><span id="wakeDirty" class="muted"></span></div>
  </form>
  <p id="voiceResult" class="settings-feedback" role="status"></p>
  <section class="settings-test">
    <h3>先检查一句话</h3>
    <p class="muted">使用上面编辑框里的词，只检查文字匹配。不保存设置、不发送问题、不调用模型，也不播放声音。</p>
    <form id="wakePreviewForm"><label for="wakePreviewText">假设语音识别出了这句话</label>
      <input id="wakePreviewText" maxlength="500" required placeholder="小心，现在有多少视频？">
      <button id="testWake" type="submit" disabled>检查文字匹配</button></form>
    <p id="wakePreviewResult" class="settings-feedback" role="status"></p>
  </section>
  <section class="settings-test"><h3>再试现场说话</h3>
    <p id="voiceLiveInstructions" class="muted">保存后，对板子说唤醒词和完整问题。这里显示识别结果，实际答复在原主对话。</p>
    <div class="voice-check-grid"><div><span class="muted">最近现场识别</span><p id="voiceHeard">尚未取得</p></div>
      <div><span class="muted">最近处理／播放</span><p id="voiceLastReply">尚未取得</p></div></div>
    <p class="muted">文字匹配通过 ≠ 现场能听清；生成音频 ≠ 你已经听到。最后需要实际说话并确认音响答复。</p>
  </section>
  <details><summary>在主对话测试查询能力</summary>
    <p class="muted">只把问题填入原主对话，仍由你点击发送。文字提问不需要唤醒词，不验证麦克风。</p>
    <div id="voiceScenarios" class="observation-actions">
      <button type="button" data-voice-question="现在有多少视频？请区分原片、候选和已批准成片，查询后简短告诉我。">视频数量</button>
      <button type="button" data-voice-question="现在磁盘用了多少，还剩多少？请区分全机空闲与录像管理额度，查询后简短告诉我。">空间占用</button>
      <button type="button" data-voice-question="录像现在还在运行吗？最新录到什么时候？请查询后简短告诉我，不要改变服务。">录像状态</button>
    </div><p id="voiceScenarioState" class="muted" role="status"></p>
  </details>`;
$('settingsDialog').insertBefore($('voiceSettings'), $('observationSettings'));

function voiceWords() {
  const words = $('wakeWord').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  if (!words.length || words.length > 5 || words.some(w => [...w].length > 32) ||
      new Set(words.map(w => w.toLowerCase())).size !== words.length)
    throw new Error('请填写1到5个不重复的唤醒词，每个最多32字。');
  return words;
}
function showVoiceSettings(c, devices = []) {
  $('wakeWord').value = c.wake_words.join('\n');
  $('followupSeconds').value = c.followup_seconds;
  $('voiceEnabled').checked = c.enabled !== false;
  $('voiceVolume').value = Math.round((c.volume ?? 0.35) * 100);
  const output = $('voiceOutput');
  output.replaceChildren(new Option('只有一个音响时自动选择', ''));
  for (const device of devices) output.add(new Option(device.label, device.name));
  if (c.output_device && !devices.some(d => d.name === c.output_device))
    output.add(new Option('已保存的音响目前未连接', c.output_device));
  output.value = c.output_device || '';
}
function showActiveWords(c) {
  voiceSettings.saved = c;
  $('activeWakeWords').replaceChildren(...c.wake_words.map(word => {
    const chip = el('span', word); chip.className = 'wake-chip'; return chip;
  }));
  const example = c.wake_words.find(w => /[\u3400-\u9fff]/.test(w)) || c.wake_words[0];
  $('voiceCurrentExample').textContent = `可以说：“${example}，现在有多少视频？”`;
  $('voiceLiveInstructions').textContent = `保存后对板子说：“${example}，现在有多少视频？”查看下面的识别结果，再到原主对话核对答复。`;
}
function renderVoiceStatus(value) {
  if (value.configuration) showActiveWords(value.configuration);
  if (!value.installed) {
    $('voiceDeviceStatus').textContent = '板端语音服务尚未配置；可保存唤醒词，但不能现场交谈。';
    return;
  }
  const names = {loading: '正在加载识别模型', disabled: '语音交互已关闭',
    waiting_recording: '等待共享录像的声音', waiting_configuration: '等待已确认的唤醒设置',
    listening: '正在听取唤醒词', retrying: '收音出错，正在独立重试', starting: '语音服务启动中'};
  const pieces = [value.playing ? '正在播报，播完继续听' : names[value.input?.state] || '正在检查收音',
    value.output_state === 'ready' ? '音响已接入' :
      value.output_state === 'choose_output' ? '请在下方选择一个音响' : '未检测到可用音响，仍可接话和查看文字'];
  if (value.configuration_error) pieces.push(value.configuration_ready ?
    '设置同步暂断，沿用已确认的唤醒词' : '设置尚未取得');
  if (value.output_error || Object.keys(value.errors || {}).length) pieces.push('部分语音工作正在重试，录像不受影响');
  $('voiceDeviceStatus').textContent = pieces.join(' · ');
  const heard = value.input?.last_recognition;
  $('voiceHeard').textContent = heard ? `“${heard.transcript}”\n${stamp(heard.recognized_at)} · ` +
    (heard.admitted ? '通过入口检查；投递结果另行核对' : '未匹配唤醒词／有效追问，未提交主对话') :
    '本次服务启动后尚未识别到完整话语。';
  const recent = value.last_event;
  const playback = {simulation: '模拟输入：未播放', delivered: '播放已结束（听感需你确认）',
    playing: '正在播放', ready: '等待播放', retry: '播放准备失败，正在重试',
    uncertain: '播放结果不确定，不自动重复播报', expired: '回复已过时，未播放',
    waiting_output: '等待音响', voice_not_requested: '未调用语音工具，仅文字回复'};
  $('voiceLastReply').textContent = recent ? (recent.simulated ? '【模拟测试，不是现场】\n' : '【现场输入】\n') +
    (playback[recent.playback?.status] || ({reply_ready: '主对话已答复，正在检查音频',
      pending: '等待送入主对话', accepted: '主对话已受理'}[recent.status] || recent.text || '正在处理')) :
    '尚无已受理的语音问题。';
  if (value.synthesis?.error === 'voice_reply_tool_not_called')
    $('voiceLastReply').textContent += '\n最近答复没有调用语音工具，未安排播报。';
  const select = $('voiceOutput'), chosen = select.value;
  const wanted = (value.outputs || []).map(d => d.name);
  if (chosen && !wanted.includes(chosen)) wanted.push(chosen);
  if ([...select.options].slice(1).map(o => o.value).join('\n') !== wanted.join('\n')) {
    select.replaceChildren(new Option('只有一个音响时自动选择', ''));
    for (const d of value.outputs || []) select.add(new Option(d.label, d.name));
    if (chosen && !(value.outputs || []).some(d => d.name === chosen))
      select.add(new Option('已选音响目前未连接', chosen));
    select.value = chosen;
  }
}
const voiceFields = ['wakeWord','followupSeconds','voiceEnabled','voiceVolume','voiceOutput','saveWake','testWake'];
function enableVoiceFields() {
  for (const id of voiceFields) $(id).disabled = !voiceSettings.loaded || voiceSettings.busy ||
    (!voiceSettings.installed && ['voiceEnabled','voiceVolume','voiceOutput'].includes(id));
}
async function loadVoiceSettings() {
  if (voiceSettings.busy) return;
  const epoch = ++voiceSettings.epoch;
  try {
    const value = await api('cloud', 'voice/device');
    const config = value.installed ? value.configuration : await api('edge', 'voice/config');
    if (epoch !== voiceSettings.epoch) return;
    voiceSettings.installed = !!value.installed;
    voiceSettings.loaded = true;
    if (!voiceSettings.dirty) showVoiceSettings(config, value.outputs);
    renderVoiceStatus(value); showActiveWords(config); enableVoiceFields();
  } catch (error) {
    $('voiceDeviceStatus').textContent = '暂时连不上语音服务，无法确认当前状态；录像不受影响。' + error.message;
  }
}
for (const id of voiceFields.slice(0, 5)) $(id).oninput = () => {
  voiceSettings.epoch++; voiceSettings.dirty = true;
  $('wakeDirty').textContent = '有未保存的修改；上方仍显示实际生效值';
  $('wakePreviewResult').textContent = '';
};
$('reloadWake').onclick = () => {
  voiceSettings.dirty = false; $('wakeDirty').textContent = ''; loadVoiceSettings();
};
$('wakeForm').onsubmit = async event => {
  event.preventDefault();
  if (voiceSettings.busy || !voiceSettings.loaded) return;
  let words;
  try { words = voiceWords(); } catch (error) { $('voiceResult').textContent = error.message; return; }
  voiceSettings.busy = true; voiceSettings.epoch++; enableVoiceFields();
  $('reloadWake').disabled = true;
  const body = {wake_words: words, followup_seconds: $('followupSeconds').valueAsNumber};
  if (voiceSettings.installed) Object.assign(body, {enabled: $('voiceEnabled').checked,
    volume: $('voiceVolume').valueAsNumber / 100, output_device: $('voiceOutput').value});
  try {
    const applied = await api(voiceSettings.installed ? 'cloud' : 'edge',
      voiceSettings.installed ? 'voice/device/config' : 'voice/config', body, 'PUT');
    voiceSettings.dirty = false; $('wakeDirty').textContent = '';
    showVoiceSettings(applied); showActiveWords(applied);
    $('voiceResult').textContent = `已保存：${applied.wake_words.join('、')}。接下来可测试文字匹配，再试现场说话。保存本身不代表声学测试通过。`;
  } catch (error) {
    $('voiceResult').textContent = '保存尚未确认，编辑内容已保留。可重新读取核对，不要把它当作已生效。' + error.message;
  } finally {
    voiceSettings.busy = false; $('reloadWake').disabled = false; enableVoiceFields();
    if (!voiceSettings.dirty) await loadVoiceSettings();
  }
};
$('wakePreviewForm').onsubmit = async event => {
  event.preventDefault(); $('testWake').disabled = true;
  try {
    const result = await api('cloud', 'voice/wake-preview',
      {wake_words: voiceWords(), text: $('wakePreviewText').value});
    $('wakePreviewResult').textContent = result.matched ?
      `匹配到“${result.wake_word}”。${result.request_text ? '问题会是：“' + result.request_text + '”' : '这里只叫了名字，尚未提供问题。'}\n仅文字检查，没有发送或播放${voiceSettings.dirty ? '；这些词尚未保存' : ''}。` :
      '未匹配到句首唤醒词。仅测试文字，不判断现场追问窗口，也没有发送问题。';
  } catch (error) { $('wakePreviewResult').textContent = error.message; }
  finally { enableVoiceFields(); }
};
$('voiceScenarios').onclick = async event => {
  const question = event.target.closest('[data-voice-question]')?.dataset.voiceQuestion;
  if (!question) return;
  const main = state.snapshots.cloud?.conversations.find(c => c.kind === 'main');
  if (!main) { $('voiceScenarioState').textContent = '主对话尚未连接，请稍后重试。'; return; }
  try {
    await choose('cloud', main.id); $('settingsDialog').close();
    if ($('prompt').value.trim()) notice('主对话已有未发送草稿，已保留。请先处理草稿，再选择试问。');
    else { $('prompt').value = question; saveDraft(); }
    $('prompt').focus();
  } catch (error) { $('voiceScenarioState').textContent = '暂时无法打开主对话：' + error.message; }
};
setInterval(async () => {
  if (voiceSettings.polling || voiceSettings.busy || document.hidden || !$('settingsDialog').open) return;
  voiceSettings.polling = true;
  try { renderVoiceStatus(await api('cloud', 'voice/device')); }
  catch { $('voiceDeviceStatus').textContent = '暂时查不到语音状态，等待恢复；不代表麦克风或音响已拔出。'; }
  finally { voiceSettings.polling = false; }
}, 2500);
