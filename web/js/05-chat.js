'use strict';

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
  // 本轮归属的会话：中途切走后，帧与收尾不再写当前视图（见 handleFrame 与 finally）
  const runSid = state.cur;
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
    isStale: () => state.cur !== runSid,
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
      if (mdBuf) {
        // 流中断但已有正文：保留已流出的内容，只追加错误说明
        renderMd(true);
        const en = document.createElement('div');
        en.className = 'err-box';
        en.innerHTML = `<div class="err-line">❌ ${esc(e.message)}</div>`;
        contentDiv.appendChild(en);
      } else {
        contentDiv.innerHTML = '';
        contentDiv.textContent = '❌ ' + e.message;
      }
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
      // 仅当用户还停在发送的会话里；中途切走后不碰当前视图。
      if (state.cur === runSid) {
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
      // 切走后提问卡会画进已摘除的气泡、用户看不见：跳过渲染，
      // 服务端 ask 超时后按未回答继续（confirm 走全局弹窗，不受影响）。
      if (ctx && ctx.isStale && ctx.isStale()) break;
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
      // 已流出的正文要保留：错误块追加到内容区末尾，不清空 mdBox；
      // 旧版 innerHTML 整块替换会把已流出的答案一起抹掉。
      const cdiv = botEl.querySelector('.content');
      const eb = document.createElement('div');
      eb.innerHTML = `<div class="err-box">
        <div class="err-line">❌ ${esc(f.message || '任务失败')}</div>
        <details class="err-more"><summary>查看详情（日志 / 堆栈）</summary>
          <pre>${esc(f.detail || f.message || '')}</pre>
          <div class="err-hint">完整日志：设置 → 运行 → 查看最近日志（工作台 .agent/logs/）</div>
        </details>
        <button class="btn small err-retry" title="移除这条失败回复并重新发送上一条">${ico('retry')} 重试这一轮</button>
      </div>`;
      cdiv.appendChild(eb.firstElementChild);
      $('#queueBar').classList.add('hidden');
      toast(f.message, true);
      break;
    }
  }
  followScroll($('#messages'));
  syncRightPanel();
  return thinkEl;
}

