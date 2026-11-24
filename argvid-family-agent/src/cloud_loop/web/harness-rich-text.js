'use strict';

// Render a deliberately small Markdown subset with DOM text nodes. Model text
// never becomes HTML, script, an embedded remote image, or an arbitrary URL.
function messageLink(href) {
  const asset = /^(?:cloud-asset|media):\/\/([\w.:-]+)$/.exec(href);
  if (asset) return '/workbench/cloud/assets/' + encodeURIComponent(asset[1]);
  try {
    const url = new URL(href);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}

function inlineText(parent, value, depth = 0) {
  const text = String(value || '');
  if (depth > 3) {
    parent.append(document.createTextNode(text));
    return parent;
  }
  const tokens = /`([^`\n]+)`|\*\*([^\n]+?)\*\*|==([^\n]+?)==|\[([^\]\n]+)\]\(([^\s)]+)\)/g;
  let offset = 0;
  for (const match of text.matchAll(tokens)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    if (match[1]) parent.append(el('code', match[1]));
    else if (match[2] || match[3]) {
      parent.append(inlineText(el(match[2] ? 'strong' : 'mark'), match[2] || match[3], depth + 1));
    } else {
      const url = messageLink(match[5]);
      const link = inlineText(el(url ? 'a' : 'span'), match[4], depth + 1);
      if (url) {
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
      }
      parent.append(link);
    }
    offset = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(offset)));
  return parent;
}

function richText(value) {
  const box = el('div', undefined, 'message-body');
  const lines = String(value || '')
    .replace(/\r\n?/g, '\n')
    .split('\n');
  const cells = (line) =>
    line
      .trim()
      .replace(/^\||\|$/g, '')
      .split('|')
      .map((s) => s.trim());
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) continue;
    if (/^\s*```/.test(line)) {
      const content = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) content.push(lines[i]);
      const pre = el('pre');
      pre.append(el('code', content.join('\n')));
      box.append(pre);
    } else if (line.trim().startsWith('|') && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] || '')) {
      const wrap = el('div', undefined, 'table-scroll'),
        table = el('table');
      const head = el('thead'),
        header = el('tr'),
        body = el('tbody');
      cells(line).forEach((c) => header.append(inlineText(el('th'), c)));
      head.append(header);
      table.append(head, body);
      i += 2;
      for (; i < lines.length && lines[i].trim().startsWith('|'); i++) {
        const row = el('tr');
        cells(lines[i]).forEach((c) => row.append(inlineText(el('td'), c)));
        body.append(row);
      }
      i--;
      wrap.append(table);
      box.append(wrap);
    } else if (/^\s*#{1,6}\s+/.test(line)) {
      box.append(inlineText(el('h4'), line.replace(/^\s*#{1,6}\s+/, '')));
    } else if (/^\s*>\s?/.test(line)) {
      const quote = el('blockquote');
      for (; i < lines.length && /^\s*>\s?/.test(lines[i]); i++) {
        quote.append(inlineText(el('p'), lines[i].replace(/^\s*>\s?/, '')));
      }
      i--;
      box.append(quote);
    } else if (/^\s*(?:[-+*]|\d+[.)])\s+/.test(line)) {
      const numbered = /^\s*\d+[.)]/.test(line),
        list = el(numbered ? 'ol' : 'ul');
      const pattern = numbered ? /^\s*\d+[.)]\s+/ : /^\s*[-+*]\s+/;
      if (numbered) list.start = Number(line.trim().match(/^\d+/)[0]);
      for (; i < lines.length && pattern.test(lines[i]); i++) {
        list.append(inlineText(el('li'), lines[i].replace(pattern, '')));
      }
      i--;
      box.append(list);
    } else box.append(inlineText(el('p'), line));
  }
  return box;
}
