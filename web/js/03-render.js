'use strict';

/* ── 工具时间线（每条回答一条，历史与实时共用）────────────── */
const TOOL_LABELS = {
  read_file: '读取文件', write_file: '写入文件', edit_file: '编辑文件',
  list_dir: '列目录', glob_search: '找文件', grep_search: '搜内容',
  delete_file: '删除文件', move_file: '移动文件', mkdir: '建目录',
  read_word: '读 Word', write_word: '写 Word', read_excel: '读 Excel',
  write_excel: '写 Excel', read_ppt: '读 PPT', write_ppt: '写 PPT',
  read_image: '看图', run_command: '执行命令', execute_python: '跑代码',
  memory_save: '记入记忆', memory_recall: '查记忆', todo: '更新清单',
  ask_user: '向你提问', delegate_task: '子任务', final_answer: '收尾',
};
function toolBrief(name, args) {
  if (!args || typeof args !== 'object') return '';
  const key = ['path', 'command', 'query', 'pattern', 'rel', 'file', 'goal']
    .find((k) => args[k]);
  let v = key ? args[key] : Object.values(args).find((x) => typeof x === 'string');
  if (v == null) return '';
  v = String(v).replace(/\s+/g, ' ').trim();
  return v.length > 46 ? v.slice(0, 46) + '…' : v;
}
// MCP 工具名 mcp__<server>__<tool> → 拆出服务名，时间线里单独标一下
function mcpInfo(name) {
  const m = String(name || '').match(/^mcp__(.+?)__(.+)$/);
  return m ? { server: m[1], tool: m[2] } : null;
}
function toolNameHtml(name) {
  const mi = mcpInfo(name);
  return mi
    ? `<span class="tl-srv" title="MCP 服务：${esc(mi.server)}">${esc(mi.server)}</span>${esc(mi.tool)}`
    : esc(name);
}
function fmtMs(ms) {
  const n = Number(ms) || 0;
  if (n <= 0) return '';
  if (n < 1000) return n + 'ms';
  return (n / 1000).toFixed(n < 10000 ? 1 : 0) + 's';
}
function toolRowHtml(name, args, ok, ms) {
  const cls = ok === true ? 'ok' : ok === false ? 'err' : 'running';
  const mark = ok === true ? ico('check') : ok === false ? ico('x') : ico('clock');
  const mi = mcpInfo(name);
  const label = mi ? (TOOL_LABELS[mi.tool] || mi.tool) : (TOOL_LABELS[name] || name);
  const srvTag = mi
    ? `<span class="tl-srv" title="MCP 服务：${esc(mi.server)}">${esc(mi.server)}</span>` : '';
  return `<div class="tl-row ${cls}" data-name="${esc(name)}">` +
    `<span class="tl-ico">${mark}</span>` +
    `<span class="tl-name">${srvTag}${esc(label)}</span>` +
    `<span class="tl-arg">${esc(toolBrief(name, args))}</span>` +
    `<span class="tl-ms">${esc(fmtMs(ms))}</span>` +
    `<span class="tl-res"></span></div>`;
}
function ensureTimeline(botEl) {
  let d = botEl.querySelector('.timeline');
  if (!d) {
    d = document.createElement('details');
    d.className = 'timeline';
    d.open = true;
    d.innerHTML = '<summary></summary><div class="tl-body"></div>';
    const acts = botEl.querySelector('.msg-acts');
    if (acts) botEl.insertBefore(d, acts); else botEl.appendChild(d);
  }
  d.classList.remove('hidden');
  return d;
}
function refreshTimeline(botEl) {
  const d = botEl.querySelector('.timeline');
  if (!d) return;
  const rows = [...d.querySelectorAll('.tl-row')];
  if (!rows.length) { d.classList.add('hidden'); return; }
  const fails = rows.filter((r) => r.classList.contains('err')).length;
  d.querySelector('summary').innerHTML =
    `${ico('tool')} 工具调用（${rows.length} 步）` +
    (fails ? ` <span class="tl-err">${fails} 失败</span>` : '');
}
function addToolStart(botEl, name, args) {
  const d = ensureTimeline(botEl);
  const body = d.querySelector('.tl-body');
  const div = document.createElement('div');
  div.innerHTML = toolRowHtml(name, args, null);
  body.appendChild(div.firstElementChild);
  refreshTimeline(botEl);
  followScroll($('#messages'));
}
function finishTool(botEl, name, ok, result, ms) {
  const d = ensureTimeline(botEl);
  let row = [...d.querySelectorAll('.tl-row.running')]
    .find((r) => r.dataset.name === name);
  if (!row) {
    const div = document.createElement('div');
    div.innerHTML = toolRowHtml(name, {}, ok);
    row = div.firstElementChild;
    d.querySelector('.tl-body').appendChild(row);
  }
  row.classList.remove('running');
  row.classList.add(ok ? 'ok' : 'err');
  row.querySelector('.tl-ico').innerHTML = ok ? ico('check') : ico('x');
  const msEl = row.querySelector('.tl-ms');
  if (msEl && ms) msEl.textContent = fmtMs(ms);
  const res = row.querySelector('.tl-res');
  const text = String(result || '').replace(/\s+/g, ' ').trim();
  if (text) {
    res.textContent = text.slice(0, 70) + (text.length > 70 ? '…' : '');
    res.title = String(result || '');
  }
  if (result) {
    const btn = document.createElement('button');
    btn.className = 'tl-copy';
    btn.title = '复制工具结果';
    btn.innerHTML = ico('copy');
    btn.onclick = (e) => { e.stopPropagation(); copyText(String(result)); };
    row.appendChild(btn);
  }
  refreshTimeline(botEl);
  followScroll($('#messages'));
}
function renderTimeline(botEl, steps) {
  if (!steps || !steps.length) return;
  const d = ensureTimeline(botEl);
  d.querySelector('.tl-body').innerHTML = steps.map((s) =>
    toolRowHtml(s.tool || s.name || 'tool', s.args || {}, s.ok !== false,
                s.ms)).join('');
  // 历史里的结果摘要（toolRowHtml 只留空位）
  const rows = [...d.querySelectorAll('.tl-row')];
  steps.forEach((s, i) => {
    const row = rows[i];
    if (!row) return;
    const text = String(s.result || '').replace(/\s+/g, ' ').trim();
    if (text) {
      const res = row.querySelector('.tl-res');
      res.textContent = text.slice(0, 70) + (text.length > 70 ? '…' : '');
      res.title = String(s.result || '');
    }
  });
  refreshTimeline(botEl);
}

