import { usageFetch } from "./usage-fetch.mjs";
import crypto from "node:crypto";

// Switch providers at a failed model request, never replay an agent/tool turn.
export function retryable(error, published = false, aborted = false) {
  return (
    !published &&
    !aborted &&
    /ECONN|ENOTFOUND|EHOST|ETIMEDOUT|fetch failed|network|connect|timed?\s*out|timeout|\b(429|500|502|503|504)\b/i.test(
      String(error?.errorMessage || error?.message || error),
    )
  );
}

export function modelRouter({ fallback, notify, record, requestId, onSwitch, outputTokens }) {
  let selected = null;
  const optionsFor = (options) => ({
    ...options,
    maxTokens: options?.maxTokens ?? (selected ? fallback.outputTokens : outputTokens),
    ...(selected ? { apiKey: fallback.key, timeoutMs: fallback.timeoutMs } : {}),
  });
  async function switchToFallback(model, error, published, options) {
    if (
      !fallback ||
      selected ||
      !retryable(error, published, options?.signal?.aborted)
    )
      return false;
    selected = fallback.model;
    await onSwitch?.(selected);
    await notify({
      from_model: model.id,
      to_model: selected.id,
      reason: String(error?.errorMessage || error?.message || error).slice(
        0,
        600,
      ),
    });
    return true;
  }
  async function* stream(original, requested, context, options) {
    for (;;) {
      const model = selected || requested;
      const id = requestId();
      const requestOptions = optionsFor(options);
      let published = false;
      const buffered = [];
      let retry = false;
      await record("model_request", {
        request_id: id,
        context,
        model: model.id,
        provider: model.provider,
      });
      try {
        const source = await original(model, context, {
          ...requestOptions,
          onPayload: async (payload, routeModel) => {
            const replacement = await requestOptions?.onPayload?.(payload, routeModel);
            const sent = replacement ?? payload;
            const fingerprint = (value) => crypto.createHash("sha256")
              .update(JSON.stringify(value)).digest("hex");
            await record("wire_request", {
              request_id: id,
              model: model.id,
              provider: model.provider,
              reasoning: sent.reasoning ?? sent.reasoning_effort ?? null,
              max_output_tokens: sent.max_output_tokens ?? sent.max_tokens ?? sent.max_completion_tokens ?? null,
              output_budget_field: ["max_output_tokens", "max_tokens", "max_completion_tokens"]
                .find((field) => sent[field] != null) ?? null,
              tool_count: sent.tools?.length ?? 0,
              tools_sha256: fingerprint(sent.tools ?? []),
              system_sha256: fingerprint(sent.instructions ?? (sent.messages ?? sent.input ?? []).filter(
                (message) => message.role === "system" || message.role === "developer")),
            });
            return replacement;
          },
          fetch: usageFetch(requestOptions?.fetch ?? globalThis.fetch, (data) =>
            record("wire_usage", { request_id: id, model: model.id, provider: model.provider, ...data })),
        });
        for await (const item of source) {
          if (item.type === "error") {
            await record("model_response", {
              request_id: id,
              message: item.error,
              error: true,
            });
            if (await switchToFallback(model, item.error, published, options)) {
              retry = true;
              break;
            }
          }
          if (item.type === "done")
            await record("model_response", {
              request_id: id,
              message: item.message,
              error: false,
            });
          if (item.type === "start") {
            buffered.push(item);
            continue;
          }
          if (!["error", "done"].includes(item.type)) published = true;
          while (buffered.length) yield buffered.shift();
          yield item;
        }
        if (!retry) return;
      } catch (error) {
        await record("model_failed", {
          request_id: id,
          error: String(error.message || error),
        });
        if (!(await switchToFallback(model, error, published, options)))
          throw error;
      }
    }
  }
  async function complete(original, requested, context, options) {
    for (;;) {
      const model = selected || requested;
      const id = requestId();
      await record("model_request", {
        request_id: id,
        context,
        model: model.id,
        provider: model.provider,
      });
      try {
        const requestOptions = optionsFor(options);
        const result = await original(model, context, {
          ...requestOptions,
          fetch: usageFetch(requestOptions?.fetch ?? globalThis.fetch, (data) =>
            record("wire_usage", {
              request_id: id, model: model.id, provider: model.provider, ...data,
            })),
        });
        const failed = result.stopReason === "error";
        await record("model_response", {
          request_id: id,
          message: result,
          error: failed,
        });
        if (
          failed &&
          (await switchToFallback(
            model,
            result,
            !!result.content?.length,
            options,
          ))
        )
          continue;
        return result;
      } catch (error) {
        await record("model_failed", {
          request_id: id,
          error: String(error.message || error),
        });
        if (!(await switchToFallback(model, error, false, options)))
          throw error;
      }
    }
  }
  return {
    stream,
    complete,
    get currentModel() { return selected; },
  };
}
