"""递归字符分块器（自包含，等价于 astrbot 的 RecursiveCharacterChunker）

按分隔符优先级递归切分，达到 chunk_size 上限即切块，
相邻块之间保留 chunk_overlap 重叠，避免语义断裂。
"""
from __future__ import annotations

from typing import List

# 分隔符优先级：先按段落/换行，再按句子标点，最后按字符
_DEFAULT_SEPARATORS = [
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    "；",
    ".",
    "!",
    "?",
    ";",
    "，",
    ",",
    " ",
    "",
]


class RecursiveCharacterChunker:
    """递归字符分块器。"""

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 100) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须 > 0")
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap 必须 < chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def _split_text(self, text: str, seps: List[str]) -> List[str]:
        """递归切分：按第一个能命中的分隔符切，保留分隔符，避免把连续文本拆成单字符。"""
        if not text:
            return []
        if len(text) <= self.chunk_size:
            return [text]
        for sep in seps:
            if sep and sep in text:
                parts = text.split(sep)
                out: List[str] = []
                for part in parts:
                    out.extend(self._split_text(part, seps))
                return out
        # 所有分隔符都不命中（无空格无标点的连续文本）：硬切
        return self._hard_split(text)

    def _hard_split(self, text: str) -> List[str]:
        """对无分隔符的连续文本按 chunk_size 硬切（带 overlap）。"""
        out: List[str] = []
        step = self.chunk_size - self.chunk_overlap
        if step <= 0:
            step = self.chunk_size
        start = 0
        n = len(text)
        while start < n:
            out.append(text[start : start + self.chunk_size])
            start += step
        return out

    def chunk(self, text: str) -> List[str]:
        """把文本切成若干块。返回字符串列表。"""
        text = (text or "").strip()
        if not text:
            return []
        if len(text) <= self.chunk_size:
            return [text]
        return [c for c in self._split_text(text, list(_DEFAULT_SEPARATORS)) if c.strip()]
