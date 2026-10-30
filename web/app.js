/* 本地 Agent 工作台前端 */
'use strict';

if (location.protocol === 'file:') {
  document.documentElement.innerHTML = `<style>
    body{margin:0;font:14px/1.7 system-ui,"Microsoft YaHei",sans-serif;background:#16181d;color:#e6e8eb}
    #fh{max-width:520px;margin:12vh auto;padding:28px 32px;background:#1e2128;border:1px solid #333845;border-radius:12px}
    h1{font-size:18px;margin:0 0 10px}
    code{background:#262a33;padding:2px 7px;border-radius:5px;font-size:13px}
    .muted{color:#9aa0aa;font-size:12px;margin-top:14px}
  </style>
  <div id="fh">
    <h1>💼 本地 Agent 工作台</h1>
    <p>检测到正在以 <code>file://</code> 方式直接打开页面，本应用需要本地服务才能运行（流式聊天 / 文件访问 / 记忆库）。</p>
    <p>请在项目目录运行：<code>python app.py</code>，然后访问 <code>http://127.0.0.1:8080</code>。</p>
    <p class="muted">正在探测 127.0.0.1:8080 上是否已有服务在运行…</p>
  </div>`;
  (async () => {
    try {
      const r = await fetch('http://127.0.0.1:8080/api/health', { signal: AbortSignal.timeout(1500) });
      if (r.ok) location.replace('http://127.0.0.1:8080/');
    } catch (e) { /* 未运行则保持提示页 */ }
  })();
  throw new Error('file:// 模式：请运行 python app.py 后通过 http://127.0.0.1:8080 访问');
}

const $ = (s) => document.querySelector(s);

/* wbclient 由 HTTP 服务端签名并以 HttpOnly cookie 下发；前端不再自报/写入身份。 */
const state = {
  sessions: [],
  cur: null,          // 当前会话 id
  busy: false,
  artShown: false,
  pendingConfirm: null,
  treeRoot: 'ws',      // 文件树当前根：ws|home|drive
  treeRootPath: '',
  attachments: [],    // 本次会话已上传的附件（随消息发送给 AI，手动移除前一直保留）
  selectMode: false,  // 多选导出模式
  selIdx: new Set(),  // 选中的消息索引（对应 renderHistory 的顺序）
  followBottom: true, // 流式输出时是否跟随到底部（用户上滑后暂停）
  ctxLimit: 0,        // 模型上下文窗口（来自 /api/health）
  compacted: null,    // 本会话手动压缩记录（{summary,upto,...}；有值=只发摘要+最近几条）
  msgCount: 0,        // 当前会话消息数（决定“压缩”按钮是否可用）
  mcpList: [],        // 当前 MCP 服务状态（设置页表单回填用）
  mcpEditing: null,   // 正编辑的 MCP 服务名（null=新增）
  wsRoot: '',         // 工作台根目录（MCP 一键模板默认路径用）
  isLoopback: true,   // 是否在本机打开（远程访问时「打开目录/用系统程序打开」无效，隐藏）
  editIdx: null,      // 「编辑上一条/编辑后重发」：发送时先从该条起截断对话
};

/* ── 统一图标集：动作类 emoji 一律换成线性 SVG（同一体例、currentColor）── */
const ICONS = {
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  trash: '<path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/>',
  fork: '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="6" r="2.5"/><path d="M6 8.5v7"/><path d="M18 8.5c0 5-6 4.5-9.5 7"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
  save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h7"/>',
  note: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/><path d="M8 13h8M8 17h5"/>',
  retry: '<path d="M20.5 12a8.5 8.5 0 1 1-2.5-6"/><path d="M21 3v6h-6"/>',
  refresh: '<path d="M20.5 12a8.5 8.5 0 1 1-2.5-6"/><path d="M21 3v6h-6"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-4"/>',
  pin: '<path d="M12 17v5"/><path d="M9 3h6l-1 6 3 3v2H7v-2l3-3z"/>',
  folder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
  box: '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
  clipboard: '<rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M9 12h6M9 16h4"/>',
  tool: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  plug: '<path d="M9 2v6M15 2v6"/><path d="M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v5"/>',
  db: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18z"/>',
  moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  chart: '<path d="M3 3v18h18"/><path d="M7 16v3M12 10v9M17 6v13"/>',
  table: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>',
  check: '<path d="M9 11l3 3 9-9"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
  stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
  up: '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
  panels: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M15 3v18"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  log: '<path d="M8 3h8a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M10 8h4M10 12h4M10 16h2"/>',
  type: '<path d="M4 7V5h16v2"/><path d="M9 19h6"/><path d="M12 5v14"/>',
  paperclip: '<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
  play: '<path d="M7 4.5 19 12 7 19.5z"/>',
  think: '<path d="M12 2a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 2z"/><path d="M9.5 18h5M10 21.5h4"/>',
  chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  img: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="1.6"/><path d="m21 15-4.5-4.5L6 21"/>',
  doc: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/><path d="M8 13h6M8 17h4"/>',
  sheet: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10M15 10v10"/>',
  slides: '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M12 17v4M8 21h8"/>',
  zip: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M12 3v4M12 11v2M12 17v2"/>',
  audio: '<path d="M9 18V6l10-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="16" r="2.5"/>',
  video: '<rect x="2" y="5" width="14" height="14" rx="2"/><path d="m16 10 5-3v10l-5-3z"/>',
  code: '<path d="m8 8-4 4 4 4"/><path d="m16 8 4 4-4 4"/><path d="M13.5 5.5l-3 13"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 2"/>',
};
// 返回内联 SVG；不认识的名字原样透传（＋ 之类文字符号）
function ico(name) {
  const p = ICONS[name];
  if (!p) return esc(String(name == null ? '' : name));
  return `<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
}

/* ── Markdown 渲染（markdown-it + highlight.js）──────────── */
const md = window.markdownit ? window.markdownit({
  html: false,
  linkify: true,
  breaks: true,
  highlight(str, lang) {
    // 代码块右上角复制按钮（始终可见，不依赖 hover），并显示语言角标。
    // 带格式一键复制：innerText 保留换行/缩进（事件委托处理，见复制委托段）。
    const langName = (lang || '').toLowerCase();
    const known = langName && window.hljs && hljs.getLanguage(langName);
    const badge = known ? `<span class="cp-lang">${esc(langName)}</span>` : '';
    const cpBtn = `<button class="cp-code" title="带格式复制代码">${ico('copy')} 复制</button>`;
    if (known) {
      try { return `<pre><code class="hljs language-${langName}">` + hljs.highlight(str, { language: langName, ignoreIllegals: true }).value + `</code>${badge}${cpBtn}</pre>`; }
      catch (e) {}
    }
    if (window.hljs) {
      try { return `<pre><code class="hljs">` + hljs.highlightAuto(str).value + `</code>${badge}${cpBtn}</pre>`; }
      catch (e) {}
    }
    return `<pre><code>${esc(str)}</code>${badge}${cpBtn}</pre>`;
  },
}) : { render: (s) => esc(s) };

/* ── 工具 ─────────────────────────────────────────────────── */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
// 输入法组合中（IME 上屏候选词）：不能当「回车发送」，否则中文用户会把半截话发出去
// （部分输入法上屏那一下 isComposing=false、keyCode=229，所以两个都判）
function isComposingKey(e) {
  return !!e && (e.isComposing === true || e.keyCode === 229);
}
function fmtSize(n) {
  if (n == null) return '';
  if (n < 1024) return n + 'B';
  if (n < 1048576) return (n / 1024).toFixed(1) + 'KB';
  return (n / 1048576).toFixed(1) + 'MB';
}
function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = isErr ? 'err' : '';
  t.classList.remove('hidden');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add('hidden'), 3200);
}
// 文件类型图标：与按钮/工具栏同一套线性 SVG
// （emoji 在不同系统里风格不一致，离线机缺 emoji 字体时还会变豆腐块）
function fileIcon(ext) {
  const e = String(ext || '').toLowerCase();
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico'].includes(e)) return ico('img');
  if (['xlsx', 'xls', 'xlsm', 'csv', 'ods', 'tsv'].includes(e)) return ico('sheet');
  if (['pptx', 'ppt', 'odp'].includes(e)) return ico('slides');
  if (['pdf', 'docx', 'doc', 'odt', 'wps', 'rtf'].includes(e)) return ico('doc');
  if (['zip', 'rar', '7z', 'tar', 'gz', 'bz2'].includes(e)) return ico('zip');
  if (['mp3', 'wav', 'flac', 'm4a', 'ogg', 'aac'].includes(e)) return ico('audio');
  if (['mp4', 'avi', 'mkv', 'mov', 'webm'].includes(e)) return ico('video');
  if (['py', 'js', 'ts', 'jsx', 'tsx', 'html', 'css', 'json', 'java', 'c', 'cpp',
       'go', 'rs', 'sh', 'ps1', 'sql', 'xml', 'yaml', 'yml', 'toml'].includes(e)) return ico('code');
  if (['md', 'txt', 'log', 'ini', 'cfg', 'conf'].includes(e)) return ico('note');
  return ico('file');
}
function artIcon(ext) {
  return fileIcon(ext);
}

/* ── 主题 ─────────────────────────────────────────────────── */
function applyTheme() {
  const dark = localStorage.getItem('theme') === 'dark';
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  const ld = $('#hlLight'), dd = $('#hlDark');
  if (ld) ld.disabled = dark;
  if (dd) dd.disabled = !dark;
  syncThemeBtn();
}
/* 顶栏用量条旁的主题按钮 + 设置页按钮：跟着主题一起刷新（一处切换、两处同步） */
function syncThemeBtn() {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  const b = $('#btnTheme');
  if (b) {
    b.innerHTML = ico(dark ? 'sun' : 'moon');
    b.title = dark ? '当前深色，点击切到浅色' : '当前浅色，点击切到深色';
    b.setAttribute('aria-label', b.title);
  }
  const s = $('#stTheme');
  if (s) s.textContent = dark ? '🌙 深色' : '☀️ 浅色';
}
/* 主题入口：顶栏用量条那一行一键切；设置页 / 命令面板共用同一函数 */
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  localStorage.setItem('theme', cur);
  applyTheme();
  syncThemeBtn();
  toast(cur === 'dark' ? '🌙 已切到深色' : '☀️ 已切到浅色');
}

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
  const r = await fetch('/api/session?id=' + encodeURIComponent(id));
  const d = await r.json();
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
  const r = await fetch('/api/session?id=' + encodeURIComponent(state.cur));
  const d = await r.json();
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
    b.disabled = empty;
  });
  const tn = $('#rpTodoN'), kn = $('#rpToolN'), an = $('#rpArtN');
  if (tn) tn.textContent = ($('#todoProgress').textContent || '').trim();
  if (kn) kn.textContent = String($('#sessionTools').querySelectorAll('.tool-line').length || '');
  if (an) an.textContent = String($('#artifactList').children.length || '');
}
$('#rpTabs').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-tab]');
  if (b) { rpTab = b.dataset.tab; syncRightPanel(); }
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

/* ── 消息级操作：分支 / 编辑重发 / 删除该条 / 导出该条（U5）── */
function msgIndex(el) {
  if (!el) return -1;
  return [...$('#messages').children].indexOf(el);
}
async function forkAt(idx) {
  if (state.busy) return toast('任务进行中，请先停止', true);
  if (!state.cur || idx < 0) return;
  try {
    const r = await fetch('/api/session/fork', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: state.cur, index: idx }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '分叉失败');
    await loadSessions();
    await selectSession(d.session.id);
    toast('⑂ 已从该条分叉出新会话（原会话未动）');
  } catch (e) {
    toast('分支失败：' + e.message, true);
  }
}
function deleteFrom(idx) {
  if (state.busy) return toast('任务进行中，请先停止', true);
  if (!state.cur || idx < 0) return;
  confirmDialog('删除对话', `将删除第 ${idx + 1} 条及其后的全部对话（不可恢复，不影响已生成的文件）。确定删除？`, async () => {
    try {
      const r = await fetch('/api/session/truncate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: state.cur, index: idx }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '删除失败');
      await loadSessions();
      await selectSession(state.cur);
      toast(`已删除 ${d.removed || 0} 条对话`);
    } catch (e) {
      toast('删除失败：' + e.message, true);
    }
  }, '删除');
}
function editResend(el) {
  if (state.busy) return toast('任务进行中，请先停止', true);
  const idx = msgIndex(el);
  if (!state.cur || idx < 0) return;
  const text = (el.querySelector('.u-txt') || {}).textContent || '';
  confirmDialog('编辑重发', '发送时会删除该条及之后的对话（不可恢复），再以编辑后的内容重新发送。原会话如需保留，建议先用「从这里分支」。', async () => {
    state.editIdx = idx;
    const inp = $('#input');
    inp.value = text;
    autoGrow(inp);
    inp.focus();
    toast('已载入原文，编辑后按 Enter 发送（将从该条起截断）');
  }, '编辑');
}
async function exportOne(idx) {
  if (idx < 0) return;
  try {
    const r = await fetch('/api/session?id=' + encodeURIComponent(state.cur || ''));
    const d = await r.json();
    const m = ((d.session || {}).messages || [])[idx];
    if (!m) throw new Error('找不到该条消息');
    const role = m.role === 'user' ? '🧑 用户' : '🤖 AI';
    const md1 = `# 单条导出 ${new Date().toLocaleString()}\n\n**${role}**：\n\n${m.content}\n`;
    const blob = new Blob([md1], { type: 'text/markdown;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `消息导出_${Date.now()}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
    toast('✅ 已导出该条');
  } catch (e) {
    toast('导出失败：' + e.message, true);
  }
}
function notifyDone(title, body) {
  // 页面在后台时弹系统通知（完全本地，离线可用）
  if (typeof Notification === 'undefined' || document.hasFocus()) return;
  if (Notification.permission !== 'granted') return;
  try { new Notification(title, { body: body || '', tag: 'agent-done' }); } catch (e) {}
}
async function saveNote(botEl) {
  if (!botEl) return;
  const content = botEl.querySelector('.content').innerText;
  const name = await promptDialog('存为笔记（存到 notes/）', '');
  const n = (name || '').trim();
  if (!n) return;
  try {
    const r = await fetch('/api/note', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: n, content }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error);
    toast('✅ 已存为笔记：' + d.path);
  } catch (err) {
    toast('存笔记失败：' + err.message, true);
  }
}

