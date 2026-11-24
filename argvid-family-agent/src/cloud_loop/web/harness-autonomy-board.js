"use strict";
// An activity view over existing records. No model calls or background monitor.
(() => {
  const view = {
    data: null,
    config: null,
    selected: null,
    detail: null,
    timer: null,
    request: 0,
    busy: false,
  };
  const names = {
    delivered: "已交付",
    working: "后台进行中",
    waiting: "等待后续结果",
    running: "正在处理",
    queued: "等待开始",
    done: "本轮结束",
    quiet: "安静结束",
    failed: "未完成",
    cancelled: "已取消",
    superseded: "已结束",
  };
  const tools = {
    read_file: ["读取文件", "查看工作指导或所需资料"],
    write_file: ["写入文件", "保存本轮工作记录或脚本"],
    edit_file: ["修改文件", "更新已有内容"],
    bash: ["执行脚本", "运行模型编写的代码或命令"],
    read_autonomy: ["查看自主安排", "读取当前开关、时间与指导"],
    configure_autonomy: ["更新自主安排", "通过共用接口保存配置"],
    read_memory: ["读取记忆", "查询已保存的信息"],
    search_memory: ["检索记忆", "查找与当前工作相关的资料"],
    get_task_context: ["了解任务", "读取任务要求和已有进度"],
    get_capabilities: ["查看可用能力", "确认当前工具与设备能力"],
    inspect_media_frames: ["查看真实画面", "将指定时间点的图片交给模型判断"],
    review_rendered_media: ["复看成片", "根据实际成片画面提交检查结果"],
    start_job: ["提交后台制作", "把选段和制作要求交给媒体工具"],
    message_work: ["接续已有工作", "将后续要求送入对应工作会话"],
    create_task: ["建立工作任务", "保存本轮要完成的具体事项"],
    current_time: ["查看时间", "读取当前时间"],
    search_observation_evidence: ["检索观察记录", "查找端侧已经筛选的录像范围"],
    query_observation: ["查看观察进度", "确认录像观察任务的已有结果"],
    list_observation_markers: ["读取保留标记", "查看端侧标记的视频时间段"],
    save_task: ["保存工作任务", "记录目标和本次制作要求"],
    attach_observation_media: [
      "取回候选素材",
      "将已选录像范围接入本次制作任务",
    ],
  };
  const panelNode = el("dialog", undefined, "autonomy-board");
  panelNode.id = "autonomyBoard";
  panelNode.setAttribute("aria-labelledby", "autonomyBoardTitle");
  panelNode.innerHTML = `<header class="ab-heading"><div><p class="eyebrow">记录 · 安排 · 成果</p><h2 id="autonomyBoardTitle">Muse 动态</h2><p class="muted">看看最近做了什么，也安排接下来要做的事。</p></div><button id="abClose" aria-label="关闭 Muse 动态">×</button></header>
  <section class="ab-overview" aria-label="当前运行安排"><div><span class="ab-label">主动模式</span><strong id="abMode">正在读取</strong><p id="abModeNote"></p></div><div><span class="ab-label">下一次唤醒</span><strong id="abNext">—</strong><p id="abNextNote"></p></div><div><span class="ab-label">默认周期</span><strong id="abPeriod">—</strong><p>你和 Muse 都可以调整</p></div><div class="ab-controls"><button id="abConfigure">调整安排</button><button id="abOnce" class="primary">运行一次 ↗</button></div></section>
  <div class="ab-guidance"><div><span class="ab-file-icon" aria-hidden="true">≡</span><span><b>醒来时读什么？</b><span id="abGuideName">自主工作文件</span></span></div><button id="abGuideToggle" aria-expanded="false">查看工作指导 ↓</button></div>
  <section id="abGuide" hidden><p class="muted">这是当前文件，供下一次运行读取，不代表历史活动当时的文件内容。</p><div id="abGuideText"></div><button id="abEditGuide">编辑工作指导</button></section>
  <p id="abFeedback" role="status"></p>
  <div class="ab-work"><aside class="ab-activities" aria-label="Muse 活动列表"><header><h3>活动记录</h3><button id="abRefresh" aria-label="刷新 Muse 动态">刷新 ↻</button></header><div class="ab-filters" role="group" aria-label="筛选活动"><button data-filter="all" aria-pressed="true">全部</button><button data-filter="active" aria-pressed="false">进行中</button><button data-filter="failed" aria-pressed="false">未完成</button></div><div id="abRuns"></div><p id="abListNote" class="muted"></p></aside><section id="abDetail" class="ab-detail" aria-label="活动详情"><p class="empty">选择一次活动，查看过程与成果。</p></section></div>
  <details class="ab-changes"><summary>安排与指导的保存记录 <span id="abChangeCount"></span></summary><p class="muted">展示每次保存后的安排；历史指导全文没有单独存档时，不展示为版本对比。</p><div id="abChanges"></div></details>
  <footer class="ab-footer"><span>读取已有记录，不调用模型进行监控</span><span id="abUpdated">尚未更新</span></footer>`;
  document.body.append(panelNode);
  let filter = "all";
  const active = (r) =>
    ["working", "waiting", "running", "queued"].includes(r.status);
  const duration = (a, b) =>
    b == null
      ? "仍在进行"
      : b - a < 60
        ? `${Math.max(0, b - a).toFixed(1)} 秒`
        : `${Math.floor((b - a) / 60)} 分 ${Math.round((b - a) % 60)} 秒`;
  const time = (t) =>
    new Date(t * 1000).toLocaleTimeString("zh-CN", { hour12: false });
  const clean = (text) =>
    String(text || "")
      .replace(/\[([^\]]+)\]\(cloud-asset:\/\/[^)]+\)/g, "$1")
      .replace(/\b(?:task-|record-|media-job-)[\w-]+/g, "对应任务");
  function configure(guide = false) {
    panelNode.close();
    panel("settingsDialog");
    if (guide) $("autonomyDuties").closest("details").open = true;
    $("autonomySettings").scrollIntoView({ block: "start" });
  }
  function renderTop() {
    const c = view.config;
    $("abMode").textContent = c.enabled ? "已开启" : "已暂停";
    $("abMode").className = c.enabled ? "is-on" : "is-paused";
    $("abModeNote").textContent = c.enabled
      ? "按当前安排自主醒来"
      : "不会自动醒来，仍可运行一次";
    $("abNext").textContent = c.enabled ? stamp(c.next_wake_at) : "等待你开启";
    $("abNextNote").textContent = c.enabled
      ? "到期后读取工作指导"
      : "已提交的任务仍会继续";
    $("abPeriod").textContent =
      `${Number((c.interval_seconds / 60).toFixed(2))} 分钟`;
    $("abGuideName").textContent = c.duties_file || "尚未配置";
    $("abGuideText").replaceChildren(
      richText(c.duties_content || "当前没有指导内容。"),
    );
    $("abOnce").disabled = c.available === false;
    $("abChangeCount").textContent = `· 最近 ${view.data.changes.length} 次`;
    $("abChanges").replaceChildren();
    for (const change of view.data.changes) {
      const row = el("div", undefined, "ab-change");
      const config = change.config;
      row.append(
        el("time", stamp(change.created)),
        el("b", change.by === "user" ? "设置入口" : "Muse / 脚本"),
        el(
          "span",
          `${config.enabled ? "开启" : "关闭"}定期唤醒 · 周期 ${config.interval_seconds / 60} 分钟${change.duties_changed ? " · 保存了工作指导" : ""}${change.persona_changed ? " · 保存了人设" : ""}`,
        ),
      );
      $("abChanges").append(row);
    }
  }
  function renderList() {
    const runs = view.data.runs.filter(
      (r) =>
        filter === "all" ||
        (filter === "active"
          ? active(r)
          : ["failed", "cancelled"].includes(r.status)),
    );
    $("abRuns").replaceChildren();
    for (const run of runs) {
      const button = el("button", undefined, "ab-run");
      button.dataset.id = run.id;
      button.setAttribute("aria-pressed", String(view.selected === run.id));
      const top = el("span", undefined, "ab-run-top");
      top.append(
        el(
          "span",
          names[run.status] || run.status,
          `ab-badge ${active(run) ? "live" : run.status}`,
        ),
        el("span", run.manual ? "手动一次" : "定时唤醒"),
      );
      button.append(
        top,
        el("strong", run.title),
        el("span", stamp(run.created), "ab-run-time"),
        el(
          "span",
          `${duration(run.created, run.finished)}${run.outputs.length ? ` · ${run.outputs.length} 份成果` : ""}`,
          "ab-run-time",
        ),
      );
      button.onclick = () => select(run.id);
      $("abRuns").append(button);
    }
    if (!runs.length) $("abRuns").append(el("p", "暂无这类活动。", "ab-empty"));
    $("abListNote").textContent =
      `最近 ${view.data.runs.length} 次真实活动 · 最多显示 ${view.data.limit} 次`;
  }
  function step(container, label, subtitle, when, content, tone = "") {
    const row = el("section", undefined, "ab-step " + tone);
    const heading = el("div", undefined, "ab-step-heading");
    heading.append(el("h4", label), el("time", when));
    row.append(heading, el("p", subtitle, "muted"));
    if (content) row.append(content);
    container.append(row);
  }
  function toolList(run) {
    const block = el("details", undefined, "ab-tool-list");
    block.append(el("summary", `查看 ${run.tools.length} 项实际操作`));
    for (const tool of run.tools) {
      const row = el("details", undefined, "ab-tool");
      const [name, description] = tools[tool.name] || [
        tool.name,
        "调用已装配的工作工具",
      ];
      const heading = el("summary");
      heading.append(
        el("time", time(tool.started)),
        el("span", name),
        el(
          "span",
          tool.status === "failed"
            ? "失败"
            : tool.finished
              ? duration(tool.started, tool.finished)
              : "进行中",
          "muted",
        ),
      );
      row.append(heading, el("p", description));
      const pairs = el("dl");
      const input = tool.input || {};
      const labels = {
        path: "文件",
        command: "执行命令",
        enabled: "定期唤醒",
        interval_seconds: "周期（秒）",
        next_wake_in_seconds: "下次等待（秒）",
        task_id: "任务",
        asset_id: "素材",
        positions_ms: "取图位置（毫秒）",
        action: "操作",
        title: "标题",
        query: "查询",
      };
      for (const [key, value] of Object.entries(input).filter(
        ([, value]) => value != null,
      ))
        pairs.append(
          el("dt", labels[key] || key),
          el(
            "dd",
            typeof value === "boolean"
              ? value
                ? "开启"
                : "关闭"
              : typeof value === "object"
                ? JSON.stringify(value)
                : String(value),
          ),
        );
      row.append(
        pairs,
        el(
          "p",
          tool.error
            ? `未完成：${tool.error}`
            : tool.finished
              ? "已收到工具回执。"
              : "尚未收到结束回执。",
          "muted",
        ),
      );
      block.append(row);
    }
    return block;
  }
  function renderDetail(run) {
    pauseMedia($("abDetail"));
    const box = $("abDetail");
    box.replaceChildren();
    const heading = el("header", undefined, "ab-detail-heading");
    heading.append(
      el(
        "p",
        `${run.manual ? "你发起的一次运行" : "按计划自主醒来"} · ${stamp(run.created)}`,
        "eyebrow",
      ),
      el("h3", run.title),
    );
    const stats = el("div", undefined, "ab-stats");
    for (const [value, label] of [
      [duration(run.created, run.finished), "从唤醒到当前结果"],
      [run.model_calls + " 次", "模型请求"],
      [run.notifications + " 条", "返回主对话"],
    ]) {
      const cell = el("div");
      cell.append(el("b", value), el("span", label));
      stats.append(cell);
    }
    box.append(heading, stats);
    if (run.outputs.length) {
      for (const output of run.outputs) {
        const film = el("article", undefined, "ab-result");
        const video = el("video");
        video.src = path(
          "cloud",
          "assets/" + encodeURIComponent(output.asset_id),
        );
        video.controls = true;
        video.preload = "metadata";
        video.playsInline = true;
        const caption = el("div");
        caption.append(
          el("span", "本次成果", "ab-label"),
          el("h4", output.title),
          el(
            "p",
            `${output.duration_ms / 1000} 秒 · ${stamp(output.created)}`,
            "muted",
          ),
          el("p", "来自已有录像。可直接播放查看。", "muted"),
        );
        film.append(video, caption);
        box.append(film);
      }
    } else
      box.append(
        el(
          "p",
          run.error ||
            (run.status === "quiet"
              ? "这一轮保持安静，没有发送新通知。"
              : active(run)
                ? "工作仍在进行，当前还没有可交付成果。"
                : "这一轮没有交付视频，具体处理见下方记录。"),
          "ab-outcome",
        ),
      );
    box.append(el("h4", "这次发生了什么", "ab-timeline-title"));
    const timeline = el("div", undefined, "ab-timeline");
    step(
      timeline,
      run.status === "queued" ? "已安排，等待开始" : "醒来，按工作指导开始",
      `${run.manual ? "由你点击运行一次" : "定时安排到期"} · 指导文件 ${run.duties_file}`,
      time(run.created),
      null,
    );
    step(
      timeline,
      "了解情况并安排工作",
      "查看资料、调用工具，必要时编写和执行脚本。",
      "",
      toolList(run),
    );
    if (run.tasks.length) {
      const tasks = el("div");
      for (const task of run.tasks) {
        const detail = el("details");
        detail.append(
          el("summary", labels[task.status] || task.status),
          el("p", clean(task.goal)),
        );
        tasks.append(detail);
      }
      step(
        timeline,
        "交给后台任务继续执行",
        "这一轮模型返回后，后台任务还会独立推进。",
        "",
        tasks,
        run.tasks.some(
          (t) =>
            !["completed", "failed", "cancelled", "expired"].includes(t.status),
        )
          ? "pending"
          : "",
      );
    }
    for (const event of run.events.filter((e) => e.kind === "media.review"))
      step(
        timeline,
        "查看制作后的实际画面",
        event.status === "done"
          ? "复看轮次已结束，具体结论保留在实际回复中。"
          : "正在复看，尚未交付。",
        time(event.created),
        event.text ? richText(clean(event.text)) : null,
      );
    const notes = run.events.filter((e) => e.kind === "task.notification");
    if (notes.length)
      step(
        timeline,
        "将结果送回主对话",
        `已保存 ${notes.length} 条任务通知。`,
        time(notes.at(-1).created),
        null,
      );
    else
      step(
        timeline,
        run.status === "quiet" ? "安静结束" : "等待后续结果",
        run.status === "quiet"
          ? "本轮没有新增主对话通知。"
          : "尚未记录回到主对话的任务通知。",
        "",
        null,
        "pending",
      );
    box.append(timeline);
    if (run.text) {
      const note = el("details", undefined, "ab-model-note");
      note.append(
        el("summary", "Muse 本轮的工作说明"),
        richText(clean(run.text)),
      );
      box.append(note);
    }
  }
  async function select(id) {
    view.selected = id;
    const request = ++view.request;
    renderList();
    $("abDetail").setAttribute("aria-busy", "true");
    try {
      const detail = await api(
        "cloud",
        "autonomy/activity/" + encodeURIComponent(id),
      );
      if (request !== view.request || !panelNode.open) return;
      view.detail = detail;
      renderDetail(detail);
    } catch (error) {
      if (request === view.request) $("abFeedback").textContent = error.message;
    } finally {
      if (request === view.request) $("abDetail").removeAttribute("aria-busy");
    }
  }
  function schedule() {
    clearTimeout(view.timer);
    if (!panelNode.open || document.hidden) return;
    const seconds = view.data?.runs.some(active) ? 15 : 60;
    view.timer = setTimeout(() => refreshBoard(), seconds * 1000);
  }
  async function refreshBoard() {
    if (view.busy || !panelNode.open || document.hidden) return;
    view.busy = true;
    $("abRefresh").disabled = true;
    try {
      const [config, data] = await Promise.all([
        api("cloud", "autonomy"),
        api("cloud", "autonomy/activity"),
      ]);
      if (!panelNode.open) return;
      const old = view.data?.runs.find((r) => r.id === view.selected);
      view.config = config;
      view.data = data;
      renderTop();
      renderList();
      if (!view.selected && $("abRuns").querySelector(".ab-run"))
        await select($("abRuns").querySelector(".ab-run").dataset.id);
      else {
        const next = data.runs.find((r) => r.id === view.selected);
        if (
          next &&
          (active(next) || JSON.stringify(next) !== JSON.stringify(old))
        )
          await select(next.id);
      }
      $("abUpdated").textContent =
        `更新于 ${time(data.now)} · 关闭页面停止刷新`;
    } catch (error) {
      $("abFeedback").textContent = error.message;
    } finally {
      view.busy = false;
      $("abRefresh").disabled = false;
      schedule();
    }
  }
  function open() {
    if (!panelNode.open) panel("autonomyBoard");
    $("openAutonomy").setAttribute("aria-expanded", "true");
    document.title = "Muse · 动态";
    history.replaceState(null, "", "#autonomy");
    refreshBoard();
  }
  $("openAutonomy").onclick = open;
  $("abClose").onclick = () => panelNode.close();
  $("abConfigure").onclick = () => configure();
  $("abEditGuide").onclick = () => configure(true);
  $("abRefresh").onclick = refreshBoard;
  $("abGuideToggle").onclick = () => {
    $("abGuide").hidden = !$("abGuide").hidden;
    $("abGuideToggle").setAttribute(
      "aria-expanded",
      String(!$("abGuide").hidden),
    );
  };
  $("abOnce").onclick = async () => {
    $("abOnce").disabled = true;
    try {
      await api("cloud", "autonomy/run", {});
      $("abFeedback").textContent = "已安排运行一次，定期唤醒开关保持不变。";
      await refreshBoard();
    } catch (error) {
      $("abFeedback").textContent = error.message;
    } finally {
      $("abOnce").disabled = false;
    }
  };
  for (const button of panelNode.querySelectorAll("[data-filter]"))
    button.onclick = () => {
      filter = button.dataset.filter;
      ++view.request;
      view.selected = null;
      view.detail = null;
      pauseMedia($("abDetail"));
      $("abDetail").replaceChildren(
        el("p", "暂无这类活动。可切换到“全部”查看已有结果。", "empty"),
      );
      panelNode
        .querySelectorAll("[data-filter]")
        .forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      renderList();
      $("abRuns").querySelector(".ab-run")?.click();
    };
  panelNode.addEventListener("close", () => {
    clearTimeout(view.timer);
    ++view.request;
    pauseMedia(panelNode);
    $("openAutonomy").setAttribute("aria-expanded", "false");
    document.title = "Muse · 对话";
    if (location.hash === "#autonomy")
      history.replaceState(null, "", location.pathname + location.search);
  });
  document.addEventListener("visibilitychange", () => {
    clearTimeout(view.timer);
    if (!document.hidden && panelNode.open) refreshBoard();
  });
  window.addEventListener("hashchange", () => {
    if (location.hash === "#autonomy") open();
  });
  if (location.hash === "#autonomy") open();
})();