/* ── 任务清单 / 上下文用量 ─────────────────────────────────── */
function renderTodoPanel(items) {
  const panel = $('#todoPanel');
  if (!panel) return;
  if (!items || !items.length) {
    panel.classList.add('hidden');
    $('#todoList').innerHTML = '';
    return;
  }
  panel.classList.remove('hidden');
  const done = items.filter((i) => i.status === 'done').length;
  $('#todoProgress').textContent = `${done}/${items.length}`;
  $('#todoList').innerHTML = items.map((i) => {
    const s = i.status === 'done' ? 'done' : i.status === 'doing' ? 'doing' : 'pending';
    const mark = s === 'done' ? '✅' : s === 'doing' ? '🔄' : '○';
    return `<div class="todo-item ${s}"><span class="ti">${mark}</span>` +
      `<span class="tt">${esc(i.text || '')}</span></div>`;
  }).join('');
}
function fmtTokens(n) {
  n = Number(n) || 0;
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k';
  return String(n);
}
function setCtxMeter(ctx) {
  if (!ctx || (!ctx.used && !ctx.limit)) return;
  const used = Number(ctx.used) || 0;
  const limit = Number(ctx.limit) || state.ctxLimit || 0;
  if (!used) return;
  const box = $('#ctxMeter');
  const pct = limit ? Math.min(100, Math.round(used / limit * 100)) : 0;
  const bar = box.querySelector('.ctx-bar i');
  bar.style.width = (limit ? pct : 30) + '%';
  box.classList.toggle('warn', !!limit && pct >= 70 && pct < 90);
  box.classList.toggle('danger', !!limit && pct >= 90);
  $('#ctxText').textContent = limit
    ? `${fmtTokens(used)} / ${fmtTokens(limit)}`
    : `${fmtTokens(used)} tokens`;
  box.title = `本轮上下文 ${used} tokens` +
    (limit ? ` / 上限 ${limit}（${pct}%）` : '') +
    (ctx.compacted ? ' · 已压缩历史' : '');
  box.classList.remove('hidden');
}

