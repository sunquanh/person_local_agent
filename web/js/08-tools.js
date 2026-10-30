'use strict';

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
  if (tip) tip.textContent = s ? `正在编辑「${s.name}」（改名会替换旧项；环境变量显示为脱敏值，不改则保留原值，删行即删除该变量）` : '';
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

