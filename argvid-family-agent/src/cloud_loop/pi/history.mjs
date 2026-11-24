import { pathToFileURL } from 'node:url';
import path from 'node:path';
const sdk = await import(
  pathToFileURL(
    path.join(process.argv[2], 'node_modules/@earendil-works/pi-coding-agent/dist/index.js'),
  )
);
const context = sdk.SessionManager.open(process.argv[3]).buildSessionContext();
console.log(
  JSON.stringify({
    messages: context.messages.map((m) => ({
      ...m,
      content: Array.isArray(m.content)
        ? m.content.filter((c) => c.type !== 'thinking')
        : m.content,
    })),
  }),
);
