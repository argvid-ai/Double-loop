'use strict';
const galleryUI = {
  mode: 'sources',
  view: null,
  items: [],
  marks: [],
  request: 0,
  markerRequest: 0,
  play: null
};
const decisionNames = {
  keep: '保留',
  discard: '不选',
  uncertain: '待定'
};

function galleryTile(title, subtitle, poster, action, badge) {
  const card = el('article', undefined, 'gallery-card'),
    button = el('button', undefined, 'gallery-cover');
  button.setAttribute('aria-label', '播放 ' + title);
  if (poster) {
    const img = el('img');
    img.src = poster;
    img.alt = title + '的真实画面';
    img.loading = 'lazy';
    img.onerror = () => {
      img.hidden = true;
      button.append(el('span', '画面暂不可用，点击重试播放', 'muted'));
    };
    button.append(img);
  }
  if (badge) button.append(el('span', badge, 'gallery-badge'));
  if (action) {
    button.append(el('span', '▶', 'gallery-play'));
    button.onclick = action;
  } else {
    button.disabled = true;
    button.setAttribute('aria-label', title + ' · 文件不可用');
  }
  card.append(button, el('h3', title), el('p', subtitle, 'muted'));
  return card;
}
async function galleryPlay(source, title, startMs, endMs) {
  if (source.availability?.state === 'deleting') {
    $('libraryStatus').textContent = '这段录像的回收尚未完成，暂不可读取；记录仍保留。';
    return;
  }
  if (source.availability && source.availability.state !== 'available') {
    $('libraryStatus').textContent = source.availability.reason === 'unanalysed_expired' ?
      '这段录像仍有未分析范围时已过期。原文件已回收，时间范围和处理记录仍保留。' :
      '这段原录像已按保留策略过期，时间范围和处理记录仍保留。';
    return;
  }
  if (source.metadata_pending) {
    try {
      const item = await api('edge', 'input-media/media/' + encodeURIComponent(source.media_ref) +
          '/info'),
        base = 'input-media/media/' + item.media_ref;
      source = {
        ...source,
        metadata_pending: false,
        url: base,
        preview_url: item.facts.modalities.includes('image') ? base + '/preview' : base,
        poster_url: item.facts.modalities.includes('image') ? base + '/poster' : null,
        preview_start_ms: item.start_ms,
        start_ms: item.start_ms,
        end_ms: item.end_ms,
        modalities: item.facts.modalities
      };
    } catch (error) {
      $('libraryStatus').textContent = '该历史原片暂时无法读取，证据记录仍保留：' + error.message;
      return;
    }
  }

  if (source.start_ms != null) startMs = Math.max(startMs, source.start_ms);
  if (source.end_ms != null) endMs = Math.min(endMs ?? source.end_ms, source.end_ms);
  const audio = source.modalities?.length === 1 && source.modalities[0] === 'audio';
  $('galleryVideo').hidden = !!audio;
  $('galleryAudio').hidden = !audio;
  const video = audio ? $('galleryAudio') : $('galleryVideo');
  pauseMedia();
  $('galleryPlayer').hidden = false;
  $('galleryPlayerTitle').textContent = title;
  rangeVideo(video, source);
  $('galleryOriginal').href = path('edge', source.url);
  galleryUI.play = () => playMediaRange(video, startMs, endMs, $('galleryPlaybackStatus'));
  $('galleryPlayer').scrollIntoView({
    block: 'nearest',
    behavior: 'smooth'
  });
  await galleryUI.play();
}
async function galleryMarkers() {
  const taskId = $('galleryTask').value,
    request = ++galleryUI.markerRequest;
  galleryUI.marks = [];
  if (!taskId) {
    renderGallery();
    return;
  }
  $('libraryStatus').textContent = '正在读取该任务的真实时间标记…';
  try {
    let after = -1,
      page, marks = [];
    do {
      page = await api('edge', 'observation/markers?task_id=' + encodeURIComponent(taskId) +
        '&after_sequence=' + after + '&limit=200');
      marks.push(...page.markers);
      after = page.next_after_sequence;
    } while (after !== null);
    if (request !== galleryUI.markerRequest || galleryUI.mode !== 'markers') return;
    galleryUI.marks = marks;
    renderGallery();
  } catch (error) {
    if (request === galleryUI.markerRequest) $('libraryStatus').textContent = '标记读取失败：' + error
      .message;
  }
}