/* ── 思考标签兜底（前端双保险：后端旧版/漏拆时也能保证答案区干净）── */
const JS_THINK_START = /<think(?:ing)?[^>]*>|<\|start_thinking\|>/i;
const JS_THINK_END = /<\/think(?:ing)?>|<\|end_thinking\|>/i;
// 未闭合思考块内的"思考→正文"转折（正文另起一段且像答案开头）
const JS_ANSWER_TRANSITION = /\n\n\s*(?:好的[，,]?\s*(?:这是|下面|以下是|最终|直接|我)|(?:最终|直接)(?:答案|给出|回答)|综上(?:所述)?|根据以上分析|以下是我|我(?:现在|来)直接|#{1,3}\s*(?:回答|答案|结果|总结|Answer|Result)|-{3,}|Here'?s (?:the |my )?|The (?:answer|result|solution)|In (?:summary|conclusion)|Based on (?:the |my )?analysis)/i;

// 历史回放：把存储内容里残留的 <think> 块提取出来（兼容旧数据）
function extractThinking(text) {
  let thinking = '';
  let content = String(text || '');
  content = content.replace(/<think(?:ing)?[^>]*>([\s\S]*?)<\/(?:think|thinking)>/gi, (m, body) => { thinking += body; return ''; });
  content = content.replace(/<\|start_thinking\|>([\s\S]*?)<\|end_thinking\|>/gi, (m, body) => { thinking += body; return ''; });
  // 未闭合 <think>（老数据常见）：从 <think 到结尾都视为思考，内部找转折点保留正文
  const i = content.toLowerCase().indexOf('<think');
  if (i >= 0) {
    const rest = content.slice(i);
    const head = rest.replace(/^<think(?:ing)?[^>]*>/i, '');
    const tm = head.match(JS_ANSWER_TRANSITION);
    if (tm) {
      thinking += head.slice(0, tm.index);
      content = content.slice(0, i) + head.slice(tm.index).replace(/^[\s\r\n\t]+/, '');
    } else {
      thinking += head;
      content = content.slice(0, i);
    }
  }
  // 无标签思考兜底：整段以思考开头且中途有"思考→正文"转折 -> 裁进思考块
  // （离线模型不带 <think> 标签直接写思考时，历史回放也能正确折叠）
  const un = splitUnmarkedThink(content);
  if (un.thinking) {
    thinking = (thinking ? thinking + '\n\n' : '') + un.thinking;
    content = un.content;
  }
  return { content: content.trim(), thinking: thinking.trim() };
}

// 流式思考拆分器：与后端同逻辑，跨 chunk 缓冲，漏拆时兜底
function createThinkSplitter(addContent, addThink) {
  let buf = '', inThink = false;
  const HOLD = 18, MAX = 16384;
  function drain(flush) {
    while (true) {
      if (inThink) {
        const m = buf.match(JS_THINK_END);
        if (m) {
          if (buf.slice(0, m.index)) addThink(buf.slice(0, m.index));
          buf = buf.slice(m.index + m[0].length);
          inThink = false;
          continue;
        }
        if (buf.length >= 20) {
          const tm = buf.match(JS_ANSWER_TRANSITION);
          if (tm) {
            if (buf.slice(0, tm.index)) addThink(buf.slice(0, tm.index));
            buf = buf.slice(tm.index).replace(/^[\s\r\n\t]+/, '');
            inThink = false;
            continue;
          }
        }
        if (buf.length > MAX) {
          addThink(buf.slice(0, -MAX));
          buf = buf.slice(-MAX);
        }
        return;
      }
      const m = buf.match(JS_THINK_START);
      if (m) {
        if (buf.slice(0, m.index)) addContent(buf.slice(0, m.index));
        buf = buf.slice(m.index + m[0].length);
        inThink = true;
        continue;
      }
      if (flush) {
        if (buf) addContent(buf);
        buf = '';
        return;
      }
      if (buf.length > HOLD) {
        addContent(buf.slice(0, -HOLD));
        buf = buf.slice(-HOLD);
      }
      return;
    }
  }
  return {
    push(t) { if (t) { buf += t; drain(false); } },
    finish() { drain(true); if (buf) { if (inThink) addThink(buf); else addContent(buf); buf = ''; } },
  };
}

/* ── 无标签思考识别（前端兜底，与后端 split_unmarked_thinking 同策略）──
 * 离线模型（尤其 Qwen3 系）常把思考直接写进正文且不带 <think> 标签，
 * 后端启发式漏判时这里整段再裁一次：思考进折叠块，正文区保持干净。 */
const JS_UNMARKED_PREFIX = [
  /^here'?s (?:my |a |the )?(?:thinking process|reasoning process)/i,
  /^step[- ]by[- ]step (?:analysis|reasoning|thinking)/i,
  /^(?:internal |hidden )?thinking(?:\s+process)?\s*[:：]/i,
  /^(?:chain[- ]of[- ])?reasoning(?:\s+process)?\s*[:：]/i,
  /^(?:detailed |step[- ]by[- ]step )?analysis\s*[:：]/i,
  /^let'?s break (?:this|it) down step by step/i,
  /^(?:let|allow) me (?:think|analyze|reason|consider|look at|review)/i,
  /^(?:first|okay|ok|alright|so),? let me (?:think|analyze|consider|check)/i,
  /^to (?:answer|solve|fix|handle) this,? (?:i |we |let )/i,
  /^the user (?:wants|asks|needs|is asking)/i,
  /^so,? (?:the|this) user (?:wants|asked)/i,
  /^now,? (?:i|we|let me) (?:need|should|can|will)/i,
  /^(?:我的)?思考过程[：:]/, /^思路分析[：:]/, /^逐步分析[：:]/, /^分析过程[：:]/, /^需求(?:是|为|：|:)/,
  /^(?:好的|嗯|恩|好|OK|可以)[，,。]?\s*(?:让我|我来|我先|我们)(?:先|来)?(?:想想|思考|分析|看看|检查|回顾|梳理|理解)/,
  /^(?:让我|我来|我先|我们)(?:先|来)?(?:想想|思考|分析|看看|检查|回顾|梳理|理解)/,
  /^(?:首先|先)[，,]?(?:我|我们)?(?:需要|来看|分析|检查|确认)/,
  /^用户(?:的)?(?:问题是|想|需要|要求|希望|想要|想让我|让我|需要我|要求我|提到|说|问|给(?:了|我)|期望|目标是)/,
  /^(?:好的|嗯|恩|好|OK|可以)[，,。]?\s*用户(?:的)?(?:问题是|想|需要|要求|希望|想要|想让我|让我|提到|说|问)/,
  /^(?:这个|当前|本次|这是一(?:个|份)|这是关于)(?:任务|需求|问题|请求|要求|文件|数据)/,
  /^(?:我看到|我注意到|我发现|我需要|我得先|我来先|先看(?:下|一下)|先来|先确认)/,
  /^(?:用户说|用户提|用户问|用户发来|用户上传了|用户给了)/,
  /^我的(?:思路|想法|计划|分析|考虑)(?:是|：|:)/,
  /^(?:这个任务|该任务|本次任务|当前任务)(?:是|要求|需要)/,
];
const JS_UNMARKED_TRANS = [
  /^here'?s (?:the |my )?(?:answer|response|reply|result|summary|solution)\s*[:：]/i,
  /^based on (?:the |my )?analysis\s*[,，]/i,
  /^(?:in summary|to summarize|in conclusion|in short|all in all)\s*[,：:]/i,
  /^(?:the (?:answer|solution|fix|result|conclusion) is)\s*[:：]/i,
  /^(?:to (?:fix|solve|resolve|answer) (?:this|it|the (?:problem|issue)))\s*[,：:]/i,
  /^(?:okay|ok|alright|so),? now\s*[,，:]/i,
  /^anyway\s*[,，]/i, /^(?:so|now),? here'?s/i, /^---+/, /^##\s+(?:Answer|Response|Solution|回答|总结|结果)/i,
  /^根据以上分析\s*[,，]/,
  /^(?:综上|综上所述|总的来说|总的说来|总而言之)\s*[,，：:]/,
  /^(?:最终答案|答案是|我的回答是|我的建议是|我的方案是)\s*[:：]/,
  /^(?:好的|好|OK)[，,。]?\s*(?:我来?|我(?:现在|来)直接|以下|这是|现在|下面)/,
  /^(?:现在|接下来|最后)[，,]?\s*(?:我(?:们)?(?:来|直接|给出|总结|看)|回答|给出|总结|看)/,
  /^直接(?:给出|回答|上|说)/, /^所以[,，:：]?/, /^因此[,，:：]?/, /^那么[,，:：]?/,
  /^(?:总结|小结)(?:一下|如下|来说)?[,，:：]?/,
  /^(?:我认为|我建议|我觉得|我推荐|我倾向于)[,，:：]/,
  /^(?:下面是|以下(?:是|为)|具体(?:做法|方案|来说)|详细(?:方案|步骤|做法)|步骤(?:如下)?|做法(?:如下)?|回答如下|结果如下)/,
  /^可以(?:这样|这么做|按以下|参考以下)/, /^先说结论/, /^那我们(?:来|现在)?(?:回答|看看|整理|总结)/,
  /^接下来我/, /^综上，我/, /^所以(?:我|我们)/,
];
function splitUnmarkedThink(content) {
  const s = String(content || '');
  if (!s || s.length < 300) return { content: s, thinking: '' };
  const head = s.slice(0, 200);
  if (!JS_UNMARKED_PREFIX.some((re) => re.test(head))) return { content: s, thinking: '' };
  const searchStart = Math.min(500, Math.floor(s.length / 4));
  // 逐行扫描转折标记（等价后端 re.MULTILINE：^ 可匹配任意行首）
  const lines = s.slice(searchStart).split('\n');
  let off = 0;
  for (const ln of lines) {
    for (const re of JS_UNMARKED_TRANS) {
      const m = re.exec(ln);
      if (m) {
        const splitPos = searchStart + off + m.index;
        const marker = m[0];
        const after = s.slice(splitPos + marker.length).replace(/^[\s\r\n\t，,：:。]+/, '');
        if (!after.trim()) continue;
        return { content: after, thinking: s.slice(0, splitPos + marker.length).trim() };
      }
    }
    off += ln.length + 1;
  }
  if (s.length > 8000) return { content: s.slice(0, 4000) + '\n\n...(内容过长，已截断)', thinking: '' };
  return { content: s, thinking: '' };
}

// 确保思考折叠块存在并返回（懒创建，默认折叠）
function ensureThinkBlock(botEl, thinkEl) {
  if (!thinkEl) {
    thinkEl = document.createElement('details');
    thinkEl.className = 'think-block';
    thinkEl.open = false;
    thinkEl.innerHTML = '<summary></summary><pre></pre>' +
      `<button class="think-copy" title="复制思考内容">${ico('copy')}</button>`;
    botEl.insertBefore(thinkEl, botEl.querySelector('.content'));
  }
  return thinkEl;
}
function appendThink(thinkEl, text) {
  const pre = thinkEl.querySelector('pre');
  pre.textContent += text;    thinkEl.querySelector('summary').innerHTML = `${ico('think')} 思考过程（${pre.textContent.length} 字）`;
}

/* ── 发送（SSE 流式）──────────────────────────────────────── */
// 多请求并发支持：每个 send 各持一个 AbortController；
// pendingCount 跟踪在跑/在排队的请求数，busy = 还有请求没结束（避免排队请求提前清 busy）。
let abortCtrls = [];
let pendingCount = 0;
let stopFlag = false;  // 手动点「停止」标记（AbortError 分支避免重复 toast）
async function send(overrideText, overrideAtts, opts) {
  const inp = $('#input');
  const text = (overrideText != null ? String(overrideText) : inp.value).trim();
  const atts = overrideAtts || state.attachments;
  const regenerate = !!(opts && opts.regenerate);
  let sendFailed = false;
  // 忙碌时也允许发送：进入服务端排队（SSE 推 queued 帧，前端显示排队条 + 可取消）
  if (!text) return;
  if (state.selectMode) exitSelectMode();
  const ctrl = new AbortController();
  abortCtrls.push(ctrl);
  pendingCount++;
  // 置忙必须发生在任何 await 之前：发送后立刻出现「⏹ 停止」，
  // 而不是等会话创建/首个反馈帧才可终止（用户之前遇到"只能等 AI 回复"）。
  state.busy = true;
  setBusyUI(true);
  // 首次发送时请求通知权限（用户手势内，浏览器才允许）
  if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
    try { Notification.requestPermission(); } catch (e) {}
  }

  // 无会话 -> 先建（重新生成必然已有会话）
  try {
    if (!state.cur) {
      const r = await fetch('/api/session/new', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const d = await r.json();
      state.cur = d.session.id;
      try { localStorage.setItem('lastSession', state.cur); } catch (e) {}
      await loadSessions();
      renderSessions();
    }
  } catch (e) {
    pendingCount--;
    abortCtrls = abortCtrls.filter((c) => c !== ctrl);
    state.busy = pendingCount > 0;
    setBusyUI(state.busy);
    toast('创建会话失败：' + e.message, true);
    return;
  }
  showWelcome(false);

  // 编辑重发：先把该条及之后的对话从服务端截掉，再以新内容重发
  if (state.editIdx != null && state.cur) {
    const ei = state.editIdx;
    state.editIdx = null;
    try {
      await fetch('/api/session/truncate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: state.cur, index: ei }),
      });
      await loadSessionMessages();
    } catch (e) { /* 截断失败不阻断发送 */ }
  }

  inp.value = '';
  autoGrow(inp);

  const box = $('#messages');
  // 重新生成时用户消息已存在，不再新建气泡
  if (!regenerate) box.appendChild(msgUser(text, null, atts));
  const botEl = msgBot('', [], []);
  const contentDiv = botEl.querySelector('.content');
  botEl.querySelector('.content').innerHTML = '';
  const cursor = document.createElement('span');
  cursor.className = 'typing';
  contentDiv.appendChild(cursor);
  box.appendChild(botEl);
  state.followBottom = true;
  setJump(false);
  box.scrollTop = box.scrollHeight;
  // 新一轮开始：右侧工具面板清空，只展示本轮的工具调用链路
  resetSessionTools();

  // 首 token 等待提示：离线大模型 prefill 常几十秒，只有一个闪烁光标会被当成卡死
  let waited = 0;
  const waitTip = document.createElement('div');
  waitTip.className = 'wait-tip';
  waitTip.textContent = '⏳ 正在请求模型…';
  contentDiv.insertBefore(waitTip, cursor);
  const waitTimer = setInterval(() => {
    waited += 1;
    if (!waitTip.isConnected) return;
    waitTip.textContent = waited >= 30
      ? `⏳ 已等待 ${waited}s · 内网大模型「首 token」可能较慢（可在 设置 → 运行 调大模型超时），点「停止」可中断`
      : `⏳ 已等待 ${waited}s…`;
  }, 1000);
  const clearWait = () => {
    clearInterval(waitTimer);
    if (waitTip.isConnected) waitTip.remove();
  };

  let thinkEl = null;  // 思考过程折叠块（首个 think 帧时创建）
  const mdBox = document.createElement('div');
  mdBox.className = 'md';
  contentDiv.insertBefore(mdBox, cursor);
  let mdBuf = '';      // 流式正文累积
  let mdRaf = null;    // 流式渲染节流
  function renderMd(force) {
    mdRaf = null;
    // 用户正在这条消息里选文字/复制时先不重排：innerHTML 替换会把选区清掉
    const sel = window.getSelection ? window.getSelection() : null;
    if (!force && sel && !sel.isCollapsed && sel.anchorNode && mdBox.contains(sel.anchorNode)) {
      mdRaf = setTimeout(() => renderMd(false), 200);
      return;
    }
    mdBox.innerHTML = md.render(mdBuf);
    followScroll(box);
  }
  // 自适应节流：正文越长整块重排越贵（低配办公机上会掉帧）
  function scheduleMd() {
    if (mdRaf) return;
    mdRaf = setTimeout(() => renderMd(false),
                       Math.min(400, 60 + Math.floor(mdBuf.length / 40)));
  }
  // 前端兜底拆分：delta 先进 splitter，正文 -> mdBuf，思考 -> 思考块（后端漏拆也能保证答案干净）
  const jsSplit = createThinkSplitter(
    (t) => { mdBuf += t; clearWait(); scheduleMd(); },
    (t) => {
      clearWait();
      thinkEl = ensureThinkBlock(botEl, thinkEl);
      appendThink(thinkEl, t);
    }
  );
  // 流上下文：gotDelta 标记本轮是否收到过正文帧——
  // result 兜底只在该轮完全没流式正文时触发，避免与渲染节流竞态导致内容双份
  const streamCtx = {
    gotDelta: false,
    clearWait,
    onDelta: (t) => { streamCtx.gotDelta = true; jsSplit.push(t); },
    flush: () => { jsSplit.finish(); if (mdRaf) { clearTimeout(mdRaf); mdRaf = null; } renderMd(true); },
  };
  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: state.cur,
        message: text,
        mode: $('#askMode').checked ? 'ask' : 'craft',
        attachments: atts.map((a) => a.path),
        regenerate: regenerate,
        model: currentModelOv() || undefined,
      }),
      signal: ctrl.signal,
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      throw new Error(d.error || ('HTTP ' + resp.status));
    }

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = frame.split('\n').find((l) => l.startsWith('data:'));
        if (!line) continue;
        try {
          thinkEl = handleFrame(JSON.parse(line.slice(5)), botEl, thinkEl, streamCtx);
        }
        catch (e) { /* 忽略坏帧 */ }
      }
    }
    jsSplit.finish();   // 流结束：未闭合思考块兜底归位
    clearWait();
    cursor.remove();
    if (mdRaf) { clearTimeout(mdRaf); mdRaf = null; }
    // 无标签思考兜底：离线模型把"用户希望…/好的，让我先分析…"直接写进正文
    // （后端启发式漏判时），前端整段再裁一次，保证思考进折叠块、正文区干净
    const un = splitUnmarkedThink(mdBuf);
    if (un.thinking) {
      thinkEl = ensureThinkBlock(botEl, thinkEl);
      appendThink(thinkEl, un.thinking);
      mdBuf = un.content;
    }
    renderMd(true);
  } catch (e) {
    sendFailed = true;
    clearWait();
    jsSplit.finish();
    cursor.remove();
    if (mdRaf) { clearTimeout(mdRaf); mdRaf = null; }
    if (e.name === 'AbortError') {
      if (mdBuf) {
        mdBox.innerHTML = md.render(mdBuf || '');
        if (!stopFlag) toast('⏹️ 已停止');
      } else {
        // 排队中取消/未收到任何内容：气泡给出明确提示，不留空白
        contentDiv.innerHTML = '';
        contentDiv.textContent = stopFlag ? '⏹️ 已停止' : '⏹️ 已取消（未开始执行）';
        if (!stopFlag) toast('⏹️ 已取消');
      }
    } else {
      toast('请求失败：' + e.message, true);
      contentDiv.innerHTML = '';
      contentDiv.textContent = '❌ ' + e.message;
    }
  } finally {
    clearWait();
    pendingCount--;
    abortCtrls = abortCtrls.filter((c) => c !== ctrl);
    // 还有请求在跑/排队时保持 busy（排队请求结束不提前恢复空闲 UI）
    state.busy = pendingCount > 0;
    setBusyUI(state.busy);
    $('#queueBar').classList.add('hidden');
    notifyDone(sendFailed ? '❌ 任务失败' : '✅ 任务完成', text.slice(0, 40));
    if (state.cur) {
      loadSessions();
      refreshSessionMeta();
      // 补绑：刚发送的用户消息在流式渲染时没有真实 ts（删除按钮需要 ts）。
      // 服务端持久化后回读，用带 ts/元数据的版本替换，让附件可单独删除。
      try {
        const s = await fetch('/api/session?id=' + encodeURIComponent(state.cur));
        const sd = await s.json();
        const msgs = (sd.session || {}).messages || [];
        const last = [...msgs].reverse().find((m) => m.role === 'user');
        if (last) {
          const users = box.querySelectorAll('.msg.user');
          const el = users[users.length - 1];
          if (el) {
            el.replaceWith(msgUser(last.content, last.ts, last.attachments));
          }
        }
        renderSessionTools(msgs);
      } catch (e) { /* 保留即时渲染即可 */ }
    }
  }
}

