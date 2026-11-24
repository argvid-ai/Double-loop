// Observe provider usage before the SDK normalizes missing cache fields to zero.
export function usageFetch(fetchImpl, report) {
  return async (url, init) => {
    const response = await fetchImpl(url, init);
    if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream"))
      return response;
    const decoder = new TextDecoder();
    let pending = "";
    let usage = null;
    let reported = false;
    async function consume(line) {
      if (!line.startsWith("data:")) return;
      const data = line.slice(5).trim();
      if (data === "[DONE]") {
        if (!reported) await report({ usage, provider_usage_present: usage !== null });
        reported = true;
        return;
      }
      let event;
      try {
        event = JSON.parse(data);
      } catch { return; }
      const observed = event.usage ?? event.response?.usage;
      if (observed != null) {
        usage = observed;
        await report({ usage, provider_usage_present: true });
        reported = true;
      }
    }
    const stream = response.body.pipeThrough(new TransformStream({
      async transform(chunk, controller) {
        pending += decoder.decode(chunk, { stream: true });
        const lines = pending.split("\n");
        pending = lines.pop();
        for (const line of lines) await consume(line);
        controller.enqueue(chunk);
      },
      async flush() {
        pending += decoder.decode();
        await consume(pending);
        if (!reported) await report({ usage, provider_usage_present: usage !== null });
      },
    }));
    return new Response(stream, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  };
}
