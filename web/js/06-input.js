'use strict';

/* ── 向用户提问（ask_user 工具）────────────────────────────── */
function showAskCard(botEl, f) {
  const card = document.createElement('div');
  card.className = 'ask-card';
  const opts = (f.options || []).slice(0, 6);
  card.innerHTML = `<div class="ask-q">❓ ${esc(f.question || '')}</div>` +
    (opts.length ? `<div class="ask-opts">${opts.map((o) =>
      `<button class="btn small ask-opt">${esc(o)}</button>`).join('')}</div>` : '') +
    `<div class="ask-custom"><input type="text" placeholder="或直接输入你的回答…">` +
    `<button class="btn small primary ask-send">回答</button>` +
    `<button class="btn small ghost ask-skip">跳过</button>` +
    `<span class="ask-tip muted"></span></div>`;
  const acts = botEl.querySelector('.msg-acts');
  if (acts) botEl.insertBefore(card, acts); else botEl.appendChild(card);
  const input = card.querySelector('input');
  input.focus();
  let left = 180;
  const tip = card.querySelector('.ask-tip');
  const timer = setInterval(() => {
    left -= 1;
    tip.textContent = left > 0 ? `${left}s 后按未回答继续` : '已超时，按未回答继续';
    if (left <= 0) clearInterval(timer);
  }, 1000);
  tip.textContent = '180s 后按未回答继续';
  let done = false;
  async function submit(answer, skipped) {
    if (done) return;
    done = true;
    clearInterval(timer);
    try {
      await fetch('/api/answer', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answer: answer || '' }) });
    } catch (e) {}
    card.classList.add('done');
    card.innerHTML = skipped
      ? '<div class="ask-done">⏭ 已跳过，AI 将按最合理的假设继续</div>'
      : `<div class="ask-done">✅ 你的回答：${esc(answer || '')}</div>`;
    followScroll($('#messages'));
  }
  card.querySelectorAll('.ask-opt').forEach((b) => {
    b.onclick = () => submit(b.textContent.trim(), false);
  });
  card.querySelector('.ask-send').onclick = () => submit(input.value.trim(), false);
  card.querySelector('.ask-skip').onclick = () => submit('', true);
  input.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submit(input.value.trim(), false); }
  };
}

