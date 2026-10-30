'use strict';

/* ── 会话列表 ─────────────────────────────────────────────── */
async function loadSessions() {
  const r = await fetch('/api/sessions');
  const d = await r.json();
  state.sessions = d.sessions || [];
  renderSessions();
}
let sbFilter = '';
function dayLabel(iso) {
  if (!iso) return '更早';
  const d = new Date(iso);
  if (isNaN(d)) return '更早';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((today - day) / 86400000);
  if (diff <= 0) return '今天';
  if (diff === 1) return '昨天';
  const now = new Date();
  const md = `${d.getMonth() + 1}月${d.getDate()}日`;
  return d.getFullYear() === now.getFullYear() ? md : `${d.getFullYear()}年${md}`;
}
function dayLabelTs(epoch) {
  if (!epoch) return '更早';
  return dayLabel(new Date(epoch * 1000).toISOString());
}

function tsLabel(epoch) {
  if (!epoch) return '';
  const d = new Date(epoch * 1000);
  if (isNaN(d)) return '';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
function lastEpoch(s) {
  if (s.last_ts) return s.last_ts;
  if (s.created_at) {
    const t = new Date(s.created_at).getTime() / 1000;
    return isNaN(t) ? 0 : t;
  }
  return 0;
}
function renderSessions() {
  const list = $('#sessionList');
  list.innerHTML = '';
  const kw = sbFilter.trim().toLowerCase();
  const items = (kw ? state.sessions.filter((s) => (s.title || '').toLowerCase().includes(kw))
                    : state.sessions.slice())
    // 置顶会话始终排最前，其余按最后活跃时间降序
    .sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) || (lastEpoch(b) - lastEpoch(a)));
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'sb-empty';
    if (kw) {
      empty.innerHTML = '没有匹配的会话<br><a class="sb-content-link">在全部会话内容中搜索 →</a>';
      empty.querySelector('.sb-content-link').onclick = () => openSearchModal(sbFilter);
    } else {
      empty.textContent = '暂无会话，点上方「＋ 新会话」开始';
    }
    list.appendChild(empty);
    return;
  }
  let lastLabel = '';
  for (const s of items) {
    const label = dayLabelTs(lastEpoch(s));
    if (label !== lastLabel) {
      const g = document.createElement('div');
      g.className = 'sb-group';
      g.textContent = label;
      list.appendChild(g);
      lastLabel = label;
    }
    const el = document.createElement('div');
    el.className = 'sess' + (s.id === state.cur ? ' active' : '') + (s.pinned ? ' pinned' : '');
    // 最后活跃时间：按最近活跃排序 + 展示，替代创建时间
    const timeTag = lastEpoch(s) ? `🕐 ${tsLabel(lastEpoch(s))} · ` : '';
    const meta = timeTag + `${s.msg_count || 0} 条消息` + (s.artifact_count ? ` · ${s.artifact_count} 个产出` : '');
    // 失败工具角标：红色胶囊，会话有问题时一眼可见；同时加 ⚠ 徽标在标题后
    const failBadge = s.fail_count
      ? ` · <span class="fail-badge" title="${s.fail_count} 次工具调用失败">${s.fail_count} 失败</span>` : '';
    const warnMark = s.fail_count ? '<span class="warn-mark" title="有工具调用失败">⚠</span>' : '';
    const pinMark = s.pinned ? `<span class="pin-mark" title="已置顶">${ico('pin')}</span>` : '';
    const preview = s.preview ? `<span class="t-prev">${esc(s.preview)}</span>` : '';
    el.innerHTML = `<span class="t" title="${esc(s.title)}">
        <span class="t-main">${pinMark}${warnMark}${esc(s.title)}</span>
        <span class="t-meta">${esc(meta)}${failBadge}</span>
        ${preview}
      </span>
      <span class="ops">
        <button class="x pin${s.pinned ? ' on' : ''}" data-act="pin" title="${s.pinned ? '取消置顶' : '置顶会话'}">${ico('pin')}</button>
        <button class="x" data-act="ren" title="重命名">${ico('edit')}</button>
        <button class="x" data-act="del" title="删除">✕</button>
      </span>`;
    el.querySelector('.t').onclick = () => selectSession(s.id);
    el.querySelector('.x[data-act="pin"]').onclick = (e) => {
      e.stopPropagation();
      togglePin(s.id, !s.pinned);
    };
    el.querySelector('.x[data-act="ren"]').onclick = (e) => {
      e.stopPropagation();
      renameSession(s.id, s.title);
    };
    el.querySelector('.x[data-act="del"]').onclick = (e) => {
      e.stopPropagation();
      confirmDialog('删除会话', `确定删除「${s.title}」吗？删除后不可恢复。`, () => delSession(s.id));
    };
    list.appendChild(el);
  }
}
/* ── 侧栏搜索（实时过滤 + 全文搜索入口）──────────────────── */
const sbSearchEl = $('#sbSearch');
function sbRenderLabel() {
  const kw = sbFilter.trim().toLowerCase();
  const cnt = kw ? state.sessions.filter((s) => (s.title || '').toLowerCase().includes(kw)).length : 0;
  $('#sbLabel').textContent = kw ? `搜索结果（${cnt}）` : '最近会话';
}
sbSearchEl.addEventListener('input', () => {
  sbFilter = sbSearchEl.value;
  $('#sbSearchClear').classList.toggle('hidden', !sbFilter);
  sbRenderLabel();
  renderSessions();
});
sbSearchEl.addEventListener('keydown', (e) => {
  if (isComposingKey(e)) return;
  if (e.key === 'Enter') openSearchModal(sbSearchEl.value.trim());
});
$('#sbSearchClear').onclick = () => {
  sbSearchEl.value = '';
  sbFilter = '';
  $('#sbSearchClear').classList.add('hidden');
  sbRenderLabel();
  renderSessions();
};

