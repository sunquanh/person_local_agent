'use strict';

/* ── 右侧工具调用面板：只显示最新一轮的工具调用链路 ──────────── */
function renderSessionTools(messages) {
  const panel = $('#toolPanel');
  if (!panel) return;
  const details = $('#sessionTools');
  const list = details.querySelector('.msg-tools');
  // 只取最后一条 assistant 消息的 tool_steps（本轮链路），不累积所有轮次
  let steps = [];
  for (let i = (messages || []).length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role === 'assistant') { steps = m.tool_steps || []; break; }
  }
  if (!steps.length) {
    panel.classList.add('hidden');
    list.innerHTML = '';
    return;
  }
  panel.classList.remove('hidden');
  const fails = steps.filter((s) => !s.ok).length;
  details.classList.toggle('has-err', fails > 0);
  details.querySelector('summary').innerHTML =
    `${ico('tool')} 本轮工具调用（${steps.length}）` + (fails ? ` <span class="tl-err">${fails} 失败</span>` : '');
  list.innerHTML = steps.map((s) => {
    const cls = s.ok ? 'ok' : 'err';
    let one = '';
    if (s.result) {
      const r1 = String(s.result).replace(/\s+/g, ' ').trim();
      one = r1 ? ` <span class="tl-res" title="${esc(String(s.result))}">${esc(r1.slice(0, 60))}${r1.length > 60 ? '…' : ''}</span>` : '';
    }
    return `<div class="tool-line ${cls}">${s.ok ? '✅' : '❌'} ${toolNameHtml(s.tool)}` +
      (s.ms ? ` <span class="tl-ms">${esc(fmtMs(s.ms))}</span>` : '') + `${one}` +
      (s.result ? ` <button class="tl-copy" data-copy="${esc(String(s.result))}" title="复制工具结果">${ico('copy')}</button>` : '') +
      `</div>`;
  }).join('');
}
// 新一轮开始：清空右侧面板，显示等待占位（工具调用将实时追加）
function resetSessionTools() {
  const panel = $('#toolPanel');
  const details = $('#sessionTools');
  if (!panel || !details) return;
  panel.classList.remove('hidden');
  details.classList.remove('has-err');
  details.querySelector('summary').innerHTML = `${ico('tool')} 本轮工具调用（0）`;
  const list = details.querySelector('.msg-tools');
  list.innerHTML = '<div class="tool-wait">思考中…（本轮工具调用将在这里实时显示）</div>';
  syncRightPanel();
}
function appendSessionTool(name, ok, result, ms) {
  const panel = $('#toolPanel');
  const details = $('#sessionTools');
  if (!panel || !details) return;
  const list = details.querySelector('.msg-tools');
  panel.classList.remove('hidden');
  // 清掉等待占位（第一条工具到达时）
  if (list.querySelector('.tool-wait')) list.innerHTML = '';
  const fails = list.querySelectorAll('.tool-line.err').length + (ok ? 0 : 1);
  details.classList.toggle('has-err', fails > 0);
  details.querySelector('summary').innerHTML =
    `${ico('tool')} 本轮工具调用（${list.children.length + 1}）` + (fails ? ` <span class="tl-err">${fails} 失败</span>` : '');
  const line = document.createElement('div');
  line.className = 'tool-line ' + (ok ? 'ok' : 'err');
  line.innerHTML = `${ok ? '✅' : '❌'} ${toolNameHtml(name)}` +
    (ms ? ` <span class="tl-ms">${esc(fmtMs(ms))}</span>` : '');
  if (result) {
    const r1 = String(result).replace(/\s+/g, ' ').trim();
    if (r1) {
      line.title = result;
      line.innerHTML += ` <span class="tl-res">${esc(r1.slice(0, 60))}${r1.length > 60 ? '…' : ''}</span>` +
        ` <button class="tl-copy" data-copy="${esc(String(result))}" title="复制工具结果">${ico('copy')}</button>`;
    }
  }
  list.appendChild(line);
}
function msgUser(text, ts, attachments) {
  const el = document.createElement('div');
  el.className = 'msg user';
  let chips = '';
  if (attachments && attachments.length) {
    chips = '<div class="msg-att">' + attachments.map((a) => {
      const o = toAttObj(a);
      const href = '/api/file?path=' + encodeURIComponent(o.path);
      return `<span class="att-item" title="${esc(o.path)}">${typeIcon(o.name)} ` +
        `<a href="${href}" target="_blank">${esc(o.name)}</a>` +
        (o.size != null ? `<span class="att-size">${fmtSize(o.size)}</span>` : '') +
        `<button class="att-del" data-path="${esc(o.path)}" title="删除该附件（移除引用并删除文件）">✕</button></span>`;
    }).join('') + '</div>';
  }
  el.innerHTML = `<div class="u-txt">${esc(text)}</div>` + chips +
    (ts ? `<div class="u-ts">${fmtTime(ts)}</div>` : '') +
    `<div class="msg-acts">` +
    `<button class="act-edit" title="编辑这条并重新发送（会删除该条及之后的对话）">${ico('edit')} 编辑重发</button>` +
    `<button class="act-more" title="更多操作" aria-haspopup="true" aria-expanded="false">⋯</button>` +
    `<div class="act-menu hidden" role="menu">` +
      `<button class="act-fork" title="从这条分叉出新会话（原会话不动）">${ico('fork')} 分支</button>` +
      `<button class="act-one" title="导出这一条为 Markdown">${ico('download')} 导出这条</button>` +
      `<button class="act-del danger" title="删除这条及其后的对话（二次确认）">${ico('trash')} 删除这条及之后</button>` +
    `</div></div>`;
  if (ts) {
    el.querySelectorAll('.att-del').forEach((b) => {
      b.onclick = (e) => { e.stopPropagation(); delAttachment(ts, b.dataset.path); };
    });
  }
  return el;
}
async function delAttachment(ts, path) {
  if (!state.cur || !ts || !path) return;
  try {
    const r = await fetch('/api/attachment/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.cur, ts, path }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error);
    // 文件已删除，同步清掉待发送条里同路径的附件
    state.attachments = state.attachments.filter((a) => a.path !== path);
    renderAttachStrip();
    toast('已删除附件：' + path.split('/').pop());
    const s = await fetch('/api/session?id=' + encodeURIComponent(state.cur));
    const sd = await s.json();
    renderHistory((sd.session || {}).messages || []);
  } catch (err) {
    toast('删除附件失败：' + err.message, true);
  }
}
function msgBot(text, toolSteps, artifacts, thinking, meta, opts) {
  opts = opts || {};
  const el = document.createElement('div');
  el.className = 'msg bot';
  let html = '';
  // 兼容旧数据：存储内容里可能还残留 <think> 标签（后端旧版没拆），这里兜底拆到思考块
  const st = extractThinking(text || '');
  const thinkText = (thinking || st.thinking || '').trim();
  const cleanText = st.content || text || '';
  if (thinkText) {
    html += `<details class="think-block"><summary>${ico('think')} 思考过程（${thinkText.length} 字）</summary>` +
      `<button class="think-copy" title="复制思考内容">${ico('copy')}</button><pre>${esc(thinkText)}</pre></details>`;
  }
  html += `<div class="content md">${md.render(cleanText)}</div>`;
  // 工具时间线（历史数据直接渲染；实时帧到达时再追加）
  html += `<details class="timeline hidden"><summary>${ico('tool')} 工具调用（0 步）</summary><div class="tl-body"></div></details>`;
  html += citationsHtml(opts.citations);
  html += todoMiniHtml(opts.todos);
  html += changeCardHtml(opts.snapshot);
  html += `<div class="msg-acts">` +
    `<button class="act-copy" title="复制回答">${ico('copy')} 复制</button>` +
    `<button class="act-more" title="更多操作" aria-haspopup="true" aria-expanded="false">⋯</button>` +
    `<div class="act-menu hidden" role="menu">` +
      `<button class="act-note" title="将回答存为笔记">${ico('note')} 存为笔记</button>` +
      `<button class="act-fork" title="从这条分叉出新会话（原会话不动）">${ico('fork')} 分支</button>` +
      `<button class="act-one" title="导出这一条为 Markdown">${ico('download')} 导出这条</button>` +
      `<button class="act-regen" title="重新生成回答">${ico('retry')} 重新生成</button>` +
      `<button class="act-del danger" title="删除这条及其后的对话（二次确认）">${ico('trash')} 删除这条及之后</button>` +
    `</div></div>`;
  if (artifacts && artifacts.length) {
    html += `<div class="msg-art">${ico('box')} ${artifacts.map((a) =>
      `<a href="/api/file?path=${encodeURIComponent(a.rel)}" data-pv="${esc(a.rel)}" target="_blank">${esc(a.name)}</a>`
    ).join('')}</div>`;
  }
  if (meta) html += `<div class="msg-meta">${esc(meta)}</div>`;
  el.innerHTML = html;
  renderTimeline(el, toolSteps || []);
  return el;
}

/* ── 复制 / 重新生成（事件委托，流式与历史回放共用）────────── */
function copyText(t) {
  if (!t) return;
  const done = () => toast('✅ 已复制');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(done).catch(() => {
      // 非 https/localhost 环境 clipboard 不可用，退回 execCommand
      const ta = document.createElement('textarea');
      ta.value = t;
      ta.style.cssText = 'position:fixed;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); done(); } catch (e) { toast('复制失败', true); }
      ta.remove();
    });
  } else {
    const ta = document.createElement('textarea');
    ta.value = t;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { toast('复制失败', true); }
    ta.remove();
  }
}
async function regenerate() {
  if (!state.cur || state.busy) return toast('任务进行中，请先停止', true);
  const r = await fetch('/api/session?id=' + encodeURIComponent(state.cur));
  const d = await r.json();
  const msgs = (d.session || {}).messages || [];
  const lastUser = [...msgs].reverse().find((m) => m.role === 'user');
  if (!lastUser) return toast('没有可重新生成的消息', true);
  const r2 = await fetch('/api/session/regenerate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: state.cur }),
  });
  if (!r2.ok) return toast('重新生成失败', true);
  // 重新加载会话（旧回复已被后端移除），然后以最后一条用户消息重跑
  await selectSession(state.cur);
  const atts = (lastUser.attachments || []).map((a) =>
    typeof a === 'string' ? { path: a, name: a.split('/').pop(), size: null } : a);
  await send(lastUser.content, atts, { regenerate: true });
}
/* ── 全局快捷键：Esc 停止 / Ctrl+L 聚焦输入 / Ctrl+B、Ctrl+J 收放侧栏 ── */
document.addEventListener('keydown', (e) => {
  const paletteOpen = !$('#palette').classList.contains('hidden');
  if (e.key === 'Escape') {
    if (paletteOpen) return;                      // 命令面板自己处理
    if (!$('#ctxMenu').classList.contains('hidden')) { closeCtxMenu(); return; }
    if (!$('#modal').classList.contains('hidden')) { closeModal(); return; }
    if (state.busy) { e.preventDefault(); stopRun(); }
    return;
  }
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === 'l') {
    e.preventDefault(); $('#input').focus();
  } else if (mod && e.key.toLowerCase() === 'b') {
    e.preventDefault(); document.body.classList.toggle('hide-left');
  } else if (mod && e.key.toLowerCase() === 'j') {
    e.preventDefault(); document.body.classList.toggle('hide-right');
  } else if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
    // Alt+↑/↓：在会话列表里上下切换（按当前排序顺序）
    e.preventDefault();
    switchSession(e.key === 'ArrowDown' ? 1 : -1);
  }
});
function switchSession(dir) {
  const items = (state.sessions || []).slice()
    .sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) || (lastEpoch(b) - lastEpoch(a)));
  if (!items.length) return;
  const i = items.findIndex((s) => s.id === state.cur);
  const nx = items[(i + dir + items.length) % items.length];
  if (nx && nx.id !== state.cur) selectSession(nx.id);
}

