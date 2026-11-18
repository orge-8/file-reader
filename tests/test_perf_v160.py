# -*- coding: utf-8 -*-
"""v1.6.0 性能与内存专项测试。

覆盖：
- BM25 保留 early-stop、紧凑 postings、同名替换、批量删除；
- 向量库入库预建、预算拒绝不改旧 entry、只对已有 chunks 重试；
- 统一获取层释放 fdata bytes、重试包生命周期；
- GZ/ZIP/解析文本硬预算与 PDF 例外；
- 通知清理、摘要全文缓存释放、队列准入。
"""

import asyncio
import gzip
import hashlib
import os
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import file_parser  # noqa: E402
import lexical_index  # noqa: E402
import vector_store  # noqa: E402
from lexical_index import LexicalIndex  # noqa: E402
from vector_store import MemoryBudgetExceeded, VectorStore  # noqa: E402


def _new_plugin(tag="v160"):
    from fakehost import bind_context, build_context, get_default_config, load_plugin_module

    mod = load_plugin_module(PLUGIN_DIR, f"file_reader_{tag}_under_test")
    plugin = mod.create_plugin()
    bind_context(plugin, build_context("org.mai-mai.file-reader"), get_default_config(mod.FileReaderConfig))
    asyncio.run(plugin.on_load())
    return plugin, mod


async def _embed(texts):
    return [[float(len(t) % 7), 1.0] for t in texts]


async def _dead_embed(_texts):
    raise RuntimeError("embedding unavailable")


# ── BM25 内存与替换语义 ────────────────────────────────────────


def test_lexical_postings_use_compact_array():
    idx = LexicalIndex()
    idx.add("a.txt", ["第三章部署流程"])
    idx.ensure_index()
    assert idx.ready
    assert idx._postings
    assert all(hasattr(v, "buffer_info") for v in idx._postings.values())


def test_lexical_early_stop_releases_partial_index():
    idx = LexicalIndex(max_tokens=5)
    idx.add("big.txt", ["这是一段足够长的中文文本用来触发护栏", "再来一段同样长的文本"])
    assert idx.search("中文") == []
    assert idx.disabled
    assert idx._postings == {}
    assert "超过上限" in idx.disable_reason


def test_lexical_replace_drops_old_generation():
    idx = LexicalIndex()
    idx.replace("same.pdf", ["阿尔法贝塔"])
    assert idx.search("阿尔法贝塔")
    idx.replace("same.pdf", ["伽马德尔塔"])
    assert idx.doc_count == 1
    assert idx.search("阿尔法贝塔") == []
    assert idx.search("伽马德尔塔")


def test_lexical_batch_remove_is_one_generation():
    idx = LexicalIndex()
    idx.add("a", ["甲一"])
    idx.add("b", ["乙一"])
    idx.add("c", ["丙一"])
    idx.remove_files({"a", "c"})
    assert idx.doc_count == 1
    assert idx.search("乙一")


# ── 向量库：预建、预算、只重试 embedding ───────────────────────


def test_add_file_prebuilds_lexical_and_matrix():
    store = VectorStore(_embed, chunk_size=64, chunk_overlap=0, retrieve_top_k=3, retrieve_use_matrix=True)
    entry = asyncio.run(store.add_file("a.txt", "部署流程与发布说明。" * 20))
    assert entry.chunks
    assert store.lexical_state["ready"] is True
    if vector_store.np is not None:
        assert store._mat_cache is not None


def test_same_name_replaces_lexical_not_append():
    store = VectorStore(_embed, chunk_size=64, chunk_overlap=0, retrieve_top_k=3)
    asyncio.run(store.add_file("same.txt", "阿尔法贝塔旧内容"))
    asyncio.run(store.add_file("same.txt", "伽马德尔塔新内容"))
    assert store.lexical_state["docs"] == 1
    assert store.search_lexical("阿尔法贝塔", 3) == []
    assert store.search_lexical("伽马德尔塔", 3)


def test_budget_rejection_keeps_previous_entry():
    store = VectorStore(_embed, chunk_size=16, chunk_overlap=0, retrieve_top_k=2)
    old = asyncio.run(store.add_file("keep.txt", "保留的旧内容"))
    with pytest.raises(MemoryBudgetExceeded):
        asyncio.run(
            store.add_file(
                "blocked.txt",
                "超大内容" * 2000,
                budget_bytes=2000,
                other_bytes=0,
            )
        )
    assert "keep.txt" in store.files
    assert "blocked.txt" not in store.files
    assert store.files["keep.txt"] is old


