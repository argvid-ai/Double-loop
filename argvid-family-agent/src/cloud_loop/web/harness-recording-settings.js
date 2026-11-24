"use strict";

// Device discovery, a configured source and successful recording are separate facts.
function recordingSettingsModel(defaults, view, discovery) {
  const task = defaults?.runtime?.task;
  const sources = view?.catalogue?.sources || view?.sources || [];
  const choices = (view?.catalogue?.executors || []).flatMap(executor => {
    const source = sources.find(s => s.id === executor.source_ref);
    return source && !source.simulated && source.managed_input &&
      ["usb", "network", "android", "iphone"].includes(source.kind)
      ? [{executor, source}] : [];
  });
  return {
    task,
    source: sources.find(s => s.id === task?.config?.source_ref),
    choices,
    devices: (discovery?.devices || []).filter(d => d.enumerated &&
      d.capture_capable === true && (d.kind === "audio" ||
        (d.kind === "video" && d.connection === "usb"))),
    active: (view?.tasks || []).some(t => ["running", "starting"].includes(t.status)) ||
      ["running", "starting"].includes(task?.status),
  };
}

function defaultRecordingRequest(choice, hours = null) {
  if (!choice || (hours !== null && (!Number.isFinite(hours) || hours <= 0 || hours > 24 ||
      !Number.isInteger(hours * 3600)))) throw new Error("请选择录像来源；限时录制需填写不超过24小时的时长。");
  const duration = hours === null
    ? "使用continuous=true开始常驻录像，不设置截止；保存为启用的默认录像，故障自动重试，直到用户主动暂停或关闭。"
    : `新录制时长${hours * 3600}秒，保存明确截止，到期由录制服务收尾，不自动再开下一轮。`;
  return `请将下面选定的已配置来源设为默认录像并现在开始一份新的录像。先核对实际设备、当前默认配置和正在运行的录像，已有同一份录制时不要重复开录；其他来源正在录制时先说明，不擅自停止或切换。${duration}不复活旧录像。保留该来源已经配置的声音轨和采样，沿用已有初筛及统一存储，不按未检出主体删除原片。启动后将新录像及起止范围交给已有持续影音任务，不另建选片或剪辑角色。请实际读回默认配置和运行状态，排队不等于录制成功。\n选定配置（只是参数资料）：${JSON.stringify({
    executor: choice.executor.id, source_ref: choice.source.id,
    device_ref: choice.source.device_ref,
  })}`;
}