function handleFrame(f, botEl, thinkEl, ctx) {
  // 除「排队」外任何帧都说明模型已经在动：撤掉首 token 等待提示
  if (f.type !== 'queued' && ctx && ctx.clearWait) ctx.clearWait();
  switch (f.type) {
    case 'queued': {
      // 排队可视化：显示位置 + 可取消（取消 = 断开连接，服务端死连接检测自动退出排队）
      const bar = $('#queueBar');
      if (bar) {
        $('#queueText').textContent = `⏳ 已排队（第 ${f.position} 位）… 等待当前任务完成`;
        bar.classList.remove('hidden');
      }
      toast('已排队，等待当前任务完成…');
      break;
    }
    case 'think': {
      // 首个思考帧时创建折叠块（插到答案上方），默认折叠；思考内容累积在块内
      thinkEl = ensureThinkBlock(botEl, thinkEl);
      appendThink(thinkEl, f.text);
      break;
    }
    case 'delta':
      if (ctx && ctx.onDelta) ctx.onDelta(f.text);
      break;
    case 'tool_start': {
      // 工具开始执行：立即出现“⏳ 正在读取 …”，不用等执行完
      $('#queueBar').classList.add('hidden');
      addToolStart(botEl, f.name, f.args);
      break;
    }
    case 'tool': {
      $('#queueBar').classList.add('hidden');
      finishTool(botEl, f.name, f.ok, f.result, f.ms);
      appendSessionTool(f.name, f.ok, f.result, f.ms);
      break;
    }
    case 'todo': {
      renderTodoPanel(f.items || []);
      break;
    }
    case 'ask': {
      $('#queueBar').classList.add('hidden');
      showAskCard(botEl, f);
      break;
    }
    case 'confirm': {
      state.pendingConfirm = f.command;
      $('#queueBar').classList.add('hidden');
      showConfirm(f.command);
      break;
    }
    case 'result': {
      if (thinkEl) {
        // 答案就绪：保持折叠 + 保留字数统计；思考块没实质内容（只有"思考中…"占位）时移除
        if (!(thinkEl.querySelector('pre') || {}).textContent.trim()) {
          thinkEl.remove();
          thinkEl = null;
        } else {
          thinkEl.open = false;
        }
      }
      if (ctx && ctx.onDelta) {
        // 兜底：仅当本轮完全没收到流式正文帧时用整包响应渲染
        // （正常流式路径 deltas 已进 splitter，再推会造成内容双份）
        if (f.response && !ctx.gotDelta) {
          ctx.onDelta(f.response);
          if (ctx.flush) ctx.flush();
        }
      }
      // 耗时 + token 统计
      const meta = [];
      if (f.elapsed_s != null) meta.push(`⏱ ${f.elapsed_s}s`);
      if (f.usage && f.usage.total_tokens != null) {
        meta.push(`⚡ ${f.usage.prompt_tokens ?? '-'} / ${f.usage.completion_tokens ?? '-'} tokens`);
      }
      if (meta.length) {
        const mm = document.createElement('div');
        mm.className = 'msg-meta';
        mm.textContent = meta.join(' · ');
        botEl.appendChild(mm);
      }
      // 产物
      if (f.artifacts && f.artifacts.length) {
        const art = document.createElement('div');
        art.className = 'msg-art';
        art.innerHTML = ico('box') + ' 产出物：' + f.artifacts.map((a) =>
          `<a href="/api/file?path=${encodeURIComponent(a.rel)}" data-pv="${esc(a.rel)}" target="_blank">${esc(a.name)}</a>`
        ).join('');
        botEl.appendChild(art);
        renderArtifacts(f.artifacts, true);
      }
      if (f.session_id) {
        state.cur = f.session_id;
        try { localStorage.setItem('lastSession', state.cur); } catch (e) {}
      }
      // 上下文预算 / 任务清单 / 来源
      if (f.context) setCtxMeter(f.context);
      // 本轮多了一问一答：刷新“压缩”按钮可用状态
      state.msgCount = (Number(state.msgCount) || 0) + 2;
      refreshCompactBtn();
      if (f.todos) renderTodoPanel(f.todos);
      if (f.citations && f.citations.length && !botEl.querySelector('.cites')) {
        botEl.insertAdjacentHTML('beforeend', citationsHtml(f.citations));
      }
      // 未回答的提问卡在本轮结束时标为超时，避免用户事后点了没反应
      botEl.querySelectorAll('.ask-card:not(.done)').forEach((c) => {
        c.classList.add('done');
        c.innerHTML = '<div class="ask-done">⌛ 提问已超时，AI 已按最合理的假设继续</div>';
      });
      // 变更审阅卡片（本轮确实写了文件才显示）
      if (f.snapshot && !botEl.querySelector('.change-card')) {
        botEl.insertAdjacentHTML('beforeend', changeCardHtml(f.snapshot));
      }
      $('#queueBar').classList.add('hidden');
      a11ySay(f.stopped ? '回复已停止' : '回复完成'
               + (f.elapsed_s != null ? `，用时 ${f.elapsed_s} 秒` : ''));
      break;
    }
    case 'warn': {
      // 长任务提醒：接近单轮上限时后端主动报一声，用户可一键放宽看门狗
      // （离线大模型 prefill / 多轮工具调用跑几十秒到几分钟是常态）
      a11ySay(String(f.message || '任务耗时较长').slice(0, 120));
      const wb = document.createElement('div');
      wb.className = 'warn-box';
      wb.innerHTML = `<div class="warn-line">${ico('clock')} ${esc(f.message || '本轮耗时较长')}</div>`;
      if (f.extendable) {
        const eb = document.createElement('button');
        eb.className = 'btn small warn-extend';
        eb.textContent = '再等 5 分钟';
        eb.title = '放宽本轮的看门狗上限：服务器继续等待这一轮，不重跑不丢进度';
        eb.onclick = async () => {
          eb.disabled = true;
          eb.textContent = '延长中…';
          try {
            const r = await fetch('/api/extend', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ seconds: 300 }),
            });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
            if (d.extended) {
              eb.remove();
              wb.insertAdjacentHTML('beforeend',
                `<span class="warn-ok">✅ 已延长 ${Math.round((d.seconds || 300) / 60)} 分钟，继续等待…</span>`);
            } else {
              eb.remove();
              wb.insertAdjacentHTML('beforeend',
                '<span class="warn-ok">本轮已结束，无需延长</span>');
            }
          } catch (e) {
            eb.disabled = false;
            eb.textContent = '再等 5 分钟';
            toast('延长失败：' + e.message, true);
          }
        };
        wb.appendChild(eb);
      }
      const wActs = botEl.querySelector('.msg-acts');
      if (wActs) botEl.insertBefore(wb, wActs); else botEl.appendChild(wb);
      break;
    }
    case 'error': {
      a11ySay('任务出错：' + String(f.message || '').slice(0, 120));
      botEl.querySelectorAll('.tl-row.running').forEach((r) => {
        r.classList.remove('running');
        r.classList.add('err');
        r.querySelector('.tl-ico').innerHTML = ico('x');
      });
      refreshTimeline(botEl);
      // 一行结论 + 可展开详情 + 一键重试（U10/错误呈现）
      const cdiv = botEl.querySelector('.content');
      cdiv.innerHTML = `<div class="err-box">
        <div class="err-line">❌ ${esc(f.message || '任务失败')}</div>
        <details class="err-more"><summary>查看详情（日志 / 堆栈）</summary>
          <pre>${esc(f.detail || f.message || '')}</pre>
          <div class="err-hint">完整日志：设置 → 运行 → 查看最近日志（工作台 .agent/logs/）</div>
        </details>
        <button class="btn small err-retry" title="移除这条失败回复并重新发送上一条">${ico('retry')} 重试这一轮</button>
      </div>`;
      $('#queueBar').classList.add('hidden');
      toast(f.message, true);
      break;
    }
  }
  followScroll($('#messages'));
  syncRightPanel();
  return thinkEl;
}

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
    closeModal();
    await fetch('/api/confirm', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ approved: true }) });
  };
  body.querySelector('#cfNo').onclick = async () => {
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
  const r = await fetch('/api/session?id=' + encodeURIComponent(state.cur));
  const d = await r.json();
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

/* ── 记忆 ─────────────────────────────────────────────────── */
const state_mem = { facts: [], filter: { imp: 'all', src: 'all', tag: 'all', sort: 'new' } };

$('#btnMemory').onclick = async () => {
  $('#modalTitle').textContent = '记忆库';
  $('#modalBody').innerHTML = '<div class="modal-loading">加载中…</div>';
  openModal();
  try {
    const r = await fetch('/api/memory');
    const d = await r.json();
    state_mem.facts = d.facts || [];
    state_mem.prefs = d.prefs || {};
    renderMemory();
  } catch (e) {
    $('#modalBody').innerHTML = '<div class="muted" style="padding:18px;text-align:center">加载失败：' + esc(e.message) + '</div>';
  }
};

function renderMemory() {
  const body = $('#modalBody');
  const st = state_mem.filter;
  // 来源选项
  const srcs = ['all', ...new Set(state_mem.facts.map((f) => f.source || 'manual'))];
  // 标签选项（聚合去重）
  const allTags = [...new Set(state_mem.facts.flatMap((f) => f.tags || []))];
  let html = `
    <div class="mem-filter">
      <select id="mfImp">
        <option value="all" ${st.imp === 'all' ? 'selected' : ''}>全部</option>
        <option value="imp" ${st.imp === 'imp' ? 'selected' : ''}>⭐ 重要</option>
      </select>
      <select id="mfSrc">${srcs.map((s) =>
        `<option value="${esc(s)}" ${st.src === s ? 'selected' : ''}>${s === 'all' ? '来源:全部' : esc(s)}</option>`).join('')}</select>
      <select id="mfTag">
        <option value="all" ${st.tag === 'all' ? 'selected' : ''}>标签:全部</option>
        ${allTags.map((t) => `<option value="${esc(t)}" ${st.tag === t ? 'selected' : ''}>#${esc(t)}</option>`).join('')}
      </select>
      <select id="mfSort">
        <option value="new" ${st.sort === 'new' ? 'selected' : ''}>最新在前</option>
        <option value="old" ${st.sort === 'old' ? 'selected' : ''}>最旧在前</option>
      </select>
      <button class="btn small" id="mfAdd">＋ 添加记忆</button>
    </div>
    <div id="mfAddForm" class="hidden">
      <textarea id="mfNewText" rows="2" placeholder="要记住的内容…"></textarea>
      <label class="mode"><input type="checkbox" id="mfNewImp"> 标记为重要</label>
      <div class="btns"><button class="btn" id="mfAddCancel">取消</button>
      <button class="btn primary" id="mfAddSave">保存</button></div>
    </div>
    <h4>关键事实（${state_mem.facts.length}）</h4>`;
  let facts = state_mem.facts.slice();
  if (st.imp === 'imp') facts = facts.filter((f) => f.important);
  if (st.src !== 'all') facts = facts.filter((f) => (f.source || 'manual') === st.src);
  if (st.tag !== 'all') facts = facts.filter((f) => (f.tags || []).includes(st.tag));
  facts.sort((a, b) => st.sort === 'new'
    ? String(b.updated_at).localeCompare(String(a.updated_at))
    : String(a.updated_at).localeCompare(String(b.updated_at)));
  if (!facts.length) html += '<div class="muted">（暂无匹配记忆）</div>';
  for (const f of facts) {
    const imp = f.important ? '⭐' : '☆';
    const cls = f.important ? ' starred' : '';
    const time = (f.created_at || '').slice(0, 16).replace('T', ' ');
    const tagsHtml = (f.tags || []).map((t) =>
      `<span class="tag">#${esc(t)} <b class="x" data-tag="${esc(t)}">✕</b></span>`
    ).join('');
    html += `<div class="mem-item${cls}" data-id="${f.id}">
      <button class="star" title="${f.important ? '取消重要' : '标记重要'}">${imp}</button>
      <div class="mem-main">
        <span class="ct" title="点击编辑">${esc(f.content)}</span>
        <div class="mem-tags">${tagsHtml}<span class="tag-add" title="加标签">＋标签</span></div>
        <span class="src">${esc(f.source || 'manual')} · ${time} · 召回 ${f.access_count || 0} 次</span>
      </div>
      <button class="edit" title="编辑">${ico('edit')}</button>
      <button class="del" title="删除">${ico('trash')}</button>
    </div>`;
  }
  html += '<h4>用户偏好</h4>';
  const prefs = state_mem.prefs || {};
  if (!Object.keys(prefs).length) html += '<div class="muted">（暂无）</div>';
  for (const [k, v] of Object.entries(prefs)) {
    html += `<div class="mem-item"><span>${esc(k)}: ${esc(v)}</span></div>`;
  }
  body.innerHTML = html;

  // 事件绑定
  body.querySelector('#mfImp').onchange = (e) => { st.imp = e.target.value; renderMemory(); };
  body.querySelector('#mfSrc').onchange = (e) => { st.src = e.target.value; renderMemory(); };
  body.querySelector('#mfTag').onchange = (e) => { st.tag = e.target.value; renderMemory(); };
  body.querySelector('#mfSort').onchange = (e) => { st.sort = e.target.value; renderMemory(); };
  body.querySelector('#mfAdd').onclick = () => {
    body.querySelector('#mfAddForm').classList.remove('hidden');
  };
  body.querySelector('#mfAddCancel').onclick = () => {
    body.querySelector('#mfAddForm').classList.add('hidden');
  };
  body.querySelector('#mfAddSave').onclick = async () => {
    const t = body.querySelector('#mfNewText').value.trim();
    if (!t) return;
    await fetch('/api/memory/add', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: t, important: body.querySelector('#mfNewImp').checked }) });
    $('#btnMemory').click();
  };
  body.querySelectorAll('.star').forEach((b) => {
    b.onclick = async () => {
      const id = +b.closest('.mem-item').dataset.id;
      await fetch('/api/memory/important', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, important: b.textContent === '☆' }) });
      $('#btnMemory').click();
    };
  });
  // 标签：删除单个
  body.querySelectorAll('.mem-tags .x').forEach((b) => {
    b.onclick = async (e) => {
      e.stopPropagation();
      const item = b.closest('.mem-item');
      const id = +item.dataset.id;
      const f = state_mem.facts.find((x) => x.id === id);
      const rest = (f.tags || []).filter((t) => t !== b.dataset.tag);
      await fetch('/api/memory/tags', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, tags: rest }) });
      $('#btnMemory').click();
    };
  });
  // 标签：添加
  body.querySelectorAll('.tag-add').forEach((b) => {
    b.onclick = async () => {
      const id = +b.closest('.mem-item').dataset.id;
      const t = prompt('输入标签（如：项目 / 个人 / 偏好）');
      if (t && t.trim()) {
        await fetch('/api/memory/tags', { method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id, tag: t.trim() }) });
        $('#btnMemory').click();
      }
    };
  });
  body.querySelectorAll('.edit').forEach((b) => {
    b.onclick = () => {
      const item = b.closest('.mem-item');
      const id = +item.dataset.id;
      const old = item.querySelector('.ct').textContent;
      item.innerHTML = `<div class="mem-edit">
        <textarea rows="2">${esc(old)}</textarea>
        <div class="btns"><button class="btn" id="edCancel">取消</button>
        <button class="btn primary" id="edSave">保存</button></div></div>`;
      item.querySelector('#edCancel').onclick = () => renderMemory();
      item.querySelector('#edSave').onclick = async () => {
        const v = item.querySelector('textarea').value.trim();
        if (v) {
          await fetch('/api/memory/update', { method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id, content: v }) });
        }
        $('#btnMemory').click();
      };
    };
  });
  body.querySelectorAll('.del').forEach((b) => {
    b.onclick = async () => {
      const id = +b.closest('.mem-item').dataset.id;
      await fetch('/api/memory/delete', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }) });
      $('#btnMemory').click();
    };
  });
}

