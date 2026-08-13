/* ============ Gateway Model Comparison / Replay Module ============ */
let REPLAY_CUR = null;   // 当前选中的报告名

function loadReplay() {
  api('/api/replay/reports').then(j => {
    const reports = j.reports || [];
    const sel = document.getElementById('replay-sel');
    if (!reports.length) {
      sel.innerHTML = '<option value="">（无报告：先跑 tests/test_gemini_replay.py）</option>';
      document.getElementById('replay-meta').textContent = '';
      document.getElementById('replay-stat').textContent = '';
      document.getElementById('replay-summary').textContent = '暂无对比报告';
      document.getElementById('replay-groups').innerHTML = '';
      REPLAY_CUR = null;
      return;
    }
    let h = '';
    for (const r of reports) {
      const selAttr = r.name === REPLAY_CUR ? ' selected' : '';
      h += '<option value="' + esc(r.name) + '"' + selAttr + '>' +
           esc(r.generated_at || r.name) + ' · ' + esc(r.model) +
           ' · 一致 ' + r.match + '/' + r.n_samples + '</option>';
    }
    sel.innerHTML = h;
    const name = sel.value || reports[0].name;
    REPLAY_CUR = name;
    loadReplayReport(name);
  }).catch(e => toast(e.message, 'err'));
}

function loadReplayReport(name) {
  if (!name) return;
  REPLAY_CUR = name;
  api('/api/replay/report?name=' + encodeURIComponent(name)).then(j => {
    document.getElementById('replay-meta').textContent =
      '报告 ' + j.name + ' · 生成 ' + (j.generated_at || '') +
      ' · 每 prompt 采样 ' + j.samples + ' 次';
    renderReplay(j);
  }).catch(e => toast(e.message, 'err'));
}

function renderReplay(j) {
  const groups = j.groups || [];
  const nGroups = groups.length;
  const nSample = groups.reduce((a, g) => a + g.samples.length, 0);
  document.getElementById('replay-stat').textContent =
    nGroups + ' 组 / ' + nSample + ' 次采样';
  document.getElementById('replay-summary').textContent =
    '共 ' + nGroups + ' 组真实场景 · 展开各组卡片对比不同模型的输出分块与耗时';

  const wrap = document.getElementById('replay-groups');
  if (!groups.length) {
    wrap.innerHTML = '<div class="empty">无分组数据</div>';
    return;
  }
  let h = '';
  groups.forEach((g, i) => {
    const labelBadge = g.label
      ? '<span class="badge blue">' + esc(g.label) + '</span>'
      : '';
    const newModelTitle = j.new_model ? esc(j.new_model) : '';
    const hasNewModel = groups.some(g => g.samples.some(s => s.new_text !== undefined));
    h += '<div class="card">' +
         '<h2>#' + String(i + 1).padStart(2, '0') + ' ' +
         esc(g.session || '?') + (g.round ? ' · ' + esc(g.round) : '') +
         ' ' + labelBadge + '</h2>' +
         '<div class="dim" style="margin-bottom:12px;font-size:13px">' +
         esc(g.scene || '（无场景）') + '</div>' +
         '<div class="split">' +
         '  <div class="col-list" style="flex:1">' +
         '    <div style="font-size:12px;color:var(--muted);margin-bottom:8px;font-weight:600">k3 (官方)</div>' +
         g.k3_outputs.map(k => renderModelCol(k.text, k.blocks, true, g.k3_meta ? g.k3_meta.latency_s : null, 'k3')).join('') +
         '  </div>' +
         '  <div class="col-detail" style="flex:1.2;border-left:1px solid var(--border-light);padding-left:14px">' +
         '    <div style="font-size:12px;color:var(--muted);margin-bottom:8px;font-weight:600">' +
         esc(j.model || 'Gemini 3.1 Pro') + '</div>' +
         g.samples.map(s => renderModelCol(s.gemini_text, s.gemini_blocks, s.gemini_valid, s.latency_s, s.model_version || j.model)).join('') +
         '  </div>' +
         (hasNewModel ?
         '  <div class="col-detail" style="flex:1.2;border-left:1px solid var(--border-light);padding-left:14px">' +
         '    <div style="font-size:12px;color:var(--muted);margin-bottom:8px;font-weight:600">' +
         (newModelTitle ? newModelTitle : 'Gemini 3.6 Flash') + '</div>' +
         g.samples.map(s => renderModelCol(s.new_text, s.new_blocks, s.new_valid, s.new_latency_s, s.new_model_version || newModelTitle)).join('') +
         '  </div>' : '') +
         '</div></div>';
  });
  wrap.innerHTML = h;
}