/* ── 分栏拖拽把手 + 窄屏抽屉（U7）──────────────────────── */
function initResizer(el, side) {
  if (!el) return;
  const KEY = side === 'left' ? 'sb-w' : 'rp-w';
  const VAR = side === 'left' ? '--sb-w' : '--rp-w';
  const MIN = side === 'left' ? 200 : 250;
  const MAX = side === 'left' ? 420 : 460;
  let saved = 0;
  try { saved = +(localStorage.getItem(KEY) || 0); } catch (e) {}
  if (saved) document.body.style.setProperty(VAR, saved + 'px');
  const target = side === 'left' ? $('#sidebar') : $('#rightPanel');
  el.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = target.getBoundingClientRect().width;
    el.classList.add('on');
    document.body.classList.add('resizing');
    const move = (ev) => {
      const w = side === 'left' ? startW + (ev.clientX - startX) : startW - (ev.clientX - startX);
      const v = Math.max(MIN, Math.min(MAX, w));
      document.body.style.setProperty(VAR, v + 'px');
    };
    const up = () => {
      el.classList.remove('on');
      document.body.classList.remove('resizing');
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      try { localStorage.setItem(KEY, String(Math.round(target.getBoundingClientRect().width))); }
      catch (err) {}
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });
  el.addEventListener('dblclick', () => {
    document.body.style.removeProperty(VAR);
    try { localStorage.removeItem(KEY); } catch (e) {}
    toast('宽度已复位');
  });
}
initResizer($('#rzLeft'), 'left');
initResizer($('#rzRight'), 'right');

