"""向量存储模块（自包含）

用 MaiBot 的 `llm.embed` 能力生成向量，numpy 做余弦相似度检索。
按 (session_id, conversation_id) 隔离，内存为主、可选 JSON 落盘到 data_dir。
不依赖 faiss / astrbot 的任何内部类。
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover
    np = None


@dataclass
class FileEntry:
    """一个已向量化文件的内存表示。"""

    file_name: str
    chunks: List[str] = field(default_factory=list)
    # v1.1.0：add_file 入库后转为单个 float32 ndarray（numpy 可用时）——
    # list-of-list 的 Python float 每维约 32B，是 float32 的 ~8 倍；ndarray 大幅省内存。
    # numpy 不可用或转换失败时保持 List[List[float]]，两条路径下游均兼容。
    vectors: Any = field(default_factory=list)
    upload_time: float = field(default_factory=time.time)
    rounds: int = 0
    summary: str = ""  # 大文件 LLM 概要（v1.0.17）；空 = 未生成或生成失败
    # 原始解析文本长度（v1.0.18）：分块合并带 overlap 后 join(chunks) 会膨胀约 20%，
    # 直注/概要阈值判定必须用原文长度；0 = 旧数据无该字段，回退 sum(len(chunks))
    source_chars: int = 0
    # 全文拼接缓存（v1.1.0）：直注/概要取样每次都要 join(chunks)，缓存一次免重复拼接
    _full_text_cache: str = field(default="", repr=False)

    def total_chars(self) -> int:
        """原文长度：优先 source_chars，旧数据回退分块拼接长度。"""
        return self.source_chars if self.source_chars > 0 else len(self.full_text())

    def full_text(self) -> str:
        """分块拼接全文（缓存）。chunks 入库后不变，缓存安全。"""
        if not self._full_text_cache and self.chunks:
            self._full_text_cache = "".join(self.chunks)
        return self._full_text_cache


def _cosine(a: Any, b: Any) -> float:
    """余弦相似度。空向量返回 0。a/b 兼容 list 与 numpy 1D 数组。"""
    if a is None or b is None or len(a) == 0 or len(b) == 0 or len(a) != len(b):
        return 0.0
    if np is None:
        # 无 numpy 兜底：纯 Python 点积
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0
    va = np.asarray(a, dtype="float32")
    vb = np.asarray(b, dtype="float32")
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


class VectorStore:
    """会话级向量库。维护多个文件条目，支持检索与过期清理。"""

    def __init__(
        self,
        embed_fn: Callable[[List[str]], Any],
        chunk_size: int = 512,
        chunk_overlap: int = 100,
        retrieve_top_k: int = 5,
        chunk_merge: bool = True,
        retrieve_use_matrix: bool = False,
    ) -> None:
        # 延迟导入 chunker，避免与 plugin.py 的 sys.path 处理耦合
        from chunker import RecursiveCharacterChunker

        self._embed_fn = embed_fn
        self._chunker = RecursiveCharacterChunker(chunk_size, chunk_overlap, merge=chunk_merge)
        self.retrieve_top_k = retrieve_top_k
        self.retrieve_use_matrix = retrieve_use_matrix  # v1.0.18 C5：矩阵化检索开关
        self._files: dict[str, FileEntry] = {}
        # 矩阵缓存（C5）：L2 归一化后的全库向量矩阵 + (file_name, chunk_index, text) 索引表
        self._mat_cache: Any = None
        self._mat_index: List[tuple[str, int, str]] = []
        self._mat_fp: Optional[tuple] = None

    @property
    def files(self) -> dict[str, FileEntry]:
        return self._files

    def list_files(self) -> List[str]:
        return list(self._files.keys())

    def total_chunks(self) -> int:
        return sum(len(f.chunks) for f in self._files.values())

    async def add_file(self, file_name: str, content: str) -> FileEntry:
        """解析文本 → 分块 → 向量化 → 存入。"""
        from chunker import RecursiveCharacterChunker  # noqa: F401  (确认可用)

        # v1.1.0：递归分块是 CPU 密集任务（大文本可达秒级），进线程避免冻结事件循环
        chunks = await asyncio.to_thread(self._chunker.chunk, content)
        if not chunks:
            chunks = [content] if content.strip() else []
        if not chunks:
            raise ValueError("文件内容为空，无可向量化文本")

        vectors = await self._embed(chunks)

        # v1.1.0：向量统一转 float32 ndarray（内存约为 list-of-float 的 1/8）；
        # 转换失败（如 ragged）回退原列表，下游 _build_matrix/_cosine 均兼容。
        if np is not None:
            try:
                vectors = np.asarray(vectors, dtype="float32")
            except (ValueError, TypeError):
                pass

        # v1.0.18：记录原文长度（overlap 合并会让 join(chunks) 膨胀，阈值判定要用原文长度）
        entry = FileEntry(file_name=file_name, chunks=chunks, vectors=vectors, source_chars=len(content))
        self._files[file_name] = entry
        self._mat_cache = None  # 置脏：下次检索重建矩阵
        return entry

    async def _embed(self, texts: List[str]) -> List[List[float]]:
        """调用注入的 embed_fn，兼容单条/批量两种返回结构。"""
        if not texts:
            return []
        # 归一化到 batch 调用，减少 RPC 次数
        result = self._embed_fn(texts)
        if asyncio.iscoroutine(result):
            result = await result

        vectors: List[List[float]] = []
        if isinstance(result, dict):
            # 批量：results 列表；单条：embedding 字段
            if isinstance(result.get("results"), list):
                for item in result["results"]:
                    if isinstance(item, dict) and "embedding" in item:
                        vectors.append(item["embedding"])
                    elif isinstance(item, (list, tuple)):
                        vectors.append(list(item))
            elif result.get("embedding") is not None and len(texts) == 1:
                vectors.append(list(result["embedding"]))
            elif isinstance(result.get("embeddings"), list):
                vectors = [list(v) for v in result["embeddings"]]
        elif isinstance(result, list):
            vectors = [list(v) for v in result]

        # 补齐/对齐：确保长度与 texts 一致
        if not vectors:
            raise RuntimeError("embedding 返回为空，请确认已配置嵌入模型（llm.embed 能力）")
        if len(vectors) != len(texts):
            # 向量数对不上：若只有 1 个且请求多个，报错（说明没走批量）
            if len(vectors) == 1 and len(texts) > 1:
                # 逐个降级重试
                vectors = []
                for t in texts:
                    single = await self._embed([t])
                    vectors.extend(single)
            else:
                raise RuntimeError(f"embedding 返回条数 {len(vectors)} 与请求 {len(texts)} 不一致")
        return vectors

    def _matrix_fingerprint(self) -> tuple:
        """库内容指纹：文件名集合 + 各文件块数。变化即重建矩阵。"""
        return tuple((fname, len(e.vectors)) for fname, e in self._files.items())

    def _build_matrix(self) -> None:
        """重建 L2 归一化矩阵 + 索引表。仅 numpy 可用时生效。"""
        if np is None:
            return
        rows: List[Any] = []
        index: List[tuple[str, int, str]] = []
        for fname, entry in self._files.items():
            # v1.1.0：entry.vectors 可能是 ndarray——不能用 `not vec` 判空（歧义真值）
            for i in range(len(entry.vectors)):
                vec = entry.vectors[i]
                if vec is None or len(vec) == 0:
                    continue
                rows.append(vec)
                index.append((fname, i, entry.chunks[i]))
        if not rows:
            self._mat_cache = None
            self._mat_index = []
            self._mat_fp = self._matrix_fingerprint()
            return
        mat = np.asarray(rows, dtype="float32")
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0  # 零向量归一化后仍为零，相似度 0
        self._mat_cache = mat / norms
        self._mat_index = index
        self._mat_fp = self._matrix_fingerprint()

    def _retrieve_matrix(self, query_vector: List[float], k: int) -> Optional[List[dict]]:
        """矩阵化检索：一次 matmul 算全库相似度。库为空或无 numpy 返回 None（回退逐条）。"""
        if np is None or not self._files:
            return None
        fp = self._matrix_fingerprint()
        if self._mat_cache is None or fp != self._mat_fp:
            self._build_matrix()
        if self._mat_cache is None or not self._mat_index:
            return [] if self._mat_index == [] and self._mat_cache is None else None
        q = np.asarray(query_vector, dtype="float32")
        qn = float(np.linalg.norm(q))
        if qn == 0:
            return []
        scores = self._mat_cache @ (q / qn)
        # v1.1.0：argpartition 取 Top-K（O(N)），只对 K 个候选排序；替代全排序 O(N log N)
        n = scores.shape[0]
        if n <= k:
            order = np.argsort(scores)[::-1]
        else:
            part = np.argpartition(scores, n - k)[n - k:]
            order = part[np.argsort(scores[part])[::-1]]
        return [
            {
                "file_name": self._mat_index[i][0],
                "text": self._mat_index[i][2],
                "score": float(scores[i]),
                "chunk_index": self._mat_index[i][1],
            }
            for i in order
        ]

    def retrieve(self, query_vector: List[float], top_k: Optional[int] = None) -> List[dict]:
        """返回 Top-K 相关块：[{file_name, text, score, chunk_index}, ...]

        v1.0.18 C5：retrieve_use_matrix 开启且 numpy 可用时走矩阵化路径
        （L2 归一化矩阵缓存 + 点积一次算全库），失败自动回退逐条余弦。
        """
        k = top_k or self.retrieve_top_k
        if self.retrieve_use_matrix:
            try:
                results = self._retrieve_matrix(query_vector, k)
                if results is not None:
                    return results
            except Exception:  # noqa: BLE001  矩阵路径异常一律回退逐条
                pass
        scored: List[dict] = []
        for fname, entry in self._files.items():
            for i, vec in enumerate(entry.vectors):
                scored.append(
                    {
                        "file_name": fname,
                        "text": entry.chunks[i],
                        "score": _cosine(query_vector, vec),
                        "chunk_index": i,
                    }
                )
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:k]

    def cleanup_expired(self, retention_seconds: float, max_rounds: int) -> List[str]:
        """清理过期文件（时间超限或轮数超限）。返回被清理的文件名。"""
        now = time.time()
        removed = []
        for fname in list(self._files.keys()):
            entry = self._files[fname]
            expired_time = (now - entry.upload_time) > retention_seconds
            expired_rounds = entry.rounds >= max_rounds
            if expired_time or expired_rounds:
                del self._files[fname]
                removed.append(fname)
        if removed:
            self._mat_cache = None  # 置脏：下次检索重建矩阵
        return removed

    def increment_rounds(self, file_name: Optional[str] = None) -> None:
        """给单个或全部文件增加使用轮数。"""
        names = [file_name] if file_name else list(self._files.keys())
        for n in names:
            if n in self._files:
                self._files[n].rounds += 1


class SessionStore:
    """按 (session_id, conversation_id) 隔离的向量库集合。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[tuple[str, str], VectorStore] = {}

    def key(self, session_id: str, conversation_id: str) -> tuple[str, str]:
        return (session_id, conversation_id)

    def get_or_create(
        self,
        session_id: str,
        conversation_id: str,
        embed_fn: Callable[[List[str]], Any],
        chunk_size: int,
        chunk_overlap: int,
        retrieve_top_k: int,
        chunk_merge: bool = True,
        retrieve_use_matrix: bool = False,
    ) -> VectorStore:
        k = self.key(session_id, conversation_id)
        if k not in self._sessions:
            self._sessions[k] = VectorStore(
                embed_fn,
                chunk_size,
                chunk_overlap,
                retrieve_top_k,
                chunk_merge=chunk_merge,
                retrieve_use_matrix=retrieve_use_matrix,
            )
        return self._sessions[k]

    def get(self, session_id: str, conversation_id: str) -> Optional[VectorStore]:
        return self._sessions.get(self.key(session_id, conversation_id))

    def drop(self, session_id: str, conversation_id: Optional[str] = None) -> int:
        """删除会话（或某对话）所有向量库，返回删除的文件数。"""
        removed = 0
        keys = list(self._sessions.keys())
        for k in keys:
            sid, cid = k
            if sid == session_id and (conversation_id is None or cid == conversation_id):
                removed += self._sessions[k].total_chunks()
                del self._sessions[k]
        return removed

    def list_sessions(self) -> List[tuple[str, str]]:
        return list(self._sessions.keys())

    # ── 可选落盘（JSON，便于跨重启恢复元数据，向量不落盘以免过大） ──
    def _meta_path(self) -> Path:
        return self.data_dir / "sessions_meta.json"

    def save_meta(self) -> None:
        meta = {
            f"{sid}\x1f{cid}": {
                "file_count": len(vs.files),
                "chunks": vs.total_chunks(),
                "files": [
                    {"name": fn, "rounds": e.rounds, "upload_time": e.upload_time}
                    for fn, e in vs.files.items()
                ],
            }
            for (sid, cid), vs in self._sessions.items()
        }
        try:
            self._meta_path().write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def load_meta(self) -> dict:
        p = self._meta_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