/* ── 工作台/文件浏览（懒加载树 + 分析入口）────────────────── */
/* 两处目录树（文件浏览 / 批量作业「选择目录」）共用同一套取数与错误处理 */
async function fetchTree(root, rel) {
  const r = await fetch('/api/tree?root=' + encodeURIComponent(root || 'ws') +
                        '&path=' + encodeURIComponent(rel || ''));
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || '加载失败');
  return d;
}
$('#btnWs').onclick = () => { state.treeRoot = 'ws'; openTree(); };

/* ── 目录选择组件（文件浏览 / 批量作业真正共用的那一个）──────
   一套交互：头部 = 切根 + 路径 + 上级 + 调用方自己的按钮，
   下面 = 子目录列表（逐层点进去）；文件列表由调用方在 onLoad 里渲染。*/
const wsDir = { root: 'ws', path: '', rootPath: '' };   // 文件浏览当前目录
const bjDir = { root: 'ws', path: '' };                 // 批量作业当前目录

async function dirPicker(box, st, opts) {
  opts = opts || {};
  box.innerHTML = '<div class="muted dp-loading">加载中…</div>';
  let d;
  try { d = await fetchTree(st.root, st.path); }
  catch (e) {
    box.innerHTML = `<div class="muted dp-loading">加载失败：${esc(e.message)}</div>`;
    return null;
  }
  st.path = d.path || '';
  st.rootPath = d.root || '';
  const hit = (opts.roots || []).find((r) => r[0] === st.root);
  const where = (hit ? hit[1] : '工作台') + (st.path ? '\\' + st.path : '');
  const dirs = (d.items || []).filter((x) => x.type === 'dir');
  box.innerHTML = `<div class="dp-head">
      ${opts.roots ? `<select class="dp-root" title="切换浏览起点">${opts.roots.map(([v, l]) =>
        `<option value="${v}"${v === st.root ? ' selected' : ''}>${l}</option>`).join('')}</select>` : ''}
      <span class="dp-path" title="${esc(d.root + '\\' + st.path)}">${ico('folder')} ${esc(where)}</span>
      <button class="btn small ghost" data-dp="up"${st.path ? '' : ' disabled'}>${ico('up')} 上级</button>
      ${opts.actions || ''}
    </div>
    <div class="dp-list">${dirs.length
      ? dirs.map((n) => `<div class="ws-row ws-dir" data-name="${esc(n.name)}">
          <span class="ic">${ico('folder')}</span><span class="nm">${esc(n.name)}</span></div>`).join('')
      : '<div class="muted dp-empty">（没有子目录）</div>'}</div>`;
  const rootSel = box.querySelector('.dp-root');
  if (rootSel) rootSel.onchange = (e) => {
    st.root = e.target.value; st.path = '';
    dirPicker(box, st, opts);
  };
  box.querySelector('[data-dp="up"]').onclick = () => {
    const i = st.path.lastIndexOf('/');
    st.path = i <= 0 ? '' : st.path.slice(0, i);
    dirPicker(box, st, opts);
  };
  box.querySelectorAll('.ws-dir').forEach((el) => {
    el.onclick = () => {
      st.path = (st.path ? st.path + '/' : '') + el.dataset.name;
      dirPicker(box, st, opts);
    };
  });
  if (opts.onLoad) opts.onLoad(d);
  return d;
}

/* ── 文件浏览：选目录（上面）+ 该目录的文件列表（下面）────── */
// 打开文件浏览，可选定位到某个目录（rel 为工作台内相对路径，空 = 根）
function openTree(rel) {
  $('#modalTitle').textContent = '文件浏览';
  $('#modalBody').innerHTML = `
    <div class="dir-picker" id="wsPicker"></div>
    <div class="tree-toolbar">
      <button class="btn small" id="treeBatch" disabled title="把勾选的文件放进一个新批量作业">批量作业 (0)</button>
      <span class="muted" id="treeTip">（点目录进下一层；点“分析”让 AI 查看该文件）</span>
    </div>
    <div class="ws-tree" id="treeBox"></div>`;
  openModal();
  wsDir.root = state.treeRoot;
  wsDir.path = rel || '';
  dirPicker($('#wsPicker'), wsDir, {
    roots: [['ws', '工作台'], ['home', '用户目录'], ['drive', '磁盘根']],
    onLoad: (d) => {
      // 同步回 state：分析入口与产出物「📂 目录」定位都读它
      state.treeRoot = wsDir.root;
      state.treePath = wsDir.path;
      state.treeRootPath = wsDir.rootPath;
      renderWsFiles(d);
    },
  });
}
// 产出物卡片「📂 目录」：打开文件浏览并定位到文件所在目录（仅工作台内有效）
function openTreeAt(dir) {
  state.treeRoot = 'ws';
  openTree(dir || '');
}

function renderWsFiles(d) {
  const box = $('#treeBox');
  if (!box) return;
  box.innerHTML = '';
  const inWs = wsDir.root === 'ws';
  const files = (d.items || []).filter((x) => x.type === 'file');
  for (const n of files) {
    const full = (wsDir.path ? wsDir.path + '/' : '') + n.name;
    const row = document.createElement('div');
    row.className = 'ws-row ws-file';
    row.innerHTML = (inWs
        ? `<input type="checkbox" class="tree-cb" data-rel="${esc(full)}"
             title="选入批量作业">`
        : '') +
      `<span class="ic">${artIcon((n.name.split('.').pop() || '').toLowerCase())}</span>
      <span class="nm" title="${esc(full)}">${esc(n.name)}</span>
      <span class="sz">${fmtSize(n.size)}</span>
      <span class="act" title="让 AI 分析此文件">${ico('search')} 分析</span>`;
    if (inWs) {
      row.title = '点击预览文件';
      row.onclick = () => window.open('/api/file?path=' + encodeURIComponent(full), '_blank');
    } else {
      row.onclick = () => toast('仅工作台内文件可预览', true);
    }
    row.querySelector('.act').onclick = (e) => {
      e.stopPropagation();
      $('#input').value = '分析文件：' + (state.treeRootPath || '') + '\\' + full;
      autoGrow($('#input'));
      $('#input').focus();
      closeModal();
      toast('已填入输入框，回车发送即可');
    };
    box.appendChild(row);
  }
  if (!files.length) {
    const m = document.createElement('div');
    m.className = 'muted';
    m.textContent = '（此目录没有文件）';
    box.appendChild(m);
  }
  // 多选文件 → 直接建批量作业（不再需要先开作业面板选目录）
  const tbtn = $('#treeBatch');
  const syncBatchBtn = () => {
    if (!tbtn) return;
    const n = box.querySelectorAll('.tree-cb:checked').length;
    tbtn.disabled = !n;
    tbtn.textContent = `批量作业 (${n})`;
  };
  box.querySelectorAll('.tree-cb').forEach((cb) => {
    cb.onclick = (e) => e.stopPropagation();   // 不触发行预览
    cb.onchange = syncBatchBtn;
  });
  if (tbtn) {
    tbtn.onclick = () => {
      const files = [...box.querySelectorAll('.tree-cb:checked')]
        .map((c) => c.dataset.rel).filter(Boolean);
      if (!files.length) return;
      closeModal();
      openBatch(files);
    };
  }
  syncBatchBtn();
}

