// Owner-authorized history maintenance. Append SDK edits; never rewrite native history.
import fs from 'node:fs';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';

export function planCleanup(manager, eligibleEvents) {
  let internal = false;
  const retired = [], sourceEvents = new Set();
  const active = new Set(manager.buildSessionProjection().entries.map(e => e.sourceEntry.id));
  for (const entry of manager.getBranch()) {
    if (entry.type !== 'message') continue;
    const message = entry.message;
    if (message.role === 'user') {
      const text = typeof message.content === 'string' ? message.content :
        message.content.filter(c => c.type === 'text').map(c => c.text).join('\n');
      internal = false;
      if (text.startsWith('[程序事件，不是用户新指令]\n')) {
        try {
          const data = JSON.parse(text.split('\n')[1]);
          internal = eligibleEvents.has(data.event_id) && data.kind === 'observation.alert';
          if (internal) sourceEvents.add(data.event_id);
        } catch { /* Unknown provenance is preserved, never guessed from wording. */ }
      }
    }
    if (internal && active.has(entry.id) && ['user','assistant','toolResult'].includes(message.role))
      retired.push(entry.id);
  }
  return {retired, sourceEvents: [...sourceEvents]};
}

export function applyCleanup(manager, plan, summary, getCurrentSystemMessage) {
  const old = manager.getBranch().filter(e => e.type === 'compaction').at(-1);
  if (!old) throw Error('No reviewed compaction to replace');
  const before = manager.buildSessionProjection();
  const keep = before.entries.filter(e => e.sourceEntry.type === 'message' &&
    ['user','assistant','toolResult'].includes(e.sourceEntry.message.role) &&
    !plan.retired.includes(e.sourceEntry.id));
  manager.appendCompaction(summary, old.firstKeptEntryId, old.tokensBefore,
    {owner_cleanup: true, replaces_summary: old.id, archived_source_events: plan.sourceEvents}, true);
  for (const id of plan.retired) manager.appendContextEdit(id, null);
  const after = manager.buildSessionProjection();
  const index = new Map(after.entries.map(e => [e.sourceEntry.id, e.messages]));
  // Native compaction moves the latest system checkpoint and refreshes its
  // timestamp. Compare its meaning, not the old checkpoint's source identity.
  const systemMeaning = messages => {
    const system = getCurrentSystemMessage(messages);
    if (!system) return null;
    const {timestamp, ...meaning} = system;
    return JSON.stringify(meaning);
  };
  if (systemMeaning(before.messages) !== systemMeaning(after.messages))
    throw Error('System checkpoint content changed');
  for (const entry of keep)
    if (JSON.stringify(index.get(entry.sourceEntry.id)) !== JSON.stringify(entry.messages))
      throw Error('Retained message changed: ' + entry.sourceEntry.id);
  for (const id of plan.retired)
    if (index.get(id)?.length) throw Error('Internal message still in model context');
  return {retired_messages: plan.retired.length, retained_messages: keep.length,
    original_summary_preserved: manager.getEntry(old.id).summary === old.summary};
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [sdkPath, sessionFile, inputFile, mode] = process.argv.slice(2);
  const {SessionManager} = await import(pathToFileURL(sdkPath));
  const {getCurrentSystemMessage} = await import(new URL('../../pi-ai/dist/index.js', pathToFileURL(sdkPath)));
  const input = JSON.parse(fs.readFileSync(inputFile, 'utf8'));
  const raw = fs.readFileSync(sessionFile);
  const manager = SessionManager.open(sessionFile);
  const old = manager.getBranch().filter(e => e.type === 'compaction').at(-1);
  if (old?.id !== input.summary_id ||
      crypto.createHash('sha256').update(old.summary).digest('hex') !== input.summary_sha256)
    throw Error('Summary changed; re-read before cleanup');
  const plan = planCleanup(manager, new Set(input.eligible_events));
  const before = {leaf: manager.getLeafId(), native_sha256: crypto.createHash('sha256').update(raw).digest('hex'),
    ...plan};
  if (mode === 'apply') {
    if (before.native_sha256 !== input.expected_native_sha256) throw Error('Native history changed since preview');
    const result = applyCleanup(manager, plan, input.summary, getCurrentSystemMessage);
    if (!fs.readFileSync(sessionFile).subarray(0, raw.length).equals(raw))
      throw Error('Native history was not append-only');
    console.log(JSON.stringify({...before, ...result}));
  } else console.log(JSON.stringify(before));
}
