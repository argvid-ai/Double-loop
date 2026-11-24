"use strict";
let autonomyView = null;
const autonomySection = el("section");
autonomySection.id = "autonomySettings";
autonomySection.innerHTML = `<h3>Muse 动态设置</h3>
<p class="muted">按工作文件自主安排事情；有值得告诉你的结果才回到主对话。你和 Muse 都可以调整这些设置。</p>
<form id="autonomyConfigForm"><label class="autonomy-switch"><input id="autonomyEnabled" type="checkbox"> 开启定期自主唤醒</label>
<div class="settings-pair"><label>默认周期（分钟）<input id="autonomyMinutes" type="number" min="0.016667" step="any" value="30"></label><label>下一次时间<input id="autonomyNext" type="datetime-local" step="1"></label></div>
<div class="autonomy-actions"><button type="submit">保存运行设置</button><button id="autonomyRunOnce" type="button">按当前文件运行一次</button></div></form>
<details><summary>自主工作文件</summary><p class="muted" id="autonomyFileLabel"></p><textarea id="autonomyDuties" rows="10" maxlength="20000" aria-label="自主工作文件内容"></textarea><button id="autonomySaveDuties">保存工作文件</button></details>
<details><summary>Muse 人设</summary><textarea id="autonomyPersona" rows="8" maxlength="12000" aria-label="Muse人设内容"></textarea><button id="autonomySavePersona">保存人设</button></details>
<p id="autonomyFeedback" role="status" class="muted"></p><div id="autonomyRecent"></div>`;
$("settingsDialog").insertBefore(autonomySection, $("observationSettings"));
const localDateValue = (value) => {
  const d = new Date(value);
  if (!Number.isFinite(d.getTime())) return "";
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 19);
};
async function loadAutonomy() {
  try {
    const v = await api("cloud", "autonomy");
    autonomyView = v;
    autonomySection
      .querySelectorAll("button,input,textarea")
      .forEach((e) => (e.disabled = v.available === false));
    if (v.available === false) {
      $("autonomyFeedback").textContent = "当前节点尚未装配自主工作能力。";
      return;
    }
    $("autonomyEnabled").checked = v.enabled;
    $("autonomyMinutes").value = v.interval_seconds / 60;
    $("autonomyNext").value = localDateValue(v.next_wake_at);
    $("autonomyDuties").value = v.duties_content;
    $("autonomyPersona").value = v.persona_content;
    $("autonomyFileLabel").textContent =
      v.duties_file + " · 下一次运行读取当前文件";
    $("autonomyFeedback").textContent = v.enabled
      ? "已开启 · 下次 " + stamp(v.next_wake_at)
      : "已关闭定期唤醒；主对话和已提交工作仍可继续。";
    $("autonomyRecent").replaceChildren(el("h4", "最近自主活动"));
    for (const run of v.recent || []) {
      const detail = el("details");
      detail.append(
        el(
          "summary",
          stamp(run.created) +
            " · " +
            (run.quiet ? "安静结束" : labels[run.status] || run.status) +
            (run.run_seconds ? " · " + run.run_seconds.toFixed(1) + "秒" : ""),
        ),
      );
      if (run.text) detail.append(richText(run.text));
      if (run.error) detail.append(el("p", "本轮未完成：" + run.error));
      $("autonomyRecent").append(detail);
    }
  } catch (error) {
    $("autonomyFeedback").textContent = error.message;
  }
}
async function saveAutonomy(changes) {
  try {
    await api("cloud", "autonomy", changes);
    await loadAutonomy();
  } catch (error) {
    $("autonomyFeedback").textContent = error.message;
  }
}
$("autonomyConfigForm").onsubmit = async (event) => {
  event.preventDefault();
  if (!autonomyView) return;
  const changes = {};
  if ($("autonomyEnabled").checked !== autonomyView.enabled)
    changes.enabled = $("autonomyEnabled").checked;
  const interval = Math.round(Number($("autonomyMinutes").value) * 60);
  if (interval !== autonomyView.interval_seconds)
    changes.interval_seconds = interval;
  if (
    $("autonomyNext").value &&
    $("autonomyNext").value !== localDateValue(autonomyView.next_wake_at)
  )
    changes.next_wake_at = new Date($("autonomyNext").value).toISOString();
  await saveAutonomy(changes);
};
$("autonomySaveDuties").onclick = () =>
  saveAutonomy({ duties_content: $("autonomyDuties").value });
$("autonomySavePersona").onclick = () =>
  saveAutonomy({ persona_content: $("autonomyPersona").value });
$("autonomyRunOnce").onclick = async () => {
  const button = $("autonomyRunOnce");
  button.disabled = true;
  try {
    await api("cloud", "autonomy/run", {});
    $("autonomyFeedback").textContent =
      "本次自主工作已安排；开关和周期保持原设置。结果会回到主对话，暂无发现时保持安静。";
  } catch (error) {
    $("autonomyFeedback").textContent = error.message;
  } finally {
    button.disabled = false;
  }
};
const beforeAutonomyPanel = panel;
panel = (id) => {
  beforeAutonomyPanel(id);
  if (id === "settingsDialog") loadAutonomy();
};