/* ── 设置 ─────────────────────────────────────────────────── */
$('#btnSettings').onclick = async () => {
  $('#modalTitle').textContent = '设置';
  $('#modalBody').innerHTML = '<div class="modal-loading">加载中…</div>';
  openModal();
  const r = await fetch('/api/settings');
  const d = await r.json();
  $('#modalBody').innerHTML = `
    <div class="st-tabs">
      <button class="st-tab on" data-p="model">${ico('plug')} 模型与提示词</button>
      <button class="st-tab" data-p="rag">${ico('search')} 检索</button>
      <button class="st-tab" data-p="run">${ico('gear')} 运行与外观</button>
      <button class="st-tab" data-p="sec">${ico('shield')} 安全与数据</button>
      <button class="st-tab" data-p="mcp">${ico('tool')} 外接 MCP</button>
    </div>
    <div class="st-pane on" data-p="model">
    <h4>模型服务（保存后立即生效，无需重启）</h4>
    <div class="st-callout" id="stModelHost" role="status"></div>
    <label>Base URL<input type="text" id="stUrl" value="${esc(d.model.base_url || '')}" placeholder="http://模型主机内网IP:端口/v1"></label>
    <div class="st-callout security-callout">
      <b>局域网共享安全说明</b>：持有共享访问令牌的设备共用同一工作台、会话和服务器文件权限；这不是多用户隔离。
      命令、Python 与 MCP 在工作台服务器上以当前操作系统用户权限运行，并非沙箱。仅在可信 LAN 中使用；不可信场景请关闭代码执行。
    </div>
    <label>模型名<input type="text" id="stModel" value="${esc(d.model.name || '')}" placeholder="qwen"></label>
    <label>API Key（留空表示不变，当前：${esc(d.model.key_masked || '未设置')}）
      <input type="password" id="stKey" placeholder="输入新 key 才会替换"></label>
    <label>思考过程显示（Qwen3 等模型会把思考写进正文）
      <select id="stThinking">
        <option value="collapse">折叠显示（推荐）</option>
        <option value="hide">完全隐藏（并请求模型服务端关闭思考）</option>
        <option value="keep">保留原文</option>
      </select></label>
    <label>单次输出上限（tokens，0=服务端默认；长回答/SQL 被截断时调大）
      <input type="number" id="stMaxTokens" value="${esc((d.model || {}).max_tokens ?? 0)}" min="0" step="256"></label>
    <label>上下文窗口（tokens，如 8192 / 32768；接近 70% 自动压缩历史，0=只按字符阈值）
      <input type="number" id="stCtx" value="${esc((d.model || {}).context_window ?? 0)}" min="0" step="1024"></label>
    <label class="mode"><input type="checkbox" id="stVision"> 模型支持图片输入（多模态；开启后附件图片会直接给模型看）</label>
    <div class="btns">
      <button class="btn" id="stTest">${ico('plug')} 测试连接</button>
    </div>
    <div id="stTestOut" class="muted" style="margin-top:6px"></div>

    <h4>常用 Prompt 模板（输入框打 /模板名 一键填入）</h4>
    <div id="stTplList"></div>
    <div class="btns"><button class="btn small" id="stTplAdd">＋ 添加模板</button>
      <button class="btn small" id="stTplSave">${ico('save')} 保存模板</button></div>

    <h4>角色与铁律（自定义提示词，注入每轮系统提示词）</h4>
    <label>角色设定（AI 的身份与职责，留空用默认）
      <textarea id="stRole" rows="3" placeholder="你是…（例如：你是用户的内网 SQL 专家，专注数据库开发、回滚脚本生成与优化）"></textarea></label>
    <label>铁律（每行一条；AI 反复犯同类错误时追加，会注入系统提示词强制遵守）
      <textarea id="stRules" rows="4" placeholder="例如：
生成的回退 SQL 必须完整包含原表所有字段
任何情况下都不得删除数据库，只能生成回退语句"></textarea></label>

    <h4>项目指令（AGENTS.md）</h4>
    <label>工作台根目录的 AGENTS.md：针对当前项目 / 客户的规则，每轮自动注入（优先级高于通用说明）
      <textarea id="stAgents" rows="6" placeholder="例如：
- 生成的回退 SQL 必须包含原表所有字段
- 周报固定用「本周完成 / 下周计划 / 风险」三段式
- 所有金额一律保留两位小数"></textarea></label>
    <div class="btns"><button class="btn small" id="stAgentsSave">${ico('save')} 保存项目指令</button>
      <span class="muted" id="stAgentsInfo"></span></div>
    </div>

    <div class="st-pane" data-p="rag">
    <h4>本地资料检索（RAG）</h4>
    <label class="mode"><input type="checkbox" id="stRag"> 提问时自动检索 inputs/notes 的资料</label>
    <div class="tree-toolbar" style="margin-top:6px">
      <span class="muted">检索条数</span>
      <select id="stTopK"><option>2</option><option>4</option><option>6</option><option>8</option></select>
      <button class="btn small" id="stRebuild">${ico('refresh')} 重建索引</button>
      <span class="muted" id="stRagInfo"></span>
    </div>
    </div>

    <div class="st-pane" data-p="run">
    <h4>工作台目录</h4>
    <label>根目录（inputs / outputs / notes / .agent 都在它下面；改完需重启工作台生效）
      <input type="text" id="stWsRoot" value="${esc((d.workspace || {}).root || '')}"></label>
    <div class="muted" id="stWsHint">${(d.workspace || {}).restart_required
      ? '⚠ 已保存新路径，重启工作台后生效' : ''}</div>

    <h4>运行参数</h4>
    <label>历史压缩阈值（字符，会话历史超过后自动压缩，0=关闭）
      <input type="number" id="stCompress" value="${esc((d.limits || {}).compress_threshold_chars ?? 0)}" min="0" step="1000"></label>
    <label>单任务超时（秒）<input type="number" id="stTimeout" value="${esc((d.limits || {}).chat_timeout_s ?? 600)}" min="10"></label>
    <label>单任务最大轮次（小模型任务复杂时可调大）<input type="number" id="stMaxTurns" value="${esc((d.limits || {}).max_turns ?? 25)}" min="1" max="100"></label>
    <label>上传上限（MB）<input type="number" id="stUpload" value="${esc((d.limits || {}).max_upload_mb ?? 50)}" min="1"></label>
    <label>模型超时（秒）—— 内网大模型首 token（prefill）常要 20~60 秒，默认 300 防误杀
      <input type="number" id="stLlmTimeout" value="${esc((d.limits || {}).llm_timeout_s ?? 300)}" min="10" step="10"></label>
    <label class="mode"><input type="checkbox" id="stMemExtract"> 后台自动提取记忆（约每 6 条消息调一次模型；单卡/单槽离线模型抢槽变慢时可关掉）</label>

    <h4>外观</h4>
    <div class="tree-toolbar">
      <span class="muted">主题</span>
      <button class="btn small" id="stTheme">${document.documentElement.getAttribute('data-theme') === 'dark' ? '🌙 深色' : '☀️ 浅色'}</button>
      <span class="muted">点一下在深/浅色间切换（立即生效，记在本机）</span>
    </div>
    <details class="logs-box"><summary>${ico('log')} 查看最近日志</summary>
      <pre id="stLogView">展开后加载…</pre></details>
    </div>

    <div class="st-pane" data-p="sec">
    <h4>安全与审批档位</h4>
    <label>审批档位（决定 AI 写文件 / 执行命令前要不要先问你）
      <select id="stProfile">
        <option value="readonly">只读 —— 禁止写文件与执行命令</option>
        <option value="suggest">建议（推荐）—— 读取自动；outputs/notes 写入自动；其余逐条确认</option>
        <option value="auto_edit">自动编辑 —— 写文件全自动；命令逐条确认</option>
        <option value="trusted">可信 —— 全部自动放行（危险命令仍会确认）</option>
      </select></label>
    <div class="aa-box">
      <div class="aa-t">「建议」档下的自动放行范围：</div>
      <label class="mode"><input type="checkbox" id="stAaOut"> outputs/ 内新建 / 覆盖</label>
      <label class="mode"><input type="checkbox" id="stAaNotes"> notes/ 内写入</label>
      <label class="mode"><input type="checkbox" id="stAaDel"> 删除 / 移动文件（危险）</label>
      <label class="mode"><input type="checkbox" id="stAaCmd"> 其余命令也自动放行（只拦危险命令）</label>
    </div>
    <label class="mode"><input type="checkbox" id="stCodeExec"> 允许 AI 执行命令 / 运行代码（关闭后 AI 只能读写文件，不能跑命令）</label>
    <div class="muted" id="stAllow"></div>
    <div class="btns"><button class="btn small" id="stAllowClear">清除记住的命令前缀</button></div>

    <h4>任务与备份</h4>
    <div id="stDeps" class="muted"></div>
    <div id="stJobs" class="muted"></div>
    <div class="btns">
      <a class="btn" href="/api/backup">${ico('box')} 备份工作台（zip）</a>
      <span class="muted" id="stWs"></span>
    </div>
    </div>

    <div class="st-pane" data-p="mcp">
    <h4>MCP 服务（stdio 子进程）</h4>
    <div id="mcpList" class="mcp-list"><div class="muted">加载中…</div></div>
    <details class="mcp-add" id="mcpAddBox">
      <summary>＋ 添加 / 编辑 MCP 服务</summary>
      <div class="mcp-form">
        <div class="mcp-tpls">
          <span class="muted">一键模板：</span>
          <button type="button" class="btn small ghost mcp-tpl" data-tpl="filesystem" title="本地文件读写（Node.js）">文件系统</button>
          <button type="button" class="btn small ghost mcp-tpl" data-tpl="fetch" title="抓取网页转 Markdown（uv）">网页抓取</button>
          <button type="button" class="btn small ghost mcp-tpl" data-tpl="sqlite" title="查询 SQLite 数据库（uv）">SQLite</button>
          <span class="muted">填入后按需改路径/命令，再点保存并连接</span>
        </div>
        <label>服务名（工具名前缀变成 <code>mcp__名字__工具</code>，字母/数字/中文/._-，不能含 __）
          <input id="mcpName" type="text" placeholder="filesystem"></label>
        <label>启动命令（可执行文件名或绝对路径）
          <input id="mcpCmd" type="text" placeholder="npx（Windows 也可写 npx.cmd 或完整路径）"></label>
        <label>参数（空格分隔；含空格的路径用双引号包起来）
          <input id="mcpArgs" type="text" placeholder='-y @modelcontextprotocol/server-filesystem "D:\\资料"'></label>
        <label>环境变量（KEY=VALUE，每行一条，可空；只对这个子进程生效）
          <textarea id="mcpEnv" rows="2" placeholder="API_KEY=xxx"></textarea></label>
        <div class="bf-row">
          <label class="mcp-num">超时（秒，默认 20）
            <input id="mcpTimeout" type="number" value="20" min="1" max="600"></label>
          <label class="mode"><input type="checkbox" id="mcpEnabled" checked> 启用（保存后自动连接）</label>
        </div>
        <div class="btns">
          <button class="btn small primary" id="mcpSave">保存并连接</button>
          <button class="btn small ghost" id="mcpAddReset">清空表单</button>
          <span class="muted" id="mcpFormTip"></span>
        </div>
      </div>
    </details>
    <div class="muted mcp-help">
      <b>怎么用：</b>填好上面表单 →「保存并连接」；连上后它的工具会出现在 AI 的工具列表里（修名 <code>mcp__名字__工具</code>），
      你在对话里说“用文件系统看看 D:\\资料”它就会自己去调。点某一行的「编辑 / 删除」可以改配置或移除；
      绿色圆点=已连上，红色=启动失败（看行内错误，常见是没装 node / 包名拼错）。<br>
      同样内容也写在 <code>config.json</code> 的 <code>mcp.servers</code>：
      <code>{name, command, args, env, enabled, timeout}</code>，两边是同一份配置。
      离线机需预置对应运行时（node / python）与依赖；
      MCP 工具安全边界更大，<b>每次调用都会逐条向你确认</b>（只读模式下直接禁用）。
    </div>
    </div>
    <div class="btns">
      <button class="btn primary" id="stSave">${ico('save')} 保存并应用</button>
      <button class="btn" id="stClose">关闭</button>
    </div>`;
  $('#stRag').checked = !!(d.rag || {}).enabled;
  $('#stTopK').value = String((d.rag || {}).top_k || 4);
  // 索引状态：已索引多少 + 是否有文件变更未入索引（“索引已过期 N 个”）
  fetch('/api/rag/stats').then((r) => r.json()).then((s) => {
    const info = $('#stRagInfo');
    if (!info) return;
    info.textContent = s.pending
      ? `⚠ 索引已过期 ${s.pending} 个文件（重建后生效）`
      : (s.chunks ? `已索引 ${s.docs} 个文件 / ${s.chunks} 块` : '尚无索引');
  }).catch(() => {});
  const modelHost = $('#stModelHost');
  const updateModelHost = () => {
    const raw = $('#stUrl').value.trim();
    let hostLabel = '地址未填写';
    let local = false;
    try {
      const u = new URL(raw);
      hostLabel = u.host;
      local = u.hostname === 'localhost' || u.hostname === '127.0.0.1' ||
        u.hostname === '::1' || /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(u.hostname);
    } catch (e) { if (raw) hostLabel = '地址格式待检查'; }
    if (modelHost) {
      modelHost.classList.toggle('local', local);
      modelHost.innerHTML = `${local ? '● 内网 / 本机地址' : '● 非典型内网地址'}：<b>${esc(hostLabel)}</b>` +
        (local ? '。此模式适合断网运行（确认模型服务已预加载）。'
          : '。离线断网时不可达；请填写模型主机的局域网 IP 与端口。');
    }
  };
  $('#stUrl').addEventListener('input', updateModelHost);
  updateModelHost();
  $('#stThinking').value = (d.model || {}).thinking || 'collapse';
  $('#stMaxTokens').value = (d.model || {}).max_tokens ?? 0;
  $('#stCtx').value = (d.model || {}).context_window ?? 0;
  $('#stVision').checked = !!(d.model || {}).vision;
  $('#stMaxTurns').value = (d.limits || {}).max_turns ?? 25;
  $('#stLlmTimeout').value = (d.limits || {}).llm_timeout_s ?? 300;
  $('#stMemExtract').checked = (d.memory || {}).extract !== false;
  // 审批档位（B4）
  const sec = d.security || {};
  const aa = sec.auto_approve || {};
  $('#stProfile').value = sec.profile || 'suggest';
  $('#stAaOut').checked = aa.write_outputs !== false;
  $('#stAaNotes').checked = aa.write_notes !== false;
  $('#stAaDel').checked = !!aa.delete_move;
  $('#stAaCmd').checked = !!aa.run_other_cmds;
  const sa = sec.session_allow || [];
  $('#stAllow').textContent = sa.length
    ? `本次运行已记住 ${sa.length} 个命令前缀：${sa.join('、')}`
    : '本次运行尚未记住任何命令（批准后自动记住，重启清空）';
  $('#stAllowClear').onclick = async () => {
    await fetch('/api/settings', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ security: { session_allow: [] } }) });
    $('#stAllow').textContent = '本次运行尚未记住任何命令（批准后自动记住，重启清空）';
    toast('已清除记住的命令前缀');
  };
  $('#stRole').value = (d.prompt || {}).role || '';
  $('#stRules').value = ((d.prompt || {}).rules || []).join('\n');
  $('#stCodeExec').checked = !!(d.security || {}).allow_code_exec;
  $('#stWs').textContent = (d.workspace || {}).root || '';
  $('#stClose').onclick = closeModal;
  // 设置页 Tab（U8）：长表单分段，不用一路滚到底
  $('#modalBody').querySelectorAll('.st-tab').forEach((t) => {
    t.onclick = () => {
      $('#modalBody').querySelectorAll('.st-tab').forEach((x) =>
        x.classList.toggle('on', x === t));
      $('#modalBody').querySelectorAll('.st-pane').forEach((p) =>
        p.classList.toggle('on', p.dataset.p === t.dataset.p));
      $('#modalBody').scrollTop = 0;
    };
  });
  // 从上下文胶囊的「调整上下文设置」进来时，直接落到「运行」页
  if (state.settingsTab) {
    const want = $('#modalBody').querySelector(`.st-tab[data-p="${state.settingsTab}"]`);
    state.settingsTab = null;
    if (want) want.click();
  }
  const themeBtn = $('#stTheme');
  if (themeBtn) themeBtn.onclick = toggleTheme;
  // 可选依赖 / 日志：离线机排查问题的两个入口，不再只能看启动控制台
  fetch('/api/health').then((r) => r.json()).then((hd) => {
    const labels = { 'python-docx': 'Word', openpyxl: 'Excel', 'python-pptx': 'PPT', pypdf: 'PDF', Pillow: '图片' };
    const deps = hd.deps || {};
    $('#stDeps').innerHTML = '依赖：' + Object.keys(labels).map((k) =>
      `${esc(labels[k])} ${deps[k] ? '✅' : '⚠️'}`).join(' · ');
  }).catch(() => {});
  const logBox = document.querySelector('.logs-box');
  if (logBox) logBox.ontoggle = async () => {
    if (!logBox.open) return;
    try {
      const r = await fetch('/api/logs?n=300');
      const ld = await r.json();
      $('#stLogView').textContent = (ld.lines || []).join('\n') || '（暂无日志）';
    } catch (e) {
      $('#stLogView').textContent = '加载失败：' + e.message;
    }
  };
  // 项目指令：读 AGENTS.md（与后端注入用的是同一份文件）
  fetch('/api/project').then((r) => r.json()).then((pd) => {
    const f = (pd.files || []).find((x) => x.rel === 'AGENTS.md');
    $('#stAgents').value = f ? (f.content || '') : '';
  }).catch(() => {});
  $('#stAgentsSave').onclick = async () => {
    const info = $('#stAgentsInfo');
    info.textContent = '保存中…';
    try {
      const r = await fetch('/api/project', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rel: 'AGENTS.md', content: $('#stAgents').value }),
      });
      const pd = await r.json();
      if (!r.ok) throw new Error(pd.error || '保存失败');
      info.textContent = `✅ 已保存（${pd.chars} 字符，下一轮生效）`;
    } catch (e) {
      info.textContent = '❌ ' + e.message;
    }
  };

  $('#stTest').onclick = async () => {
    const out = $('#stTestOut');
    out.textContent = '测试中…';
    const r = await fetch('/api/test_llm', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_url: $('#stUrl').value.trim(),
        model: $('#stModel').value.trim(), api_key: $('#stKey').value.trim() }) });
    const t = await r.json();
    out.textContent = t.ok
      ? `✅ 连接成功（${t.elapsed_s}s）：${t.content}`
      : '❌ ' + (t.error || '测试失败');
  };
  state.wsRoot = (d.workspace || {}).root || '';
  renderMcpList(d.mcp || []);
  $('#mcpSave').onclick = mcpFormSave;
  $('#mcpAddReset').onclick = () => mcpFillForm(null);
  document.querySelectorAll('.mcp-tpl').forEach((b) => {
    b.onclick = () => mcpApplyTemplate(b.dataset.tpl);
  });
  $('#stSave').onclick = async () => {
    const r = await fetch('/api/settings', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: { base_url: $('#stUrl').value.trim(), name: $('#stModel').value.trim(),
                 api_key: $('#stKey').value.trim(),
                 thinking: $('#stThinking').value || 'collapse',
                 max_tokens: +$('#stMaxTokens').value || 0,
                 context_window: +$('#stCtx').value || 0,
                 vision: $('#stVision').checked },
        limits: { chat_timeout_s: +$('#stTimeout').value || 600,
                  max_turns: +$('#stMaxTurns').value || 25,
                  max_upload_mb: +$('#stUpload').value || 50,
                  compress_threshold_chars: +$('#stCompress').value || 0,
                  llm_timeout_s: +$('#stLlmTimeout').value || 300 },
        memory: { extract: $('#stMemExtract').checked },
        rag: { enabled: $('#stRag').checked, top_k: +$('#stTopK').value || 4 },
        security: { allow_code_exec: $('#stCodeExec').checked,
                     profile: $('#stProfile').value,
                     auto_approve: { write_outputs: $('#stAaOut').checked,
                                     write_notes: $('#stAaNotes').checked,
                                     delete_move: $('#stAaDel').checked,
                                     run_other_cmds: $('#stAaCmd').checked } },
        workspace: { root: (($('#stWsRoot') || {}).value || '').trim() },
        prompt: { role: $('#stRole').value.trim(),
                  rules: $('#stRules').value.split('\n').map((s) => s.trim()).filter(Boolean) },
        mcp: [...document.querySelectorAll('.mcp-cb')]
               .map((c) => ({ name: c.dataset.name, enabled: c.checked })) }) });
    const d = await r.json();
    if (!r.ok) return toast(d.error || '保存失败', true);
    const wsr = d.workspace || {};
    if (wsr.restart_required) {
      toast('⚠ 设置已保存；工作台目录需重启后生效', true);
      const hint = $('#stWsHint');
      if (hint) hint.textContent = '⚠ 已保存新路径，重启工作台后生效';
    } else {
      toast('✅ 设置已保存并生效');
    }
    $('#stKey').value = '';
    $('#stTestOut').textContent = '';
  };
  $('#stRebuild').onclick = async () => {
    const info = $('#stRagInfo');
    info.textContent = '重建中…';
    const r = await fetch('/api/rag/index');
    const d = await r.json();
    info.textContent = `✅ ${d.docs} 个文件 / ${d.chunks} 块（新增 ${d.new}，更新 ${d.updated}，移除 ${d.removed}）`;
  };
  // 常用模板
  let tpls = [];
  async function tplLoad() {
    try {
      const tr = await fetch('/api/templates');
      tpls = ((await tr.json()).templates || []).slice();
    } catch (e) { tpls = []; }
    tplRender();
  }
  function tplRender() {
    const box = $('#stTplList');
    box.innerHTML = tpls.length ? tpls.map((t, i) =>
      `<div class="tpl-item"><input class="tpl-name" value="${esc(t.name)}" placeholder="模板名（如 周报）">
        <textarea class="tpl-ct" rows="2" placeholder="模板内容（输入框打 /模板名 填入）">${esc(t.content)}</textarea>
        <button class="btn small ghost tpl-del" data-i="${i}">删除</button></div>`
    ).join('') : '<div class="muted">（暂无模板，点「＋ 添加模板」）</div>';
    box.querySelectorAll('.tpl-del').forEach((b) => {
      b.onclick = () => { tpls.splice(+b.dataset.i, 1); tplRender(); };
    });
  }
  $('#stTplAdd').onclick = () => { tpls.push({ name: '', content: '' }); tplRender(); };
  $('#stTplSave').onclick = async () => {
    const out = [];
    $('#stTplList').querySelectorAll('.tpl-item').forEach((el) => {
      out.push({ name: el.querySelector('.tpl-name').value.trim(),
                 content: el.querySelector('.tpl-ct').value.trim() });
    });
    const r = await fetch('/api/templates', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ templates: out }) });
    const d = await r.json();
    if (!r.ok) return toast(d.error || '保存失败', true);
    tpls = d.templates || [];
    tplRender();
    toast('✅ 模板已保存，输入框打 /' + (tpls[0] ? tpls[0].name : '模板名') + ' 可填入');
  };
  tplLoad();

  // 最近任务
  const jr = await fetch('/api/jobs');
  const jd = await jr.json();
  const jobs = (jd.jobs || []).slice(0, 5);
  $('#stJobs').innerHTML = '<b>最近任务</b>' + (jd.queue ? `<span class="tag">排队中 ${jd.queue}</span>` : '') +
    (jobs.length ? '<ul>' + jobs.map((j) =>
      `<li>${esc(j.done_at || '')} ${j.ok ? '✅' : '❌'} ${esc(j.message || '').slice(0, 40)}（${j.elapsed_s || 0}s）</li>`
    ).join('') + '</ul>' : '<div>（暂无）</div>');
  openModal();
};

