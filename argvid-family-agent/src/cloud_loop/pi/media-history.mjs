import crypto from "node:crypto";

// SessionManager still owns persistence, tree IDs, compaction and recovery.
// Pi 1.5 reprojects its persisted entries before every request. Keep the current
// run's pixels separately in memory and restore them only into that projection.
export function withoutImageBytes(value) {
  if (Array.isArray(value)) return value.map(withoutImageBytes);
  if (!value || typeof value !== "object") return value;
  let encoded;
  if (value.type === "image" && typeof value.data === "string")
    encoded = value.data;
  else if (value.type === "image_url" && value.image_url?.url?.startsWith("data:image/"))
    encoded = value.image_url.url;
  if (encoded !== undefined) {
    const digest = crypto.createHash("sha256").update(encoded).digest("hex");
    return {type: "text", text: `[Transient image omitted from stored history; encoded_sha256=${digest}. Reread the referenced media when needed.]`};
  }
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, withoutImageBytes(item)]));
}

export function useTextOnlyMediaHistory(manager) {
  const transient = new Map();
  const remember = (id, content, storedContent) => {
    if (JSON.stringify(content) !== JSON.stringify(storedContent))
      transient.set(id, { content, stored: JSON.stringify(storedContent) });
    return id;
  };
  const appendMessage = manager.appendMessage.bind(manager);
  manager.appendMessage = (message) => {
    const stored = withoutImageBytes(message);
    return remember(appendMessage(stored), message.content, stored.content);
  };
  const appendCustom = manager.appendCustomMessageEntry.bind(manager);
  manager.appendCustomMessageEntry = (kind, content, display, details) => {
    const stored = withoutImageBytes(content);
    return remember(
      appendCustom(kind, stored, display, withoutImageBytes(details)), content, stored,
    );
  };
  const buildProjection = manager.buildSessionProjection.bind(manager);
  manager.buildSessionProjection = () => {
    const projection = buildProjection();
    // Context edits and native compaction remain authoritative. Never restore
    // an omitted message or overwrite a replacement chosen by the SDK.
    const edited = new Set(projection.entries
      .filter(({ sourceEntry }) => sourceEntry.type === "context_edit")
      .map(({ sourceEntry }) => sourceEntry.targetId));
    const active = new Set(projection.entries.map(({ sourceEntry }) => sourceEntry.id));
    for (const id of transient.keys())
      if (!active.has(id) || edited.has(id)) transient.delete(id);
    const entries = projection.entries.map((entry) => {
      const live = transient.get(entry.sourceEntry.id);
      if (!live) return entry;
      return { ...entry, messages: entry.messages.map((message) =>
        JSON.stringify(message.content) === live.stored
          ? { ...message, content: live.content }
          : message,
      ) };
    });
    return { ...projection, entries, messages: entries.flatMap((entry) => entry.messages) };
  };
  return manager;
}
