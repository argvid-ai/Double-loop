// Read only this invocation's native events, never a previous turn's failure.
// Compaction may replace the session projection, so array offsets are not IDs.
export function runOutcome() {
  let assistant = null;
  let compactionError = null;
  return {
    observe(event) {
      if (event.type === "compaction_end")
        compactionError = event.errorMessage || null;
      if (event.type === "message_end" && event.message?.role === "assistant") {
        assistant = event.message;
        // A later successful response means the earlier recovery succeeded.
        if (assistant.stopReason === "stop" || assistant.stopReason === "toolUse")
          compactionError = null;
      }
    },
    result() {
      const text = (assistant?.content || [])
        .filter((item) => item.type === "text").map((item) => item.text).join("");
      const error = compactionError || assistant?.errorMessage ||
        (assistant?.stopReason === "length" ? "Model response reached its output limit" :
         assistant?.stopReason === "error" ? "Model request failed" :
         assistant?.stopReason === "aborted" ? "Model request aborted" :
         !assistant ? "No model response was produced in this run" : null);
      return {text, error};
    },
  };
}