/* ── 搜索会话（可复用：侧栏按钮 / 侧栏搜索回车 / 空态入口）── */
function openSearchModal(prefill) {
  $('#modalTitle').textContent = '搜索会话';
  $('#modalBody').innerHTML = `
    <input type="text" id="srchQ" placeholder="输入关键词搜索全部会话内容…">
    <div id="srchRes" class="srch-res"></div>`;
  openModal();
  const q = $('#srchQ');
  if (prefill) q.value = prefill;
  q.focus();
  q.setSelectionRange(q.value.length, q.value.length);
  let timer = null;
  q.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const v = q.value.trim();
      if (!v) { $('#srchRes').innerHTML = ''; return; }
      const r = await fetch('/api/search?q=' + encodeURIComponent(v));
      const d = await r.json();
      const res = d.results || [];
      $('#srchRes').innerHTML = res.length ? res.map((x, i) =>
        `<div class="srch-item" data-i="${i}">
          <div class="srch-title">${x.role === 'user' ? '🧑' : '🤖'} ${esc(x.title)} <span class="muted">${esc(x.role || '')}</span></div>
          <div class="srch-snip">${esc(x.snippet)}</div>
        </div>`).join('') : '<div class="muted">（无结果）</div>';
      $('#srchRes').querySelectorAll('.srch-item').forEach((el) => {
        el.onclick = () => { closeModal(); selectSession(res[+el.dataset.i].sid); };
      });
    }, 250);
  };
  if (prefill) q.dispatchEvent(new Event('input'));
}
const btnSearchEl = $('#btnSearch');
if (btnSearchEl) btnSearchEl.onclick = () => openSearchModal('');

/* ── 导出当前会话 ─────────────────────────────────────────── */
$('#btnExport').onclick = () => {
  if (!state.cur) return toast('请先选择要导出的会话', true);
  const a = document.createElement('a');
  a.href = '/api/export_session?id=' + encodeURIComponent(state.cur);
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
};
$('#selAll').onclick = () => {
  const cbs = document.querySelectorAll('#messages .sel-cb');
  const allOn = [...cbs].every((c) => c.checked);
  cbs.forEach((c) => { c.checked = !allOn; });
  state.selIdx.clear();
  if (!allOn) cbs.forEach((c, i) => state.selIdx.add(i));
  document.querySelectorAll('#messages .msg').forEach((el, i) => el.classList.toggle('sel-on', !allOn));
  selBarUpdate();
};
$('#selExport').onclick = exportSelectedMd;
$('#selCancel').onclick = exitSelectMode;

/* ── MCP 服务：列表 + 界面增删改（不用再手改 config.json）──── */
const MCP_ST = { connected: '已连接', starting: '连接中', error: '连接失败', stopped: '未启动' };
// 一键模板：只填表，不自动保存（路径/命令还能改），包名取自官方 reference servers
const MCP_TEMPLATES = {
  // 超时给到 90s：首次运行 npx/uvx 要先下载包，30s 会握手超时（下载完可改回 20–30）
  filesystem: (root) => ({
    name: 'filesystem', command: 'npx', timeout: 90,
    args: ['-y', '@modelcontextprotocol/server-filesystem', root || '.'],
    hint: '需要 Node.js（npx）。默认把当前工作台目录给 AI 读写，可改成别的路径；首次会下载包（已在下方把超时设为 90s）。',
  }),
  fetch: () => ({
    name: 'fetch', command: 'uvx', timeout: 90,
    args: ['mcp-server-fetch'],
    hint: '需要 uv（pip install uv）。只负责抓网页转 Markdown，不碰本地文件；首次会下载包（超时已设 90s）。',
  }),
  // 官方 Python 版 mcp-server-sqlite 已归档，跟新版 mcp 包不兼容（list_resources 报错），
  // 换维护中的 npm 版（数据库路径是位置参数）。
  sqlite: (root) => ({
    name: 'sqlite', command: 'npx', timeout: 90,
    args: ['-y', 'mcp-server-sqlite-npx', (root || '.') + '/notes.db'],
    hint: '需要 Node.js（npx）且首次能联网下载包（可能较慢）。默认库放工作台根目录的 notes.db，可改成现有库路径。',
  }),
};

function mcpRowsHtml(ms) {
  if (!ms.length) return '<div class="muted">（还没有 MCP 服务，下面点「添加 / 编辑」）</div>';
  return ms.map((s) => `
    <div class="mcp-row" data-name="${esc(s.name)}">
      <div class="mcp-l1">
        <label class="mode"><input type="checkbox" class="mcp-cb"
          data-name="${esc(s.name)}" ${s.enabled ? 'checked' : ''}> <b>${esc(s.name)}</b></label>
        <span class="mcp-st ${esc(s.status)}">${MCP_ST[s.status] || esc(s.status)}</span>
        <span class="muted">${(s.tools || []).length} 个工具</span>
        <span class="mcp-acts">
          <button class="btn small ghost mcp-re" data-name="${esc(s.name)}">重连</button>
          <button class="btn small ghost mcp-edit" data-name="${esc(s.name)}">编辑</button>
          <button class="btn small ghost mcp-del" data-name="${esc(s.name)}">删除</button>
        </span>
      </div>
      <code class="mcp-cmd">${esc(s.command)}${(s.args || []).length ? ' ' + esc(s.args.join(' ')) : ''}</code>
      ${(s.tools || []).length ? `<div class="muted mcp-tools" title="${esc((s.tools || []).map((x) => x.name).join(', '))}">${esc((s.tools || []).map((x) => x.name).join(' · '))}</div>` : ''}
      ${(s.resources || []).length ? `<details class="mcp-extra"><summary>资源（${s.resources.length}）</summary>` +
        s.resources.map((r) => `<div class="mcp-item">` +
          `<a href="#" class="mcp-r" data-server="${esc(s.name)}" data-uri="${esc(r.uri)}">${esc(r.name || r.uri)}</a>` +
          `<span class="muted">${esc(r.uri)}</span></div>`).join('') +
        `<pre class="mcp-out hidden"></pre></details>` : ''}
      ${(s.prompts || []).length ? `<details class="mcp-extra"><summary>提示词（${s.prompts.length}）</summary>` +
        s.prompts.map((p) => `<div class="mcp-item">` +
          `<a href="#" class="mcp-p" data-server="${esc(s.name)}" data-name="${esc(p.name)}">${esc(p.name)}</a>` +
          `<span class="muted">${esc(p.description || '')}</span></div>`).join('') +
        `</details>` : ''}
      ${s.error ? `<div class="mcp-err">${esc(s.error)}</div>` : ''}
    </div>`).join('');
}

function renderMcpList(ms) {
  const mlist = $('#mcpList');
  if (!mlist) return;
  state.mcpList = ms || [];
  mlist.innerHTML = mcpRowsHtml(state.mcpList);
  wireMcpList(mlist);
}

function wireMcpList(mlist) {
  // 启用开关
  mlist.querySelectorAll('.mcp-cb').forEach((c) => {
    c.onchange = async () => {
      try {
        const r = await fetch('/api/settings', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mcp: [{ name: c.dataset.name, enabled: c.checked }] }),
        });
        const jd = await r.json();
        if (!r.ok) throw new Error(jd.error || '保存失败');
        toast(c.checked ? `✅ 已启用 MCP：${c.dataset.name}` : `已停用 MCP：${c.dataset.name}`);
      } catch (e) { toast('操作失败：' + e.message, true); c.checked = !c.checked; }
    };
  });
  // 资源：点开看内容；提示词：一键填进输入框
  mlist.querySelectorAll('.mcp-r').forEach((a) => {
    a.onclick = async (e) => {
      e.preventDefault();
      const det = a.closest('.mcp-extra');
      const out = det && det.querySelector('.mcp-out');
      if (out) { out.classList.remove('hidden'); out.textContent = '读取中…'; }
      try {
        const r = await fetch('/api/mcp/resource', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ server: a.dataset.server, uri: a.dataset.uri }),
        });
        const jd = await r.json();
        if (!r.ok) throw new Error(jd.error || '读取失败');
        if (out) out.textContent = jd.text || '（空）';
      } catch (err) {
        if (out) out.textContent = '读取失败：' + err.message;
      }
    };
  });
  mlist.querySelectorAll('.mcp-p').forEach((a) => {
    a.onclick = async (e) => {
      e.preventDefault();
      try {
        const r = await fetch('/api/mcp/prompt', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ server: a.dataset.server, name: a.dataset.name }),
        });
        const jd = await r.json();
        if (!r.ok) throw new Error(jd.error || '读取失败');
        const ta = $('#input');
        if (ta) {
          ta.value = jd.text || '';
          ta.dispatchEvent(new Event('input', { bubbles: true }));
        }
        closeModal();
        if (ta) ta.focus();
        toast('已填入 MCP 提示词：' + a.dataset.name);
      } catch (err) { toast('读取提示词失败：' + err.message, true); }
    };
  });
  // 重连
  mlist.querySelectorAll('.mcp-re').forEach((b) => {
    b.onclick = async () => {
      b.disabled = true; b.textContent = '重连中…';
      try {
        const r = await fetch('/api/mcp/reload', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: b.dataset.name }),
        });
        const jd = await r.json();
        const st = ((jd.mcp || []).find((x) => x.name === b.dataset.name) || {}).status;
        toast(st === 'connected' ? `✅ ${b.dataset.name} 已连接` : `❌ ${b.dataset.name} 重连失败`, st !== 'connected');
        renderMcpList(jd.mcp || state.mcpList);
      } catch (e) { toast('重连失败：' + e.message, true); }
      b.disabled = false; b.textContent = '重连';
    };
  });
  // 编辑 / 删除（界面化配置）
  mlist.querySelectorAll('.mcp-edit').forEach((b) => {
    b.onclick = () => mcpFillForm(b.dataset.name);
  });
  mlist.querySelectorAll('.mcp-del').forEach((b) => {
    b.onclick = async () => {
      const nm = b.dataset.name;
      if (!confirm(`删除 MCP 服务「${nm}」？（只删配置并停掉子进程，不影响任何文件）`)) return;
      b.disabled = true;
      try {
        const r = await fetch('/api/mcp/delete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: nm }),
        });
        const jd = await r.json();
        if (!r.ok) throw new Error(jd.error || '删除失败');
        renderMcpList(jd.mcp || []);
        if (state.mcpEditing === nm) mcpFillForm(null);
        toast(`已删除 MCP 服务：${nm}`);
      } catch (e) { toast('删除失败：' + e.message, true); b.disabled = false; }
    };
  });
}

function mcpQuoteArg(a) {
  a = String(a);
  return /\s/.test(a) ? `"${a}"` : a;
}