/* ── 顶栏动作收纳：<1280px 把次要动作搬进「⋯」菜单 ─────────────
   同一个 DOM 节点搬家，id 绑定与状态都不变；≥1280px 自动搬回平铺，
   窄桌面窗口也能继续访问次要操作。 */
const HEAD_OVERFLOW = ['#modelSel', '#btnBatch', '#btnExport'];
const headMq = window.matchMedia('(max-width: 1280px)');
function closeMoreMenu() {
  const m = $('#moreMenu');
  if (m) m.classList.add('hidden');
  const b = $('#btnMore');
  if (b) b.setAttribute('aria-expanded', 'false');
}
function syncHeadActions() {
  const bar = $('#chActions'), menu = $('#moreMenu');
  if (!bar || !menu) return;
  const narrow = headMq.matches;
  const target = narrow ? menu : bar;
  HEAD_OVERFLOW.forEach((sel) => {
    const el = document.querySelector(sel);
    if (el && el.parentElement !== target) target.appendChild(el);
  });
  if (!narrow) closeMoreMenu();
}
(function initHeadActions() {
  const btn = $('#btnMore');
  if (!btn) return;
  btn.onclick = (e) => {
    e.stopPropagation();
    const m = $('#moreMenu');
    const open = !m.classList.toggle('hidden');
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  const menu = $('#moreMenu');
  menu.addEventListener('click', (e) => {
    if (e.target.closest('button')) closeMoreMenu();   // 点动作后收起（下拉框不收）
  });
  document.addEventListener('click', (e) => {
    if (!$('#moreMenu').classList.contains('hidden')
        && !e.target.closest('.ch-more')) closeMoreMenu();
  });
  if (headMq.addEventListener) headMq.addEventListener('change', syncHeadActions);
  else if (headMq.addListener) headMq.addListener(syncHeadActions);
  syncHeadActions();
})();

$('#btnBatch').onclick = () => openBatch();

/* ── 右栏检查器 Tab：任务 / 工具 / 产物（三块不再争高度）── */
let rpTab = null;   // 用户手动选的 tab；其无内容时自动回退到有内容的面板
function rpHas() {
  return {
    todo: !$('#todoPanel').classList.contains('hidden'),
    tool: !$('#toolPanel').classList.contains('hidden'),
    art: !$('#artifactPanel').classList.contains('hidden'),
  };
}
function rpActive() {
  const has = rpHas();
  if (rpTab && has[rpTab]) return rpTab;
  // 运行中优先看工具（实时状态）；空闲时优先看任务清单（本轮计划）
  const order = state.busy ? ['tool', 'todo', 'art'] : ['todo', 'tool', 'art'];
  return order.find((k) => has[k]) || null;
}
function syncRightPanel() {
  const bar = $('#rpTabs');
  if (!bar) return;
  const active = rpActive();
  const has = rpHas();
  $('#rightPanel').dataset.tab = active || '';
  bar.classList.toggle('hidden', !active);
  const empty = $('#rpEmpty');
  if (empty) empty.classList.toggle('hidden', !!active);
  bar.querySelectorAll('button').forEach((b) => {
    b.classList.toggle('on', b.dataset.tab === active);
    // 没内容的面板：置灰且不可点（避免点了被自动弹回）
    const empty = !has[b.dataset.tab];
    b.classList.toggle('off', empty);
    // 不用 disabled：焦点正停在该按钮上时，每帧 sync 会把它踢出焦点，
    // Tab 键导航会被打断；aria-disabled + 点击守卫同样不可点、焦点不丢。
    b.setAttribute('aria-disabled', empty ? 'true' : 'false');
  });
  const tn = $('#rpTodoN'), kn = $('#rpToolN'), an = $('#rpArtN');
  if (tn) tn.textContent = ($('#todoProgress').textContent || '').trim();
  if (kn) kn.textContent = String($('#sessionTools').querySelectorAll('.tool-line').length || '');
  if (an) an.textContent = String($('#artifactList').children.length || '');
}
$('#rpTabs').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-tab]');
  if (!b || b.getAttribute('aria-disabled') === 'true') return;
  rpTab = b.dataset.tab; syncRightPanel();
});

