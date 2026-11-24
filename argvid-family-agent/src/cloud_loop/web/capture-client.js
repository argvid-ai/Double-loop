'use strict';
const captureElement = id => document.getElementById(id);
const captureState = {
  connected: false,
  stream: null,
  observationRecorder: null,
  voiceRecorder: null,
  abort: null,
  source: null,
  config: null,
  timer: null,
  heartbeatBusy: false,
  desired: null,
  recordingRevision: null,
  clientId: crypto.randomUUID()
};
captureElement('endpoint').value = location.origin;

function captureStatus(text) {
  captureElement('status').textContent = text;
}

function captureUrl(path) {
  const settings = captureState.config || {
    endpoint: captureElement('endpoint').value,
    source: captureElement('source').value.trim()
  };
  const base = new URL(settings.endpoint),
    local = ['localhost', '127.0.0.1', '[::1]'].includes(base.hostname);
  if ((base.protocol !== 'https:' && !(local && base.protocol === 'http:')) || base.username || base
    .password || base.search || base.hash) throw new Error('请使用安全服务地址，或本机回环地址；不要在地址中填写凭据。');
  return base.href.replace(/\/$/, '') + '/harness/capture/' + encodeURIComponent(settings.source) +
    '/' + path;
}
async function captureRequest(path, {
  method = 'GET',
  body,
  headers = {},
  voice = false
} = {}) {
  const key = captureState.config?.[voice ? 'voiceKey' : 'sourceKey'] || captureElement(voice ?
    'voiceKey' : 'sourceKey').value;
  const response = await fetch(captureUrl(path), {
    method,
    body,
    headers: {
      Authorization: 'Capture ' + key,
      ...headers
    },
    signal: captureState.abort?.signal,
    credentials: 'omit'
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || result.detail || '接收端未确认；已发送内容的结果请在云端查询。');
  return result;
}

function mediaFormat(audioOnly) {
  if (typeof MediaRecorder === 'undefined') throw new Error('此浏览器未提供录制接口，可改用文件输入。');
  const candidates = audioOnly ? ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm'] : [
    'video/mp4', 'video/webm;codecs=vp8,opus', 'video/webm'
  ];
  return candidates.find(t => MediaRecorder.isTypeSupported(t)) || '';
}

function recordCaptureWindow(stream, duration, {
  audioOnly = false,
  channel = 'observation'
} = {}) {
  return new Promise((resolve, reject) => {
    const mime = mediaFormat(audioOnly),
      recorder = new MediaRecorder(stream, mime ? {
        mimeType: mime
      } : undefined),
      parts = [];
    const key = channel + 'Recorder';
    captureState[key] = recorder;
    let timer = null;
    recorder.ondataavailable = e => {
      if (e.data.size) parts.push(e.data);
    };
    recorder.onerror = () => {
      clearTimeout(timer);
      reject(new Error('设备录制失败。'));
    };
    recorder.onstop = () => {
      clearTimeout(timer);
      if (captureState[key] === recorder) captureState[key] = null;
      resolve(new Blob(parts, {
        type: recorder.mimeType
      }));
    };
    recorder.start();
    if (duration) timer = setTimeout(() => {
      if (recorder.state !== 'inactive') recorder.stop();
    }, duration);
  });
}

function stopObservationDevice() {
  const recorder = captureState.observationRecorder;
  if (recorder && recorder.state !== 'inactive') recorder.stop();
  for (const track of captureState.stream?.getTracks() || []) track.stop();
  captureState.stream = null;
  captureElement('preview').srcObject = null;
  captureElement('preview').hidden = true;
}

