"""会话级词法索引（BM25）——零依赖，纯模块（不得出现 self.ctx）。

为什么需要它
------------
纯向量检索对「查具体符号」的请求近乎无能：函数名、报错码、编号、表格里的专有
名词，语义相近但字面不同的块常常排到前面，而字面完全匹配的块被挤掉。
BM25 补的正是这一路。两路分数量纲不同（余弦 0~1 vs BM25 无界），不能直接加权
求和，所以用 RRF（倒数排名融合）合并排名。

设计取舍
--------
- **不引 jieba**：插件跑在 MaiBot 的独立子进程里，装库要找准那个 python.exe，
  成本高风险大。中文按二元组切（「第三章」→ 第三/三章）不需要词典。
- **英文标识符同时保留整体与子词**：查 `read_any_file_to_text` 能整体命中，
  查 `file` 也能命中。
- **内存护栏（v1.6.0 强化）**：postings 用 `array('I')` 扁平存放
  `[doc_id, tf, doc_id, tf, ...]`，替代每个 posting 一个 Python tuple；
  并在构建过程中**逐块检查** token / posting 上限，超限立即停止，不再等
  完整倒排表建完才清空。
"""
from __future__ import annotations

import heapq
import math
import re
import sys
from array import array
from typing import Any

# CJK 统一表意文字（含扩展 A）
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
# 英文标识符与独立数字：read_any_file_to_text / deploy.sh / 404 / v1_2
# 允许 . _ - 连接，这样「报告.pdf」「deploy.sh」这类文件名能作为整体命中，
# 同时仍会额外产出子词（见 tokenize），查 `file` 也能命中 read_any_file_to_text。
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[._-][A-Za-z0-9_]+)*|[0-9]+")
_SUBSPLIT = re.compile(r"[._\-]")

K1 = 1.2
B = 0.75


def tokenize(text: str) -> list[str]:
    """分词：CJK 走二元组，英文/数字走标识符（整体 + 子词）。

    >>> tokenize("第三章")
    ['第三', '三章']
    >>> tokenize("read_any_file_to_text")
    ['read_any_file_to_text', 'read', 'any', 'file', 'to', 'text']
    """
    if not text:
        return []
    tokens: list[str] = []
    for m in _CJK.finditer(text):
        run = m.group()
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    for m in _IDENT.finditer(text):
        word = m.group().lower()
        tokens.append(word)
        if any(ch in word for ch in "._-"):
            tokens.extend(t for t in _SUBSPLIT.split(word) if t)
    return tokens


