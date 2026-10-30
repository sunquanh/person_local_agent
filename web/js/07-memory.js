'use strict';

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

