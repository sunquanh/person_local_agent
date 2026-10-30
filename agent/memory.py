# -*- coding: utf-8 -*-
"""持久记忆（SQLite，纯标准库）。

- facts: 关键事实（从会话中提取/用户明确告知），带关键词召回
- prefs: 用户偏好（如"报告用中文写""周报放周五发"）
- summaries: 会话摘要（每条会话一条，跨会话可追溯）

注入: 每次对话把相关记忆（recall(query) 按关键词评分）+ 用户偏好 + 近期会话摘要
拼进 system prompt，让模型"记得"之前的事。
"""
import os
import re
import sqlite3
import threading
import time
from datetime import datetime

_STOPWORDS = set("的了是我你他她它们这那有在要和与跟就都对给把被让请帮我我们您一个等".strip())

_LOCK = threading.Lock()


def _split_words(text: str):
    """简易分词：英文单词 + 中文单字，去掉停用词。"""
    words = []
    for m in re.finditer(r'[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]', text or ''):
        w = m.group(0).lower()
        if w not in _STOPWORDS:
            words.append(w)
    return words


class Memory:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db = db_path
        # 提取中标记：离线单卡/单槽模型下，一次提取会占着推理槽，
        # 上一次没跑完就不要再排新的（否则前后台抢同一个槽，用户侧明显变慢）
        self._extracting = threading.Event()
        self._init_schema()

    def extracting(self) -> bool:
        """是否已有一次记忆提取在进行中。"""
        return self._extracting.is_set()

    def _conn(self):
        conn = sqlite3.connect(self.db, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS facts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                source TEXT DEFAULT '',
                important INTEGER DEFAULT 0,
                tags TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                access_count INTEGER DEFAULT 0)""")
            c.execute("""CREATE TABLE IF NOT EXISTS prefs(
                key TEXT PRIMARY KEY, value TEXT NOT NULL,
                updated_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS summaries(
                session_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL)""")
            # 旧库迁移：facts 表缺 important/tags 列时补上
            cols = [r[1] for r in c.execute('PRAGMA table_info(facts)').fetchall()]
            if 'important' not in cols:
                c.execute('ALTER TABLE facts ADD COLUMN important INTEGER DEFAULT 0')
            if 'tags' not in cols:
                c.execute('ALTER TABLE facts ADD COLUMN tags TEXT DEFAULT ""')

    # ── facts ────────────────────────────────────────────────────
    @staticmethod
    def _norm(s: str) -> str:
        """归一化：去空白/标点/统一小写，用于相似度比较。"""
        return re.sub(r"[\s\u3000，。、；：！？,.;:!?()（）\[\]{}'\"“”‘’\-—_]+", "",
                      str(s or '')).lower()

    @staticmethod
    def _similar(a: str, b: str) -> float:
        """文本相似度 0~1（最长公共子串占短句长度比例）。

        对"同一信息的小变体"（增删几个字、换词）识别准确：
        "门禁回归每周五跑一次" vs "门禁回归每周五执行" -> 0.78；
        "用户负责门禁测试" vs "用户负责周报整理"     -> 0.57（不合并）。
        长文本（>120 字）不做子串匹配，返回 0 避免误伤。
        """
        na, nb = Memory._norm(a), Memory._norm(b)
        if not na or not nb:
            return 0.0
        if na == nb:
            return 1.0
        m, n = len(na), len(nb)
        if m > 120 or n > 120:
            return 0.0
        prev = [0] * (n + 1)
        best = 0
        for i in range(1, m + 1):
            cur = [0] * (n + 1)
            ci = na[i - 1]
            for j in range(1, n + 1):
                if ci == nb[j - 1]:
                    cur[j] = prev[j - 1] + 1
                    if cur[j] > best:
                        best = cur[j]
            prev = cur
        return best / min(m, n)

    def save_fact(self, content: str, source: str = "", important: bool = False):
        """存一条关键事实；自动去重：完全重复只更新时间戳，高度相似合并为一条。

        合并规则：保留内容较长者（重要标记取 OR），来源合并，时间戳更新。
        避免同一信息（含语义相近的小变体）记多遍。
        """
        content = (content or '').strip()
        if not content:
            return
        now = datetime.now().isoformat(timespec='seconds')
        imp = 1 if important else 0
        with _LOCK, self._conn() as c:
            # 1) 完全重复 -> 只更新时间戳（重要标记沿用已存值）
            row = c.execute("SELECT id, important FROM facts WHERE content=?",
                            (content,)).fetchone()
            if row:
                c.execute("UPDATE facts SET updated_at=? WHERE id=?", (now, row['id']))
                return
            # 2) 高度相似 -> 合并到已有条
            rows = c.execute(
                "SELECT id, content, source, important FROM facts").fetchall()
            for r in rows:
                if Memory._similar(content, r['content']) >= 0.75:
                    keep = content if len(content) >= len(r['content']) else r['content']
                    # 来源合并（去重拼接，不丢任何来源）
                    merged_src = []
                    for s in ((r['source'] or ''), source):
                        if s and s not in merged_src:
                            merged_src.append(s)
                    new_imp = 1 if (r['important'] or imp) else 0
                    c.execute(
                        "UPDATE facts SET content=?, source=?, important=?, updated_at=? "
                        "WHERE id=?", (keep, ' / '.join(merged_src), new_imp, now, r['id']))
                    return
            # 3) 新增
            c.execute(
                "INSERT INTO facts(content, source, important, created_at, updated_at) "
                "VALUES(?,?,?,?,?)", (content, source, imp, now, now))

    def save_facts(self, contents, source: str = ""):
        for x in contents or []:
            self.save_fact(x, source)

    def update_fact(self, fact_id: int, content: str):
        """手动编辑事实内容。"""
        content = (content or '').strip()
        if not content:
            return
        now = datetime.now().isoformat(timespec='seconds')
        with _LOCK, self._conn() as c:
            c.execute("UPDATE facts SET content=?, updated_at=? WHERE id=?",
                      (content, now, fact_id))

    def set_important(self, fact_id: int, important: bool):
        """标记/取消重要记忆。"""
        with _LOCK, self._conn() as c:
            c.execute("UPDATE facts SET important=? WHERE id=?",
                      (1 if important else 0, fact_id))

    @staticmethod
    def _normalize_tags(tags) -> str:
        """标签规范化：去空白/去重/截断，返回逗号分隔串。"""
        out = []
        for t in (tags or []):
            t = re.sub(r'[\s,，;；]+', '', str(t)).strip()
            if t and t not in out:
                out.append(t[:20])
        return ','.join(out)

    def set_tags(self, fact_id: int, tags):
        """给事实设置标签（覆盖式，传空列表清空）。"""
        s = self._normalize_tags(tags)
        now = datetime.now().isoformat(timespec='seconds')
        with _LOCK, self._conn() as c:
            c.execute("UPDATE facts SET tags=?, updated_at=? WHERE id=?",
                      (s, now, fact_id))

    def add_tag(self, fact_id: int, tag: str):
        """给事实追加一个标签（去重）。"""
        with _LOCK, self._conn() as c:
            row = c.execute("SELECT tags FROM facts WHERE id=?", (fact_id,)).fetchone()
            if not row:
                return
            cur = [t for t in (row['tags'] or '').split(',') if t]
            tag = re.sub(r'[\s,，;；]+', '', str(tag)).strip()[:20]
            if tag and tag not in cur:
                cur.append(tag)
                now = datetime.now().isoformat(timespec='seconds')
                c.execute("UPDATE facts SET tags=?, updated_at=? WHERE id=?",
                          (','.join(cur), now, fact_id))

    @staticmethod
    def _row_with_tags(r) -> dict:
        """sqlite Row -> dict，并把逗号分隔的 tags 拆成列表。"""
        d = dict(r)
        d['tags'] = [t for t in (d.get('tags') or '').split(',') if t]
        return d

    def recall(self, query: str, limit: int = 8) -> list:
        """按关键词评分召回相关事实；重要记忆优先、最新优先。"""
        words = _split_words(query)
        if not words:
            return []
        with _LOCK, self._conn() as c:
            rows = c.execute(
                "SELECT id, content, source, important, tags, created_at, updated_at, "
                "access_count FROM facts").fetchall()
        scored = []
        for r in rows:
            s = sum(1 for w in words if w in r['content'].lower())
            if s > 0:
                scored.append((s, r))
        scored.sort(key=lambda x: (-x[0], -x[1]['important'], x[1]['updated_at']))
        out = []
        ids = []
        for s, r in scored[:limit]:
            out.append(self._row_with_tags(r))
            ids.append(r['id'])
        if ids:
            with _LOCK, self._conn() as c:
                c.executemany("UPDATE facts SET access_count=access_count+1 WHERE id=?",
                              [(i,) for i in ids])
        return out

    def all_facts(self, limit: int = 200) -> list:
        with _LOCK, self._conn() as c:
            rows = c.execute(
                "SELECT id, content, source, important, tags, created_at, updated_at, "
                "access_count FROM facts ORDER BY important DESC, updated_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._row_with_tags(r) for r in rows]

    def delete_fact(self, fact_id: int):
        with _LOCK, self._conn() as c:
            c.execute("DELETE FROM facts WHERE id=?", (fact_id,))

    # ── prefs ────────────────────────────────────────────────────
    def save_pref(self, key: str, value: str):
        now = datetime.now().isoformat(timespec='seconds')
        with _LOCK, self._conn() as c:
            c.execute(
                "INSERT INTO prefs(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?",
                (key, value, now, value, now))

    def get_prefs(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM prefs ORDER BY updated_at DESC").fetchall()
        return {r['key']: r['value'] for r in rows}

    # ── summaries ────────────────────────────────────────────────
    def save_summary(self, session_id: str, title: str, summary: str):
        now = datetime.now().isoformat(timespec='seconds')
        with _LOCK, self._conn() as c:
            c.execute(
                "INSERT INTO summaries(session_id, title, summary, created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET title=?, summary=?, created_at=?",
                (session_id, title, summary, now, title, summary, now))

    def recent_summaries(self, limit: int = 5) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT session_id, title, summary, created_at FROM summaries "
                "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── 注入上下文 ───────────────────────────────────────────────
    def build_context(self, user_input: str, limit_facts: int = 6) -> str:
        """拼装注入 system 的记忆上下文（相关事实 + 偏好 + 近期摘要）。"""
        parts = []
        facts = self.recall(user_input, limit=limit_facts)
        if facts:
            lines = "\n".join(
                ("- ⭐ " if f.get('important') else "- ") + f['content']
                for f in facts)
            parts.append(f"【长期记忆·相关事实】\n{lines}")
        prefs = self.get_prefs()
        if prefs:
            plines = "\n".join(f"- {k}: {v}" for k, v in prefs.items())
            parts.append(f"【用户偏好】\n{plines}")
        sums = self.recent_summaries(limit=3)
        if sums:
            slines = "\n".join(f"- ({s['created_at'][:10]}) {s['title']}: {s['summary'][:120]}"
                               for s in sums)
            parts.append(f"【近期任务回顾】\n{slines}")
        return "\n\n".join(parts)

    # ── 会话摘要提取（后台线程，LLM 不可用则跳过）──────────────────
    def extract_summary(self, llm, session_id: str, title: str, history: list, max_len: int = 6000):
        """用 LLM 从会话历史提取摘要 + 关键事实 + 偏好，异步落库。"""

        def _work():
            self._extracting.set()
            try:
                text = ""
                for m in history[-40:]:
                    role = m.get('role')
                    c = (m.get('content') or '')[:600]
                    if c:
                        text += f"[{role}] {c}\n"
                if not text.strip():
                    return
                text = text[-max_len:]
                prompt = (
                    "请阅读以下人机对话，输出三部分（用标记分隔）：\n"
                    "## 摘要\n一句话总结本次任务及结果。\n"
                    "## 事实\n每行一条，列出值得长期记住的关键事实（用户身份/项目/决策/偏好等）。没有则写\"无\"。\n"
                    "## 偏好\n每行一条，列出用户的明确偏好（格式/风格/习惯）。没有则写\"无\"。\n\n"
                    f"对话：\n{text}"
                )
                r = llm.chat([{"role": "user", "content": prompt}], temperature=0.3)
                out = (r.get('content') or '').strip()
                if not out:
                    return
                summary, facts, prefs = self._parse_extract(out)
                if summary:
                    self.save_summary(session_id, title, summary)
                if facts:
                    self.save_facts(facts, source=f"session:{session_id}")
                for p in prefs:
                    if ':' in p:
                        k, v = p.split(':', 1)
                        self.save_pref(k.strip()[:60], v.strip()[:300])
            except Exception:
                pass  # 记忆提取失败不影响主流程
            finally:
                self._extracting.clear()

        threading.Thread(target=_work, daemon=True).start()

    @staticmethod
    def _parse_extract(text: str):
        summary, facts, prefs = "", [], []
        section = None
        for line in text.splitlines():
            line = line.strip().lstrip('#').strip()
            if line.startswith('摘要'):
                section = 's'
                continue
            if line.startswith('事实'):
                section = 'f'
                continue
            if line.startswith('偏好'):
                section = 'p'
                continue
            if not line or line in ('无', '无。', 'N/A'):
                continue
            if section == 's':
                summary = line
            elif section == 'f':
                facts.append(line.lstrip('- '))
            elif section == 'p':
                prefs.append(line.lstrip('- '))
        return summary, facts, prefs
