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
  sessionGen: 0,      // 会话切换世代戳：旧请求的回包不许覆盖新会话
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
      try { return `<pre><code class="hljs language-${esc(langName)}">` + hljs.highlight(str, { language: langName, ignoreIllegals: true }).value + `</code>${badge}${cpBtn}</pre>`; }
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
/* fmtSize 唯一定义在附件工具区（toAttObj 上方）；此处原重复定义已删——
   函数声明会提升，后者全局生效，留两份只会误导。 */
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

