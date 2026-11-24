'use strict';

function pauseMedia(root = document) {
  for (const media of root.querySelectorAll('video,audio')) media.pause();
}

function invalidateVideo(video) {
  video.dataset.playRequest = String(Number(video.dataset.playRequest || 0) + 1);
  video.dataset.reloadMedia = 'true';
  video.dataset.checkingFrames = 'false';
  video.cancelFrameWait?.();
}

function reloadVideo(video) {
  const url = video.getAttribute('src');
  if (!url) return;
  video.pause();
  video.removeAttribute('src');
  video.load();
  video.src = url;
  video.preload = 'metadata';
  video.load();
  video.dataset.reloadMedia = 'false';
}

function waitVideoMetadata(video) {
  if (video.readyState >= 1) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error('预览准备超时，请重试。')), 190000);

    function finish(error) {
      clearTimeout(timer);
      video.removeEventListener('loadedmetadata', ready);
      video.removeEventListener('error', failed);
      error ? reject(error) : resolve();
    }

    function ready() {
      finish();
    }

    function failed() {
      finish(new Error('预览无法播放，请重试或打开原视频。'));
    }
    video.addEventListener('loadedmetadata', ready);
    video.addEventListener('error', failed);
  });
}

function waitVideoFrame(video) {
  if (video.tagName === 'AUDIO') return Promise.resolve('audio');
  if (typeof video.requestVideoFrameCallback !== 'function') return Promise.resolve('unconfirmed');
  video.cancelFrameWait?.();
  return new Promise(resolve => {
    let frameId = null,
      timer = null,
      settled = false;

    function finish(result) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (frameId !== null) video.cancelVideoFrameCallback(frameId);
      video.cancelFrameWait = null;
      resolve(result);
    }
    video.cancelFrameWait = () => finish('cancelled');
    frameId = video.requestVideoFrameCallback((_, metadata) => {
      video.dataset.presentedTime = String(metadata.mediaTime);
      finish('presented');
    });
    timer = setTimeout(() => finish('missing'), 3000);
  });
}

function rangeVideo(video, source) {
  video.pause();
  invalidateVideo(video);
  video.dataset.rangeEnd = '';
  video.dataset.rangeOffset = String(source.preview_start_ms || 0);
  video.dataset.sourceId = source.id;
  video.removeAttribute('src');
  video.load();
  if (source.poster_url) video.poster = path('edge', source.poster_url);
  else video.removeAttribute('poster');
  video.src = path('edge', source.preview_url || source.url);
  video.controls = true;
  video.preload = 'metadata';
  video.playsInline = true;
  video.dataset.reloadMedia = 'false';
  video.dataset.playStatus = video.id === 'observationVideo' ? 'observationPreviewStatus' :
    'galleryPlaybackStatus';
}
async function playMediaRange(video, startMs, endMs, status) {
  video.cancelFrameWait?.();
  const request = String(Number(video.dataset.playRequest || 0) + 1);
  video.dataset.playRequest = request;
  video.dataset.playStatus = status?.id || '';
  video.dataset.checkingFrames = 'true';
  video.pause();
  const offset = Number(video.dataset.rangeOffset || 0),
    start = Math.max(0, (startMs - offset) / 1000),
    end = endMs == null ? null : (endMs - offset) / 1000;
  if (status) status.textContent = '正在准备画面，首次生成网页预览可能需要稍等…';
  try {
    if (video.dataset.reloadMedia === 'true') reloadVideo(video);
    if (video.readyState < 1) {
      const metadata = waitVideoMetadata(video);
      video.load();
      await metadata;
    }
    if (video.dataset.playRequest !== request || !mediaVisible(video)) return;
    if (start >= video.duration) throw new Error('该时间标记超出可预览范围。');
    video.dataset.rangeEnd = end == null ? '' : String(Math.min(end, video.duration));
    video.currentTime = start;
    let frame = waitVideoFrame(video);
    await video.play();
    let shown = await frame;
    if (video.dataset.playRequest !== request || !mediaVisible(video) || shown === 'cancelled')
      return;
    if (shown === 'missing') {
      if (status) status.textContent = '画面尚未显示，正在重新加载视频…';
      video.dataset.frameRetries = String(Number(video.dataset.frameRetries || 0) + 1);
      reloadVideo(video);
      await waitVideoMetadata(video);
      if (video.dataset.playRequest !== request || !mediaVisible(video)) return;
      video.currentTime = start;
      frame = waitVideoFrame(video);
      await video.play();
      shown = await frame;
    }
    if (video.dataset.playRequest !== request || !mediaVisible(video) || shown === 'cancelled')
      return;
    if (shown === 'missing') {
      video.pause();
      throw new Error('未能显示视频画面，请点击重播。原录像仍保留。');
    }
    if (status) status.textContent = shown === 'audio' ? '正在播放 · 原音频' : shown === 'presented' ?
      '正在播放 · 网页媒体' : '播放已开始，请确认画面是否显示。';
  } catch (error) {
    if (video.dataset.playRequest === request && status) status.textContent = error.message;
  } finally {
    if (video.dataset.playRequest === request) video.dataset.checkingFrames = 'false';
  }
}
document.addEventListener('timeupdate', event => {
  const media = event.target,
    end = media.dataset?.rangeEnd;
  if (end && media.currentTime >= Number(end)) {
    media.pause();
    media.dataset.rangeEnd = '';
    const status = document.getElementById?.(media.dataset.playStatus);
    if (status) status.textContent = '本段已播放完，可以重新播放。';
  }
}, true);

