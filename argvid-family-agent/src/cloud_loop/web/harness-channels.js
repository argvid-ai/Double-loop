"use strict";
(() => {
  const section = el("section");
  section.id = "feishuSettings";
  section.innerHTML = `<h3>飞书连接</h3><p id="feishuState" class="muted">打开设置时读取连接状态。</p><p class="muted">飞书私聊进入同一个 Muse 主对话。首次接入需在这里允许你的账号；只有实际收到平台回执，才记为发送成功。</p><button id="feishuRefresh">刷新状态</button><div id="feishuPending"></div><details><summary>最近的发送回执</summary><div id="feishuReceipts"></div></details><p id="feishuFeedback" class="muted" role="status"></p>`;
  $("settingsDialog").insertBefore(section, $("observationSettings"));
  async function load() {
    try {
      const data = await api("cloud", "channels/feishu");
      $("feishuState").textContent = !data.configured
        ? "尚未配置应用凭据。"
        : !data.enabled
          ? "应用已配置，连接未开启。"
          : data.connected
            ? "飞书长连接已建立。"
            : "应用已配置，正在等待连接。";
      if (data.error)
        $("feishuState").textContent += " 当前问题：" + data.error;
      $("feishuPending").replaceChildren();
      for (const user of data.pending_users || []) {
        const row = el("div", undefined, "rule");
        row.append(
          el("p", "待接入账号：" + user.sender),
          el("p", "最近联系：" + stamp(user.seen), "muted"),
        );
        const button = el("button", "允许这个账号使用 Muse");
        button.onclick = async () => {
          button.disabled = true;
          try {
            await api(
              "cloud",
              "channels/feishu/authorize/" + encodeURIComponent(user.sender),
              {},
            );
            await load();
            $("feishuFeedback").textContent =
              "已保存账号绑定，请在飞书重新发送一条指令。";
          } catch (error) {
            $("feishuFeedback").textContent = error.message;
            button.disabled = false;
          }
        };
        row.append(button);
        $("feishuPending").append(row);
      }
      $("feishuReceipts").replaceChildren();
      for (const item of data.deliveries || []) {
        const row = el("div", undefined, "rule");
        row.append(
          el(
            "p",
            `${stamp(item.updated)} · ${{ delivered: "已收到飞书回执", pending: "等待发送", retry: "发送失败，等待重试", failed: "发送失败，未交付" }[item.status] || item.status}`,
          ),
        );
        if (item.receipt)
          row.append(
            el(
              "p",
              `平台回执 ${(item.receipt.message_ids || []).length} 条${item.receipt.media_files ? " · 包含视频/文件" : ""}`,
              "muted",
            ),
          );
        if (item.error) row.append(el("p", item.error, "muted"));
        $("feishuReceipts").append(row);
      }
      if (!(data.deliveries || []).length)
        $("feishuReceipts").append(el("p", "还没有实际发送回执。", "muted"));
    } catch (error) {
      $("feishuFeedback").textContent = error.message;
    }
  }
  $("feishuRefresh").onclick = load;
  const prior = panel;
  panel = (id) => {
    prior(id);
    if (id === "settingsDialog") load();
  };
})();