function heartbeatBody() {
  return {
    client_id: captureState.clientId,
    connected: captureState.connected,
    recording: captureState.observationRecorder?.state === 'recording',
    tracks_active: !!captureState.stream?.getTracks().some(t => t.readyState === 'live')
  };
}
async function captureHeartbeat() {
  if (!captureState.connected || captureState.heartbeatBusy) return;
  captureState.heartbeatBusy = true;
  try {
    const state = await captureRequest('heartbeat', {
      method: 'POST',
      body: JSON.stringify(heartbeatBody()),
      headers: {
        'Content-Type': 'application/json'
      }
    });
    captureState.desired = state;
    if (!state.active || (captureState.recordingRevision && state.capture_revision !==
        captureState.recordingRevision)) stopObservationDevice();
  } finally {
    captureState.heartbeatBusy = false;
  }
}
async function captureLoop() {
  while (captureState.connected) {
    const state = await captureRequest('state');
    captureState.source = state.source;
    captureState.desired = state;
    if (state.source.spec.capture?.driver === 'paired_stream') {
      throw new Error('这个机位使用持续采集客户端。请在连接相机的电脑上运行已配置的设备接入程序。');
    }
    if (state.source.spec.binding.finite) {
      captureStatus('文件输入已连接。发送素材后，可在云端交代处理目标。');
      return;
    }
    if (!state.active) {
      stopObservationDevice();
      captureStatus('设备已连接，等待云端观察任务；当前不录制或发送媒体。');
      await new Promise(r => setTimeout(r, 1000));
      continue;
    }
    if (!captureState.stream) {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error(
      '摄像头/麦克风需要安全上下文与浏览器支持；文件输入仍可使用。');
      const modes = state.source.spec.binding.modalities;
      const stream = await navigator.mediaDevices.getUserMedia({
        video: modes.includes('image'),
        audio: modes.includes('audio')
      });
      if (!captureState.connected || !captureState.desired?.active || captureState.desired
        .capture_revision !== state.capture_revision) {
        for (const t of stream.getTracks()) t.stop();
        continue;
      }
      captureState.stream = stream;
      captureElement('preview').srcObject = stream;
      captureElement('preview').hidden = !modes.includes('image');
    }
    captureState.recordingRevision = state.capture_revision;
    captureStatus('正在录制独立窗口；画面和声音是观察数据，不会成为用户指令。');
    const recorded = new Date().toISOString(),
      blob = await recordCaptureWindow(captureState.stream, state.window_ms, {
        audioOnly: !state.source.spec.binding.modalities.includes('image')
      });
    if (!captureState.connected) return;
    const latest = await captureRequest('state');
    if (!latest.active || latest.capture_revision !== captureState.recordingRevision) {
      stopObservationDevice();
      captureStatus('任务已停止或变化，未把旧窗口作为新一轮输入。');
      continue;
    }
    const id = crypto.randomUUID();
    await captureRequest('window', {
      method: 'POST',
      body: blob,
      headers: {
        'Content-Type': blob.type || 'application/octet-stream',
        'x-media-event': JSON.stringify({
          event_id: id,
          segment_id: id,
          start_ms: 0,
          recorded_start_at: recorded,
          clock_authority: 'device_reported',
          capture_revision: captureState.recordingRevision
        })
      }
    });
    captureStatus('本窗口已收到；原影音保留，筛选结果以云端任务回执为准。');
  }
}