/* ── 变更 diff / 撤销 ──────────────────────────────────────── */
/* ── 站内预览：产物点开不再直接下载/新开页（U6）────────── */
async function openPreview(rel, name) {
  if (!rel) return;
  $('#modalTitle').textContent = (name || rel.split('/').pop());
  $('#modalBody').innerHTML = '<div class="modal-loading">加载中…</div>';
  openModal();
  $('#modal').classList.add('pv');
  const fileUrl = '/api/file?path=' + encodeURIComponent(rel);
  let d;
  let vers = [];
  try {
    const r = await fetch('/api/preview?path=' + encodeURIComponent(rel));
    d = await r.json();
    if (!r.ok) throw new Error(d.error || '加载失败');
  } catch (e) {
    $('#modalBody').innerHTML = `<div class="pv-hint">预览失败：${esc(e.message)}</div>`;
    return;
  }
  // 历史版本（B3 产物版本）：覆盖写前自动存的旧版
  try {
    const vr = await fetch('/api/artifact/versions?path=' + encodeURIComponent(rel));
    const vd = await vr.json();
    vers = vd.versions || [];
  } catch (e) { vers = []; }
  const meta = `<div class="pv-meta"><span class="pv-name">${esc(name || rel)}</span>` +
    `<span>${fmtSize(d.size)}</span>` +
    (d.truncated ? '<span>（内容过长，仅显示开头）</span>' : '') +
    `<span class="muted">${esc(rel)}</span></div>`;
  let body;
  if (d.type === 'image') {
    body = `<img src="${fileUrl}" alt="${esc(name || '')}">`;
  } else if (d.type === 'pdf') {
    body = `<iframe src="${fileUrl}" title="${esc(name || 'PDF')}"></iframe>`;
  } else if (d.type === 'text' && d.ext === 'csv') {
    body = csvTableHtml(d.text || '');
  } else if (d.type === 'text' && (d.ext === 'md' || d.ext === 'html')) {
    body = `<div class="md">${md.render(d.text || '')}</div>`;
  } else if (d.type === 'text' || d.type === 'doc') {
    body = `<pre>${esc(d.text || '（空文件）')}</pre>`;
  } else if (d.type === 'unsupported') {
    body = `<div class="pv-hint">${esc(d.hint || '缺少对应依赖，无法提取内容')}<br>仍可下载原始文件或用系统程序打开。</div>`;
  } else {
    body = `<div class="pv-hint">${esc((d.ext || '').toUpperCase() || '该')} 类型暂不支持内联预览，可下载原始文件或用系统程序打开。</div>`;
  }
  const verBar = vers.length ? `<div class="pv-vers">
      <span class="pv-vlab">历史版本</span>
      <select id="pvVer"><option value="">当前（最新）</option>` +
      vers.map((v) => `<option value="${esc(v.ts)}">${esc(fmtVerTs(v.ts))} · ${fmtSize(v.size)}</option>`).join('') +
      `</select>
      <button class="btn small" id="pvDiff" disabled>与当前对比</button>
      <button class="btn small" id="pvRestore" disabled>恢复此版本</button>
      <a class="btn small hidden" id="pvDl" target="_blank" rel="noopener">${ico('download')} 下载</a>
    </div><div id="pvVerBody"></div>` : '';
  $('#modalBody').innerHTML = meta + verBar + body +
    `<div class="btns">
      <a class="btn" href="${fileUrl}" target="_blank" rel="noopener">${ico('download')} 原始文件</a>
      ${state.isLoopback
        ? `<button class="btn" id="pvFolder">${ico('folder')} 打开所在目录</button>`
          + `<button class="btn primary" id="pvOpen">${ico('monitor')} 用系统程序打开</button>`
        : `<span class="muted">远程访问：「打开目录 / 用系统程序打开」只作用于服务器本机，已隐藏</span>`}
    </div>`;
  const pvFolder = $('#pvFolder');
  if (pvFolder) pvFolder.onclick = () => openArtifactFolder(rel);
  const pvOpen = $('#pvOpen');
  if (pvOpen) pvOpen.onclick = async () => {
    try {
      const r = await fetch('/api/open-file?path=' + encodeURIComponent(rel));
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || '打开失败');
      toast('🖥 已用系统程序打开');
    } catch (e) { toast('打开失败：' + e.message, true); }
  };
  // 历史版本：切换预览 / 下载 / 恢复
  const verSel = $('#pvVer');
  if (verSel) {
    const vbody = $('#pvVerBody');
    const restoreBtn = $('#pvRestore');
    const dl = $('#pvDl');
    const diffBtn = $('#pvDiff');
    verSel.onchange = async () => {
      const ts = verSel.value;
      restoreBtn.disabled = !ts;
      if (diffBtn) diffBtn.disabled = !ts;
      dl.classList.toggle('hidden', !ts);
      if (!ts) { vbody.innerHTML = ''; return; }
      dl.href = `/api/artifact/version?path=${encodeURIComponent(rel)}&ts=${encodeURIComponent(ts)}`;
      dl.download = '';
      const vurl = dl.href;
      if (d.type === 'image') { vbody.innerHTML = `<img src="${vurl}" alt="历史版本">`; return; }
      if (d.type === 'pdf') { vbody.innerHTML = `<iframe src="${vurl}"></iframe>`; return; }
      if (d.type === 'text' || d.type === 'doc') {
        try {
          const r = await fetch(vurl);
          const txt = await r.text();
          vbody.innerHTML = (d.type === 'text' && (d.ext === 'md' || d.ext === 'html'))
            ? `<div class="md">${md.render(txt)}</div>`
            : `<pre>${esc(txt.slice(0, 400000))}</pre>`;
        } catch (e) { vbody.innerHTML = `<div class="pv-hint">加载失败：${esc(e.message)}</div>`; }
        return;
      }
      vbody.innerHTML = `<div class="pv-hint">该类型不支持内联预览，点「下载」查看</div>`;
    };
    let diffData = null;
    let diffMode = 'unified';
    const renderDiff = () => {
      if (!diffData) return;
      const hasSide = !!(diffData.side && diffData.side.rows);
      const view = (diffMode === 'side' && hasSide)
        ? sideDiffHtml(diffData) : diffViewHtml(diffData);
      vbody.innerHTML = `<div class="diff-tools">
          <button class="btn small${diffMode === 'unified' ? ' primary' : ''}"
            id="diffUnified">统一</button>
          ${hasSide ? `<button class="btn small${diffMode === 'side' ? ' primary' : ''}"
            id="diffSide">并排</button>` : ''}
        </div>` + view;
      if (diffMode === 'side' && hasSide) wireSideFolds(vbody);
      const u = $('#diffUnified');
      const sd = $('#diffSide');
      if (u) u.onclick = () => { diffMode = 'unified'; renderDiff(); };
      if (sd) sd.onclick = () => { diffMode = 'side'; renderDiff(); };
    };
    if (diffBtn) diffBtn.onclick = async () => {
      const ts = verSel.value;
      if (!ts) return;
      vbody.innerHTML = '<div class="pv-hint">对比中…</div>';
      try {
        const r = await fetch('/api/artifact/diff?path=' + encodeURIComponent(rel) +
                              '&ts=' + encodeURIComponent(ts));
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || '对比失败');
        diffData = d;
        renderDiff();
      } catch (e) {
        diffData = null;
        vbody.innerHTML = `<div class="pv-hint">对比失败：${esc(e.message)}</div>`;
      }
    };
    restoreBtn.onclick = () => {
      const ts = verSel.value;
      if (!ts) return;
      confirmDialog('恢复历史版本', `将把 ${name || rel} 恢复到 ${fmtVerTs(ts)} 的版本（当前内容会先存一版，可再反悔）。确定？`, async () => {
        try {
          const r = await fetch('/api/artifact/restore', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: rel, ts }),
          });
          const jd = await r.json();
          if (!r.ok) throw new Error(jd.error || '恢复失败');
          toast('✅ 已恢复到 ' + fmtVerTs(ts));
          openPreview(rel, name);   // 重新加载（当前预览 + 版本列表）
        } catch (e) { toast('恢复失败：' + e.message, true); }
      }, '恢复');
    };
  }
}
function diffViewHtml(f) {
  if (f.binary) {
    return `<div class="pv-hint">二进制文件（${fmtSize(f.size_before)} → ` +
      `${fmtSize(f.size_after)}），无法逐行对比</div>`;
  }
  const rows = (f.lines || []).map((l) => {
    const cls = l.t === '+' ? 'add' : l.t === '-' ? 'del' : l.t === '@' ? 'hunk' : '';
    return `<div class="dl ${cls}">${esc(l.s) || ' '}</div>`;
  }).join('');
  return `<div class="diff-file"><div class="df-head"><b>${esc(f.rel)}</b>` +
    `<span class="df-tag">${esc(f.ts)} → 当前 <i class="add">+${f.added}</i> ` +
    `<i class="del">−${f.removed}</i></span></div>` +
    (rows ? `<div class="diff-body">${rows}</div>`
          : '<div class="muted">内容无差异</div>') +
    (f.truncated ? '<div class="muted">（差异过长，已截断）</div>' : '') + '</div>';
}
let sideFolds = [];   // 并排视图里折叠的原行（按 id 存，展开时不用再请求）
function sideRowHtml(t, l, r) {
  const cls = { '=': 'eq', '+': 'add', '-': 'del', '~': 'chg' }[t] || 'eq';
  return `<tr class="s-${cls}"><td>${esc(l) || ' '}</td><td>${esc(r) || ' '}</td></tr>`;
}
function sideDiffHtml(f) {
  const rows = (f.side && f.side.rows) || [];
  sideFolds = [];
  if (!rows.length) return '<div class="muted">内容无差异</div>';
  const body = rows.map((r) => {
    if (r.t === '≋') {
      const id = sideFolds.length;
      sideFolds.push({ l: r.l || [], r: r.r || [] });
      return `<tr class="s-fold" data-fold="${id}" title="点击展开这几行">` +
        `<td colspan="2">⋯ 折叠了 ${r.n || (r.l || []).length} 行未改动内容，点击展开</td></tr>`;
    }
    return sideRowHtml(r.t, r.l, r.r);
  }).join('');
  return `<div class="diff-file"><div class="df-head"><b>${esc(f.rel)}</b>` +
    `<span class="df-tag">${esc(f.ts)} → 当前 <i class="add">+${f.added}</i> ` +
    `<i class="del">−${f.removed}</i></span></div>` +
    `<div class="diff-side"><table><thead><tr><th>旧版本</th><th>当前</th></tr></thead><tbody>` +
    body + `</tbody></table></div>` +
    ((f.side && f.side.truncated) ? '<div class="muted">（改动过多，已截断）</div>' : '') +
    `</div>`;
}
// 折叠行展开：就地插回原行，展开后变灰不可再点（避免重复插入）
function wireSideFolds(root) {
  (root || document).querySelectorAll('tr.s-fold').forEach((tr) => {
    tr.onclick = () => {
      if (tr.dataset.done) return;
      const fd = sideFolds[+tr.dataset.fold];
      if (!fd) return;
      const n = Math.max(fd.l.length, fd.r.length);
      let html = '';
      for (let i = 0; i < n; i++) html += sideRowHtml('=', fd.l[i] ?? '', fd.r[i] ?? '');
      tr.insertAdjacentHTML('afterend', html);
      tr.dataset.done = '1';
      tr.classList.add('s-fold-done');
      tr.lastElementChild.textContent = `⋯ 已展开 ${n} 行（点上面行可继续对比）`;
    };
  });
}
function fmtVerTs(ts) {
  const m = String(ts).match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/);
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` : String(ts);
}
function csvTableHtml(text) {
  const rows = String(text).split(/\r?\n/).filter((l) => l.length).slice(0, 201);
  if (!rows.length) return '<div class="pv-hint">（空表）</div>';
  const split = (l) => l.split(/,(?=(?:[^"]*"[^"]*")*[^"]*$)/)
    .map((c) => c.replace(/^"|"$/g, ''));
  const head = split(rows[0]);
  const body = rows.slice(1).map((l) => {
    const cells = split(l);
    return '<tr>' + head.map((_, i) => `<td>${esc(cells[i] || '')}</td>`).join('') + '</tr>';
  }).join('');
  return `<table class="pv-table"><thead><tr>` +
    head.map((h) => `<th>${esc(h)}</th>`).join('') + `</tr></thead><tbody>${body}</tbody></table>` +
    (rows.length > 201 ? '<div class="pv-hint">（仅显示前 200 行）</div>' : '');
}

async function showTurnDiff(turn) {
  if (!state.cur || !turn) return toast('缺少轮次信息', true);
  $('#modalTitle').textContent = '本轮改动';
  $('#modalBody').innerHTML = '<div class="modal-loading">加载中…</div>';
  openModal();
  $('#modal').classList.add('wide');
  try {
    const r = await fetch(`/api/turn/diff?id=${encodeURIComponent(state.cur)}&turn=${encodeURIComponent(turn)}`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '加载失败');
    const files = d.files || [];
    if (!files.length) {
      $('#modalBody').innerHTML = '<div class="muted">这一轮没有可展示的改动。</div>';
      return;
    }
    $('#modalBody').innerHTML = `<div class="diff-sum">共 ${files.length} 个文件</div>` +
      files.map((f) => {
        const label = { added: '新增', modified: '修改', deleted: '删除' }[f.status] || f.status;
        if (f.binary) {
          return `<div class="diff-file"><div class="df-head"><b>${esc(f.rel)}</b>` +
            `<span class="df-tag">${label} · 二进制 ${fmtSize(f.size_before)} → ${fmtSize(f.size_after)}</span></div></div>`;
        }
        const rows = (f.lines || []).map((l) => {
          const cls = l.t === '+' ? 'add' : l.t === '-' ? 'del' : l.t === '@' ? 'hunk' : '';
          return `<div class="dl ${cls}">${esc(l.s) || ' '}</div>`;
        }).join('');
        return `<div class="diff-file"><div class="df-head"><b>${esc(f.rel)}</b>` +
          `<span class="df-tag">${label} <i class="add">+${f.added}</i> <i class="del">−${f.removed}</i></span></div>` +
          (rows ? `<div class="diff-body">${rows}</div>` : '<div class="muted">内容无差异</div>') +
          (f.truncated ? '<div class="muted">（差异过长，已截断）</div>' : '') + '</div>';
      }).join('');
  } catch (e) {
    $('#modalBody').innerHTML = `<div class="muted">加载失败：${esc(e.message)}</div>`;
  }
}
async function undoTurn(turn) {
  if (!state.cur || !turn) return toast('缺少轮次信息', true);
  confirmDialog('撤销本轮', '将把这一轮写入/修改的文件还原到执行前，无法恢复本轮改动。确定撤销？', async () => {
    try {
      const r = await fetch('/api/undo', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: state.cur, turn }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '撤销失败');
      toast(`↩ 已撤销：还原 ${d.restored || 0} 个，删除 ${d.removed || 0} 个`);
      await selectSession(state.cur);
    } catch (e) {
      toast('撤销失败：' + e.message, true);
    }
  }, '撤销');
}
document.addEventListener('click', (e) => {
  const df = e.target.closest('[data-diff]');
  if (df) {
    const card = df.closest('.change-card');
    if (card) showTurnDiff(card.dataset.turn);
    return;
  }
  const un = e.target.closest('[data-undo]');
  if (un) {
    const card = un.closest('.change-card');
    if (card) undoTurn(card.dataset.turn);
  }
});

function showConfirm(cmd) {
  const body = $('#modalBody');
  $('#modalTitle').textContent = '需要你确认';
  body.innerHTML = `<p>Agent 请求执行：</p>
    <div class="danger">${esc(cmd)}</div>
    <div class="btns">
      <button class="btn" id="cfNo">拒绝</button>
      <button class="btn primary" id="cfYes">允许执行</button>
    </div>`;
  openModal();
  body.querySelector('#cfYes').onclick = async () => {
    state.pendingConfirm = null;  // 先清：closeModal 才不会再发一遍拒绝
    closeModal();
    await fetch('/api/confirm', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ approved: true }) });
  };
  body.querySelector('#cfNo').onclick = async () => {
    state.pendingConfirm = null;  // 先清：closeModal 才不会再发一遍拒绝
    closeModal();
    await fetch('/api/confirm', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ approved: false }) });
  };
}

function setBusyUI(b) {
  // 发送按钮永远是发送（忙碌时可再发一条进排队）；
  // 独立的「⏹ 停止」按钮在 busy/排队时出现，点击立即终止（含排队中的任务）。
  $('#btnSend').disabled = false;
  $('#input').disabled = false;
  $('#btnSend').onclick = () => send();
  $('#btnStop').classList.toggle('hidden', !b);
}
async function stopRun() {
  // 1. 标记手动停止（AbortError 分支不再重复弹 toast）
  stopFlag = true;
  setTimeout(() => { stopFlag = false; }, 1200);
  // 2. 中止所有在跑/排队中的请求（含排队取消）
  abortCtrls.forEach((c) => c.abort());
  // 3. 通知后端终止 agent（stop_event + abort LLM socket，立即生效）
  try { await fetch('/api/stop', { method: 'POST' }); } catch (e) {}
  // 4. 立即清理排队条并提示（UI 复位由各请求 finally 同步 pendingCount）
  $('#queueBar').classList.add('hidden');
  toast('⏹️ 已停止');
}

/* ── 输入区 ───────────────────────────────────────────────── */
const inp = $('#input');

/* ── 输入框斜杠命令（/记忆 /文件 /设置 …）──────────────── */
const SLASH = [
  { cmd: '/记忆', label: '打开记忆库', icon: 'db', run: () => $('#btnMemory').click() },
  { cmd: '/搜索', label: '搜索全部会话内容', icon: 'search', run: () => openSearchModal('') },
  { cmd: '/新会话', label: '新建会话', icon: '＋', run: () => $('#btnNew').click() },
  { cmd: '/上传', label: '上传文件到 inputs', icon: 'paperclip', run: () => $('#btnUpload').click() },
  { cmd: '/导出', label: '导出当前会话', icon: 'upload', run: () => $('#btnExport').click() },
  { cmd: '/导出网页', label: '导出为网页 HTML（双击可看）', icon: 'globe', run: () => exportHtml() },
  { cmd: '/只读', label: '切换只读模式', icon: 'shield', run: () => { const c = $('#askMode'); c.checked = !c.checked; toast(c.checked ? '🛡 已开启只读模式（AI 不能写文件/执行命令）' : '只读模式已关闭'); } },
  { cmd: '/停止', label: '停止当前任务', icon: 'stop', run: () => { if (state.busy) stopRun(); else toast('当前没有运行中的任务'); } },
  { cmd: '/批量', label: '批量作业（一个目录一次跑完）', icon: 'box', run: () => openBatch() },
];
let slashIdx = 0, slashItems = [];
// 常用模板：/模板名 一键填入输入框（设置页维护，后端 .agent/templates.json 存储）
let slashTpls = [];
function slashFillTemplates() {
  fetch('/api/templates').then((r) => r.json()).then((d) => {
    slashTpls = (d.templates || []).filter((t) => t.name && t.content);
    if (inp.value.startsWith('/')) slashUpdate();
  }).catch(() => {});
}
function slashUpdate() {
  const menu = $('#slashMenu');
  const v = inp.value;
  if (!v.startsWith('/')) { menu.classList.add('hidden'); return; }
  const q = v.slice(1).toLowerCase();
  const tplItems = slashTpls.filter((t) => t.name.toLowerCase().startsWith(q))
    .map((t) => ({ cmd: '/' + t.name, label: '填入模板（' + t.name + '）', icon: 'note', tpl: t,
      run: () => { inp.value = t.content; autoGrow(inp); $('#input').focus(); } }));
  slashItems = SLASH.filter((s) => s.cmd.slice(1).toLowerCase().startsWith(q)).concat(tplItems);
  slashIdx = Math.min(slashIdx, Math.max(0, slashItems.length - 1));
  if (!slashItems.length) { menu.classList.add('hidden'); return; }
  menu.innerHTML = slashItems.map((s, i) =>
    `<div class="sl-item${i === slashIdx ? ' active' : ''}" data-i="${i}">` +
    `<span class="sl-ic">${ico(s.icon)}</span><span class="sl-cmd">${esc(s.cmd)}</span>` +
    `<span class="sl-label">${esc(s.label)}</span></div>`).join('');
  menu.classList.remove('hidden');
}
function slashRun(i) {
  const s = slashItems[i];
  if (!s) return;
  $('#slashMenu').classList.add('hidden');
  if (s.tpl) { s.run(); return; }  // 模板：填入内容，不清空输入框
  inp.value = '';
  autoGrow(inp);
  s.run();
}
inp.addEventListener('input', () => { autoGrow(inp); slashUpdate(); if (!inp.value.trim()) state.editIdx = null; });
function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}
inp.addEventListener('keydown', (e) => {
  if (isComposingKey(e)) return;   // 输入法上屏中：不发送、不触发 ↑ 编辑等快捷键
  const menuOpen = !$('#slashMenu').classList.contains('hidden');
  if (menuOpen) {
    if (e.key === 'ArrowDown') { e.preventDefault(); slashIdx = (slashIdx + 1) % slashItems.length; slashUpdate(); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); slashIdx = (slashIdx - 1 + slashItems.length) % slashItems.length; slashUpdate(); return; }
    if (e.key === 'Enter') { e.preventDefault(); slashRun(slashIdx); return; }
    if (e.key === 'Escape') { e.preventDefault(); $('#slashMenu').classList.add('hidden'); return; }
  }
  // busy 时 Enter 也允许发送：进入服务端排队（显示排队条），无需干等当前任务
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
    return;
  }
  // ↑：输入框为空时载入上一条用户消息编辑重发
  if (e.key === 'ArrowUp' && !inp.value && !state.busy) {
    const prev = [...document.querySelectorAll('#messages .msg.user')].pop();
    if (prev && state.cur) { e.preventDefault(); editResend(prev); }
  }
});
$('#slashMenu').addEventListener('mousedown', (e) => {
  const item = e.target.closest('.sl-item');
  if (item) { e.preventDefault(); slashRun(+item.dataset.i); }
});
$('#btnSend').onclick = () => send();
$('#btnStop').onclick = () => stopRun();

/* ── 附件工具 ─────────────────────────────────────────────── */
function typeIcon(name) {
  const ext = String(name || '').split('.').pop().toLowerCase();
  return fileIcon(ext);
}
function fmtSize(n) {
  if (n == null || isNaN(n)) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}
function toAttObj(a) {
  if (typeof a === 'string') return { path: a, name: a.split('/').pop(), size: null };
  return { path: (a && a.path) || '', name: (a && a.name) || String((a && a.path) || '').split('/').pop(), size: (a && a.size != null) ? a.size : null };
}

/* ── 上传（按钮 + 拖拽）─────────────────────────────────── */
async function uploadFiles(files) {
  if (!files || !files.length) return;
  $('#uploadHint').textContent = `上传中 ${files.length} 个文件…`;
  for (const f of files) {
    try {
      const r = await fetch('/api/upload?filename=' + encodeURIComponent(f.name), {
        method: 'POST', body: f,
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error);
      // 附件进入待发送条：输入消息前一直保留，随消息发给 AI，可手动移除
      if (!state.attachments.some((a) => a.path === d.path)) {
        state.attachments.push({ name: d.name, path: d.path, size: d.size });
        renderAttachStrip();
      }
      $('#uploadHint').textContent = `✅ 已上传 ${f.name} → ${d.path}`;
    } catch (err) {
      $('#uploadHint').textContent = '';
      toast('上传失败：' + err.message, true);
    }
  }
  setTimeout(() => ($('#uploadHint').textContent = ''), 4000);
}
$('#btnUpload').onclick = () => $('#fileInput').click();
$('#fileInput').onchange = async (e) => {
  uploadFiles([...e.target.files]);
  e.target.value = '';
};
// 粘贴即上传：Ctrl+V 粘贴的图片/文件直接进附件条（离线环境截图场景高频）
inp.addEventListener('paste', (e) => {
  const files = [...((e.clipboardData || {}).files || [])];
  if (files.length) {
    e.preventDefault();
    uploadFiles(files);
  }
});
// 拖拽上传：拖到输入区高亮，松手上传
(() => {
  const zone = $('#inputBar');
  let depth = 0;
  ['dragenter', 'dragover'].forEach((ev) => zone.addEventListener(ev, (e) => {
    e.preventDefault();
    depth++;
    zone.classList.add('drag-over');
  }));
  zone.addEventListener('dragleave', (e) => {
    e.preventDefault();
    depth = Math.max(0, depth - 1);
    if (!depth) zone.classList.remove('drag-over');
  });
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    depth = 0;
    zone.classList.remove('drag-over');
    uploadFiles([...(e.dataTransfer || {}).files || []]);
  });
})();

/* ── 附件条（上传后保留，输入消息不消失，随消息发送）───────────── */
function renderAttachStrip() {
  const strip = $('#attachStrip');
  if (!strip) return;
  const list = state.attachments || [];
  strip.classList.toggle('hidden', !list.length);
  strip.innerHTML = '';
  for (const a of list) {
    const chip = document.createElement('span');
    chip.className = 'attach-chip';
    chip.title = a.path;
    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.innerHTML = typeIcon(a.name) + ' ' + esc(a.name) +
      (a.size != null ? `（${esc(fmtSize(a.size))}）` : '');
    const x = document.createElement('button');
    x.className = 'x';
    x.title = '移除附件';
    x.textContent = '✕';
    x.onclick = () => {
      state.attachments = state.attachments.filter((b) => b.path !== a.path);
      renderAttachStrip();
    };
    chip.appendChild(nm);
    chip.appendChild(x);
    strip.appendChild(chip);
  }
}
renderAttachStrip();

/* ── 产出物面板（右侧）────────────────────────────────────── */
function renderArtifacts(arts, show) {
  // 只要有产物就展开面板（刷新/切换会话后不再"变空"——旧版只在流式 result 帧才展开）
  if (arts && arts.length) {
    $('#artifactPanel').classList.remove('hidden');
    state.artShown = true;
  }
  const list = $('#artifactList');
  if (!list) return;
  list.innerHTML = '';
  for (const a of arts) {
    const el = document.createElement('div');
    el.className = 'art-card';
    const miss = a.missing ? ' <span class="art-missing">（文件已移动）</span>' : '';
    el.innerHTML = `<span class="kind">${artIcon(a.ext)}</span>
      <span class="nm" title="${esc(a.rel)}">${esc(a.name)}</span>
      <span class="sz">${fmtSize(a.size)}</span>
      ${state.isLoopback ? `<button class="art-open" title="在文件管理器中打开所在文件夹">${ico('folder')} 目录</button>` : ''}${miss}`;
    el.onclick = () => openPreview(a.rel, a.name);
    const openBtn = el.querySelector('.art-open');
    if (openBtn) openBtn.onclick = (e) => {
      e.stopPropagation();
      openArtifactFolder(a.rel);
    };
    list.appendChild(el);
  }
  syncRightPanel();
}
// 「📂 目录」：在系统文件管理器中打开产物所在文件夹并选中该文件（参考老版 agent）
async function openArtifactFolder(rel) {
  try {
    const r = await fetch('/api/open-folder?path=' + encodeURIComponent(rel));
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '无法打开');
    toast('📂 已打开所在文件夹');
  } catch (e) {
    toast('打开文件夹失败：' + e.message, true);
  }
}
$('#btnHideArts').onclick = () => {
  $('#artifactPanel').classList.add('hidden');
  state.artShown = false;
};
// 打包本会话全部产出物为 zip 下载
$('#btnZipArts').onclick = () => {
  if (!state.cur) return toast('请先选择会话', true);
  const a = document.createElement('a');
  a.href = '/api/export_artifacts?id=' + encodeURIComponent(state.cur);
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
};
// 取消排队：断开 SSE 连接（服务端死连接检测会自动退出排队）
$('#btnCancelQueue').onclick = () => {
  // 只取消排队中的请求（最后加入的），不影响正在跑的任务
  const c = abortCtrls[abortCtrls.length - 1];
  if (c) c.abort();
  $('#queueBar').classList.add('hidden');
  toast('已取消排队');
};
async function refreshSessionMeta() {
  if (!state.cur) return;
  const sid = state.cur;
  const r = await fetch('/api/session?id=' + encodeURIComponent(sid));
  const d = await r.json();
  if (state.cur !== sid) return;  // 期间已切会话，不写旧会话的标题
  if (d.session) {
    $('#chatTitle').textContent = d.session.title || '新会话';
    $('#chatMeta').textContent =
      `${d.session.messages.length} 条消息 · ${(d.session.artifacts || []).length} 个产出物`;
    if (!state.artShown) renderArtifacts(d.session.artifacts || []);
  }
}

/* ── 弹窗 ─────────────────────────────────────────────────── */
/* 读屏播报：只播报结论（回复完成/出错），不播报逐字流式内容 */
function a11ySay(text) {
  const el = $('#a11yLive');
  if (el) el.textContent = String(text || '');
}
let modalReturnFocus = null;
function modalFocusables() {
  const box = $('#modal');
  if (!box) return [];
  return [...box.querySelectorAll('a[href], button:not([disabled]), ' +
    'input:not([disabled]):not([type=hidden]), select:not([disabled]), ' +
    'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
    .filter((el) => el.offsetParent !== null);
}
function openModal() {
  modalReturnFocus = document.activeElement;
  $('#modal').classList.remove('hidden');
  $('#modalMask').classList.remove('hidden');
  $('#modal').classList.remove('wide', 'pv');
  // 焦点进弹窗：优先第一个输入框，否则第一个可点元素（键盘/读屏用户不用摸黑 Tab）
  setTimeout(() => {
    const body = $('#modalBody');
    const first = (body && body.querySelector('input:not([type=hidden]), textarea, select'))
      || modalFocusables()[0] || $('#modalClose');
    try { if (first) first.focus(); } catch (e) {}
  }, 30);
  const t = $('#modalTitle');
  a11ySay(t && t.textContent ? t.textContent : '对话框已打开');
}
function closeModal() {
  $('#modal').classList.add('hidden');
  $('#modal').classList.remove('wide');
  $('#modalMask').classList.add('hidden');
  closeBatchPoll();   // 批量作业面板关掉后停止轮询
  // 未答复的确认（Esc/点遮罩/点 X 关弹窗）：按拒绝处理并通知后端，
  // 否则 agent 在服务端空等满 180 秒。Yes/No 按钮已先清 pendingConfirm，
  // 不会重复发。
  if (state.pendingConfirm) {
    state.pendingConfirm = null;
    fetch('/api/confirm', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved: false }) }).catch(() => {});
    toast('已拒绝未确认的执行请求', true);
  }
  if (modalReturnFocus && modalReturnFocus.focus) {
    try { modalReturnFocus.focus(); } catch (e) {}
  }
  modalReturnFocus = null;
}
// Tab 焦点陷阱：弹窗打开时焦点只在弹窗内循环（不再跑到背后的页面上）
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Tab' || $('#modal').classList.contains('hidden')) return;
  const items = modalFocusables();
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1], cur = document.activeElement;
  const inside = $('#modal').contains(cur);
  if (e.shiftKey && (!inside || cur === first)) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && (!inside || cur === last)) { e.preventDefault(); first.focus(); }
});
$('#modalClose').onclick = closeModal;
$('#modalMask').onclick = closeModal;

