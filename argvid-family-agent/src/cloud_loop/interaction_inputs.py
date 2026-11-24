"""Mock/ASR transcripts enter the existing typed, freshness-checked input boundary."""

from .contracts import UserMessage
from .inputs import InputIngress

# Exact commands only: do not treat e.g. “不要暂停” as a pause instruction.
CONTROL_PHRASES = {
    "暂停": "pause",
    "暂停当前拍摄任务": "pause",
    "请暂停当前拍摄任务": "pause",
    "停止当前拍摄任务": "pause",
    "继续当前任务": "resume",
    "恢复当前任务": "resume",
    "取消当前任务": "cancel",
    "取消当前拍摄任务": "cancel",
}


class ConversationIngress(InputIngress):
    def _transcript(self, db, event):
        if not db.execute("SELECT 1 FROM conversations WHERE id=?", (event.task_id,)).fetchone():
            return super()._transcript(db, event)
        action = CONTROL_PHRASES.get(event.data.text.strip().rstrip("。！!"))
        if action:
            result = self.store.control(
                event.task_id, action, event_id=event.event_id, source_text=event.data.text, _db=db
            )
            return {
                "accepted": True,
                "control": action,
                "revision": result["revision"],
                "simulated": event.simulated,
            }
        result = self.store.submit_message(
            event.task_id, UserMessage(event_id=event.event_id, text=event.data.text), _db=db
        )
        return {"accepted": True, "revision": result["revision"], "simulated": event.simulated}
