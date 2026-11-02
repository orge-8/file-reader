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

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 100, merge: bool = True) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须 > 0")
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap 必须 < chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        # v1.0.18：短碎片合并开关；False = 旧行为（逐碎片成块）
        self.merge = bool(merge)

    def _split_text(self, text: str, seps: List[str]) -> List[str]:
        """递归切分：按第一个能命中的分隔符切，**分隔符附着在前一片段末尾**。

        v1.3.0 修复：旧实现 `text.split(sep)` 切完直接丢掉分隔符，递归也不拼回，
        `"你好。世界"` 变成 `"你好世界"`、段落换行全部消失——注入给模型的文本没有
        标点与分段，代码/表格最严重。这个 bug 不报错、不少块，只能靠
        `join(chunks) == 原文` 的等价性断言发现。

        两个关键约束：
        - 递归必须传 `seps[i + 1:]`：片段末尾带了刚用过的分隔符，继续传全量 seps
          会在「片段 + 分隔符」上再次按同一分隔符命中，导致无限递归。
        - 空片段（连续分隔符）直接跳过：允许丢掉多余分隔符，但不能为了保留它
          产出纯空白块。
        """
        if not text:
            return []
        if len(text) <= self.chunk_size:
            return [text]
        for i, sep in enumerate(seps):
            if not sep or sep not in text:
                continue
            parts = text.split(sep)
            last = len(parts) - 1
            # 连续分隔符（"\n\n\n"、缩进空格）会产生空片段。空片段本身不入库，
            # 但它代表的分隔符必须并进相邻片段——否则段落空行与代码缩进照样丢失。
            merged: List[str] = []
            pending = ""
            for j, part in enumerate(parts):
                tail = sep if j < last else ""
                if part == "":
                    if merged:
                        merged[-1] = merged[-1] + tail
                    else:
                        pending = pending + tail
                    continue
                if pending:
                    part = pending + part
                    pending = ""
                merged.append(part + tail)
            out: List[str] = []
            for piece in merged:
                out.extend(self._split_text(piece, seps[i + 1 :]))
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

    def _pack(self, pieces: List[str]) -> List[str]:
        """贪心装箱：把相邻短碎片合并到接近 chunk_size，并在相邻块间补 overlap。

        v1.0.18 性能关键：递归切分只保证「每块不超过 chunk_size」，不保证「接近」——
        docx 解析逐段输出时每段只有一百多字，实测 8850 字切出 61 块（平均 143 字），
        块数被放大约 3 倍，直接乘进 embedding 耗时（块数 × 每批 2s）。
        合并后块数降到约 1/3，同时给递归路径补上原本缺失的 overlap。
        """
        if not pieces or not self.merge:
            return list(pieces)

        out: List[str] = []
        buf = ""
        for piece in pieces:
            if not piece:
                continue
            if len(piece) > self.chunk_size:
                # 超长块（硬切残留）：不拆，先落盘缓冲区再原样保留
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(piece)
                continue
            if not buf:
                buf = piece
                continue
            if len(buf) + len(piece) <= self.chunk_size:
                buf += piece  # 保序拼接；v1.3.0 起碎片**确实**自带分隔符后缀，不额外插入
                continue
            out.append(buf)
            # 新块以旧块尾部 overlap 字符开头，避免语义断裂
            prefix = buf[-self.chunk_overlap:] if self.chunk_overlap > 0 else ""
            cand = prefix + piece
            # 装不下就丢弃 overlap，保证最终块长恒 ≤ chunk_size
            buf = cand if len(cand) <= self.chunk_size else piece
        if buf:
            out.append(buf)
        return out

    def chunk(self, text: str) -> List[str]:
        """把文本切成若干块。返回字符串列表。"""
        text = (text or "").strip()
        if not text:
            return []
        if len(text) <= self.chunk_size:
            return [text]
        pieces = self._split_text(text, list(_DEFAULT_SEPARATORS))
        # v1.3.0：纯空白片段（段落空行、缩进残留）**并入相邻块**而不是丢弃。
        # 旧版 `if c.strip()` 会把 "\n\n" 整段扔掉——分隔符保留做得再对，
        # 到这里照样把段落与缩进结构丢光。放不下的空白片段才丢弃（宁可少一个空行，
        # 也不产出超长块）。
        normalized: List[str] = []
        for piece in pieces:
            if not piece:
                continue
            if piece.strip():
                normalized.append(piece)
                continue
            if normalized and len(normalized[-1]) + len(piece) <= self.chunk_size:
                normalized[-1] = normalized[-1] + piece
        return self._pack(normalized)