// 应用一键模板：填表单（同名自动加序号），具体路径用户再改
function mcpApplyTemplate(id) {
  const tpl = MCP_TEMPLATES[id];
  if (!tpl) return;
  const t = tpl(state.wsRoot || '');
  const box = $('#mcpAddBox');
  if (box) box.open = true;
  const used = new Set((state.mcpList || []).map((x) => x.name));
  let name = t.name, n = 2;
  while (used.has(name)) name = t.name + (n++);
  const set = (sel, v) => { const el = $(sel); if (el) el.value = v; };
  set('#mcpName', name);
  set('#mcpCmd', t.command);
  set('#mcpArgs', (t.args || []).map(mcpQuoteArg).join(' '));
  set('#mcpEnv', '');
  set('#mcpTimeout', t.timeout || 20);
  const en = $('#mcpEnabled');
  if (en) en.checked = true;
  state.mcpEditing = null;
  const save = $('#mcpSave');
  if (save) save.textContent = '保存并连接';
  const tip = $('#mcpFormTip');
  if (tip) tip.textContent = t.hint || '';
  if (box) box.scrollIntoView({ block: 'nearest' });
  const nm = $('#mcpName');
  if (nm) nm.focus();
}

// 把某个服务填进表单（null = 清空为新增）
function mcpFillForm(name) {
  const s = name ? (state.mcpList || []).find((x) => x.name === name) : null;
  const box = $('#mcpAddBox');
  if (box) { box.open = true; }
  const set = (sel, v) => { const el = $(sel); if (el) el.value = v; };
  set('#mcpName', s ? s.name : '');
  set('#mcpCmd', s ? s.command : '');
  set('#mcpArgs', s ? (s.args || []).map(mcpQuoteArg).join(' ') : '');
  set('#mcpEnv', s ? Object.entries(s.env || {}).map(([k, v]) => `${k}=${v}`).join('\n') : '');
  set('#mcpTimeout', s ? (s.timeout || 20) : 20);
  const en = $('#mcpEnabled');
  if (en) en.checked = s ? !!s.enabled : true;
  state.mcpEditing = s ? s.name : null;
  const tip = $('#mcpFormTip');
  if (tip) tip.textContent = s ? `正在编辑「${s.name}」（改名会替换旧项）` : '';
  const save = $('#mcpSave');
  if (save) save.textContent = s ? '保存修改并重连' : '保存并连接';
  if (box && name) box.scrollIntoView({ block: 'nearest' });
}

async function mcpFormSave() {
  const tip = $('#mcpFormTip');
  const srv = {
    name: (($('#mcpName') || {}).value || '').trim(),
    command: (($('#mcpCmd') || {}).value || '').trim(),
    args: (($('#mcpArgs') || {}).value || '').trim(),
    env: (($('#mcpEnv') || {}).value || ''),
    timeout: +($('#mcpTimeout') || {}).value || 20,
    enabled: !!($('#mcpEnabled') || {}).checked,
  };
  if (!srv.name) return toast('请填写服务名', true);
  if (!srv.command) return toast('请填写启动命令', true);
  const btn = $('#mcpSave');
  if (btn) btn.disabled = true;
  if (tip) tip.textContent = '保存中…';
  try {
    const r = await fetch('/api/mcp/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server: srv, original_name: state.mcpEditing || '' }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '保存失败');
    if (tip) tip.textContent = '';
    mcpFillForm(null);
    let list = d.mcp || [];
    renderMcpList(list);
    // 刚保存的服务还在握手：等一会儿再回读最终状态，避免显示“未启动/0 个工具”
    const mine0 = list.find((x) => x.name === srv.name) || {};
    // 启用的服务刚保存时可能还在「连接中/未启动」的瞬时状态：等一等再回读最终结果
    if (mine0.enabled && mine0.status !== 'connected') {
      await new Promise((r) => setTimeout(r, 1500));
      try {
        const rr = await fetch('/api/settings');
        const s2 = await rr.json();
        list = s2.mcp || list;
        renderMcpList(list);
      } catch (e) { /* 保留当前列表 */ }
    }
    const st = list.find((x) => x.name === srv.name) || {};
    const ok = st.status === 'connected';
    toast(ok
      ? `✅ 已连接 ${srv.name}（${(st.tools || []).length} 个工具）`
      : `已保存 ${srv.name}，未连接：${st.error || st.status || '未知'}`, !ok);
  } catch (e) {
    if (tip) tip.textContent = '';
    toast('保存失败：' + e.message, true);
  }
  if (btn) btn.disabled = false;
}

/* ── 批量作业（B8）：一个目录一次跑完，带台账 ──────────────── */
const batchState = { timer: null, dir: 'inputs', expanded: new Set() };

function closeBatchPoll() {
  if (batchState.timer) { clearInterval(batchState.timer); batchState.timer = null; }
}

async function openBatch(prefill) {
  closeBatchPoll();
  $('#modalTitle').textContent = '批量作业';
  $('#modalBody').innerHTML = batchFormHtml() +
    '<div id="batchList"><div class="muted">加载中…</div></div>';
  openModal();
  $('#bjLoad').onclick = () => batchLoadDir(false);
  $('#bjStart').onclick = batchCreate;
  const pickBtn = $('#bjPick');
  if (pickBtn) pickBtn.onclick = () => openBatchPicker();
  if (Array.isArray(prefill) && prefill.length) {
    // 从文件浏览多选进来：直接把这些文件列出来（已勾选）
    const dirs = prefill.map((f) => f.split('/').slice(0, -1).join('/'));
    const uniq = [...new Set(dirs)];
    const dirEl = $('#bjDir');
    if (dirEl) { batchState.dir = uniq[0] || ''; dirEl.value = batchState.dir; }
    batchRenderFiles(prefill, true,
      uniq.length === 1 ? `来自文件浏览：${uniq[0] || '根目录'}` : '来自文件浏览的多选');
  } else {
    await batchLoadDir(true);
  }
  await renderBatchList();
  // 面板开着就每 2s 刷一次进度（只重绘列表，不打断输入）
  batchState.timer = setInterval(renderBatchList, 2000);
}

/* 新建作业：选目录 -> 加载文件 -> 勾选 -> 开始（可先跑前 3 项预览） */
function batchFormHtml() {
  return `<div class="batch-form">
    <h4>新建作业</h4>
    <div class="bf-row">
      <label>名称<input id="bjName" type="text" placeholder="例如：合同条款抽取"></label>
    </div>
    <label>每项要做什么（模板提示词，会带上每个文件各跑一遍）
      <textarea id="bjTpl" rows="3" placeholder="提取甲乙方、金额、到期日，结果写入 outputs/"></textarea></label>
    <div class="bf-row">
      <label class="bf-dir">目录（工作台内相对路径，空=根目录）
        <input id="bjDir" type="text" value="${esc(batchState.dir)}" placeholder="inputs"></label>
      <button id="bjPick" class="btn small" title="像文件浏览一样逐层选目录">${ico('folder')} 选择目录</button>
      <button id="bjLoad" class="btn small">加载文件</button>
      <label class="mode"><input type="checkbox" id="bjPreview"> 先跑前 3 项预览</label>
    </div>
    <div id="bjPicker" class="dir-picker hidden"></div>
    <div class="batch-safety">批量处理会调用当前配置的模型，并按模板写入工作台的 outputs/；单 worker 串行执行，模型首 token 较慢时单项可能耗时较长。</div>
    <div id="bjFiles" class="bj-files">（点「加载文件」选择要处理的文件）</div>
    <div class="btns">
      <button id="bjStart" class="btn primary">开始作业</button>
      <span class="muted" id="bjTip"></span>
    </div>
  </div>`;
}

/* 目录选择器：与文件浏览共用 dirPicker，选完回填并直接加载文件（不用手打路径） */
async function openBatchPicker() {
  const dirEl = $('#bjDir');
  bjDir.root = 'ws';
  bjDir.path = ((dirEl && dirEl.value) || '').trim().replace(/\\/g, '/')
    .replace(/^\/+|\/+$/g, '');
  const box = $('#bjPicker');
  if (!box) return;
  box.classList.remove('hidden');
  await dirPicker(box, bjDir, {
    actions: `<button class="btn small primary" id="dpUse">用这个目录</button>
              <button class="btn small ghost" id="dpClose">收起</button>`,
    onLoad: () => {
      $('#dpClose').onclick = () => box.classList.add('hidden');
      $('#dpUse').onclick = () => {
        const el = $('#bjDir');
        if (el) el.value = bjDir.path;
        batchState.dir = bjDir.path;
        box.classList.add('hidden');
        batchLoadDir(false);
      };
    },
  });
}

async function batchLoadDir(silent) {
  const dirEl = $('#bjDir');
  const dir = ((dirEl && dirEl.value) || batchState.dir || 'inputs').trim();
  const box = $('#bjFiles');
  if (!box) return;
  box.innerHTML = '加载中…';
  try {
    const r = await fetch('/api/tree?root=ws&path=' + encodeURIComponent(dir));
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '加载失败');
    batchState.dir = dir;
    const rels = (d.items || []).filter((x) => x.type === 'file')
      .map((f) => (dir ? dir + '/' : '') + f.name);
    if (!rels.length) {
      box.innerHTML = `<span class="muted">${esc(dir || '根目录')} 下没有文件</span>`;
      return;
    }
    batchRenderFiles(rels, false, `共 ${rels.length} 个文件`);
  } catch (e) {
    box.innerHTML = `<span class="muted">加载失败：${esc(e.message)}</span>`;
    if (!silent) toast('加载文件失败：' + e.message, true);
  }
}

// 文件清单：勾选列表（默认全选与否由 checked 决定）
function batchRenderFiles(rels, checked, note) {
  const box = $('#bjFiles');
  if (!box) return;
  box.innerHTML = `<div class="bf-ops">
      <button id="bjAll" class="btn small ghost">全选</button>
      <button id="bjNone" class="btn small ghost">清空</button>
      <span class="muted">${esc(note || '')}</span></div>` +
    rels.map((rel) => `<label class="bf-file"><input type="checkbox" class="bj-cb"
      value="${esc(rel)}"${checked ? ' checked' : ''}> ${esc(rel.split('/').pop())}</label>`).join('');
  $('#bjAll').onclick = () => document.querySelectorAll('.bj-cb')
    .forEach((c) => { c.checked = true; });
  $('#bjNone').onclick = () => document.querySelectorAll('.bj-cb')
    .forEach((c) => { c.checked = false; });
}

async function batchCreate() {
  const name = ($('#bjName') || {}).value || '';
  const template = (($('#bjTpl') || {}).value || '').trim();
  const files = [...document.querySelectorAll('.bj-cb:checked')].map((c) => c.value);
  const tip = $('#bjTip');
  if (!template) { if (tip) tip.textContent = '请填写“每项要做什么”'; return; }
  if (!files.length) { if (tip) tip.textContent = '请勾选要处理的文件'; return; }
  if (tip) tip.textContent = '创建中…';
  try {
    const r = await fetch('/api/batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, template, files,
        preview: !!($('#bjPreview') || {}).checked }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '创建失败');
    if (tip) tip.textContent = '';
    toast(`▶ 作业已开始：${d.job.total} 项（单 worker 串行，可随时暂停）`);
    await renderBatchList();
  } catch (e) {
    if (tip) tip.textContent = '';
    toast('创建失败：' + e.message, true);
  }
}

function batchItemsHtml(j) {
  const items = j.items || [];
  const stIco = { done: '✅', failed: '❌', running: '⏳', pending: '○', canceled: '⏹' };
  return items.map((it) => {
    const arts = (it.artifacts || []).filter(Boolean).map((rel) =>
      `<a href="/api/file?path=${encodeURIComponent(rel)}" target="_blank">${esc(rel.split('/').pop())}</a>`).join(' ');
    const body = (it.response || it.error)
      ? `<details class="bj-res"><summary>结果</summary><pre>${esc(it.response || it.error)}</pre>` +
        (arts ? `<div class="bj-arts">产物：${arts}</div>` : '') + `</details>`
      : '';
    return `<div class="bj-item ${esc(it.status)}">
        <span class="bj-i-st">${stIco[it.status] || '○'}</span>
        <span class="bj-i-nm" title="${esc(it.file)}">${esc(it.file.split('/').pop())}</span>
        ${it.error ? `<span class="bj-i-err" title="${esc(it.error)}">${esc(it.error.slice(0, 60))}</span>` : ''}
        <span class="bj-i-t muted">${it.elapsed ? it.elapsed + 's' : ''}</span>
      </div>${body}`;
  }).join('');
}

function batchCardHtml(j) {
  const c = j.counts || {}, total = j.total || 0;
  const fin = (c.done || 0) + (c.failed || 0) + (c.canceled || 0);
  const pct = total ? Math.round(fin / total * 100) : 0;
  const current = (j.items || []).find((it) => it.status === 'running');
  const durations = (j.items || []).filter((it) => it.status === 'done' && Number(it.elapsed) > 0)
    .map((it) => Number(it.elapsed));
  const avgSeconds = durations.length
    ? durations.reduce((sum, n) => sum + n, 0) / durations.length : 0;
  const pending = Math.max(0, total - fin - (c.running || 0));
  const etaSeconds = Math.ceil(avgSeconds * pending);
  const etaLabel = avgSeconds
    ? (etaSeconds >= 3600 ? `${Math.floor(etaSeconds / 3600)}小时${Math.ceil((etaSeconds % 3600) / 60)}分`
      : etaSeconds >= 60 ? `${Math.floor(etaSeconds / 60)}分${etaSeconds % 60}秒`
      : `${etaSeconds}秒`)
    : '';
  const stTxt = { running: '运行中', paused: '已暂停', done: '已完成', canceled: '已取消' }[j.status] || j.status;
  const canPause = j.status === 'running';
  const canResume = j.status === 'paused';
  const canCancel = j.status === 'running' || j.status === 'paused';
  const canRetry = (c.failed || 0) + (c.canceled || 0) > 0;
  const open = batchState.expanded.has(j.id);
  return `<div class="bj-card" data-id="${esc(j.id)}">
    <div class="bj-head">
      <b>${esc(j.name)}</b>
      <span class="bj-st ${esc(j.status)}">${stTxt}</span>
      <span class="muted">${total} 项 · 完成 ${c.done || 0}${(c.failed || 0) ? ` · 失败 ${c.failed}` : ''}</span>
      <span class="muted bj-when">${esc(j.created_at || '')}</span>
    </div>
    <div class="bj-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="批量作业完成进度"><i style="width:${pct}%"></i></div>
    ${j.status === 'running' ? `<div class="bj-progress-note">${current
      ? `正在处理 ${esc(current.file.split('/').pop())} · ${fin}/${total} 项已完成`
      : `已完成 ${fin}/${total} 项`}${etaLabel ? ` · 预计还需约 ${etaLabel}` : ' · 收集首项耗时后估算剩余时间'}</div>` : ''}
    ${j.note ? `<div class="bj-note">${esc(j.note)}</div>` : ''}
    <div class="bj-ops">
      ${canPause ? '<button class="btn small" data-act="pause">暂停</button>' : ''}
      ${canResume ? '<button class="btn small" data-act="resume">继续</button>' : ''}
      ${canCancel ? '<button class="btn small danger" data-act="cancel">取消</button>' : ''}
      ${canRetry ? '<button class="btn small" data-act="retry">重试失败项</button>' : ''}
      <a class="btn small ghost" href="/api/batch/csv?id=${esc(j.id)}">导出 CSV</a>
      <button class="btn small ghost" data-act="toggle">${open ? '收起逐项' : '逐项结果'}</button>
    </div>
    <div class="bj-items${open ? '' : ' hidden'}">${batchItemsHtml(j)}</div>
  </div>`;
}