def test_add_chunks_skips_chunking_for_retry():
    store = VectorStore(_embed, chunk_size=16, chunk_overlap=0, retrieve_top_k=2)

    def _boom(_text):
        raise AssertionError("重试路径不得重新分块")

    store._chunker.chunk = _boom
    entry = asyncio.run(store.add_chunks("retry.txt", ["第一段", "第二段"], source_chars=20))
    assert entry.chunks == ["第一段", "第二段"]


# ── 解析预算 ───────────────────────────────────────────────────


def test_gzip_decompressed_limit(monkeypatch, tmp_path):
    p = tmp_path / "bomb.txt.gz"
    p.write_bytes(gzip.compress(b"x" * 4096))
    monkeypatch.setattr(file_parser, "_MAX_UNCOMPRESSED_BYTES", 128)
    with pytest.raises(RuntimeError, match="压缩炸弹|上限"):
        file_parser._read_gz(str(p))


def test_non_pdf_text_limit(monkeypatch, tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("中" * 200, encoding="utf-8")
    monkeypatch.setattr(file_parser, "_MAX_TEXT_CHARS", 50)
    with pytest.raises(RuntimeError, match="parsed_text_max_chars"):
        file_parser.read_any_file_to_text(str(p), "big.txt")


def test_pdf_uses_page_limit_not_text_limit(monkeypatch, tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setitem(file_parser.SUPPORTED_EXTENSIONS, "pdf", lambda _p: "中" * 200)
    monkeypatch.setattr(file_parser, "_MAX_TEXT_CHARS", 10)
    assert file_parser.read_any_file_to_text(str(p), "doc.pdf") == "中" * 200


def test_zip_budget_rejects_declared_member(tmp_path):
    p = tmp_path / "a.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("word/document.xml", "x" * 2048)
    with zipfile.ZipFile(p) as zf:
        budget = file_parser._ZipBudget("DOCX")
        budget.limit = 100
        with pytest.raises(RuntimeError, match="超过上限"):
            budget.read(zf, "word/document.xml")


# ── 获取层与重试包 ─────────────────────────────────────────────


def test_acquire_source_spools_and_drops_fdata_bytes():
    plugin, _ = _new_plugin("spool")
    data = {"name": "a.txt", "bytes": b"payload" * 100}
    try:
        path, napcat, size, digest = asyncio.run(plugin._acquire_source(data, "a.txt", 1024))
        assert "bytes" not in data
        assert os.path.exists(path)
        assert size == 700
        assert digest == hashlib.sha256(b"payload" * 100).hexdigest()
        assert napcat is None
        os.remove(path)
    finally:
        asyncio.run(plugin.on_unload())


def test_embedding_failure_writes_bundle_and_releases_raw_temp():
    plugin, mod = _new_plugin("bundle")
    calls = {"parse": 0}
    orig = mod.read_any_file_to_text

    def _parse(_p, _n):
        calls["parse"] += 1
        return "可重试的文档内容。" * 30

    mod.read_any_file_to_text = _parse
    plugin._embed = _dead_embed
    try:
        asyncio.run(plugin._handle_file("s", "c", "", {"name": "a.txt", "bytes": b"abc"}))
        assert calls["parse"] == 1
        assert len(plugin._embed_retry_queue) == 1
        item = plugin._embed_retry_queue[0]
        assert item["bundle_path"] and os.path.exists(item["bundle_path"])
        assert item["bundle_bytes"] > 0
        assert plugin._retry_disk_bytes == item["bundle_bytes"]
        bundle = plugin._read_retry_bundle(item["bundle_path"])
        assert bundle["chunks"] and bundle["sha256"]

        # 重试路径只使用已保存 chunks；把 chunker 改成爆炸也能成功
        plugin._embed = _embed
        vs = plugin._store.get("s", "c")
        if vs is None:
            vs = plugin._get_vs("s", "c")
        vs._embed_fn = _embed
        vs._chunker.chunk = lambda _t: (_ for _ in ()).throw(AssertionError("不得重新分块"))
        entry = asyncio.run(
            vs.add_chunks("a.txt", [str(c) for c in bundle["chunks"]], source_chars=bundle["source_chars"])
        )
        assert entry.chunks
    finally:
        mod.read_any_file_to_text = orig
        asyncio.run(plugin.on_unload())
    assert not os.path.exists(item["bundle_path"])
    assert plugin._retry_disk_bytes == 0


def test_retry_loop_reuses_bundle_without_reparse():
    plugin, mod = _new_plugin("retryloop")
    calls = {"parse": 0}
    orig = mod.read_any_file_to_text

    def _parse(_p, _n):
        calls["parse"] += 1
        return "重试循环使用已保存分块。" * 20

    mod.read_any_file_to_text = _parse
    plugin._embed = _dead_embed
    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds, *a, **kw):
        await real_sleep(0)

    async def _drive():
        await plugin._handle_file("r", "r", "", {"name": "loop.txt", "bytes": b"loop"})
        assert calls["parse"] == 1
        item = plugin._embed_retry_queue[0]
        item["next_ts"] = 0
        plugin._embed = _embed
        plugin._store.get("r", "r")._embed_fn = _embed
        await plugin._start_retry_loop()
        import unittest.mock as mock

        with mock.patch.object(asyncio, "sleep", _fast_sleep):
            await real_sleep(0.3)
        plugin._retry_task.cancel()
        await asyncio.gather(plugin._retry_task, return_exceptions=True)
        assert calls["parse"] == 1, "重试绝不能重新解析原文件"
        assert "loop.txt" in plugin._store.get("r", "r").files
        assert not plugin._embed_retry_queue

    try:
        asyncio.run(_drive())
    finally:
        mod.read_any_file_to_text = orig
        asyncio.run(plugin.on_unload())


def test_retry_disk_budget_refuses_without_writing(monkeypatch):
    plugin, _ = _new_plugin("retrybudget")
    try:
        p, n = plugin._persist_retry_bundle("a.txt", ["内容"], 2, False, "d" * 64)
        plugin._discard_retry_bundle({"bundle_path": p, "bundle_bytes": n})
        plugin.config.reader.retry_disk_budget_mb = 1
        plugin._retry_disk_bytes = 1024 * 1024
        with pytest.raises(RuntimeError, match="超过"):
            plugin._persist_retry_bundle("b.txt", ["内容" * 5000], 10000, False, "e" * 64)
    finally:
        asyncio.run(plugin.on_unload())


# ── 生命周期与诊断 ─────────────────────────────────────────────


def test_prune_notices_removes_delivered_and_empty_keys():
    plugin, _ = _new_plugin("notices")
    try:
        plugin._file_notices["s"] = [
            {"name": "a", "reason": "x", "ts": time.time(), "delivered": True},
            {"name": "b", "reason": "x", "ts": time.time() - 99999, "delivered": False},
        ]
        plugin._prune_notices()
        assert "s" not in plugin._file_notices
    finally:
        asyncio.run(plugin.on_unload())


def test_summary_releases_full_text_cache():
    plugin, _ = _new_plugin("summary")
    try:
        vs = plugin._get_vs("s", "c")
        vs._embed_fn = _embed
        entry = asyncio.run(vs.add_file("big.txt", "摘要内容。" * 2000))
        assert entry.total_chars() > int(plugin.config.reader.direct_inject_max_chars)
        assert entry.full_text()
        asyncio.run(plugin._generate_file_summary(entry))
        assert entry._full_text_cache == ""
    finally:
        asyncio.run(plugin.on_unload())


def test_queue_full_rejects_new_without_dropping_old():
    plugin, mod = _new_plugin("queue")
    orig = mod.read_any_file_to_text

    def _slow(_p, _n):
        time.sleep(0.25)
        return "慢解析内容。"

    mod.read_any_file_to_text = _slow
    plugin._embed = _embed
    plugin.config.reader.pending_file_limit = 1
    plugin._pending_sem = asyncio.Semaphore(1)
    try:
        async def _drive():
            t1 = asyncio.create_task(plugin._handle_file("q", "q", "", {"name": "a.txt", "bytes": b"1"}))
            await asyncio.sleep(0.02)
            t2 = asyncio.create_task(plugin._handle_file("q", "q", "", {"name": "b.txt", "bytes": b"2"}))
            await asyncio.gather(t1, t2)

        asyncio.run(_drive())
        assert plugin._load_metrics["rejected_queue"] == 1
        assert "a.txt" in plugin._store.get("q", "q").files
    finally:
        mod.read_any_file_to_text = orig
        asyncio.run(plugin.on_unload())


def test_status_reports_pid_and_budget():
    plugin, _ = _new_plugin("status")
    try:
        ok, text, _ = asyncio.run(plugin.cmd_status(stream_id=""))
        assert ok
        assert "进程 PID" in text and "数据占用估算" in text and "处理槽" in text
        assert "postings" in text
    finally:
        asyncio.run(plugin.on_unload())
