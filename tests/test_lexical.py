# -*- coding: utf-8 -*-
"""词法索引 / RRF / 阈值测试（v1.3.0 批次 2）。

纯模块测试，不加载插件、不需要 embedding——BM25 与融合逻辑必须是可脱机验证的，
否则"检索变好还是变差"永远只能靠体感。
"""

from lexical_index import (
    LexicalIndex,
    apply_relevance_gate,
    rrf_fuse,
    tokenize,
)

CHUNKS = [
    "第三章 部署流程。执行 deploy.sh 完成发布。",
    "函数 read_any_file_to_text 负责把文件读成纯文本。",
    "预算表：一季度 120 万，二季度 150 万。",
    " unrelated 段落，讲天气不错适合出游。",
]


def _index(chunks=None, max_tokens=0):
    idx = LexicalIndex(max_tokens=max_tokens)
    idx.add("手册.pdf", chunks if chunks is not None else CHUNKS)
    return idx


# ── 分词 ────────────────────────────────────────────────────────


def test_tokenize_cjk_bigram():
    assert tokenize("第三章") == ["第三", "三章"]


def test_tokenize_single_cjk_char():
    assert tokenize("猫") == ["猫"]


def test_tokenize_identifier_whole_and_subwords():
    toks = tokenize("read_any_file_to_text")
    assert "read_any_file_to_text" in toks
    for sub in ("read", "any", "file", "to", "text"):
        assert sub in toks


def test_tokenize_numbers():
    assert "404" in tokenize("返回 404 错误")


def test_tokenize_mixed():
    toks = tokenize("部署 deploy.sh")
    assert "部署" in toks
    assert "deploy.sh" in toks
    assert "deploy" in toks and "sh" in toks


def test_tokenize_empty():
    assert tokenize("") == []


# ── BM25 ────────────────────────────────────────────────────────


def test_bm25_exact_function_name_ranks_first():
    """纯向量最容易丢的一类：查具体函数名，字面命中必须第一。"""
    hits = _index().search("read_any_file_to_text", k=4)
    assert hits, "字面上完全存在的函数名竟然检索不到"
    assert hits[0]["chunk_index"] == 1
    assert "read_any_file_to_text" in hits[0]["text"]


def test_bm25_chinese_phrase_hit():
    hits = _index().search("部署流程", k=4)
    assert hits and hits[0]["chunk_index"] == 0


def test_bm25_irrelevant_chunk_scores_zero():
    """查询词一个都不在的块不得出现在结果里。"""
    hits = _index().search("季度预算报表汇总", k=4)
    idxs = {h["chunk_index"] for h in hits}
    assert 3 not in idxs  # 天气段落


def test_bm25_respects_k():
    assert len(_index().search("部署", k=2)) <= 2


def test_bm25_empty_query_returns_empty():
    assert _index().search("", k=3) == []
    assert _index().search("   ", k=3) == []


# ── 删除与重建 ──────────────────────────────────────────────────


def test_remove_file_drops_its_chunks():
    # 刻意用不共享二元组的词，避免「内容甲/内容乙」因共有 token 而互相命中
    idx = LexicalIndex()
    idx.add("a.pdf", ["阿尔法贝塔"])
    idx.add("b.pdf", ["伽马德尔塔"])
    idx.remove_file("a.pdf")
    assert idx.search("阿尔法贝塔", k=5) == []
    assert idx.search("伽马德尔塔", k=5)


def test_rebuild_after_removal_keeps_others():
    idx = LexicalIndex()
    idx.add("a.pdf", ["甲一", "甲二"])
    idx.add("b.pdf", ["乙一"])
    idx.search("甲", k=5)  # 建索引
    idx.remove_file("a.pdf")
    hits = idx.search("乙一", k=5)
    assert len(hits) == 1 and hits[0]["file_name"] == "b.pdf"


# ── 内存护栏 ────────────────────────────────────────────────────


def test_token_budget_disables_index_without_raising():
    idx = LexicalIndex(max_tokens=5)  # 极小上限，必然触发
    idx.add("big.pdf", ["这是一段足够长的中文文本用来触发护栏", "再来一段同样长的文本"])
    hits = idx.search("中文", k=3)
    assert hits == []
    assert idx.disabled is True
    assert "超过上限" in idx.disable_reason


