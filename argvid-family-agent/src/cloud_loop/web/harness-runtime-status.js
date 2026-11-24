// Operational facts, not model-authored claims. Old snapshots stay labelled old.
function renderRuntimeStatus() {
  const panel = $('runtimeStatus');
  if (!panel) return;
  const rows = [];
  for (const side of ['edge', 'cloud']) {
    const status = state.snapshots[side]?.runtime_status;
    if (!online(side)) {
      rows.push({title: (side === 'edge' ? '录像端' : '主服务') + '状态暂未取得，实际状态未知；正在重试',
        severity: 'warning', observed_at: status?.checked_at, stale: true});
      continue;
    }
    rows.push(...(status?.items || []));
  }
  const visible = rows.some(row => row.severity !== 'ok');
  if (!changed('runtime-status', [visible, rows])) return;
  panel.hidden = !visible;
  panel.replaceChildren();
  if (!visible) return;
  panel.append(el('strong', '运行情况 · 有问题需要留意'));
  const list = el('div', undefined, 'runtime-status-grid');
  for (const row of rows) {
    const item = el('div', undefined, 'runtime-status-item ' + row.severity);
    item.append(el('b', row.title));
    const details = [];
    if (row.last_success) details.push('最近完成：' + stamp(row.last_success));
    if (row.observed_at) details.push((row.stale ? '上次收到状态：' : '发生时间：') + stamp(row.observed_at));
    if (row.pending_seconds > 0) details.push('累计未判断 ' + Math.ceil(row.pending_seconds / 60) +
      ' 分钟（含历史未完成，不代表当前延迟）');
    if (row.unavailable_seconds > 0) details.push('无法判断 ' + row.unavailable_seconds + ' 秒，未计为成功');
    if (row.retry_seconds > 0) details.push('自动重试间隔：' + row.retry_seconds + ' 秒');
    if (details.length) item.append(el('small', details.join('；')));
    list.append(item);
  }
  panel.append(list);
}