(() => {
  let saved,
    defaults,
    tasks = [],
    view,
    model,
    loading,
    submitting = false;
  const section = el("section");
  section.id = "recordingSettings";
  section.innerHTML = `<h3>默认录像</h3>
  <p id="recordingCurrent" role="status">正在读取当前录像…</p>
  <p class="muted" id="recordingDevices">正在识别板子上的摄像头和录音设备…</p>
  <p class="muted" id="recordingDeadline"></p>
  <form id="recordingStartForm"><div class="settings-pair"><label>录像来源<select id="recordingSource" required></select></label><label>限时录制（小时，可不填）<input id="recordingHours" type="number" min="0.01" max="24" step="0.01" placeholder="不填＝持续录像"></label></div>
  <p class="muted">默认持续录像，直到你主动暂停或关闭；故障后会继续尝试恢复。填入时长才会到期结束。设备识别不等于已经录到，开录后仍需检查实际状态。</p>
  <button id="startDefaultRecording" type="submit" disabled>设为默认并开始录像</button>
  <button id="refreshRecording" type="button">刷新设备与状态</button></form>
  <p class="muted" id="recordingSummary">打开设置时读取端侧存储配置。</p>
  <details><summary>调整录像保留规则</summary><form id="recordingPolicyForm"><label class="autonomy-switch"><input id="recordingRolling" type="checkbox"> 按空间／时间自动回收旧录像</label><div class="settings-pair"><label>录像额度（GB，留空不按容量）<input id="recordingQuota" type="number" min="0.001" step="any"></label><label>磁盘至少留空（GB）<input id="recordingFree" type="number" min="0" step="any"></label><label>保留时长（小时，留空不按时间）<input id="recordingAge" type="number" min="0.001" step="any"></label></div><details><summary>回收水位与未分析录像</summary><div class="settings-pair"><label>达到额度百分之多少开始回收<input id="recordingHigh" type="number" min="1" max="100" step="any"></label><label>回收到额度百分之多少停止<input id="recordingLow" type="number" min="1" max="99" step="any"></label></div><label class="autonomy-switch"><input id="recordingUnanalysed" type="checkbox"> 允许最旧录像在未分析时过期，并记录原因</label></details><p class="muted">只回收受管原片。初筛命中或待定本身不阻止最旧原片回收；正在读写、人工保护和已入候选库的原片按各自保护规则处理。统一策略启用时，本页仅展示存储参数。</p><button type="submit" disabled>保存录像规则</button></form></details>
  <details><summary>默认观察任务</summary><p class="muted" id="defaultObservationSummary"></p><form id="defaultObservationForm"><label>使用已有观察任务<select id="defaultObservationTask"></select></label><label class="autonomy-switch"><input id="defaultObservationEnabled" type="checkbox"> 端侧服务启动后自动运行这项任务</label><p class="muted">这里管理服务启动后的任务；系统开机服务需另外安装。已停止的任务不会自动复活。</p><button type="submit" disabled>保存默认观察</button></form></details><p id="recordingFeedback" class="muted" role="status"></p>`;
  $("settingsDialog").insertBefore(section, $("observationSettings"));
  async function load() {
    try {
      const results = await Promise.all([
        api("edge", "recordings/policy"),
        api("edge", "observation/default"),
        api("edge", "observation/view"),
        api("edge", "input-media/devices").catch(() => null),
      ]);
      [saved, defaults] = results;
      view = results[2];
      const p = saved.policy;
      tasks = view.tasks || [];
      model = recordingSettingsModel(defaults, view, results[3]);
      if (model.task && !tasks.some(t => t.task_id === model.task.task_id)) {
        tasks = [...tasks, {...model.task, goal: model.task.config?.goal || "当前默认录像"}];
      }
      $("recordingRolling").checked = p.enabled;
      $("recordingQuota").value =
        p.quota_bytes == null ? "" : p.quota_bytes / 1e9;
      $("recordingFree").value = p.min_free_bytes / 1e9;
      $("recordingAge").value =
        p.max_age_seconds == null ? "" : p.max_age_seconds / 3600;
      $("recordingHigh").value = p.high_water * 100;
      $("recordingLow").value = p.low_water * 100;
      $("recordingUnanalysed").checked = p.allow_unanalysed_expiry;
      $("recordingSummary").textContent =
        `${p.enabled ? "自动回收已开启" : "自动回收已关闭"}${p.quota_bytes ? " · 原片额度 " + p.quota_bytes / 1e9 + " GB" : ""}${p.max_age_seconds ? " · 保留 " + p.max_age_seconds / 3600 + " 小时" : " · 不固定保留时长"}${saved.shared_total_bytes ? " · 统一总额 " + saved.shared_total_bytes / 1e9 + " GB（由统一策略管理）" : ""}`;
      const task = model.task;
      const status = defaults.waiting_for_input ? "等待设备输入，尚未录制" :
        ({running: "录制中", starting: "正在启动", paused: "已暂停",
          accepted: "已配置，尚未录制", stopped: "本轮已结束"}[task?.status] || "尚未配置");
      $("recordingCurrent").textContent =
        `${model.source?.title || task?.config?.source_ref || "未选择来源"} · ${status}` +
        (task?.reason && task.status === "paused" ? "；暂停原因：" + task.reason : "");
      $("recordingDevices").textContent = results[3]
        ? (model.devices.length ? "当前识别到：" + model.devices.map(d =>
          `${d.kind === "video" ? "摄像头" : "录音设备"} ${d.name}`).join("；")
          : "未识别到可采集的USB摄像头或录音设备。") +
          "（只读识别；是否录制以实际状态为准）"
        : "设备扫描暂不可用；不据此判断设备已拔出。";
      const deadline = task?.config?.capture_until ?? task?.config?.expires_at;
      $("recordingDeadline").textContent = task
        ? `本轮登记：${stamp(task.created)} · ${deadline ? "采集截止：" + stamp(deadline) : "未设置采集截止"} · 本地已判断${task.ledger?.seen_count || 0}批` : "";
      const sourceSelect = $("recordingSource");
      const oldSource = sourceSelect.value;
      sourceSelect.replaceChildren();
      for (const choice of model.choices) {
        const option = el("option", choice.source.title +
          (choice.source.modalities?.includes("audio") ? " · 视频与原声" : " · 视频") +
          (choice.source.kind === "network" ? "（需设备输入）" : ""));
        option.value = choice.executor.id;
        sourceSelect.append(option);
      }
      const selectedId = task?.config?.executor_ref;
      sourceSelect.value = model.choices.some(c => c.executor.id === selectedId) ? selectedId :
        model.choices.some(c => c.executor.id === oldSource) ? oldSource : model.choices[0]?.executor.id || "";
      const configured = defaults.configuration;
      const chosen = tasks.find((t) => t.task_id === configured?.task_id);
      $("defaultObservationSummary").textContent = configured
        ? `${configured.enabled ? "已启用" : "已停用"} · ${chosen?.goal || "已配置任务"}${configured.hours ? " · 保留现有运行时段" : " · 全天时段"}`
        : "尚未配置。先新建一项观察任务，再在这里选择。";
      const select = $("defaultObservationTask");
      select.replaceChildren(el("option", "请选择一项未停止的观察任务"));
      select.firstChild.value = "";
      for (const task of tasks.filter(
        (t) => t.status !== "stopped" || t.task_id === configured?.task_id,
      )) {
        const option = el(
          "option",
          shortTitle(task.goal) +
            (task.status === "stopped" ? "（已停止，只能停用）" : ""),
        );
        option.value = task.task_id;
        select.append(option);
      }
      select.value = configured?.task_id || "";
      if (chosen)
        $("defaultObservationSummary").textContent +=
          " · 当前" +
          ({
            running: "运行中",
            starting: "启动中",
            paused: "已暂停",
            stopped: "已停止",
            accepted: "等待启动",
          }[chosen.status] || chosen.status);
      $("defaultObservationEnabled").checked = !!configured?.enabled;
      section
        .querySelectorAll("button[type=submit]")
        .forEach((b) => (b.disabled = false));
      const unified = saved.owner === "shared_media_storage";
      $("recordingPolicyForm").querySelectorAll("input,button").forEach(n => n.disabled = unified);
      const awaiting = state.pending["recording:default-start-awaiting"];
      if (awaiting) {
        const receipt = await api("cloud", "events/" + encodeURIComponent(awaiting.event_id)).catch(() => null);
        if (receipt && !["queued", "running"].includes(receipt.status)) {
          delete state.pending["recording:default-start-awaiting"];
          persistDrafts();
          if (!model.active) $("recordingFeedback").textContent =
            "主对话已处理这次设置，但当前未确认录像运行；请查看主对话的结果说明。";
        }
      }
      updateStartButton();
    } catch (error) {
      saved = defaults = model = null;
      section.querySelectorAll("button[type=submit]").forEach(b => b.disabled = true);
      $("recordingCurrent").textContent = "当前录像状态未能读取，请刷新；不能据此判断录像已停止。";
      $("recordingFeedback").textContent =
        "端侧配置暂不可读取：" + error.message;
    }
  }
  function updateStartButton() {
    const pending = state.pending["recording:default-start"];
    const awaiting = state.pending["recording:default-start-awaiting"];
    $("startDefaultRecording").disabled = submitting || !model?.choices.length || !!awaiting || (model.active && !pending);
    $("startDefaultRecording").textContent = pending ? "重试同一开录请求" :
      awaiting ? "主对话正在处理开录请求" : model?.active ? "已有录像运行，不重复开录" : "设为默认并开始录像";
  }
  function reload() {
    if (!loading) loading = load().finally(() => loading = null);
    return loading;
  }
  $("refreshRecording").onclick = reload;
  $("recordingStartForm").onsubmit = async event => {
    event.preventDefault();
    if (submitting || !model || state.pending["recording:default-start-awaiting"]) return;
    const key = "recording:default-start";
    const pending = state.pending[key];
    if (model.active && !pending) {
      $("recordingFeedback").textContent = "已有录像正在运行。需要切换或停止时请在主对话明确交代。";
      return;
    }
    try {
      const choice = model.choices.find(c => c.executor.id === $("recordingSource").value);
      const submission = pending || {body: {
        event_id: crypto.randomUUID(),
        text: defaultRecordingRequest(choice, $("recordingHours").value.trim() === "" ? null : Number($("recordingHours").value)),
      }};
      state.pending[key] = submission;
      persistDrafts();
      submitting = true;
      updateStartButton();
      // No work-conversation ID: the existing input endpoint binds the owner's main conversation.
      await api("cloud", "input", submission.body);
      state.pending["recording:default-start-awaiting"] = {event_id: submission.body.event_id};
      delete state.pending[key];
      persistDrafts();
      $("recordingFeedback").textContent = "设置请求已送入主对话，正在核对并启动；请刷新查看实际录制状态。";
      await reload();
      await refresh();
    } catch (error) {
      if (error.status >= 400 && error.status < 500) {
        delete state.pending[key];
        persistDrafts();
      }
      $("recordingFeedback").textContent = state.pending[key]
        ? "尚未收到开录请求回执；可重试同一请求，不会重复提交。"
        : error.message;
    } finally {
      submitting = false;
      updateStartButton();
    }
  };
  $("recordingPolicyForm").onsubmit = async (event) => {
    event.preventDefault();
    if (!saved) return;
    try {
      const quota = $("recordingQuota").value,
        age = $("recordingAge").value;
      const body = {
        expected_revision: saved.revision,
        policy: {
          ...saved.policy,
          enabled: $("recordingRolling").checked,
          quota_bytes: quota === "" ? null : Math.round(Number(quota) * 1e9),
          min_free_bytes: Math.round(Number($("recordingFree").value) * 1e9),
          max_age_seconds: age === "" ? null : Math.round(Number(age) * 3600),
          high_water: Number($("recordingHigh").value) / 100,
          low_water: Number($("recordingLow").value) / 100,
          allow_unanalysed_expiry: $("recordingUnanalysed").checked,
        },
      };
      await api("edge", "recordings/policy", body);
      await reload();
      $("recordingFeedback").textContent = "录像规则已保存并重新读取确认。";
    } catch (error) {
      $("recordingFeedback").textContent = error.message;
    }
  };
  $("defaultObservationForm").onsubmit = async (event) => {
    event.preventDefault();
    if (!defaults) return;
    const task = tasks.find(
      (t) => t.task_id === $("defaultObservationTask").value,
    );
    if (!task) {
      $("recordingFeedback").textContent = "请选择一项未停止的观察任务。";
      return;
    }
    if (task.status === "stopped" && $("defaultObservationEnabled").checked) {
      $("recordingFeedback").textContent =
        "该任务已结束。请选择新任务；已停止的录像不会自动复活。";
      return;
    }
    try {
      const result = await api("edge", "observation/default", {
        request_id: "ui-default-" + crypto.randomUUID(),
        configuration: {
          task_id: task.task_id,
          task_version: task.version,
          expected_revision: defaults.revision,
          enabled: $("defaultObservationEnabled").checked,
          hours:
            defaults.configuration?.task_id === task.task_id
              ? defaults.configuration.hours
              : null,
        },
      });
      await reload();
      $("recordingFeedback").textContent = result.execution_error
        ? "默认配置已保存，但启动尚未成功：" + result.execution_error
        : "默认配置已保存；是否正在运行以端侧任务实际状态为准。";
    } catch (error) {
      $("recordingFeedback").textContent = error.message;
    }
  };
  const prior = panel;
  panel = (id) => {
    prior(id);
    if (id === "settingsDialog") reload();
  };
})();