def test_disabled_index_stays_usable_for_vector_path():
    """停用后不能抛异常——主流程只是退回纯向量。"""
    idx = LexicalIndex(max_tokens=1)
    idx.add("x.pdf", ["中文内容"])  # 3 个二元组 token，必然超上限 1
    assert idx.search("中文内容") == []
    assert idx.disabled


def test_max_tokens_zero_means_unlimited():
    idx = LexicalIndex(max_tokens=0)
    idx.add("x.pdf", ["中文内容"])
    assert idx.search("中文内容")


# ── RRF 融合 ────────────────────────────────────────────────────


def _hit(name, idx, score=0.0):
    return {"file_name": name, "chunk_index": idx, "text": f"{name}#{idx}", "score": score}


def test_rrf_two_lists_beat_single_list():
    """向量第 1 + 词法第 5 的块，应排在只上一榜的块之前。"""
    vector = [_hit("f", 9), _hit("f", 1), _hit("f", 2)]
    lexical = [_hit("f", 3), _hit("f", 4), _hit("f", 5), _hit("f", 6), _hit("f", 1)]
    fused = rrf_fuse(vector, lexical, rrf_k=60)
    top_idx = [h["chunk_index"] for h in fused[:3]]
    assert 1 in top_idx, "两榜都出现的块必须排在前面"


def test_rrf_source_labels():
    fused = rrf_fuse([_hit("f", 1)], [_hit("f", 1), _hit("f", 2)], rrf_k=60)
    by_idx = {h["chunk_index"]: h for h in fused}
    assert by_idx[1]["source"] == "both"
    assert by_idx[2]["source"] == "lexical"


def test_rrf_single_sided_lists():
    """一路为空时直接用另一路，不报错、不产生空结果。"""
    fused = rrf_fuse([_hit("f", 1)], [], rrf_k=60)
    assert len(fused) == 1 and fused[0]["source"] == "vector"
    fused = rrf_fuse([], [_hit("f", 2)], rrf_k=60)
    assert len(fused) == 1 and fused[0]["source"] == "lexical"


def test_rrf_empty_both():
    assert rrf_fuse([], []) == []


def test_rrf_preserves_scores():
    fused = rrf_fuse([_hit("f", 1, 0.9)], [_hit("f", 2, 3.5)], rrf_k=60)
    by_idx = {h["chunk_index"]: h for h in fused}
    assert by_idx[1]["vector_score"] == 0.9
    assert by_idx[2]["lexical_score"] == 3.5


# ── 相关度阈值 ──────────────────────────────────────────────────


def test_gate_drops_low_vector_score():
    hits = [
        {"chunk_index": 0, "score": 1.0, "vector_score": 0.8},
        {"chunk_index": 1, "score": 0.2, "vector_score": 0.05},
    ]
    out = apply_relevance_gate(hits, min_score=0.15, min_ratio=0)
    assert [h["chunk_index"] for h in out] == [0]


def test_gate_keeps_lexical_only_hits():
    """纯词法命中没有向量分，不能被余弦阈值掐死（否则降级通路失效）。"""
    hits = [
        {"chunk_index": 0, "score": 1.0, "vector_score": 0.9},
        {"chunk_index": 1, "score": 0.5, "vector_score": None},
    ]
    out = apply_relevance_gate(hits, min_score=0.15, min_ratio=0)
    assert {h["chunk_index"] for h in out} == {0, 1}


def test_gate_ratio_filters_relative_to_top1():
    hits = [
        {"chunk_index": 0, "score": 1.0, "vector_score": 0.9},
        {"chunk_index": 1, "score": 0.2, "vector_score": 0.8},
    ]
    out = apply_relevance_gate(hits, min_score=0, min_ratio=0.5)
    assert [h["chunk_index"] for h in out] == [0]


def test_gate_all_filtered_returns_empty():
    hits = [{"chunk_index": 0, "score": 0.1, "vector_score": 0.01}]
    assert apply_relevance_gate(hits, min_score=0.5, min_ratio=0) == []


def test_gate_disabled_when_zero():
    hits = [{"chunk_index": 0, "score": 0.1, "vector_score": 0.01}]
    assert apply_relevance_gate(hits, min_score=0, min_ratio=0) == hits
