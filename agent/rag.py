# -*- coding: utf-8 -*-
"""本地 RAG 知识库（纯标准库，零第三方依赖，离线可用）。

- 索引范围: inputs/ + notes/（上传素材与长期知识库）
- 支持格式: txt/md/csv/json/常见代码 (utf-8) + docx/xlsx（zip XML 抽取纯文本）
- 检索: 中文二元分词 + TF-IDF 轻量打分（适合个人文档规模）
- 数据落盘: .agent/rag.db（sqlite3）

线程安全（重要）:
    ThreadingHTTPServer 每个请求一个线程，而 Rag 在主线程构造。sqlite3 连接默认
    绑定创建线程，跨线程使用会抛 ProgrammingError——旧版因此在 Agent 的
    `except: rag_ctx=''` 里被静默吞掉，RAG 从未真正生效。这里用
    check_same_thread=False + 全局可重入锁解决。

性能:
    df（文档频率）在建索引时算好存表，检索时只算 query 侧 TF-IDF，
    避免每次提问都全量分词所有 chunk（旧版 4000 块约 1.7s/次）。
"""
import math
import os
import re
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

from . import logsetup

log = logsetup.get('rag')

_TEXT_EXT = {'.txt', '.md', '.csv', '.json', '.py', '.js', '.ts', '.html', '.css',
             '.xml', '.log', '.ini', '.yaml', '.yml', '.sql', '.java', '.go', '.c',
             '.cpp', '.h', '.sh', '.bat', '.ps1'}
_MAX_FILE = 2 * 1024 * 1024          # 单文件读取上限 2MB
_CHUNK = 800                          # 分块大小（字符）
_OVERLAP = 120                        # 相邻块重叠（保持上下文连贯）
_MAX_CHUNKS = 4000                    # 索引块总数上限

_SUB_RE = re.compile(r'<[^>]+>')


def _iter_workspace_files(base: Path):
    """遍历普通文件，排除链接，防止把工作区外内容带入本地索引。"""
    if base.is_symlink() or not base.is_dir():
        return
    for dirpath, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = [name for name in dirs
                   if not (Path(dirpath) / name).is_symlink()]
        for name in files:
            path = Path(dirpath) / name
            if not name.startswith('.') and not path.is_symlink():
                yield path


def tokenize(text: str):
    """轻量分词：中文按二元组（bigram），西文按单词/数字。"""
    tokens = []
    for seg in re.findall(r'[A-Za-z0-9_]+|[\u4e00-\u9fff]+', (text or '').lower()):
        if seg[0].isascii():
            tokens.append(seg)
        else:
            for i in range(len(seg) - 1):
                tokens.append(seg[i:i + 2])
    return tokens


def extract_text(path: Path) -> str:
    """抽取文件纯文本（不支持的格式返回空串）。"""
    ext = path.suffix.lower()
    try:
        if ext in _TEXT_EXT and path.stat().st_size <= _MAX_FILE:
            return path.read_text(encoding='utf-8', errors='replace')
        if ext == '.docx':
            with zipfile.ZipFile(path) as z:
                xml = z.read('word/document.xml').decode('utf-8', errors='replace')
            return _SUB_RE.sub(' ', xml)
        if ext == '.xlsx':
            out = []
            with zipfile.ZipFile(path) as z:
                for name in ('xl/sharedStrings.xml',):
                    if name in z.namelist():
                        xml = z.read(name).decode('utf-8', errors='replace')
                        for cell in re.findall(r'<si>(.*?)</si>', xml, re.S):
                            t = _SUB_RE.sub(' ', cell)
                            t = re.sub(r'\s+', ' ', t).strip()
                            if t:
                                out.append(t)
            return '\n'.join(out)
    except Exception as e:
        log.debug('抽取文本失败 %s: %s', path, e)
    return ''


def chunk_text(text: str):
    """按段落优先、字符兜底切块。"""
    paras = [p.strip() for p in re.split(r'\n{2,}', (text or '')) if p.strip()]
    chunks = []
    buf = ''
    for p in paras:
        if len(p) > _CHUNK:
            if buf:
                chunks.append(buf)
                buf = ''
            for i in range(0, len(p), _CHUNK - _OVERLAP):
                chunks.append(p[i:i + _CHUNK])
        else:
            if len(buf) + len(p) + 2 > _CHUNK:
                chunks.append(buf)
                buf = p
            else:
                buf = (buf + '\n\n' + p) if buf else p
    if buf:
        chunks.append(buf)
    return chunks