function decideTag(text) {
  const m = String(text || '').match(/<(reply|task|tool|silent)/g) || [];
  const tags = m.map(x => x.slice(1));
  if (!tags.length) return 'invalid';
  if (tags.every(t => t === 'silent')) return 'silent';
  if (tags.includes('task')) return 'task';
  if (tags.includes('reply')) return 'reply×' + tags.filter(t => t === 'reply').length;
  return tags.join(',');
}

function renderModelCol(text, blocks, isValid, latency, modelVersion) {
  if (!text) {
    return '<div style="border:1px solid var(--border-light);border-radius:var(--r-sm);padding:10px;margin-bottom:8px;color:var(--muted);font-size:13px">（无输出）</div>';
  }
  const d = decideTag(text);
  const validBadge = (isValid !== false)
    ? '<span class="badge">合法</span>'
    : '<span class="badge coral">非法XML</span>';
  return '<div style="border:1px solid var(--border-light);border-radius:var(--r-sm);' +
         'padding:10px;margin-bottom:8px;background:var(--surface)">' +
         '<div style="margin-bottom:6px;display:flex;align-items:center;gap:6px">' +
         '<span class="badge">' + esc(d) + '</span> ' + validBadge +
         ' <span class="dim" style="font-size:12px;margin-left:auto">' + (latency != null ? latency + 's' : '') +
         ' ' + esc(modelVersion || '') + '</span></div>' +
         renderBlocks(blocks || []) +
         xmlDetails(text) + '</div>';
}

function renderK3(k) {
  return renderModelCol(k.text, k.blocks, true, null, 'k3');
}

function renderSample(s) {
  return renderModelCol(s.gemini_text, s.gemini_blocks, s.gemini_valid, s.latency_s, s.model_version);
}

function renderNewSample(s) {
  return renderModelCol(s.new_text, s.new_blocks, s.new_valid, s.new_latency_s, s.new_model_version);
}

function renderBlocks(blocks) {
  if (!blocks || !blocks.length)
    return '<div class="dim" style="font-size:12px;margin:6px 0">（无动作块）</div>';
  return '<div style="display:flex;flex-direction:column;gap:6px;margin-top:6px">' +
         blocks.map(renderBlock).join('') + '</div>';
}

function renderBlock(b) {
  let head = '<span class="badge dark">&lt;' + esc(b.tag) + '&gt;</span>';
  const attrBadges = Object.entries(b.attrs || {}).map(([k, v]) =>
    '<span class="badge blue">' + esc(k) + '=' + esc(v) + '</span>').join(' ');
  if (attrBadges) head += ' ' + attrBadges;
  if (!b.valid) head += ' <span class="badge coral">坏块: ' + esc(b.error) + '</span>';

  let body = '';
  if (b.tag === 'reply') {
    const bubbles = (b.texts || []).map(t =>
      '<div style="background:var(--pale-green);border-radius:14px;padding:6px 12px;margin:3px 0;' +
      'font-size:13px;display:inline-block;max-width:100%;word-break:break-word;color:var(--deep-green)">' +
      esc(t) + '</div>').join('');
    const extras = []
      .concat((b.quotes || []).map(q => '<div class="dim" style="font-size:12px">💬 引用: ' + esc(q) + '</div>'))
      .concat((b.files || []).map(f => '<div class="dim" style="font-size:12px">📁 文件: ' + esc(f) + '</div>'))
      .concat((b.images || []).map(i => '<div class="dim" style="font-size:12px">🖼 图片: ' + esc(i) + '</div>'))
      .join('');
    body = bubbles + (extras ? '<div style="margin-top:4px">' + extras + '</div>' : '');
  } else if (b.tag === 'silent') {
    body = '<div class="dim" style="font-size:12px">（保持沉默，不回复）</div>';
  } else if (b.inner) {
    body = '<div style="font-size:12px;white-space:pre-wrap;color:var(--ink-soft)">' + esc(b.inner.slice(0, 300)) + '</div>';
  }
  return '<div style="background:var(--stone);padding:8px 10px;border-radius:var(--r-sm)">' + head + (body ? '<div style="margin-top:4px">' + body + '</div>' : '') + '</div>';
}

function xmlDetails(text) {
  if (!text) return '';
  return '<details style="margin-top:6px;font-size:12px;color:var(--muted)">' +
         '<summary style="cursor:pointer">原始 XML</summary>' +
         '<pre style="margin:4px 0 0;padding:8px;background:var(--primary);color:#fff;border-radius:var(--r-sm);white-space:pre-wrap;word-break:break-word">' +
         esc(text) + '</pre></details>';
}