function renderGallery() {
  const box = $('libraryItems'),
    view = galleryUI.view;
  box.replaceChildren();
  $('galleryMarkerFilters').hidden = galleryUI.mode !== 'markers';
  for (const button of document.querySelectorAll('[data-gallery]')) button.setAttribute(
    'aria-pressed', String(button.dataset.gallery === galleryUI.mode));
  if (galleryUI.mode === 'sources') {
    for (const source of [...(view?.sources || []).filter(s => !s.managed_input), ...(view
        ?.media_inputs || [])]) {
      if (!source.url) continue;
      const expired = source.availability && source.availability.state !== 'available';
      const duration = source.end_ms == null ? '已登记范围' : ((source.end_ms - (source.start_ms || 0)) /
        1000).toFixed(1) + '秒';
      const range = source.end_ms == null ? '' :
        ` · 原文件 ${markerTime(source.start_ms||0)}–${markerTime(source.end_ms)}`;
      const title = videoTitle(source.title, source.recorded_start_at || source.created || source.registered_at, '原始录像');
      box.append(galleryTile(title, duration + range, source.poster_url ? path('edge', source
          .poster_url) : null, () => galleryPlay(source, title, source.start_ms || 0,
          source.end_ms), expired ? (source.availability.state === 'deleting' ? '回收待完成' :
          '已过期 · 记录保留') : source.modalities?.length === 1 && source
        .modalities[0] === 'audio' ?
        '音频素材' : '影音素材'));
    }
    for (const item of galleryUI.items.filter(i => i.category !== 'deliverable')) {
      const url = path(item.side, item.url);
      const unavailable = assetUnavailable(item);
      const binding = item.provenance?.binding;
      const range = binding?.start_ms != null && binding?.end_ms != null ?
        ` · 原片 ${markerTime(binding.start_ms)}–${markerTime(binding.end_ms)}` : '';
      box.append(galleryTile(item.title, assetAvailabilityText(item) || (sideName(item.side) +
        (item.category === 'original' ? ' · 已登记原片' : ' · 工作素材') + range),
        unavailable ? null : url + '?preview=poster',
        unavailable ? null : () => galleryAsset(item),
        unavailable ? '文件不可用 · 记录保留' : item.category === 'original' ? '原片' : '工作素材'));
    }
    $('libraryStatus').textContent = box.childElementCount ?
      `${box.childElementCount}份输入素材 · 点击画面播放。长原片预览已配置范围，原文件可单独打开。` : '尚未登记视频素材。';
  } else if (galleryUI.mode === 'markers') {
    const marks = galleryUI.marks.filter(m => $('galleryDecision').value === 'all' || m.decision ===
      $('galleryDecision').value);
    for (const mark of marks) {
      const parts = mark.media_parts?.length ? mark.media_parts : [mark.media_ref];
      for (const [partIndex, ref] of parts.entries()) {
        const source = view?.media_inputs?.find(s => s.media_ref === ref) || view?.sources.find(s => s.id === mark.source_ref && !s.managed_input) || {id: ref,title: '历史原片',media_ref: ref,metadata_pending: true};
        const title = markerTime(mark.start_ms) + '–' + markerTime(mark.end_ms) + ' · ' + decisionNames[mark.decision] + (parts.length > 1 ? ` · 原段 ${partIndex + 1}/${parts.length}` : '');
        const poster = source.poster_url ? path('edge', source.poster_url) : parts.length === 1 ? path('edge', 'observation/markers/' + encodeURIComponent($('galleryTask').value) + '/' + encodeURIComponent(mark.id) + '/frame') : null;
        box.append(galleryTile(title, source.title, source.modalities?.length === 1 && source.modalities[0] === 'audio' ? null : poster, () => galleryPlay(source, title, mark.start_ms, mark.end_ms), decisionNames[mark.decision]));
      }
    }
    $('libraryStatus').textContent = marks.length ? `${marks.length}条时间标记 · 回看原录像对应范围，尚未剪成独立视频。` :
      '当前任务在这个状态下没有时间标记；待定不会显示为不选。';
  } else {
    const exports = galleryUI.items.filter(i => i.category === 'deliverable');
    for (const item of exports) {
      const url = path(item.side, item.url);
      const unavailable = assetUnavailable(item);
      const location = item.local_copy_role === 'delivery_cache' && item.storage_side === 'edge'
        ? 'O6N 原件 · 网页按需读取' : sideName(item.side) + ' · 已导出视频';
      box.append(galleryTile(item.title, assetAvailabilityText(item) || location,
        unavailable ? null : url + '?preview=poster',
        unavailable ? null : () => galleryAsset(item),
        unavailable ? '已清理／不可用 · 记录保留' : '已导出'));
    }
    for (const item of view?.media || []) {
      const id = item.asset_id || item.id,
        url = 'observation/media/' + encodeURIComponent(id);
      if (exports.some(x => x.side === 'edge' && x.url === url)) continue;
      box.append(galleryTile('观察保留视频', '端侧 · 原片段与声音保留', path('edge', url) + '?preview=poster', () =>
        galleryAsset({
          title: '观察保留视频',
          side: 'edge',
          url
        }), '已导出'));
    }
    $('libraryStatus').textContent = box.childElementCount ? `${box.childElementCount}个已导出视频` :
      '尚无已导出视频。当前任务只标记原录像时间，请在“输入素材”或“时间标记”回看。';
  }
  if (!box.childElementCount) box.append(el('p', '这里还没有内容', 'gallery-empty'));
}
async function galleryAsset(item) {
  if (assetUnavailable(item)) {
    $('libraryStatus').textContent = assetAvailabilityText(item);
    return;
  }
  pauseMedia();
  $('galleryPlayer').hidden = false;
  const video = $('galleryVideo');
  video.hidden = false;
  $('galleryAudio').hidden = true;
  video.dataset.rangeOffset = '0';
  video.dataset.rangeEnd = '';
  $('galleryPlayerTitle').textContent = item.title;
  const url = path(item.side, item.url);
  browserVideo(video, url);
  $('galleryOriginal').href = url;
  galleryUI.play = () => playMediaRange(video, 0, null, $('galleryPlaybackStatus'));
  $('galleryPlayer').scrollIntoView({block: 'nearest', behavior: 'smooth'});
  await galleryUI.play();
}
async function loadGallery() {
  const request = ++galleryUI.request;
  $('libraryStatus').textContent = '正在读取素材与任务…';
  document.querySelectorAll('[data-gallery]').forEach(button => button.onclick = () => {
    galleryUI.mode = button.dataset.gallery;
    galleryUI.markerRequest++;
    pauseMedia($('galleryPlayer'));
    $('galleryPlayer').hidden = true;
    renderGallery();
    if (galleryUI.mode === 'markers') galleryMarkers();
  });
  $('galleryTask').onchange = () => {
    pauseMedia($('galleryPlayer'));
    $('galleryPlayer').hidden = true;
    galleryMarkers();
  };
  $('galleryDecision').onchange = renderGallery;
  $('reloadGallery').onclick = loadGallery;
  $('closeGalleryPlayer').onclick = () => {
    pauseMedia($('galleryPlayer'));
    $('galleryVideo').dataset.playRequest = String(Number($('galleryVideo').dataset
      .playRequest || 0) + 1);
    $('galleryPlayer').hidden = true;
  };
  $('replayGalleryVideo').onclick = () => galleryUI.play?.();
  const results = await Promise.allSettled([api('edge', 'observation/view'), api('edge',
    'library'), api('cloud', 'library')]);
  if (request !== galleryUI.request) return;
  galleryUI.view = results[0].status === 'fulfilled' ? results[0].value : null;
  galleryUI.items = results.slice(1).flatMap(r => r.status === 'fulfilled' ? r.value.items : [])
    .map(item => ({...item, title: videoTitle(item.title, item.created)}));
  const tasks = (galleryUI.view?.tasks || []).filter(t => t.output_mode === 'markers');
  selectOptions('galleryTask', tasks.map((t, i) => [t.task_id, (i === 0 ? '最近任务 · ' : '') + t.goal
    .slice(0, 60)
  ]));
  renderGallery();
  if (galleryUI.mode === 'markers') await galleryMarkers();
  if (results.some(r => r.status === 'rejected')) $('libraryStatus').textContent +=
    ' 部分节点暂时无法读取，请刷新重试。';
}