class LexicalIndex:
    """增量追加、惰性建索引、超限自动停用的 BM25 倒排索引。"""

    def __init__(self, max_tokens: int = 2_000_000, max_postings: int = 0) -> None:
        self.max_tokens = int(max_tokens) if max_tokens and max_tokens > 0 else 0
        self.max_postings = int(max_postings) if max_postings and max_postings > 0 else 0
        self._texts: list[str] = []
        self._refs: list[tuple[str, int]] = []  # doc_id -> (file_name, chunk_index)
        # token -> flat array [doc_id, tf, doc_id, tf, ...]
        self._postings: dict[str, array] = {}
        self._doc_len: list[int] = []
        self._total_doc_len = 0
        self._token_count = 0
        self._posting_count = 0
        self._dirty = False
        self._disabled = False
        self._disable_reason = ""
        self.last_build_seconds = 0.0

    # ── 状态 ────────────────────────────────────────────────────
    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def disable_reason(self) -> str:
        return self._disable_reason

    @property
    def token_count(self) -> int:
        return self._token_count

    @property
    def posting_count(self) -> int:
        return self._posting_count

    @property
    def doc_count(self) -> int:
        return len(self._texts)

    @property
    def ready(self) -> bool:
        return bool(self._postings) and not self._dirty and not self._disabled

    def estimated_bytes(self) -> int:
        """索引自身的近似 Python 堆占用（不含被 FileEntry 共享的 chunk 字符串）。"""
        total = sys.getsizeof(self._postings) + sys.getsizeof(self._doc_len)
        total += sys.getsizeof(self._texts) + sys.getsizeof(self._refs)
        for token, values in self._postings.items():
            total += sys.getsizeof(token) + sys.getsizeof(values) + values.buffer_info()[1] * values.itemsize
        return total

    # ── 写入 ────────────────────────────────────────────────────
    def add(self, file_name: str, chunks: list[str]) -> None:
        """追加一个文件的分块。只登记文本，倒排表随后由 ensure_index 构建。"""
        if not chunks:
            return
        if not self._texts:
            # 上一个文件删空后，旧的“超限停用”状态不应阻止新会话内容重新建索引。
            self._disabled = False
            self._disable_reason = ""
        for i, chunk in enumerate(chunks):
            self._texts.append(chunk or "")
            self._refs.append((file_name, i))
        self._dirty = True

    def replace(self, file_name: str, chunks: list[str]) -> None:
        """用同一文件名的新分块替换旧分块（v1.6.0）。"""
        keep = [i for i, (fn, _idx) in enumerate(self._refs) if fn != file_name]
        if len(keep) != len(self._refs):
            self._texts = [self._texts[i] for i in keep]
            self._refs = [self._refs[i] for i in keep]
        self._disabled = False
        self._disable_reason = ""
        self._dirty = True
        self.add(file_name, chunks)

    def remove_file(self, file_name: str) -> None:
        """按文件名移除（重建语义）：O(N) 过滤，块数级别的操作。"""
        self.remove_files({file_name})

    def remove_files(self, file_names: set[str]) -> None:
        """一次移除多个文件（v1.6.0）：批量过期只复制一遍列表。"""
        if not file_names:
            return
        keep = [i for i, (fn, _idx) in enumerate(self._refs) if fn not in file_names]
        if len(keep) == len(self._refs):
            return
        self._texts = [self._texts[i] for i in keep]
        self._refs = [self._refs[i] for i in keep]
        self._dirty = True
        if self._texts:
            self._disabled = False
            self._disable_reason = ""

    def disable(self, reason: str) -> None:
        """外部容量预算要求停用词法索引：释放倒排表但保留文本引用。"""
        self._disabled = True
        self._disable_reason = reason
        self._postings = {}
        self._doc_len = []
        self._total_doc_len = 0
        self._token_count = 0
        self._posting_count = 0
        self._dirty = False

    def _abort_over_budget(self, total_tokens: int, total_postings: int) -> bool:
        if self.max_tokens and total_tokens > self.max_tokens:
            self.disable(
                f"词法索引 token 数超过上限 {self.max_tokens}（已处理 {total_tokens}），"
                "已停用并退回纯向量检索"
            )
            return True
        if self.max_postings and total_postings > self.max_postings:
            self.disable(
                f"词法索引 posting 数超过上限 {self.max_postings}（已处理 {total_postings}），"
                "已停用并退回纯向量检索"
            )
            return True
        return False

    # ── 建索引 ──────────────────────────────────────────────────
    def ensure_index(self) -> None:
        """确保倒排表已构建；v1.6.0 起在入库工作线程中主动调用。"""
        if not self._dirty or self._disabled:
            return
        import time as _time

        started = _time.perf_counter()
        postings: dict[str, array] = {}
        doc_len: list[int] = []
        total = 0
        posting_count = 0
        try:
            for doc_id, text in enumerate(self._texts):
                toks = tokenize(text)
                doc_len.append(len(toks))
                total += len(toks)
                if self._abort_over_budget(total, posting_count):
                    return
                tf: dict[str, int] = {}
                for token in toks:
                    tf[token] = tf.get(token, 0) + 1
                posting_count += len(tf)
                if self._abort_over_budget(total, posting_count):
                    return
                for token, n in tf.items():
                    values = postings.get(token)
                    if values is None:
                        values = array("I")
                        postings[token] = values
                    values.append(doc_id)
                    values.append(n)
            self._postings = postings
            self._doc_len = doc_len
            self._total_doc_len = total
            self._token_count = total
            self._posting_count = posting_count
            self._dirty = False
        finally:
            self.last_build_seconds = _time.perf_counter() - started

    # 兼容旧调用名
    _ensure_index = ensure_index

    # ── 检索 ────────────────────────────────────────────────────
    def search(self, query: str, k: int = 20) -> list[dict[str, Any]]:
        """BM25 检索。停用或空库时返回 []。"""
        if self._disabled or not self._texts:
            return []
        self.ensure_index()
        if self._disabled or not self._postings:
            return []
        qtokens = set(tokenize(query))
        if not qtokens:
            return []
        n = len(self._doc_len)
        avgdl = (self._total_doc_len / n) if n else 0.0
        scores: dict[int, float] = {}
        for token in qtokens:
            values = self._postings.get(token)
            if not values:
                continue
            df = len(values) // 2
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for pos in range(0, len(values), 2):
                doc_id = values[pos]
                tf = values[pos + 1]
                dl = self._doc_len[doc_id]
                denom = tf + K1 * (1 - B + B * dl / avgdl) if avgdl > 0 else tf + K1
                if denom <= 0:
                    continue
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * tf * (K1 + 1) / denom
        if not scores:
            return []
        top = heapq.nlargest(max(1, int(k)), scores.items(), key=lambda x: x[1])
        return [
            {
                "file_name": self._refs[doc_id][0],
                "chunk_index": self._refs[doc_id][1],
                "text": self._texts[doc_id],
                "score": float(score),
            }
            for doc_id, score in top
        ]


