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
- **内存护栏**：30MB 纯文本约 3 万块，二元组倒排可达千万级条目。超
  `max_tokens` 即停用该会话的词法索引并释放倒排表，退回纯向量，不抛错。
"""
from __future__ import annotations

import heapq
import math
import re
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

    def __init__(self, max_tokens: int = 2_000_000) -> None:
        self.max_tokens = int(max_tokens) if max_tokens and max_tokens > 0 else 0
        self._texts: list[str] = []
        self._refs: list[tuple[str, int]] = []  # doc_id -> (file_name, chunk_index)
        self._postings: dict[str, list[tuple[int, int]]] = {}  # token -> [(doc_id, tf)]
        self._doc_len: list[int] = []
        self._token_count = 0
        self._dirty = False
        self._disabled = False
        self._disable_reason = ""

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
    def doc_count(self) -> int:
        return len(self._texts)

    # ── 写入 ────────────────────────────────────────────────────
    def add(self, file_name: str, chunks: list[str]) -> None:
        """追加一个文件的分块。只登记文本，倒排表下次检索前统一建。"""
        if not chunks:
            return
        for i, chunk in enumerate(chunks):
            self._texts.append(chunk or "")
            self._refs.append((file_name, i))
        self._dirty = True

    def remove_file(self, file_name: str) -> None:
        """按文件名移除（重建语义）：O(N) 过滤，块数级别的操作，可接受。"""
        keep = [i for i, (fn, _idx) in enumerate(self._refs) if fn != file_name]
        if len(keep) == len(self._refs):
            return
        self._texts = [self._texts[i] for i in keep]
        self._refs = [self._refs[i] for i in keep]
        self._dirty = True

    # ── 建索引 ──────────────────────────────────────────────────
    def _ensure_index(self) -> None:
        if not self._dirty or self._disabled:
            return
        self._postings = {}
        self._doc_len = []
        total = 0
        for doc_id, text in enumerate(self._texts):
            toks = tokenize(text)
            self._doc_len.append(len(toks))
            total += len(toks)
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            for t, n in tf.items():
                self._postings.setdefault(t, []).append((doc_id, n))
        self._token_count = total
        self._dirty = False
        if self.max_tokens and total > self.max_tokens:
            # 护栏：宁可退回纯向量，也不让插件子进程吃掉上百 MB
            self._disabled = True
            self._disable_reason = f"词法索引 token 数 {total} 超过上限 {self.max_tokens}，已停用并退回纯向量检索"
            self._postings = {}

    # ── 检索 ────────────────────────────────────────────────────
    def search(self, query: str, k: int = 20) -> list[dict[str, Any]]:
        """BM25 检索。返回 [{file_name, chunk_index, text, score}]，停用或空库时返回 []。"""
        if self._disabled or not self._texts:
            return []
        self._ensure_index()
        if self._disabled or not self._postings:
            return []
        qtokens = set(tokenize(query))
        if not qtokens:
            return []
        n = len(self._doc_len)
        avgdl = (sum(self._doc_len) / n) if n else 0.0
        scores: dict[int, float] = {}
        for t in qtokens:
            plist = self._postings.get(t)
            if not plist:
                continue
            df = len(plist)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for doc_id, tf in plist:
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
    """RRF 融合两路排名：`score = Σ 1 / (rrf_k + rank)`。

    两路分数量纲不同（余弦 0~1 vs BM25 无界），只有排名可比，所以融合排名
    而不是加权求和。返回项带 `source` ∈ {vector, lexical, both} 供诊断使用。
    """
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
    """相关度闸门：绝对下限（向量余弦）+ 相对 top1 比例。

    - `min_score` 只作用于**有向量分**的候选（纯词法命中不受影响，否则词法降级通路
      会被余弦阈值整条掐死）；
    - `min_ratio` 作用于融合后的分数，低于 `top1 × ratio` 的丢弃。
    两者为 0 时表示关闭对应闸门。
    """
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