function disconnectCapture() {
  const wasConnected = captureState.connected,
    config = captureState.config;
  captureState.connected = false;
  clearInterval(captureState.timer);
  captureState.timer = null;
  stopObservationDevice();
  if (captureState.voiceRecorder && captureState.voiceRecorder.state !== 'inactive') captureState
    .voiceRecorder.stop();
  for (const t of captureState.voiceStream?.getTracks() || []) t.stop();
  captureState.voiceStream = null;
  if (wasConnected && config) {
    fetch(captureUrl('heartbeat'), {
      method: 'POST',
      headers: {
        Authorization: 'Capture ' + config.sourceKey,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(heartbeatBody()),
      keepalive: true,
      credentials: 'omit'
    }).catch(() => {});
  }
  captureState.abort?.abort();
  captureState.config = null;
  captureState.recordingRevision = null;
  for (const id of ['endpoint', 'source', 'sourceKey', 'voiceKey']) captureElement(id).disabled =
    false;
  captureElement('connect').disabled = false;
  captureElement('disconnect').disabled = true;
  captureElement('upload').disabled = true;
  captureElement('ask').disabled = true;
  window.speechSynthesis?.cancel();
}
captureElement('connectForm').onsubmit = async event => {
  event.preventDefault();
  disconnectCapture();
  captureState.abort = new AbortController();
  captureState.config = {
    endpoint: captureElement('endpoint').value,
    source: captureElement('source').value.trim(),
    sourceKey: captureElement('sourceKey').value,
    voiceKey: captureElement('voiceKey').value
  };
  try {
    const state = await captureRequest('state');
    captureState.connected = true;
    captureState.source = state.source;
    for (const id of ['endpoint', 'source', 'sourceKey', 'voiceKey']) captureElement(id)
      .disabled = true;
    captureElement('connect').disabled = true;
    captureElement('disconnect').disabled = false;
    captureElement('upload').disabled = !state.source.spec.binding.finite;
    captureElement('ask').disabled = !(captureState.config.voiceKey && state.voice_available);
    captureState.timer = setInterval(() => captureHeartbeat().catch(error => {
      disconnectCapture();
      captureStatus(error.message);
    }), 1000);
    captureLoop().catch(error => {
      if (error.name !== 'AbortError') {
        disconnectCapture();
        captureStatus(error.message);
      }
    });
  } catch (error) {
    disconnectCapture();
    captureStatus(error.message);
  }
};
captureElement('disconnect').onclick = () => {
  disconnectCapture();
  captureStatus('本机设备已断开；服务端是否确认停止以任务状态为准。');
};
captureElement('upload').onclick = async () => {
  const files = [...captureElement('uploadFile').files];
  if (!files.length) return;
  captureElement('upload').disabled = true;
  try {
    await captureRequest('begin-dataset', {
      method: 'POST'
    });
    for (const file of files) {
      const id = crypto.randomUUID();
      captureStatus('正在发送 ' + file.name);
      await captureRequest('window', {
        method: 'POST',
        body: file,
        headers: {
          'Content-Type': file.type || 'application/octet-stream',
          'x-media-event': JSON.stringify({
            event_id: id,
            segment_id: id,
            start_ms: 0
          })
        }
      });
    }
    await captureRequest('seal', {
      method: 'POST'
    });
    captureStatus('素材已核验并封闭，原文件保留。现在可在云端交代处理目标。');
  } catch (error) {
    captureStatus(error.message);
  } finally {
    captureElement('upload').disabled = !captureState.connected;
  }
};
window.addEventListener('pagehide', () => {
  disconnectCapture();
  captureElement('sourceKey').value = '';
  captureElement('voiceKey').value = '';
});

async function showVoiceReply(eventId, initial) {
  let result = initial;
  const deadline = performance.now() + 180000;
  while (captureState.connected) {
    if (result.reply) {
      const box = captureElement('reply');
      box.textContent = result.reply.text;
      box.hidden = false;
      if (captureElement('speak').checked) {
        if (window.speechSynthesis && typeof SpeechSynthesisUtterance !== 'undefined') {
          window.speechSynthesis.cancel();
          const speech = new SpeechSynthesisUtterance(result.reply.text);
          speech.lang = 'zh-CN';
          speech.onerror = () => captureStatus('文字回复已显示；本机朗读未完成。');
          window.speechSynthesis.speak(speech);
        } else captureStatus('文字回复已显示，此浏览器未提供朗读能力。');
      }
      await captureRequest('voice/' + eventId + '/delivered', {
        method: 'POST',
        voice: true,
        body: JSON.stringify({
          event_id: eventId,
          reply_digest: result.reply_digest
        }),
        headers: {
          'Content-Type': 'application/json'
        }
      });
      return;
    }
    if (['ignored', 'expired', 'rejected', 'failed'].includes(result.status)) throw new Error(
      result.text || '语音未执行：' + result.status);
    if (performance.now() >= deadline) throw new Error('本次等待已结束，尚未取得回复；请在云端查看原有输入，不要重复提交。');
    await new Promise(resolve => setTimeout(resolve, 500));
    if (!captureState.connected) return;
    result = await captureRequest('voice/' + eventId, {
      voice: true
    });
  }
}
captureElement('ask').onclick = async () => {
  if (captureState.voiceRecorder?.state === 'recording') {
    captureState.voiceRecorder.stop();
    return;
  }
  const config = captureState.config;
  if (!captureState.connected || !config?.voiceKey) return;
  captureElement('ask').disabled = true;
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('当前浏览器不能使用麦克风。');
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: true,
      video: false
    });
    if (!captureState.connected || captureState.config !== config) {
      for (const t of stream.getTracks()) t.stop();
      return;
    }
    captureState.voiceStream = stream;
    captureElement('ask').disabled = false;
    captureElement('ask').textContent = '结束并发送提问';
    captureStatus('正在录制明确的用户提问，最长30秒；观察通道的声音不会进入此入口。');
    const blob = await recordCaptureWindow(stream, 30000, {
      audioOnly: true,
      channel: 'voice'
    });
    for (const track of stream.getTracks()) track.stop();
    captureState.voiceStream = null;
    if (!captureState.connected) return;
    captureElement('ask').disabled = true;
    captureStatus('提问已录制，正在转写并等待云端回复…');
    const id = crypto.randomUUID(),
      result = await captureRequest('voice/' + id, {
        method: 'POST',
        voice: true,
        body: blob,
        headers: {
          'Content-Type': blob.type || 'application/octet-stream'
        }
      });
    await showVoiceReply(id, result);
  } catch (error) {
    if (error.name !== 'AbortError') captureStatus(error.message);
  } finally {
    for (const track of captureState.voiceStream?.getTracks() || []) track.stop();
    captureState.voiceStream = null;
    captureElement('ask').textContent = '开始语音提问';
    captureElement('ask').disabled = !captureState.connected;
  }
};