class Rag:
    def __init__(self, root: str | Path, db_path: str | None = None):
        self.root = Path(root or '.')
        dbp = Path(db_path or (self.root / '.agent' / 'rag.db'))
        try:
            dbp.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # check_same_thread=False：连接由 self._lock 串行化，允许跨线程使用
        self.db = sqlite3.connect(str(dbp), check_same_thread=False, timeout=10)
        self._lock = threading.RLock()
        with self._lock:
            self.db.execute('CREATE TABLE IF NOT EXISTS docs('
                            'relpath TEXT PRIMARY KEY, mtime REAL, size INT, '
                            'chunk_count INT, indexed_at REAL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS chunks('
                            'id INTEGER PRIMARY KEY AUTOINCREMENT, relpath TEXT, seq INT, text TEXT)')
            self.db.execute('CREATE INDEX IF NOT EXISTS idx_chunks_rel ON chunks(relpath)')
            self.db.execute('CREATE TABLE IF NOT EXISTS df(token TEXT PRIMARY KEY, n INT)')
            # FTS5 候选检索：先把查询词和入库文本都变成二元组/单词再交给 SQLite，
            # 避免每次提问都全量取出所有 chunk（旧版 4000 块每轮都拉一遍）
            self.db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING '
                            "fts5(relpath UNINDEXED, seq UNINDEXED, text, tokenize='unicode61')")
            self.db.commit()
            # 老库迁移：有 chunks 没 fts 时补一次
            if self.db.execute('SELECT count(*) FROM chunks').fetchone()[0] and \
                    not self.db.execute('SELECT count(*) FROM chunks_fts').fetchone()[0]:
                self._rebuild_fts()
                self.db.commit()

    # ── 索引 ────────────────────────────────────────────────────
    def refresh(self) -> dict:
        """增量重建：按 mtime 检测变化，删除失效文件，重算 df。返回统计。"""
        t0 = time.time()
        found = []          # relpath -> (mtime, size)
        for top in ('inputs', 'notes'):
            base = self.root / top
            for fp in _iter_workspace_files(base):
                rel = fp.relative_to(self.root).as_posix()
                try:
                    st = fp.stat()
                except OSError:
                    continue
                found.append((rel, st.st_mtime, st.st_size))
        with self._lock:
            known = {r: (m, s) for r, m, s in
                     self.db.execute('SELECT relpath, mtime, size FROM docs')}
            new_docs, updated, removed, chunks_total = [], 0, 0, 0
            now = time.time()
            for rel, mtime, size in found:
                if rel in known and known[rel] == (mtime, size):
                    rows = self.db.execute('SELECT count(*) FROM chunks WHERE relpath=?', (rel,)).fetchone()
                    chunks_total += rows[0] if rows else 0
                    continue
                text = extract_text(self.root / rel)
                chunks = chunk_text(text)
                if not chunks:
                    continue
                rem = len(chunks)
                # 块数上限保护
                if chunks_total + rem > _MAX_CHUNKS:
                    rem = max(0, _MAX_CHUNKS - chunks_total)
                    chunks = chunks[:rem]
                self.db.execute('DELETE FROM chunks WHERE relpath=?', (rel,))
                self.db.execute('DELETE FROM chunks_fts WHERE relpath=?', (rel,))
                self.db.execute('INSERT OR REPLACE INTO docs(relpath, mtime, size, chunk_count, indexed_at) '
                                'VALUES(?,?,?,?,?)', (rel, mtime, size, rem, now))
                self.db.executemany('INSERT INTO chunks(relpath, seq, text) VALUES(?,?,?)',
                                    [(rel, i, c) for i, c in enumerate(chunks)])
                self.db.executemany('INSERT INTO chunks_fts(relpath, seq, text) VALUES(?,?,?)',
                                    [(rel, i, ' '.join(tokenize(c))) for i, c in enumerate(chunks)])
                if rel in known:
                    updated += 1
                else:
                    new_docs.append(rel)
                chunks_total += rem
            found_set = {r for r, _, _ in found}
            for rel in known:
                if rel not in found_set:
                    self.db.execute('DELETE FROM chunks WHERE relpath=?', (rel,))
                    self.db.execute('DELETE FROM chunks_fts WHERE relpath=?', (rel,))
                    self.db.execute('DELETE FROM docs WHERE relpath=?', (rel,))
                    removed += 1
            self._rebuild_df()
            self.db.commit()
        stats = {"docs": len(found), "new": len(new_docs), "updated": updated,
                 "removed": removed, "chunks": chunks_total,
                 "elapsed_s": round(time.time() - t0, 2)}
        log.info('RAG 索引完成: %s', stats)
        return stats

    def _rebuild_df(self):
        """重算文档频率表（建索引时一次性做，检索时直接读）。"""
        self.db.execute('DELETE FROM df')
        acc = {}
        for (text,) in self.db.execute('SELECT text FROM chunks'):
            for tok in set(tokenize(text)):
                acc[tok] = acc.get(tok, 0) + 1
        self.db.executemany('INSERT OR REPLACE INTO df(token, n) VALUES(?,?)',
                            list(acc.items()))

    def _rebuild_fts(self):
        """从 chunks 重建 FTS 索引（迁移/损坏时用）。"""
        self.db.execute('DELETE FROM chunks_fts')
        self.db.executemany('INSERT INTO chunks_fts(relpath, seq, text) VALUES(?,?,?)',
                            [(rel, seq, ' '.join(tokenize(text)))
                             for rel, seq, text in self.db.execute(
                                 'SELECT relpath, seq, text FROM chunks')])

    def pending(self) -> int:
        """inputs/notes 里已变更但还没入索引的文件数（设置页显示“索引已过期 N 个”）。"""
        try:
            known = {r: (m, s) for r, m, s in
                     self.db.execute('SELECT relpath, mtime, size FROM docs')}
        except sqlite3.Error:
            return 0
        n = 0
        for top in ('inputs', 'notes'):
            base = self.root / top
            for fp in _iter_workspace_files(base):
                rel = fp.relative_to(self.root).as_posix()
                try:
                    st = fp.stat()
                except OSError:
                    continue
                if known.get(rel) != (st.st_mtime, st.st_size):
                    n += 1
        return n

    def stats(self) -> dict:
        with self._lock:
            docs = self.db.execute('SELECT count(*) FROM docs').fetchone()[0]
            chunks = self.db.execute('SELECT count(*) FROM chunks').fetchone()[0]
            df = self.db.execute('SELECT count(*) FROM df').fetchone()[0]
        out = {"docs": docs, "chunks": chunks, "tokens": df}
        try:
            out["pending"] = self.pending()
        except Exception:
            out["pending"] = 0
        return out

    def _fts_candidates(self, toks, limit: int = 80):
        """用 FTS5 打出候选块（返回 (relpath, seq) 列表）。

        查询词和入库文本都已变成二元组/单词，所以直接 OR 拼接即可；
        FTS 不可用（旧库/损坏）时返回 None，调用方回退全量扫描。
        """
        expr = ' OR '.join('"' + t.replace('"', '""') + '"' for t in toks)
        try:
            return self.db.execute(
                'SELECT relpath, seq FROM chunks_fts WHERE chunks_fts MATCH ? '
                'ORDER BY bm25(chunks_fts) LIMIT ?', (expr, limit)).fetchall()
        except sqlite3.Error as e:
            log.debug('FTS 检索不可用，回退全量: %s', e)
            return None

    # ── 检索 ────────────────────────────────────────────────────
    def search(self, query: str, top_k: int = 4) -> list:
        q = [t for t in tokenize(query) if len(t) > 1]
        if not q:
            return []
        try:
            with self._lock:
                n = self.db.execute('SELECT count(*) FROM chunks').fetchone()[0]
                if not n:
                    return []
                fts_n = self.db.execute('SELECT count(*) FROM chunks_fts').fetchone()[0]
                if not fts_n:                    # 索引表空但有块：补建一次
                    self._rebuild_fts()
                    self.db.commit()
                    fts_n = n
                cand = self._fts_candidates(q)
                if cand is None:                 # FTS 不可用：回退全量扫描
                    rows = self.db.execute('SELECT relpath, text FROM chunks').fetchall()
                else:
                    rows = []
                    for rel, seq in cand:
                        r = self.db.execute('SELECT text FROM chunks '
                                            'WHERE relpath=? AND seq=?',
                                            (rel, seq)).fetchone()
                        if r:
                            rows.append((rel, r[0]))
                if not rows:
                    return []
                df = dict(self.db.execute('SELECT token, n FROM df'))
            if not df:                       # 旧库未建 df：兜底算一次并落表
                with self._lock:
                    self._rebuild_df()
                    self.db.commit()
                    df = dict(self.db.execute('SELECT token, n FROM df'))
            # n = 全库块数（语料规模），idf 才不会随候选数变小而失真
            scored = []
            for rel, text in rows:
                toks = tokenize(text)
                if not toks:
                    continue
                score = 0.0
                for tok in q:
                    if tok not in df:
                        continue
                    tf = toks.count(tok) / len(toks)
                    idf = max(0.0, math.log((n + 1) / (df.get(tok, 0) + 1)))
                    score += tf * idf
                if score > 0:
                    scored.append((score, rel, text))
            scored.sort(key=lambda x: -x[0])
            out = []
            for score, rel, text in scored[:top_k]:
                si = 0
                for tok in q:
                    i = text.lower().find(tok)
                    if i >= 0:
                        si = i
                        break
                start = max(0, si - 80)
                snip = text[start:start + 320]
                if start > 0:
                    snip = '…' + snip
                out.append({"path": rel, "score": round(score, 4),
                            "snippet": snip.replace('\n', ' ').strip()})
            return out
        except Exception as e:
            # 绝不静默：RAG 失败要能在日志里看见（旧版吞异常导致功能假死）
            log.warning('RAG 检索失败: %s: %s', type(e).__name__, e)
            return []