$('#btnNew').onclick = async () => {
  if (state.busy) return toast('任务进行中，请先停止', true);
  const r = await fetch('/api/session/new', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  const d = await r.json();
  await loadSessions();
  await selectSession(d.session.id, true);
};
async function selectSession(id, isNew) {
  state.cur = id;
  const gen = ++state.sessionGen;  // 快速连点两个会话时，旧请求的回包不许覆盖新会话
  try { localStorage.setItem('lastSession', id); } catch (e) {}
  if (state.selectMode) exitSelectMode();
  showWelcome(false);
  closeCtxMenu();
  showSkeleton();          // 骨架屏：避免“点了没反应”
  renderModelSel();        // 会话级临时模型：恢复该会话的选择
  // 切换会话后清空待发送附件条（已发送的附件仍保留在对话记录里）
  state.attachments = [];
  renderAttachStrip();
  renderSessions();
  let d;
  try {
    const r = await fetch('/api/session?id=' + encodeURIComponent(id));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    d = await r.json();
  } catch (e) {
    if (gen !== state.sessionGen) return;  // 已被更新的请求取代，静默丢弃
    // 加载失败也要清掉骨架屏，否则“点了没反应”的占位图一直挂着
    $('#messages').innerHTML = '<div class="muted" style="padding:16px">会话加载失败：' + esc(e.message) + '，点左侧会话重试。</div>';
    toast('会话加载失败：' + e.message, true);
    return;
  }
  if (gen !== state.sessionGen) return;  // 旧回包不渲染
  const s = d.session || {};
  $('#chatTitle').textContent = s.title || '新会话';
  $('#chatMeta').textContent =
    `${s.messages ? s.messages.length : 0} 条消息 · ${(s.artifacts || []).length} 个产出物`;
  state.compacted = s.compact || null;
  state.msgCount = (s.messages || []).length;
  refreshCompactBtn();
  renderHistory(s.messages || []);
  renderArtifacts(s.artifacts || []);
  if (isNew) $('#input').focus();
}
function showWelcome(show) {
  const w = $('#welcome');
  if (w) w.classList.toggle('hidden', !show);
  if (show) { renderWelcomeStats(); renderWelcomeChips(); }
}
/* 欢迎页建议动态化：用真实会话/模板/最近文件生成，而不是固定三句 */
let wChipActions = [];
async function renderWelcomeChips() {
  const box = $('#wChips');
  if (!box) return;
  const fill = (q) => { const i = $('#input'); i.value = q; autoGrow(i); i.focus(); };
  const acts = [];
  // 1) 继续最近的会话
  (state.sessions || []).slice()
    .sort((a, b) => lastEpoch(b) - lastEpoch(a))
    .filter((s) => (s.msg_count || 0) > 0).slice(0, 2)
    .forEach((s) => acts.push({ icon: 'play', label: `继续：${s.title}`,
                                run: () => selectSession(s.id) }));
  // 2) 第一个 prompt 模板（设置页维护）
  if (slashTpls && slashTpls.length) {
    const t = slashTpls[0];
    acts.push({ icon: 'note', label: `用模板：${t.name}`, run: () => fill(t.content) });
  }
  // 3) inputs 里最新的文件（刚上传的东西最可能想处理）
  try {
    const r = await fetch('/api/tree?root=ws&path=inputs');
    const d = await r.json();
    const files = (d.items || []).filter((x) => x.type === 'file')
      .sort((a, b) => (b.mtime || 0) - (a.mtime || 0)).slice(0, 1);
    files.forEach((f) => acts.push({ icon: 'file',
      label: `预览刚上传的 ${f.name}`,
      run: () => openPreview('inputs/' + f.name, f.name) }));
  } catch (e) { /* 静态兑底已够 */ }
  // 4) 兑底能力提示
  acts.push({ icon: 'chart', label: '分析 inputs 里的数据',
              run: () => fill('分析 inputs 目录下的数据并汇报') });
  acts.push({ icon: 'table', label: '起草一份项目汇报的 PPT 大纲',
              run: () => fill('起草一份项目汇报的 PPT 大纲') });
  wChipActions = acts.slice(0, 5);
  box.innerHTML = wChipActions.map((a, i) =>
    `<button class="w-chip" data-i="${i}">${ico(a.icon)} ${esc(a.label)}</button>`).join('');
}
async function renderWelcomeStats() {
  const box = $('#wStats');
  if (!box) return;
  const sessions = state.sessions || [];
  let memCount = 0;
  try {
    const r = await fetch('/api/memory');
    const d = await r.json();
    memCount = (d.facts || []).length;
  } catch (e) {}
  const arts = sessions.reduce((s, x) => s + (x.artifact_count || 0), 0);
  box.innerHTML =
    `<span class="ws-item">${ico('chat')} <b>${sessions.length}</b> 个会话</span>` +
    `<span class="ws-item">${ico('db')} <b>${memCount}</b> 条记忆</span>` +
    `<span class="ws-item">${ico('box')} <b>${arts}</b> 个产出物</span>`;
}
async function renameSession(id, current) {
  const title = await promptDialog('重命名会话', current || '');
  const t = (title || '').trim();
  if (!t || t === (current || '')) return;
  await fetch('/api/session/rename', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, title: t }),
  });
  if (id === state.cur) $('#chatTitle').textContent = t;
  await loadSessions();
}
function promptDialog(title, value) {
  return new Promise((resolve) => {
    $('#modalTitle').textContent = title;
    $('#modalBody').innerHTML = `
      <input type="text" id="pdInput" value="${esc(value)}" placeholder="输入新名称">
      <div class="btns">
        <button class="btn" id="pdCancel">取消</button>
        <button class="btn primary" id="pdOk">确定</button>
      </div>`;
    openModal();
    const inp = $('#pdInput');
    inp.focus();
    inp.select();
    const done = (v) => { closeModal(); resolve(v); };
    $('#pdOk').onclick = () => done(inp.value);
    $('#pdCancel').onclick = () => done(null);
    inp.onkeydown = (e) => {
      if (isComposingKey(e)) return;
      if (e.key === 'Enter') done(inp.value);
      else if (e.key === 'Escape') done(null);
    };
  });
}
function confirmDialog(title, text, onOk, okLabel) {
  $('#modalTitle').textContent = title;
  $('#modalBody').innerHTML = `
    <p class="cd-text">${esc(text)}</p>
    <div class="btns">
      <button class="btn" id="cdNo">取消</button>
      <button class="btn primary danger" id="cdYes">${esc(okLabel || '删除')}</button>
    </div>`;
  openModal();
  $('#cdYes').onclick = () => { closeModal(); onOk(); };
  $('#cdNo').onclick = () => closeModal();
}
async function togglePin(id, pinned) {
  try {
    const r = await fetch('/api/session/pin', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, pinned }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error);
    toast(pinned ? '📌 已置顶' : '已取消置顶');
    await loadSessions();
  } catch (e) {
    toast('置顶失败：' + e.message, true);
  }
}
async function delSession(id) {
  await fetch('/api/session/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id }),
  });
  if (state.cur === id) {
    state.cur = null;
    $('#messages').innerHTML = '';
    $('#chatTitle').textContent = '新会话';
    $('#chatMeta').textContent = '';
    showWelcome(true);
  }
  await loadSessions();
}

/* ── 流式跟随滚动 + 回到底部 ───────────────────────────────── */
function isNearBottom(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight < 140;
}
function setJump(v) {
  const b = $('#jumpBottom');
  if (b) b.classList.toggle('hidden', !v);
}
function followScroll(box) {
  if (!box) return;
  if (state.followBottom) {
    box.scrollTop = box.scrollHeight;
    setJump(false);
  } else if (!isNearBottom(box)) {
    setJump(true);
  }
}
$('#messages').addEventListener('scroll', () => {
  const box = $('#messages');
  state.followBottom = isNearBottom(box);
  setJump(!state.followBottom && box.scrollHeight > box.clientHeight + 200);
});
$('#jumpBottom').onclick = () => {
  const box = $('#messages');
  state.followBottom = true;
  box.scrollTop = box.scrollHeight;
  setJump(false);
};