/* ── 立即压缩（长会话主动释放上下文）─────────────────────── */
function refreshCompactBtn() {
  const b = $('#btnCompact');
  if (!b) return;
  const has = !!state.cur;
  b.classList.toggle('hidden', !has);
  if (!has) return;
  const c = state.compacted;
  b.classList.toggle('on', !!c);
  b.textContent = c ? '↩ 撤销压缩' : '压缩历史';
  b.disabled = !c && (Number(state.msgCount) || 0) < 4;
  b.title = c
    ? `已压缩历史：更早的 ${c.before_msgs || '?'} 条已换成摘要（省 ${c.saved_pct ?? '?'}% 上下文）。点击撤销，恢复完整历史`
    : (b.disabled
      ? '消息还不够多（至少 4 条）'
      : '立即压缩本会话历史：更早的对话换成摘要，释放上下文（完整对话仍可回看/导出）');
  // 胶囊本身也要能看出“已压缩”，否则菜单关着就看不到了
  const pill = $('#ctxMeter');
  if (pill) pill.classList.toggle('compact-on', !!c);
}
async function compactSession() {
  if (!state.cur) return toast('先打开一个会话', true);
  if (state.busy) return toast('任务正在运行，结束后再压缩', true);
  const btn = $('#btnCompact');
  const undo = !!state.compacted;
  btn.disabled = true;
  btn.textContent = undo ? '恢复中…' : '压缩中…';
  try {
    const r = await fetch('/api/compact', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(undo ? { session_id: state.cur, clear: true }
                                : { session_id: state.cur }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '操作失败');
    if (undo) {
      state.compacted = null;
      toast('↩ 已恢复完整历史（下一轮重新发送全部对话）');
      await loadSessionMessages();
    } else {
      state.compacted = d.compact;
      toast(`✅ 已压缩：${d.compact.before_msgs} 条 → 摘要 + ${d.compact.after_msgs - 1} 条，省 ${d.compact.saved_pct}% 上下文`);
      setCtxMeter({ used: d.compact.est_tokens, limit: state.ctxLimit,
                    compacted: true });
    }
  } catch (e) {
    toast((undo ? '撤销失败：' : '压缩失败：') + e.message, true);
  } finally {
    refreshCompactBtn();
  }
}
$('#btnCompact').onclick = compactSession;

/* 上下文胶囊：用量条 + 压缩合成一个入口，点开才看到动作（顶栏少一个控件）*/
function closeCtxMenu() {
  const m = $('#ctxMenu'), b = $('#ctxMeter');
  if (m) m.classList.add('hidden');
  if (b) b.setAttribute('aria-expanded', 'false');
}
$('#ctxMeter').onclick = (e) => {
  e.stopPropagation();
  const m = $('#ctxMenu');
  if (!m) return;
  const willOpen = m.classList.contains('hidden');
  m.classList.toggle('hidden', !willOpen);
  $('#ctxMeter').setAttribute('aria-expanded', willOpen ? 'true' : 'false');
};
document.addEventListener('click', (e) => {
  if (!e.target.closest('#ctxWrap')) closeCtxMenu();
});
$('#btnCtxSet').onclick = () => {
  closeCtxMenu();
  state.settingsTab = 'run';
  $('#btnSettings').click();
};
// 主题切换就放在 token 用量条同一行（紧挨着，互不干扰：不在 ⋯ 收纳名单里，永远可见）
/* 点它不拦冒泡：点外面顺手关掉上下文菜单/更多菜单（与其它顶栏按钮一致） */
$('#btnTheme').onclick = () => toggleTheme();

/* ── 变更审阅卡片 ─────────────────────────────────────────── */
function changeCardHtml(snap) {
  if (!snap || (!snap.turn && !snap.count)) return '';
  const n = snap.count != null ? snap.count : (snap.files || []).length;
  if (!n) return '';
  if (snap.undone) {
    return '<div class="change-card undone">' + ico('undo') + ' 本轮改动已撤销</div>';
  }
  return `<div class="change-card" data-turn="${esc(snap.turn || '')}">` +
    `<span class="cc-ico">${ico('edit')}</span><span>本轮改动 <b>${n}</b> 个文件</span>` +
    `<button class="btn small" data-diff>查看改动</button>` +
    `<button class="btn small ghost" data-undo>${ico('undo')} 撤销本轮</button></div>`;
}
function citationsHtml(list) {
  if (!list || !list.length) return '';
  return `<div class="cites">📎 来源：` + list.map((c) =>
    `<a href="/api/file?path=${encodeURIComponent(c.rel || '')}" target="_blank" title="${esc(c.snippet || '')}">${esc((c.rel || '').split('/').pop())}</a>`
  ).join('') + '</div>';
}
function todoMiniHtml(items) {
  if (!items || !items.length) return '';
  const done = items.filter((i) => i.status === 'done').length;
  return `<details class="todo-mini"><summary>${ico('clipboard')} 任务清单 ${done}/${items.length}</summary>` +
    items.map((i) => `<div class="tm-row ${esc(i.status || 'pending')}">` +
      `${i.status === 'done' ? '✅' : i.status === 'doing' ? '🔄' : '○'} ${esc(i.text || '')}</div>`).join('') +
    '</details>';
}

/* ── 消息渲染 ─────────────────────────────────────────────── */
function fmtTime(ts) {
  try { return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
  catch (e) { return ''; }
}
function renderHistory(messages) {
  state.msgCount = (messages || []).length;
  refreshCompactBtn();
  const box = $('#messages');
  box.innerHTML = '';
  const sel = state.selectMode;
  for (let i = 0; i < messages.length; i++) {
    const m = messages[i];
    let el;
    if (m.role === 'user') el = msgUser(m.content, m.ts, m.attachments);
    else if (m.role === 'assistant') {
      // 历史里的 artifacts 可能是 rel 字符串（旧数据）也可能是 {rel,name}，统一成对象
      const arts = (m.artifacts || []).map((a) => (typeof a === 'string'
        ? { rel: a, name: a.split('/').pop() } : a));
      el = msgBot(m.content, m.tool_steps || [], arts, m.thinking || '', null, {
        snapshot: m.snapshot, todos: m.todos,
        citations: m.citations, context: m.context,
      });
    } else continue;
    el.style.animationDelay = Math.min(i * 18, 260) + 'ms';
    // 消息级操作（分支/编辑/删除）靠 DOM 位置定位索引：
    // #messages 的子节点与会话 messages 数组始终一一对应
    if (sel) {
      // 选择模式：加复选框 + 点击整条切换选中
      el.classList.add('selectable');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'sel-cb';
      cb.checked = state.selIdx.has(i);
      cb.dataset.idx = i;
      el.prepend(cb);
      cb.onclick = (e) => {
        e.stopPropagation();
        toggleSel(i, cb.checked);
      };
      el.onclick = () => {
        const checked = !state.selIdx.has(i);
        state.selIdx.has(i) ? state.selIdx.delete(i) : state.selIdx.add(i);
        cb.checked = checked;
        el.classList.toggle('sel-on', checked);
        selBarUpdate();
      };
      if (cb.checked) el.classList.add('sel-on');
    }
    box.appendChild(el);
  }
  if (state.selectMode) selBarUpdate();   // 进多选后计数立刻对上（含 Shift+点击进入的那条）
  state.followBottom = true;
  setJump(false);
  box.scrollTop = box.scrollHeight;
  // 会话级工具面板：全部消息的工具调用聚合到单一折叠块
  renderSessionTools(messages);
  // 刷新/切换会话后，右侧任务清单与上下文条也要恢复（不能只在实时帧时出现）
  const lastWithTodo = [...messages].reverse().find(
    (m) => m.role === 'assistant' && Array.isArray(m.todos) && m.todos.length);
  renderTodoPanel(lastWithTodo ? lastWithTodo.todos : []);
  const lastWithCtx = [...messages].reverse().find(
    (m) => m.role === 'assistant' && m.context && m.context.used);
  if (lastWithCtx) setCtxMeter(lastWithCtx.context);
  syncRightPanel();
}

/* ── 多选导出 Markdown ───────────────────────────────────── */
function toggleSel(i, checked) {
  if (checked) state.selIdx.add(i);
  else state.selIdx.delete(i);
  selBarUpdate();
}
function selBarUpdate() {
  const n = state.selIdx.size;
  $('#selCount').textContent = `已选 ${n} 条`;
  const b = $('#selExport');
  b.disabled = n === 0;
  b.textContent = `导出所选 (${n})`;
}
/* 多选导出：不再占顶栏一个按钮，Shift+点击消息直接进入（idx 为首次选中的那条）*/
function enterSelectMode(idx) {
  state.selectMode = true;
  state.selIdx.clear();
  if (idx != null && idx >= 0) state.selIdx.add(idx);
  $('#selBar').classList.remove('hidden');
  loadSessionMessages();  // 重渲染带复选框
}
function exitSelectMode() {
  state.selectMode = false;
  state.selIdx.clear();
  $('#selBar').classList.add('hidden');
  loadSessionMessages();
}
async function loadSessionMessages() {
  if (!state.cur) return;
  const sid = state.cur;
  const gen = state.sessionGen;
  const r = await fetch('/api/session?id=' + encodeURIComponent(sid));
  const d = await r.json();
  if (state.cur !== sid || gen !== state.sessionGen) return;  // 期间已切会话，旧回包丢弃
  const s = d.session || {};
  state.compacted = s.compact || null;
  state.msgCount = (s.messages || []).length;
  refreshCompactBtn();
  renderHistory(s.messages || []);
}
function exportSelectedMd() {
  const box = $('#messages');
  // 按 DOM 顺序收集勾选的消息（复选框在每条消息里，保留聊天顺序）
  const parts = [];
  for (const cb of box.querySelectorAll('.sel-cb')) {
    if (!cb.checked) continue;
    const el = cb.closest('.msg');
    const isUser = el.classList.contains('user');
    const role = isUser ? '🧑 用户' : '🤖 AI';
    const text = el.querySelector('.u-txt, .content').innerText;
    parts.push(`**${role}**：\n\n${text}\n`);
  }
  const md = `# 会话导出 ${new Date().toLocaleString()}\n\n${parts.join('\n---\n\n')}\n`;
  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `会话导出_${Date.now()}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
  toast('✅ 已导出所选 ' + state.selIdx.size + ' 条消息');
  exitSelectMode();
}