/* ── 骨架屏 + 断连横幅（减少“点了没反应”感）─────────────── */
function showSkeleton() {
  const box = $('#messages');
  if (box) box.innerHTML = '<div class="sk"><div class="sk-line"></div>' +
    '<div class="sk-line"></div><div class="sk-line"></div></div>';
}
setInterval(() => {
  const b = $('#netBanner');
  if (!b) return;
  fetch('/api/health', { signal: (AbortSignal && AbortSignal.timeout) ? AbortSignal.timeout(4000) : undefined })
    .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(() => b.classList.add('hidden'))
    .catch(() => b.classList.remove('hidden'));
}, 15000);

/* ── 断流续看（B6 最小方案）：刷新页面后仍能看到“上一轮在跑”，
      跑完自动刷新本会话（不必做完整事件回放）── */
let runSeen = false;
setInterval(async () => {
  const b = $('#runBanner');
  if (!b) return;
  if (state.busy) { b.classList.add('hidden'); return; }   // 本地在跑时由 SSE 负责
  try {
    const r = await fetch('/api/jobs');
    const d = await r.json();
    const mine = !!d.running && d.running_sid && d.running_sid === state.cur;
    b.classList.toggle('hidden', !mine);
    if (d.running && d.running_owner_is_me === false && !state.busy) {
      // 局域网：别的浏览器正在跑任务，排队是正常的，直接说清楚
      b.textContent = `⏳ 会话「${d.running_title || '其他会话'}」正在运行（由其他用户发起）；你发的消息会排队等待`;
    } else {
      b.textContent = '⏳ 上一轮仍在后台运行，完成后自动刷新本会话';
    }
    if (mine) {
      runSeen = true;
    } else if (runSeen) {
      runSeen = false;
      await loadSessionMessages();
      toast('✅ 后台任务已完成，会话已刷新');
    }
  } catch (e) { /* 断连时由 netBanner 负责提示 */ }
}, 5000);

