"""v1.3.4：`FileEntry.full_text()` 必须按 chunk_overlap 去重。

真机 2026-11-02 22:04 日志：5875 字的 docx 经 15 块 / overlap=100 切片后，
直注给模型的全文多出约 1400 字重复段，模型在思考里直接写出
「The extracted document has repetition and some corruption」——
不是解析坏了，是 `"".join(chunks)` 把每两块的 overlap 重复拼了一遍。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chunker import RecursiveCharacterChunker  # noqa: E402
from vector_store import FileEntry  # noqa: E402


def _make_text() -> str:
    """构造多块文本：含重复短语（考验最长重叠搜索不会误切）与段落分隔符。"""
    para = "情绪识别只作为强度微调的辅助信号，运动部件锁定与物理急停不可由软件替代。"
    return "\n\n".join(f"第{i}段：{para * 3}" for i in range(1, 13))


def test_full_text_dedups_overlap():
    text = _make_text()
    chunker = RecursiveCharacterChunker(chunk_size=200, chunk_overlap=50)
    chunks = chunker.chunk(text)
    assert len(chunks) > 2, f"应切成多块，实际 {len(chunks)}"

    entry = FileEntry(file_name="x.txt", chunks=chunks, vectors=[], source_chars=len(text), chunk_overlap=50)
    merged = entry.full_text()
    assert merged == text, (
        f"拼接全文应与原文一致（当前 {len(merged)} 字 vs 原文 {len(text)} 字，"
        f"多出 {len(merged) - len(text)} 字重复）"
    )
    assert len(merged) == len(text)


def test_full_text_without_overlap_keeps_old_behaviour():
    """chunk_overlap=0（旧数据）时保持直接拼接，不改变历史语义。"""
    entry = FileEntry(file_name="x.txt", chunks=["aa", "bb"], vectors=[], chunk_overlap=0)
    assert entry.full_text() == "aabb"


def test_full_text_single_chunk():
    entry = FileEntry(file_name="x.txt", chunks=["只有一块"], vectors=[], chunk_overlap=100)
    assert entry.full_text() == "只有一块"


def test_full_text_no_false_cut_when_no_overlap_present():
    """块之间没有真实重叠时不得误切正文（短块 / 合并块场景）。"""
    entry = FileEntry(file_name="x.txt", chunks=["abc", "def", "ghi"], vectors=[], chunk_overlap=50)
    assert entry.full_text() == "abcdefghi"


def test_total_chars_prefers_source_chars():
    """阈值判定口径不变：仍是原文长度，不受去重影响。"""
    text = _make_text()
    chunker = RecursiveCharacterChunker(chunk_size=200, chunk_overlap=50)
    chunks = chunker.chunk(text)
    entry = FileEntry(file_name="x.txt", chunks=chunks, vectors=[], source_chars=len(text), chunk_overlap=50)
    assert entry.total_chars() == len(text)