async function renderBatchList() {
  const list = $('#batchList');
  if (!list) { closeBatchPoll(); return; }
  let d = {};
  try {
    const r = await fetch('/api/batch');
    d = await r.json();
  } catch (e) { return; }   // 断连时保留旧内容，下一轮再试
  const jobs = d.jobs || [];
  // 展开的作业才去拉逐项台账（列表接口只给摘要）
  await Promise.all(jobs.map(async (j) => {
    if (!batchState.expanded.has(j.id)) return j;
  try {
    const r = await fetch('/api/batch?id=' + encodeURIComponent(j.id));
      const jd = await r.json();
      if (jd.job) Object.assign(j, jd.job);
    } catch (e) { /* 拉不到就先显示摘要 */ }
    return j;
  }));
  const list2 = $('#batchList');
  if (!list2) { closeBatchPoll(); return; }
  list2.innerHTML = jobs.length
    ? jobs.map(batchCardHtml).join('')
    : '<div class="muted batch-empty">还没有作业：在上面选好文件点「开始作业」。</div>';
  list2.querySelectorAll('.bj-card').forEach((card) => {
    const id = card.dataset.id;
    card.querySelectorAll('[data-act]').forEach((b) => {
      b.onclick = async () => {
        const act = b.dataset.act;
        if (act === 'toggle') {
          if (batchState.expanded.has(id)) batchState.expanded.delete(id);
          else batchState.expanded.add(id);
          renderBatchList();
          return;
        }
        if (act === 'cancel' && !confirm('取消这个作业？未完成的项将不再执行。')) return;
        try {
          const r = await fetch('/api/batch/control', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id, action: act }),
          });
          const jd = await r.json();
          if (!r.ok) throw new Error(jd.error || '操作失败');
          renderBatchList();
        } catch (e) { toast('操作失败：' + e.message, true); }
      };
    });
  });
}

/* ── 命令面板（Ctrl/Cmd+K）──────────────────────────────── */
const PALETTE = [
  { icon: '＋', label: '新会话', kw: 'new 新建 会话 session', run: () => $('#btnNew').click() },
  { icon: 'db', label: '打开记忆库', kw: 'memory 记忆 事实 偏好', run: () => $('#btnMemory').click() },
  { icon: 'folder', label: '浏览工作台文件', kw: 'file 文件 目录 tree 工作台', run: () => $('#btnWs').click() },
  { icon: 'gear', label: '打开设置', kw: 'settings 设置 模型 key 超时 上传', run: () => $('#btnSettings').click() },
  { icon: 'search', label: '搜索全部会话内容', kw: 'search 搜索 全文', run: () => openSearchModal('') },
  { icon: 'type', label: '搜索会话标题', kw: 'title 标题 会话列表', run: () => { closePalette(); sbSearchEl.focus(); } },
  { icon: 'paperclip', label: '上传文件到 inputs', kw: 'upload 上传 附件 文件', run: () => $('#btnUpload').click() },
  { icon: 'upload', label: '导出当前会话（Markdown）', kw: 'export 导出 备份', run: () => $('#btnExport').click() },
  { icon: 'globe', label: '导出当前会话（网页 HTML）', kw: 'html 网页 导出 export', run: () => exportHtml() },
  { icon: 'moon', label: '切换主题（深/浅色）', kw: 'theme 主题 深色 浅色 dark light', run: () => toggleTheme() },
  { icon: 'shield', label: '切换只读模式', kw: 'readonly 只读 安全', run: () => { const c = $('#askMode'); c.checked = !c.checked; toast(c.checked ? '🛡 已开启只读模式（AI 不能写文件/执行命令）' : '只读模式已关闭'); } },
  { icon: 'stop', label: '停止当前任务', kw: 'stop 停止 中止 中断', run: () => { if (state.busy) stopRun(); else toast('当前没有运行中的任务'); } },
  { icon: 'box', label: '批量作业（一个目录一次跑完）', kw: 'batch 作业 批量 job 合同', run: () => openBatch() },
];
let palItems = [];
let palActive = 0;
function palRender(q) {
  const kw = (q || '').trim().toLowerCase();
  palItems = PALETTE.filter((c) => !kw || (c.label + ' ' + c.kw).toLowerCase().includes(kw));
  palActive = Math.min(palActive, Math.max(0, palItems.length - 1));
  $('#palList').innerHTML = palItems.map((c, i) =>
    `<div class="pal-item${i === palActive ? ' active' : ''}" data-i="${i}">` +
    `<span class="pal-ic">${ico(c.icon)}</span><span>${esc(c.label)}</span></div>`).join('') ||
    '<div class="pal-empty muted">没有匹配的命令</div>';
}
function openPalette() {
  $('#palette').classList.remove('hidden');
  $('#palQ').value = '';
  palActive = 0;
  palRender('');
  $('#palQ').focus();
}
function closePalette() {
  $('#palette').classList.add('hidden');
}
function palRun(idx) {
  const c = palItems[idx];
  if (!c) return;
  closePalette();
  c.run();
}
document.addEventListener('keydown', (e) => {
  // Ctrl/Cmd + K 唤起命令面板（离线快捷键，不依赖任何外部资源）
  if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault();
    $('#palette').classList.contains('hidden') ? openPalette() : closePalette();
    return;
  }
  if ($('#palette').classList.contains('hidden')) return;
  if (isComposingKey(e)) return;
  if (e.key === 'Escape') { e.preventDefault(); closePalette(); return; }
  const q = $('#palQ');
  if (document.activeElement !== q) return;
  if (e.key === 'ArrowDown') { e.preventDefault(); palActive = (palActive + 1) % palItems.length; palRender($('#palQ').value); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); palActive = (palActive - 1 + palItems.length) % palItems.length; palRender($('#palQ').value); }
  else if (e.key === 'Enter') { e.preventDefault(); palRun(palActive); }
});
$('#palQ').addEventListener('input', () => { palActive = 0; palRender($('#palQ').value); });
$('#palList').addEventListener('click', (e) => {
  const item = e.target.closest('.pal-item');
  if (item) palRun(+item.dataset.i);
});
$('#palette').addEventListener('mousedown', (e) => { if (e.target.classList.contains('pal-mask')) closePalette(); });

/* ── 启动 ─────────────────────────────────────────────────── */
/* ── 会话级临时换模型（B9）：只覆盖模型名，同 base_url/key ── */
let modelOvMap = {};
try { modelOvMap = JSON.parse(localStorage.getItem('modelOverrides') || '{}'); } catch (e) { modelOvMap = {}; }
function currentModelOv() { return (state.cur && modelOvMap[state.cur]) || ''; }
function setModelOv(name) {
  if (!state.cur) return toast('请先选择会话', true);
  if (name) modelOvMap[state.cur] = String(name).trim();
  else delete modelOvMap[state.cur];
  try { localStorage.setItem('modelOverrides', JSON.stringify(modelOvMap)); } catch (e) {}
  renderModelSel();
}
async function renderModelSel() {
  const sel = $('#modelSel');
  if (!sel) return;
  let def = '';
  try {
    const r = await fetch('/api/health');
    const h = await r.json();
    def = (h.model || {}).name || '';
  } catch (e) {}
  const used = [...new Set(Object.values(modelOvMap))].slice(0, 6);
  const cur = currentModelOv();
  sel.innerHTML = `<option value="">模型：${esc(def || '默认')}</option>` +
    used.map((m) => `<option value="${esc(m)}">↳ ${esc(m)}</option>`).join('') +
    '<option value="__other__">其他模型…</option>';
  if (cur && !used.includes(cur)) {
    sel.insertAdjacentHTML('afterbegin', `<option value="${esc(cur)}">↳ ${esc(cur)}</option>`);
  }
  sel.value = cur || '';
  sel.title = cur ? `本会话临时使用 ${cur}（同 base_url/key）` : '临时切换模型（仅本会话生效）';
}
if ($('#modelSel')) {
  $('#modelSel').onchange = async () => {
    const v = $('#modelSel').value;
    if (v === '__other__') {
      const name = await promptDialog('临时模型名（仅本会话生效，留空恢复默认）', currentModelOv());
      setModelOv((name || '').trim());
      return;
    }
    setModelOv(v);
    toast(v ? `↳ 本会话已切换到模型 ${v}` : '已恢复默认模型');
  };
}

/* ── 导出 HTML（双击即可在浏览器查看，零依赖）───────────── */
async function exportHtml() {
  if (!state.cur) return toast('请先选择要导出的会话', true);
  let s;
  try {
    const r = await fetch('/api/session?id=' + encodeURIComponent(state.cur));
    s = (await r.json()).session || {};
  } catch (e) { return toast('读取会话失败：' + e.message, true); }
  const parts = (s.messages || []).map((m) => {
    if (m.role === 'user') {
      return `<section class="msg u"><div class="role">🧑 用户</div>` +
        `<div class="txt">${esc(m.content).replace(/\n/g, '<br>')}</div></section>`;
    }
    if (m.role !== 'assistant') return '';
    return `<section class="msg a"><div class="role">🤖 AI</div>` +
      `<div class="txt md">${md.render(m.content || '')}</div></section>`;
  }).join('\n');
  const html = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(s.title || '会话导出')}</title><style>
body{margin:0;background:#f7f9ff;color:#1b1e2e;font:14.5px/1.72 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:780px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:20px;margin:0 0 4px}.meta{color:#6b7290;font-size:12px;margin-bottom:22px}
.msg{margin:16px 0;padding:13px 16px;border:1px solid #e3e8f5;border-radius:12px;background:#fff}
.msg.u{background:#eef3ff}.role{font-size:11.5px;color:#6b7290;margin-bottom:6px}
.md p{margin:8px 0}.md pre{background:#f6f8fd;border:1px solid #e3e8f5;border-radius:10px;padding:10px 12px;overflow:auto;font-size:12.5px}
.md code{font-family:ui-monospace,Consolas,monospace;background:#f0f3fb;padding:1px 5px;border-radius:5px}
.md table{border-collapse:collapse}.md td,.md th{border:1px solid #e3e8f5;padding:4px 8px;font-size:13px}
.md img{max-width:100%}hr{border:none;border-top:1px dashed #e3e8f5;margin:20px 0}
.pt{position:fixed;top:14px;right:14px;display:flex;gap:8px}
.pt button{border:1px solid #d9dff5;background:#fff;color:#2b3350;border-radius:9px;
  padding:7px 13px;font-size:13px;cursor:pointer}
.pt button:hover{border-color:#4e7bff;color:#3d66e8}
.pt .hint{font-size:12px;color:#6b7290;align-self:center}
@media print{
  body{background:#fff}
  .wrap{max-width:none;padding:0}
  .pt{display:none}
  .msg{break-inside:avoid;page-break-inside:avoid;box-shadow:none}
  a{color:inherit;text-decoration:none}
}
</style></head><body>
<div class="pt"><span class="hint">Ctrl/Cmd + P 也可保存为 PDF</span>
  <button onclick="window.print()">打印 / 存为 PDF</button></div>
<div class="wrap"><h1>${esc(s.title || '会话导出')}</h1>
<div class="meta">导出时间 ${new Date().toLocaleString()} · ${(s.messages || []).length} 条消息 · 本地 Agent 工作台</div>
${parts}</div></body></html>`;
  const blob = new Blob([html], { type: 'text/html;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `会话_${(s.title || '导出').replace(/[\\/:*?\"<>|]/g, '_')}_${Date.now()}.html`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
  toast('✅ 已导出 HTML（双击查看，右上角「打印 / 存为 PDF」');
}

// ── 页面生命周期：关掉所有网页后，服务端等几秒自动退出 ────────
// 做法：本标签页打开时登记，关闭时用 sendBeacon 注销，每 5 秒心跳续期。
// 服务端要等登记表变空之后才开始倒计时，所以刷新、多标签、切到别的窗口
// 都不会误杀；倒计时期间只要来了新页面，计时就取消。
const WB_PAGE_ID = 'p' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
let wbPageTimer = null;
let wbPageClosed = false;

function wbPageRegister() {
  wbPageClosed = false;
  fetch('/api/page/open', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: WB_PAGE_ID }),
  })
    .then((r) => r.json())
    .then((d) => {
      if (!d || !d.auto_exit) return;   // 服务端关了这个功能，什么都不用做
      if (wbPageTimer) clearInterval(wbPageTimer);
      // 心跳只用来清「浏览器崩溃没发 close」的残留登记，页面不可见也照发：
      // Chrome 会把后台标签的定时器节流甚至冻结，跳过发送反而更容易被误判。
      wbPageTimer = setInterval(() => {
        fetch('/api/page/alive?id=' + encodeURIComponent(WB_PAGE_ID), {
          method: 'POST',
        }).catch(() => {});
      }, 5000);
    })
    .catch(() => {});
}

function wbPageUnregister() {
  if (wbPageClosed) return;
  wbPageClosed = true;
  if (wbPageTimer) { clearInterval(wbPageTimer); wbPageTimer = null; }
  const url = '/api/page/close?id=' + encodeURIComponent(WB_PAGE_ID);
  try {
    // 卸载阶段只有 sendBeacon 能可靠发出去，普通 fetch 会被浏览器掐断
    if (navigator.sendBeacon && navigator.sendBeacon(url)) return;
  } catch (e) {}
  try { fetch(url, { method: 'POST', keepalive: true }).catch(() => {}); } catch (e) {}
}

window.addEventListener('pagehide', wbPageUnregister);
window.addEventListener('beforeunload', wbPageUnregister);
// 从往返缓存里恢复（按后退键回来）：重新登记，否则服务可能已经退出了
window.addEventListener('pageshow', (e) => {
  if (e.persisted) wbPageRegister();
});
// 标签从冻结/后台恢复时立刻续期一次，别等服务端把它当残留清掉
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible' || wbPageClosed) return;
  fetch('/api/page/alive?id=' + encodeURIComponent(WB_PAGE_ID), {
    method: 'POST',
  }).catch(() => {});
});

wbPageRegister();

applyTheme();
slashFillTemplates();
loadSessions().then(() => {
  // 记住最后会话：刷新后自动恢复上次打开的会话，不用手动找
  let last = null;
  try { last = localStorage.getItem('lastSession'); } catch (e) {}
  if (last && state.sessions.some((s) => s.id === last)) {
    selectSession(last);
  } else if (!state.cur) {
    showWelcome(true);
  }
});
setBusyUI(false);
fetch('/api/health').then((r) => r.json()).then((d) => {
  if (!d || !d.ok) return;
  state.ctxLimit = Number(d.context_window) || 0;
  state.isLoopback = d.is_loopback !== false;
  $('#sbStatus').innerHTML =
    `<div class="st-name">${esc((d.model || {}).name || '未配置模型')}</div>` +
    `<div class="st-ws" title="${esc(d.workspace || '')}">${esc(d.workspace || '')}</div>`;
}).catch(() => {});
// 欢迎页建议：动态生成的用 data-i（wChipActions），静态兜底用 data-q
const wChipsEl = $('#wChips');
if (wChipsEl) wChipsEl.addEventListener('click', (e) => {
  const c = e.target.closest('.w-chip');
  if (!c) return;
  if (c.dataset.i != null && wChipActions[+c.dataset.i]) {
    wChipActions[+c.dataset.i].run();
    return;
  }
  $('#input').value = c.dataset.q || '';
  autoGrow($('#input'));
  $('#input').focus();
});
$('#chatTitle').onclick = () => {
  if (state.cur) renameSession(state.cur, $('#chatTitle').textContent);
};
