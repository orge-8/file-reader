# -*- coding: utf-8 -*-
"""分块器行为测试（v1.3.0 批次 1：分隔符保留）。

为什么要这组用例
----------------
旧实现 `text.split(sep)` 切完把分隔符丢掉，递归时也不拼回，结果是：
`"你好。世界"` 变成 `"你好世界"`，段落换行全部消失。注入给模型的文本没有标点
和分段——代码/表格最严重。这个 bug 不会报错、不会少块，只能靠「拼接回原文」的
等价性断言抓住（块数断言抓不到：块数可以不变更丢内容）。

判定标准（join 等价性）：
  chunk_overlap=0 时，`"".join(chunks) == 原文` 必须成立（merge 开/关各一组）。
  有 overlap 时拼接会重复，不适用该断言，只验块长上限。
"""

import pytest

from chunker import RecursiveCharacterChunker

ZH = (
    "第一章 项目背景。本项目旨在解决文件解析中的结构丢失问题，"
    "第二章 技术方案。采用递归字符分块与分隔符保留策略，"
    "第三章 验收标准。所有分块拼接后必须与原文完全一致。"
)

CODE = (
    "def read_any_file_to_text(path: str) -> str:\n"
    "    with open(path, 'rb') as f:\n"
    "        raw = f.read()\n"
    "    return raw.decode('utf-8')\n"
)

PARA = "第一段讲的是背景。\n\n第二段讲的是方案。\n\n第三段讲的是验收。\n"


def _chunk(text, size=64, overlap=0, merge=True):
    return RecursiveCharacterChunker(size, overlap, merge=merge).chunk(text)


# ── 等价性：分块不得丢字符 ──────────────────────────────────────


def test_join_equals_source_with_merge():
    """开启碎片合并时，拼接结果必须等于原文。"""
    assert "".join(_chunk(ZH, size=40, overlap=0, merge=True)) == ZH


def test_join_equals_source_without_merge():
    """关闭碎片合并（旧行为兼容路径）时同样不得丢字符。"""
    assert "".join(_chunk(ZH, size=40, overlap=0, merge=False)) == ZH


def test_join_equals_source_code_sample():
    """代码样例：换行与缩进必须完整保留。

    注：chunk() 入口会 `text.strip()`（既有语义，去掉首尾空白），所以基准用 strip 后的原文。
    """
    assert "".join(_chunk(CODE, size=30, overlap=0, merge=True)) == CODE.strip()


def test_join_equals_source_paragraphs():
    """段落分隔（\\n\\n）不得被吞掉。"""
    assert "".join(_chunk(PARA, size=20, overlap=0, merge=True)) == PARA.strip()


# ── 分隔符本身不丢 ──────────────────────────────────────────────


def test_chinese_punctuation_kept():
    """中文句号/逗号/问号保留：拼接后标点数量与原文一致。"""
    src = "你好。世界，你好吗？我还行。"
    out = "".join(_chunk(src, size=6, overlap=0, merge=True))
    assert out == src
    for p in ("。", "，", "？"):
        assert out.count(p) == src.count(p)


def test_paragraph_break_kept():
    """\\n\\n 是最高优先级分隔符，切完必须仍在。"""
    src = "AAAAAAAAAAAA。\n\nBBBBBBBBBBBB。"
    out = "".join(_chunk(src, size=10, overlap=0, merge=True))
    assert out.count("\n\n") == src.count("\n\n")


def test_hard_split_without_separator():
    """无分隔符长串：硬切且块长不超过 chunk_size，拼接仍等于原文。"""
    src = "A" * 500
    chunks = _chunk(src, size=64, overlap=0, merge=True)
    assert len(chunks) > 1
    assert all(len(c) <= 64 for c in chunks)
    assert "".join(chunks) == src


# ── 基本边界 ────────────────────────────────────────────────────


def test_short_text_returned_as_is():
    c = RecursiveCharacterChunker(512, 100)
    assert c.chunk("很短的一句话。") == ["很短的一句话。"]


def test_empty_text_returns_empty():
    c = RecursiveCharacterChunker(512, 100)
    assert c.chunk("") == []
    assert c.chunk("   \n\t  ") == []


def test_overlap_ge_chunk_size_raises():
    with pytest.raises(ValueError):
        RecursiveCharacterChunker(100, 100)
    with pytest.raises(ValueError):
        RecursiveCharacterChunker(100, 200)


def test_chunk_size_must_be_positive():
    with pytest.raises(ValueError):
        RecursiveCharacterChunker(0, 0)


def test_overlap_blocks_within_size_limit():
    """有 overlap 时块长仍不得超过 chunk_size（装箱阶段会丢弃放不下的 overlap）。"""
    chunks = _chunk(ZH, size=40, overlap=10, merge=True)
    assert chunks
    assert all(len(c) <= 40 for c in chunks)


def test_no_infinite_recursion_on_repeated_sep():
    """连续重复分隔符（如多个空行）不得导致无限递归或纯空白块。"""
    src = "AAAAAAAAAAAA。\n\n\n\n\nBBBBBBBBBBBB。"
    chunks = _chunk(src, size=10, overlap=0, merge=True)
    assert chunks
    assert all(c.strip() for c in chunks)
