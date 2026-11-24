'use strict';
const markerUI = {
  task: null,
  busy: false,
  range: null,
  playRequest: 0,
  signature: null
};
const markerPanel = el('section');
markerPanel.id = 'observationMarkerPanel';
markerPanel.hidden = true;
markerPanel.setAttribute('aria-label', '端侧时间标记');
markerPanel.append(el('h3', '时间标记'), el('p', '逐批记录，原录像与原声保留。合并和剪辑留待后续取用。', 'muted'));
const markerSummary = el('p');
markerSummary.id = 'observationMarkerSummary';
markerPanel.append(markerSummary);
const markerList = el('div');
markerList.id = 'observationMarkerList';
markerList.className = 'marker-list';
markerPanel.append(markerList);
$('observationTasks').after(markerPanel);

function markerTime(ms) {
  const seconds = Math.floor(ms / 1000);
  return String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2,
    '0');
}
async function playObservationMarker(mark) {
  const request = ++markerUI.playRequest,
    video = $('observationVideo'),
    status = $('observationPreviewStatus'),
    registered = observationUI.view.sources.find(s => s.id === mark.source_ref);
  if (!registered) return;
  $('observationSource').value = registered.id;
  pauseMedia();
  status.textContent = '正在读取这条标记对应的原录像…';
  try {
    let source = {...registered};
    if (registered.managed_input) {
      const item = await api('edge', 'input-media/media/' + encodeURIComponent(mark.media_ref) + '/info');
      if (item.recording_state === 'growing') throw new Error('这段原录像尚未录完，请稍后再播放。');
      const base = 'input-media/media/' + encodeURIComponent(item.media_ref);
      source = {...source, media_ref: item.media_ref, url: base,
        preview_url: base + '/preview', poster_url: base + '/poster',
        start_ms: item.start_ms, end_ms: item.end_ms, preview_start_ms: item.start_ms,
        modalities: item.facts.modalities};
    }
    if (request !== markerUI.playRequest || !$('sceneDialog').open ||
        $('observationSource').value !== registered.id) return;
    const start = Math.max(mark.start_ms, source.start_ms || 0),
      end = Math.min(mark.end_ms, source.end_ms ?? mark.end_ms);
    if (start >= end) throw new Error('这条标记不在所引用原片的可播放范围内。');
    observationUI.previewSource = source;
    observationUI.source = JSON.stringify([source.id, source.media_ref, source.start_ms || 0,
      source.end_ms ?? null]);
    observationUI.selectedRange = [start, end];
    rangeVideo(video, source);
    const badge = $('observationSourceBadge');
    if (badge) badge.textContent = '所选时间标记 · 原片回放';
    video.scrollIntoView({block: 'center', behavior: 'smooth'});
    await playMediaRange(video, start, end, status);
    if (request === markerUI.playRequest && (start !== mark.start_ms || end !== mark.end_ms))
      status.textContent += '；该标记跨原片，当前仅播放所引用这一份的范围。';
  } catch (error) {
    if (request === markerUI.playRequest) status.textContent = error.message;
  }
}
async function loadMarkers() {
  const task = observationUI.view?.tasks.find(t => t.task_id === observationUI.task && t
    .output_mode === 'markers');
  markerPanel.hidden = !task;
  if (task && markerUI.task !== task.task_id) {
    markerUI.task = task.task_id;
    markerUI.signature = null;
    markerList.replaceChildren();
    markerSummary.textContent = '正在读取所选任务的标记…';
  }
  if (!task || markerUI.busy) return;
  markerUI.busy = true;
  try {
    let after = -1,
      markers = [],
      page;
    do {
      page = await api('edge', 'observation/markers?task_id=' + encodeURIComponent(task.task_id) +
        '&after_sequence=' + after + '&limit=200');
      markers.push(...page.markers);
      after = page.next_after_sequence;
    } while (after !== null);
    if (observationUI.task !== task.task_id) return;
    const summary = page.summary;
    markerSummary.textContent =
      `已标记 ${markers.length} 批 · 保留 ${(summary.keep.duration_ms/1000).toFixed(1)} 秒 · 不选 ${(summary.discard.duration_ms/1000).toFixed(1)} 秒 · 待定 ${(summary.uncertain.duration_ms/1000).toFixed(1)} 秒`;
    const signature = JSON.stringify([task.task_id, markers]);
    if (markerUI.signature === signature) return;
    markerUI.signature = signature;
    markerUI.task = task.task_id;
    markerList.replaceChildren();
    for (const mark of markers) {
      const card = el('article', undefined, 'marker-card marker-' + mark.decision),
        label = ({
          keep: '保留',
          discard: '不选',
          uncertain: '待定'
        })[mark.decision];
      const thumb = el('img');
      thumb.loading = 'lazy';
      thumb.alt = '该批次实际输入画面';
      thumb.src = path('edge', 'observation/markers/' + encodeURIComponent(task.task_id) + '/' +
        encodeURIComponent(mark.id) + '/frame');
      const button = el('button',
        `${markerTime(mark.start_ms)}–${markerTime(mark.end_ms)} · ${label}`);
      button.setAttribute('aria-label', '查看原录像 ' + markerTime(mark.start_ms) + '至' + markerTime(
        mark.end_ms) + ' ' + label);
      button.onclick = () => playObservationMarker(mark);

      card.append(thumb, button);
      markerList.append(card);
    }
    if (!markers.length) markerList.append(el('p', '等待端侧实际判别并提交第一批标记。', 'muted'));
  } catch (error) {
    markerSummary.textContent = '标记暂时无法读取：' + error.message;
  } finally {
    markerUI.busy = false;
  }
}
$('observationVideo').addEventListener('timeupdate', () => {
  const video = $('observationVideo');
  if (markerUI.range && video.currentTime >= markerUI.range[1]) {
    video.pause();
    markerUI.range = null;
  }
});
const markerRender = renderObservation;
renderObservation = function() {
  markerRender();
  loadMarkers();
};
$('observationSource').addEventListener('change', () => {
  markerUI.range = null;
  markerUI.playRequest++;
});