document.addEventListener('click', (e) => {
  // 站内预览：产出物链接不再直接新开页/下载
  const pv = e.target.closest('[data-pv]');
  if (pv) { e.preventDefault(); openPreview(pv.dataset.pv, pv.textContent.trim()); return; }
  // 复制：工具结果（data-copy）/ 代码块 / 思考块 / 回答
  const cp = e.target.closest('[data-copy]');
  if (cp) { copyText(cp.dataset.copy); return; }
  const cc = e.target.closest('.cp-code');
  if (cc) {
    const code = cc.closest('pre');
    copyText(code ? code.querySelector('code').innerText : '');
    return;
  }
  const tc = e.target.closest('.think-copy');
  if (tc) {
    const pre = tc.closest('.think-block');
    copyText(pre ? pre.querySelector('pre').innerText : '');
    return;
  }
  const ac = e.target.closest('.act-copy');
  if (ac) {
    const msg = ac.closest('.msg.bot');
    copyText(msg ? msg.querySelector('.content').innerText : '');
    return;
  }
  const nt = e.target.closest('.act-note');
  if (nt) { saveNote(nt.closest('.msg.bot')); return; }
  const rg = e.target.closest('.act-regen');
  if (rg) { regenerate(); return; }
  const er = e.target.closest('.err-retry');
  if (er) { regenerate(); return; }
  const ed = e.target.closest('.act-edit');
  if (ed) { editResend(ed.closest('.msg')); return; }
  const fk = e.target.closest('.act-fork');
  if (fk) { forkAt(msgIndex(fk.closest('.msg'))); return; }
  const dl = e.target.closest('.act-del');
  if (dl) { deleteFrom(msgIndex(dl.closest('.msg'))); return; }
  const on = e.target.closest('.act-one');
  if (on) { exportOne(msgIndex(on.closest('.msg'))); return; }
  // Shift+点击消息 = 进入多选导出模式（替代原来的顶栏「选择」按钮）
  if (e.shiftKey && !state.selectMode && state.cur) {
    const mm = e.target.closest('#messages > .msg');
    if (mm && !e.target.closest('a,button,input,select,textarea,pre,code')) {
      e.preventDefault();
      enterSelectMode([...$('#messages').children].indexOf(mm));
    }
  }
});

/* 气泡内「⋯」菜单：主操作常驻，次要操作收起来（一条对话从 10 个按钮降到 2 个）*/
document.addEventListener('click', (e) => {
  const more = e.target.closest('.act-more');
  if (!more) return;
  e.stopPropagation();
  const acts = more.closest('.msg-acts');
  const menu = acts && acts.querySelector('.act-menu');
  const willOpen = !!(menu && menu.classList.contains('hidden'));
  document.querySelectorAll('.act-menu').forEach((m) => m.classList.add('hidden'));
  document.querySelectorAll('.act-more').forEach((b) => b.setAttribute('aria-expanded', 'false'));
  if (willOpen) {
    menu.classList.remove('hidden');
    more.setAttribute('aria-expanded', 'true');
  }
}, true);
document.addEventListener('click', (e) => {
  if (e.target.closest('.act-more')) return;
  document.querySelectorAll('.act-menu:not(.hidden)').forEach((m) => m.classList.add('hidden'));
  document.querySelectorAll('.act-more[aria-expanded="true"]')
    .forEach((b) => b.setAttribute('aria-expanded', 'false'));
});