# ─── 融合与阈值（纯函数，便于单测与回滚） ────────────────────────


def rrf_fuse(
    vector_hits: list[dict[str, Any]],
    lexical_hits: list[dict[str, Any]],
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    """RRF 融合两路排名：`score = Σ 1 / (rrf_k + rank)`。"""
    fused: dict[tuple[Any, Any], dict[str, Any]] = {}
    k = int(rrf_k) if rrf_k and rrf_k > 0 else 60
    for source, hits in (("vector", vector_hits or []), ("lexical", lexical_hits or [])):
        for rank, h in enumerate(hits, 1):
            key = (h.get("file_name"), h.get("chunk_index"))
            entry = fused.get(key)
            if entry is None:
                entry = {
                    "file_name": h.get("file_name"),
                    "chunk_index": h.get("chunk_index"),
                    "text": h.get("text", ""),
                    "score": 0.0,
                    "vector_score": None,
                    "lexical_score": None,
                    "sources": [],
                }
                fused[key] = entry
            entry["score"] += 1.0 / (k + rank)
            entry["sources"].append(source)
            if source == "vector":
                entry["vector_score"] = h.get("score")
            else:
                entry["lexical_score"] = h.get("score")
    out = list(fused.values())
    for entry in out:
        entry["source"] = "both" if len(entry["sources"]) > 1 else entry["sources"][0]
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def apply_relevance_gate(
    hits: list[dict[str, Any]],
    min_score: float = 0.0,
    min_ratio: float = 0.0,
) -> list[dict[str, Any]]:
    """相关度闸门：向量绝对下限 + 融合后相对 Top1 下限。"""
    if not hits:
        return []
    out = hits
    if min_score and min_score > 0:
        out = [
            h
            for h in out
            if h.get("vector_score") is None or (h.get("vector_score") or 0.0) >= min_score
        ]
    if not out:
        return []
    if min_ratio and 0 < min_ratio < 1:
        top1 = max((h.get("score") or 0.0) for h in out)
        if top1 > 0:
            out = [h for h in out if (h.get("score") or 0.0) >= top1 * min_ratio]
    return out
