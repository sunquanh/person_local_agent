'use strict';

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
<div class="meta">导出时间 ${new Date().toLocaleString()} · ${(s.messages || []).length} 条消息 · 加菲猫的工作室</div>
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
    .then((r) => {
      if (!r.ok) throw new Error('register failed: ' + r.status);
      return r.json();
    })
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
    .catch(() => {
      // 登记失败（服务端刚重启/网络抖动）：5 秒后重试，否则心跳永不启动，
      // 服务端收不到登记，自动退出 silently 失效。
      if (!wbPageClosed) setTimeout(wbPageRegister, 5000);
    });
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
