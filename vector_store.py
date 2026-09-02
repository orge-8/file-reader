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
    vectors: List[List[float]] = field(default_factory=list)
    upload_time: float = field(default_factory=time.time)
    rounds: int = 0
    summary: str = ""  # 大文件 LLM 概要（v1.0.17）；空 = 未生成或生成失败


def _cosine(a: List[float], b: List[float]) -> float:
    """余弦相似度。空向量返回 0。"""
    if not a or not b or len(a) != len(b):
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
    ) -> None:
        # 延迟导入 chunker，避免与 plugin.py 的 sys.path 处理耦合
        from chunker import RecursiveCharacterChunker

        self._embed_fn = embed_fn
        self._chunker = RecursiveCharacterChunker(chunk_size, chunk_overlap)
        self.retrieve_top_k = retrieve_top_k
        self._files: dict[str, FileEntry] = {}

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

        chunks = self._chunker.chunk(content)
        if not chunks:
            chunks = [content] if content.strip() else []
        if not chunks:
            raise ValueError("文件内容为空，无可向量化文本")

        vectors = await self._embed(chunks)

        entry = FileEntry(file_name=file_name, chunks=chunks, vectors=vectors)
        self._files[file_name] = entry
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

    def retrieve(self, query_vector: List[float], top_k: Optional[int] = None) -> List[dict]:
        """返回 Top-K 相关块：[{file_name, text, score, chunk_index}, ...]"""
        k = top_k or self.retrieve_top_k
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
    ) -> VectorStore:
        k = self.key(session_id, conversation_id)
        if k not in self._sessions:
            self._sessions[k] = VectorStore(embed_fn, chunk_size, chunk_overlap, retrieve_top_k)
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
