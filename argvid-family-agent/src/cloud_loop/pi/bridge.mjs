// Pi owns the agent loop and session log. This process only adapts its SDK to
// the Python host's event-scoped tool API. One process per run isolates state.
import { pathToFileURL } from "node:url";
import path from "node:path";
import fs from "node:fs/promises";
import readline from "node:readline";
import crypto from "node:crypto";
import { modelRouter } from "./model-routing.mjs";
import { useTextOnlyMediaHistory } from "./media-history.mjs";
import { appendContextMail, retireContextMail } from "./context-mail.mjs";
import { runOutcome } from "./run-outcome.mjs";
const pending = new Map();
let session;
let cancelled = false;
const emit = (type, data = {}) =>
  process.stdout.write(JSON.stringify({ type, ...data }) + "\n");
const call = (type, data = {}) =>
  new Promise((resolve, reject) => {
    const id = crypto.randomUUID();
    pending.set(id, { resolve, reject });
    emit(type, { id, ...data });
  });
const lines = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});
let configured = false;
lines.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    process.exitCode = 1;
    return;
  }
  if (message.type === "reply") {
    const p = pending.get(message.id);
    if (p) {
      pending.delete(message.id);
      message.error
        ? p.reject(new Error(message.error))
        : p.resolve(message.value);
    }
    return;
  }
  if (message.type === "cancel") {
    cancelled = true;
    void session?.abort();
    return;
  }
  if (message.type === "run" && !configured) {
    configured = true;
    void run(message).catch((e) => {
      emit("failed", { error: e.message });
      lines.close();
    });
  }
});
async function run(cfg) {
  const pkg = JSON.parse(
    await fs.readFile(
      path.join(
        cfg.pi_dir,
        "node_modules/@earendil-works/pi-coding-agent/package.json",
      ),
      "utf8",
    ),
  );
  if (pkg.version !== "1.5.0")
    throw new Error(
      "This adapter is qualified for Pi 1.5.0; validate a new version before deployment.",
    );
  const sdk = await import(
    pathToFileURL(
      path.join(
        cfg.pi_dir,
        "node_modules/@earendil-works/pi-coding-agent/dist/index.js",
      ),
    )
  );
  const ai = await import(
    pathToFileURL(
      path.join(cfg.pi_dir, "node_modules/@earendil-works/pi-ai/dist/index.js"),
    )
  );
  process.env.MUSE_TOOL_SOCKET = cfg.tool_socket;
  process.env.MUSE_TOOL_TOKEN = cfg.tool_token;
  process.env.PYTHONPATH = cfg.tool_module_dir;
  const credentials = {
    read: async () => undefined,
    list: async () => [],
    modify: async () => {
      throw new Error("credential writes disabled");
    },
    delete: async () => {},
  };
  const models = await sdk.ModelRuntime.create({
    credentials,
    modelsPath: null,
    modelsStorePath: path.join(cfg.agent_dir, "models-cache.json"),
    refreshOnCreate: false,
    allowModelNetwork: false,
  });
  const primaryCapabilities = cfg.model_capabilities;
  const modelDefinition = (id, capabilities) => ({
    id,
    name: id,
    reasoning: capabilities.reasoning,
    input: capabilities.input_modalities,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: capabilities.context_window,
    maxTokens: capabilities.max_output_tokens,
  });
  models.registerProvider("muse", {
    baseUrl: cfg.endpoint,
    api: cfg.api,
    apiKey: cfg.key,
    models: [modelDefinition(cfg.model, primaryCapabilities)],
  });
  if (cfg.fallback)
    models.registerProvider("muse-backup", {
      baseUrl: cfg.fallback.endpoint,
      api: cfg.fallback.api,
      apiKey: cfg.fallback.key,
      models: [modelDefinition(cfg.fallback.model, cfg.fallback.capabilities)],
    });
  const settings = sdk.SettingsManager.inMemory({
    compaction: {
      enabled: true,
      ...cfg.compaction,
    },
    cacheWarming: "off",
    retry: { enabled: (cfg.request_retries ?? 2) > 0,
             maxRetries: cfg.request_retries ?? 2, baseDelayMs: 2000 },
  });
  const loader = new sdk.DefaultResourceLoader({
    cwd: cfg.workspace,
    agentDir: cfg.agent_dir,
    settingsManager: settings,
    noExtensions: true,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    systemPrompt: cfg.system_prompt,
  });
  await loader.reload();
  process.on("SIGTERM", () => {
    void (async () => {
      await session?.abort();
      process.exit(0);
    })();
  });
  const native = {
    read_file: sdk.createReadTool(cfg.workspace),
    write_file: sdk.createWriteTool(cfg.workspace),
    edit_file: sdk.createEditTool(cfg.workspace),
    list_dir: sdk.createLsTool(cfg.workspace),
    find_files: sdk.createFindTool(cfg.workspace),
    grep: sdk.createGrepTool(cfg.workspace),
    bash: sdk.createBashTool(cfg.workspace),
  };
  const scrub = (value) => {
    let text = JSON.stringify(value);
    for (const secret of cfg.redactions || [])
      text = text.replaceAll(secret, "[redacted]");
    return JSON.parse(text);
  };
  const customTools = cfg.tools.map((tool) => ({
    name: tool.name,
    label: tool.name,
    description: tool.description,
    parameters: tool.parameters,
    executionMode: "sequential",
    async execute(id, args, signal, onUpdate, ctx) {
      if (native[tool.name]) {
        await call("native_started", { call_id: id, name: tool.name, args });
        try {
          const result = scrub(
            await native[tool.name].execute(id, args, signal, onUpdate, ctx),
          );
          await call("native_completed", {
            call_id: id,
            name: tool.name,
            args,
            result,
          });
          return result;
        } catch (e) {
          await call("native_failed", {
            call_id: id,
            name: tool.name,
            error: e.message,
          });
          throw e;
        }
      }
      const result = await call("tool", { call_id: id, name: tool.name, args });
      return {
        content: [
          { type: "text", text: JSON.stringify(result.value) },
          ...(result.images || []),
        ],
        details: result.value,
        structuredContent: result.value,
        ...(result.terminate ? { terminate: true } : {}),
      };
    },
  }));
  const manager = useTextOnlyMediaHistory(sdk.SessionManager.continueRecent(
    cfg.workspace,
    cfg.session_dir,
  ));
  retireContextMail(manager, cfg.retired_context_mail_ids || []);
  const created = await sdk.createAgentSession({
    cwd: cfg.workspace,
    agentDir: cfg.agent_dir,
    modelRuntime: models,
    model: models.getModel("muse", cfg.model),
    ...(cfg.thinking_level != null ? { thinkingLevel: cfg.thinking_level } : {}),
    settingsManager: settings,
    resourceLoader: loader,
    tools: cfg.tools.map((t) => t.name),
    customTools,
    sessionManager: manager,
  });
  session = created.session;
  const router = modelRouter({
    fallback: cfg.fallback
      ? {
          model: models.getModel("muse-backup", cfg.fallback.model),
          key: cfg.fallback.key,
          outputTokens: cfg.fallback.capabilities.output_tokens,
          timeoutMs: cfg.fallback.request_timeout_ms ?? undefined,
        }
      : null,
    notify: (data) => call("model_route", scrub(data)),
    record: (kind, data) => call(kind, data),
    requestId: () => crypto.randomUUID(),
    outputTokens: primaryCapabilities.output_tokens,
    onSwitch: (model) => session.setModel(model),
  });
  for (const name of ["streamSimple", "stream"]) {
    const original = models[name].bind(models);
    models[name] = (model, context, options) => {
      const out = ai.createAssistantMessageEventStream();
      void (async () => {
        try {
          for await (const item of router.stream(original, model, context, {
            ...options,
            ...(cfg.request_timeout_ms != null ? { timeoutMs: cfg.request_timeout_ms } : {}),
            maxRetries: 0,
          })) {
            if (item.type === "error" &&
                /forbidden|unauthorized|invalid.api.key|\b(401|403)\b/i.test(
                  String(item.error?.errorMessage || "")))
              settings.applyOverrides({retry: {enabled: false}});
            out.push(item);
          }
        } catch (error) {
          const failedModel = router.currentModel ?? model;
          out.push({
            type: "error",
            reason: "error",
            error: {
              role: "assistant",
              content: [],
              api: failedModel.api,
              provider: failedModel.provider,
              model: failedModel.id,
              usage: {
                input: 0,
                output: 0,
                cacheRead: 0,
                cacheWrite: 0,
                totalTokens: 0,
                cost: {
                  input: 0,
                  output: 0,
                  cacheRead: 0,
                  cacheWrite: 0,
                  total: 0,
                },
              },
              stopReason: "error",
              errorMessage: scrub(String(error.message || error)),
              timestamp: Date.now(),
            },
          });
        } finally {
          out.end();
        }
      })();
      return out;
    };
  }
  for (const name of ["completeSimple", "complete"]) {
    const original = models[name].bind(models);
    models[name] = (model, context, options) =>
      router.complete(original, model, context, {
        ...options,
        ...(cfg.request_timeout_ms != null ? { timeoutMs: cfg.request_timeout_ms } : {}),
        maxRetries: 0,
      });
  }
  session.agent.toolExecution = "sequential";
  const outcome = runOutcome();
  session.subscribe((e) => {
    outcome.observe(e);
    if (e.type === "compaction_start" || e.type === "compaction_end")
      emit("compacted", { result: scrub(e) });
    if (
      e.type === "message_update" &&
      e.assistantMessageEvent?.type === "text_delta"
    )
      emit("text_delta", { text: e.assistantMessageEvent.delta });
  });
  emit("ready", {
    session_file: manager.getSessionFile(),
    active_tools: session.getActiveToolNames(),
    model_capabilities: primaryCapabilities,
    thinking_level: session.thinkingLevel,
    compaction: settings.getCompactionSettings(session.model),
  });
  try {
    if (cancelled) {
      emit("done", { text: "", messages: [], stats: {} });
      return;
    }
    if (cfg.compact_instructions) {
      const result = await session.compact(cfg.compact_instructions);
      emit("compacted", { result: scrub(result) });
      emit("done", { text: "会话已通过Pi原生检查点整理。", error: null, messages: [], stats: session.getSessionStats() });
      return;
    }
    await appendContextMail(session, manager, cfg.context_mail || []);
    if (cfg.current_facts)
      await session.sendCustomMessage({
        customType: "muse-current-task",
        content: cfg.current_facts,
        display: false,
        details: { event_id: cfg.event_id, origin: "current_task_snapshot" },
      }, { triggerTurn: false });
    if (cfg.program_event)
      await session.sendCustomMessage(
        {
          customType: "muse-program-event",
          content: cfg.prompt,
          display: false,
          details: { event_id: cfg.event_id, origin: "program" },
        },
        { triggerTurn: true },
      );
    else await session.prompt(cfg.prompt);
    // A brand-new program-only session is not persisted by Pi until it has a
    // user/assistant entry. Never acknowledge in-memory-only delivery.
    if ((cfg.context_mail || []).length) {
      let saved = false;
      try { saved = (await fs.stat(manager.getSessionFile())).isFile(); } catch {}
      if (saved) await call("context_delivered", { ids: cfg.context_mail.map((item) => item.id) });
    }
    emit("done", {
      ...outcome.result(),
      stats: session.getSessionStats(),
      session_file: manager.getSessionFile(),
      messages: session.messages.map((m) => ({
        ...m,
        content: Array.isArray(m.content)
          ? m.content.filter((c) => c.type !== "thinking")
          : m.content,
      })),
    });
  } finally {
    session.dispose();
    lines.close();
  }
}