function mediaVisible(media) {
  const dialog = media.closest('dialog'),
    details = media.closest('details');
  return !document.hidden && (!dialog || dialog.open) && (!details || details.open) && !media
    .closest('[hidden]');
}

function assetUnavailable(asset) {
  return ['expired', 'missing'].includes(asset?.availability?.state);
}

function assetAvailabilityText(asset) {
  const state = asset?.availability?.state;
  if (state === 'missing') return '视频文件暂不可用，来源和制作记录仍保留。';
  if (state === 'reloadable') return '网页副本已清理，打开时从保留原件回读。';
  if (state === 'expired') return asset.kind === 'observation_input' || asset.category === 'working' ?
    '临时素材已清理，来源和制作记录仍保留。' : '视频已按保留策略清理，制作记录仍保留。';
  return '';
}

function browserVideo(video, url) {
  const separator = url.includes('?') ? '&' : '?';
  video.controls = true;
  video.preload = 'metadata';
  video.playsInline = true;
  video.dataset.originalUrl = url;
  video.src = url + separator + 'preview=video';
  video.poster = url + separator + 'preview=poster';
  video.onerror = () => {
    let message = video.parentElement?.querySelector('[data-playback-error]');
    if (!message) {
      message = document.createElement('p');
      message.dataset.playbackError = '';
      message.className = 'error-text';
      message.setAttribute('role', 'status');
      video.after(message);
    }
    message.textContent = '视频预览暂时无法播放，请重试或打开原文件。';
  };
}
document.addEventListener('close', event => {
  if (event.target.tagName === 'DIALOG') {
    pauseMedia(event.target);
    for (const video of event.target.querySelectorAll('video')) invalidateVideo(video);
  }
}, true);
document.addEventListener('toggle', event => {
  if (event.target.tagName === 'DETAILS' && !event.target.open) pauseMedia(event.target);
}, true);
document.addEventListener('play', event => {
  if (!['VIDEO', 'AUDIO'].includes(event.target.tagName)) return;
  if (!mediaVisible(event.target)) {
    event.target.pause();
    return;
  }
  for (const media of document.querySelectorAll('video,audio'))
    if (media !== event.target) media.pause();
  const video = event.target;
  if (video.tagName === 'VIDEO' && video.dataset.playStatus && video.dataset.checkingFrames !==
    'true') {
    const offset = Number(video.dataset.rangeOffset || 0),
      end = video.dataset.rangeEnd;
    playMediaRange(video, offset + video.currentTime * 1000, end ? offset + Number(end) * 1000 :
      null, document.getElementById(video.dataset.playStatus));
  }
}, true);
document.addEventListener('pause', event => {
  const video = event.target;
  if (video.tagName === 'VIDEO' && video.paused && video.dataset.checkingFrames === 'true')
    video.cancelFrameWait?.();
}, true);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    pauseMedia();
    for (const video of document.querySelectorAll('video')) invalidateVideo(video);
  }
});
window.addEventListener('pagehide', () => {
  pauseMedia();
  for (const video of document.querySelectorAll('video')) invalidateVideo(video);
});

document.addEventListener('ended', event => {
  const status = document.getElementById(event.target.dataset?.playStatus);
  if (status) status.textContent = '播放已结束，可以重新播放。';
}, true);
