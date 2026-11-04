# -*- coding: utf-8 -*-
"""v1.4.0 读取提速专项：A0 分段耗时 / A1 重复处理去重 / A2 PDF 引擎 / A4 编码快路径 / A5 重发缓存。

对应施工蓝图 `docs/perf-plan.md`。每条用例直接锚定真机证据：
  - A1：11-03 15:47「已入库 奶蛙学…pdf：182 块」出现两次（解析 + embedding 各做两遍）
  - A2：11-03 15:22~15:26 main loop 多次秒级卡顿，落在 pdfminer 解析窗口内
  - A5：同一会话重发同一文件，解析与向量化全部重跑
"""

import asyncio
import io
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import file_parser  # noqa: E402
import vector_store  # noqa: E402


def _new_plugin(tag: str = "perf"):
    """构造真实插件实例（FakeHost 上下文），用完必须 on_unload。"""
    from fakehost import bind_context, build_context, get_default_config, load_plugin_module

    mod = load_plugin_module(PLUGIN_DIR, f"file_reader_{tag}_under_test")
    plugin = mod.create_plugin()
    bind_context(plugin, build_context("org.mai-mai.file-reader"), get_default_config(mod.FileReaderConfig))
    asyncio.run(plugin.on_load())
    return plugin, mod


async def _fake_embed(texts):
    """伪 embedding：不依赖真模型，返回长度固定的小向量。"""
    return [[float(len(t) % 7), 1.0] for t in texts]


# ─── A2：PDF 引擎分层 ───────────────────────────────────────────────


class _FakePage:
    def __init__(self, text: str):
        self._text = text
        self.closed = False

    def get_textpage(self):
        return SimpleNamespace(get_text_range=lambda: self._text)

    def close(self):
        self.closed = True


class _FakePdf:
    def __init__(self, pages):
        self._pages = [_FakePage(t) for t in pages]
        self.closed = False

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, i):
        return self._pages[i]

    def close(self):
        self.closed = True


def _patch_pdfium(monkeypatch, pages):
    """注入伪 pypdfium2 模块对象（不依赖真轮子）。"""
    holder = {}

    def _factory(_path):
        pdf = _FakePdf(pages)
        holder["pdf"] = pdf
        return pdf

    monkeypatch.setattr(file_parser, "_pdfium", SimpleNamespace(PdfDocument=_factory))
    return holder


def test_pdf_prefers_pypdfium2(monkeypatch, tmp_path):
    """pypdfium2 可用时必须走它，且不再调用 pdfminer（C 扩展释放 GIL，不拖事件循环）。"""
    pdf_path = tmp_path / "a.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    holder = _patch_pdfium(monkeypatch, ["第一页内容", "第二页内容"])

    called = {"pdfminer": False}

    def _boom(*a, **k):
        called["pdfminer"] = True
        return "不该走到这里"

    monkeypatch.setattr(file_parser, "extract_text", _boom)
    text = file_parser._read_pdf(str(pdf_path))

    assert "第一页内容" in text and "第二页内容" in text
    assert called["pdfminer"] is False, "pypdfium2 成功时不得再跑 pdfminer"
    assert holder["pdf"].closed, "PDF 文档句柄必须关闭（真机会句柄泄漏）"


def test_pdf_truncates_to_max_pages(monkeypatch, tmp_path):
    """超过 reader.pdf_max_pages 时截断，并把「共 N 页、只读前 M 页」写进解析提示。"""
    pdf_path = tmp_path / "b.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    _patch_pdfium(monkeypatch, [f"第{i}页" for i in range(5)])
    monkeypatch.setattr(file_parser, "_PDF_MAX_PAGES", 2)

    notes: list[str] = []
    text = file_parser._read_pdf(str(pdf_path), notes=notes)

    assert text.count("页") == 2, f"应只解析 2 页: {text!r}"
    assert notes and "共 5 页" in notes[0] and "前 2 页" in notes[0]


def test_pdf_empty_text_reports_scanned_pdf(monkeypatch, tmp_path):
    """纯扫描件（无文本层）直接给准确原因，不让 pdfminer 白跑几分钟。"""
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    _patch_pdfium(monkeypatch, ["", ""])

    called = {"pdfminer": False}
    monkeypatch.setattr(file_parser, "extract_text", lambda *a, **k: called.__setitem__("pdfminer", True) or "x")

    with pytest.raises(RuntimeError) as ei:
        file_parser._read_pdf(str(pdf_path))
    assert "扫描件" in str(ei.value)
    assert called["pdfminer"] is False


def test_pdf_falls_back_to_pdfminer(monkeypatch, tmp_path):
    """pypdfium2 抛异常（损坏 PDF / 缺 DLL）时降级 pdfminer，行为与旧版一致。"""
    pdf_path = tmp_path / "c.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    def _broken(_path):
        raise RuntimeError("pdfium 打不开")

    monkeypatch.setattr(file_parser, "_pdfium", SimpleNamespace(PdfDocument=_broken))
    monkeypatch.setattr(file_parser, "extract_text", lambda *a, **k: "pdfminer 救回来了")
    assert file_parser._read_pdf(str(pdf_path)) == "pdfminer 救回来了"


def test_pdf_both_engines_fail_mentions_both(monkeypatch, tmp_path):
    """双引擎都失败时，报错必须带上两边的失败原因（否则排障只能猜）。"""
    pdf_path = tmp_path / "d.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(
        file_parser, "_pdfium", SimpleNamespace(PdfDocument=lambda _p: (_ for _ in ()).throw(RuntimeError("A挂了")))
    )
    monkeypatch.setattr(
        file_parser, "extract_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("B也挂了"))
    )
    with pytest.raises(RuntimeError) as ei:
        file_parser._read_pdf(str(pdf_path))
    msg = str(ei.value)
    assert "A挂了" in msg and "B也挂了" in msg


def test_pdf_without_any_engine_gives_actionable_error(monkeypatch, tmp_path):
    pdf_path = tmp_path / "e.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(file_parser, "_pdfium", None)
    monkeypatch.setattr(file_parser, "extract_text", None)
    with pytest.raises(RuntimeError) as ei:
        file_parser._read_pdf(str(pdf_path))
    assert "pypdfium2" in str(ei.value)


def test_parse_notes_reach_event_loop_through_to_thread(monkeypatch, caplog):
    """ContextVar 必须能穿过 asyncio.to_thread 回到事件循环侧（并发文件互不串台）。

    这是 A2「截断要在日志/回执里说清楚」的实现机制；穿不过去就等于提示只在
    工作线程里生成、主线程永远读不到。
    """
    plugin, _ = _new_plugin("pdfflow")

    def _factory(_path):
        return _FakePdf(["第一页", "第二页"])  # 2 页 + 上限 1 页 ⇒ 必然触发截断提示

    monkeypatch.setattr(file_parser, "_pdfium", SimpleNamespace(PdfDocument=_factory))
    monkeypatch.setattr(file_parser, "_PDF_MAX_PAGES", 1)
    plugin._embed = _fake_embed
    try:
        with caplog.at_level(logging.WARNING):
            asyncio.run(
                plugin._handle_file(
                    "p", "p", "", {"name": "一页.pdf", "bytes": b"%PDF-1.4 fake"}
                )
            )
        assert "解析提示" in caplog.text and "前 1 页" in caplog.text, caplog.text[-600:]
        vs = plugin._store.get("p", "p")
        assert vs is not None and "一页.pdf" in vs.files
    finally:
        asyncio.run(plugin.on_unload())


# ─── A4：文本编码快路径 ─────────────────────────────────────────────


def test_txt_utf8_fast_path_skips_detection(tmp_path, monkeypatch):
    """UTF-8 文件必须零探测：chardet.detect 一次都不能调（50MB 日志的秒级开销在这）。"""
    p = tmp_path / "log.txt"
    p.write_text("中文日志内容\n" * 1000, encoding="utf-8")

    calls = {"n": 0}

    class _Trap:
        @staticmethod
        def detect(_raw):
            calls["n"] += 1
            return {"encoding": "utf-8", "confidence": 0.99}

    monkeypatch.setattr(file_parser, "chardet", _Trap)
    text = file_parser._read_txt(str(p))
    assert text.startswith("中文日志内容")
    assert calls["n"] == 0, "UTF-8 快路径命中时不应触发编码探测"


def test_txt_gbk_falls_back_correctly(tmp_path):
    """非 UTF-8 仍要正确解码（GBK 老文件不能变成乱码）。"""
    p = tmp_path / "gbk.txt"
    p.write_bytes("这是一段 GBK 编码的中文".encode("gbk"))
    assert file_parser._read_txt(str(p)) == "这是一段 GBK 编码的中文"


def test_txt_detection_only_reads_head(tmp_path, monkeypatch):
    """探测样本必须被截到 64KB 以内（全量探测是旧版的性能坑）。"""
    p = tmp_path / "big.txt"
    p.write_bytes(b"\xff\xfe" + b"a" * 200000)  # 非法 UTF-8，强制走探测分支

    seen = {}

    class _Trap:
        @staticmethod
        def detect(raw):
            seen["len"] = len(raw)
            return {"encoding": "utf-8", "confidence": 0.1}

    monkeypatch.setattr(file_parser, "chardet", _Trap)
    file_parser._read_txt(str(p))
    assert seen.get("len", 0) <= 65536, f"探测样本过大: {seen}"


# ─── A0：分段耗时观测 ───────────────────────────────────────────────


def test_add_file_reports_timing_breakdown():
    """add_file 的 timing 出参必须含 chunk/embed/index/total 四段且量纲合理。"""
    store = vector_store.VectorStore(_fake_embed, chunk_size=64, chunk_overlap=0, retrieve_top_k=3)
    timing: dict[str, float] = {}

    entry = asyncio.run(store.add_file("t.txt", "内容" * 500, timing=timing))

    assert entry.chunks
    for key in ("chunk", "embed", "index", "total"):
        assert key in timing, f"缺 {key} 段耗时: {timing}"
        assert timing[key] >= 0
    assert timing["total"] >= max(timing["chunk"], timing["embed"])


def test_handle_file_logs_four_stage_timing(caplog):
    """真机再发一次文件，日志能直接读出四段耗时（26min 空窗的归因地基）。"""
    plugin, mod = _new_plugin("timing")
    orig = mod.read_any_file_to_text
    mod.read_any_file_to_text = lambda p, n: "情绪识别只作为强度微调的辅助信号。" * 20
    plugin._embed = _fake_embed
    try:
        with caplog.at_level(logging.INFO):
            asyncio.run(plugin._handle_file("s", "c", "", {"name": "t.txt", "bytes": b"abc"}))
        assert "取字节=" in caplog.text and "embedding=" in caplog.text, caplog.text[-800:]
    finally:
        mod.read_any_file_to_text = orig
        asyncio.run(plugin.on_unload())


# ─── A1：重复处理去重（in-flight 注册表） ────────────────────────────


def test_concurrent_duplicate_triggers_parsed_once():
    """同一 file_id 被并发投递两次：只解析一次、只入库一次。

    旧版 `_recent_file_keys` 是 check-then-act，两个任务同时通过检查 ⇒ 成本 ×2
    （真机 11-03 15:47 的「已入库 …182 块」出现两次）。
    """
    plugin, mod = _new_plugin("dedup")
    calls = {"parse": 0}
    orig = mod.read_any_file_to_text

    def _counting_parse(_p, _n):
        calls["parse"] += 1
        time.sleep(0.2)
        return "内容" * 100

    mod.read_any_file_to_text = _counting_parse
    plugin._embed = _fake_embed
    try:

        async def _drive():
            fdata = {"name": "同样的.pdf", "file_id": "F1", "bytes": b"x" * 10}
            t1 = asyncio.create_task(plugin._handle_file("g", "g", "", dict(fdata)))
            t2 = asyncio.create_task(plugin._handle_file("g", "g", "", dict(fdata)))
            await asyncio.gather(t1, t2)

        asyncio.run(_drive())
        assert calls["parse"] == 1, f"同一文件被并发触发只应解析一次，实际 {calls['parse']} 次"
        vs = plugin._store.get("g", "g")
        assert vs is not None and len(vs.files) == 1
        assert not plugin._file_inflight, "全部结束后必须清空 in-flight 登记"
    finally:
        mod.read_any_file_to_text = orig
        asyncio.run(plugin.on_unload())


def test_different_files_in_same_session_both_processed():
    """去重不能误伤：同会话两个不同文件必须都处理。"""
    plugin, mod = _new_plugin("dedup2")
    parsed: list[str] = []
    orig = mod.read_any_file_to_text

    def _parse(_p, name):
        parsed.append(name)
        time.sleep(0.1)
        return "内容" * 100

    mod.read_any_file_to_text = _parse
    plugin._embed = _fake_embed
    try:

        async def _drive():
            await asyncio.gather(
                plugin._handle_file("g", "g", "", {"name": "a.txt", "file_id": "A", "bytes": b"1"}),
                plugin._handle_file("g", "g", "", {"name": "b.txt", "file_id": "B", "bytes": b"2"}),
            )

        asyncio.run(_drive())
        assert sorted(parsed) == ["a.txt", "b.txt"], parsed
        assert len(plugin._store.get("g", "g").files) == 2
    finally:
        mod.read_any_file_to_text = orig
        asyncio.run(plugin.on_unload())


def test_inflight_released_after_failure():
    """失败路径也必须释放登记，否则该文件后续永远被判「正在处理中」。"""
    plugin, _ = _new_plugin("dedup3")
    try:
        asyncio.run(plugin._handle_file("s", "s", "", {"name": "x.zzz", "bytes": b"nope"}))
        assert not plugin._file_inflight, plugin._file_inflight
    finally:
        asyncio.run(plugin.on_unload())


def test_identity_prefers_file_id_over_name():
    """身份键优先级：file_id > url > (name, size)；占位符名不同也能合并。"""
    from plugin import FileReaderPlugin

    ident = FileReaderPlugin._file_identity
    a = ident("s", "c", {"file_id": "F9", "name": "附件"})
    b = ident("s", "c", {"file_id": "F9", "name": "奶蛙学 必修第一册.pdf"})
    assert a == b, "同 file_id 的两条消息必须同身份（真机 15:21 的 2× 解析根因）"
    assert ident("s", "c", {"name": "a.txt", "size_hint": 1}) != a
    assert ident("s", "c", {"url": "http://x/1"}) != ident("t", "c", {"url": "http://x/1"}), "不同会话必须隔离"
    assert ident("s", "c1", {"url": "http://x/1"}) != ident("s", "c2", {"url": "http://x/1"}), (
        "同会话不同对话各有独立向量库，去重键必须区分"
    )


# ─── A5：会话内重发缓存 ─────────────────────────────────────────────


def test_resend_same_bytes_reuses_entry():
    """同会话重发同一文件：回执秒回，且零解析、零 embedding。"""
    plugin, mod = _new_plugin("resend")
    calls = {"parse": 0, "embed": 0}
    orig = mod.read_any_file_to_text

    def _parse(_p, _n):
        calls["parse"] += 1
        return "同一份文件的内容。" * 200

    async def _embed(texts):
        calls["embed"] += 1
        return [[1.0, float(len(t) % 5)] for t in texts]

    mod.read_any_file_to_text = _parse
    plugin._embed = _embed
    try:
        payload = {"name": "重复.pdf", "file_id": "R1", "bytes": b"same-bytes-here"}

        async def _drive():
            await plugin._handle_file("s", "c", "", dict(payload))
            first = dict(calls)
            # 第二次：file_id 不同（模拟转发/重发后 id 变了），但字节完全相同
            await plugin._handle_file("s", "c", "", {"name": "重复(1).pdf", "file_id": "R2", "bytes": b"same-bytes-here"})
            return first

        first = asyncio.run(_drive())
        assert first["parse"] == 1 and first["embed"] == 1
        assert calls["parse"] == 1, "重发同一内容不应再解析"
        assert calls["embed"] == 1, "重发同一内容不应再向量化"
        assert len(plugin._store.get("s", "c").files) == 1
        assert plugin._hash_index, "成功后必须记入重发缓存"
    finally:
        mod.read_any_file_to_text = orig
        asyncio.run(plugin.on_unload())


def test_resend_cache_invalidated_after_clear():
    """清文件后缓存必须失效，否则重发会命中「已不存在的库」。"""
    plugin, mod = _new_plugin("resend2")
    orig = mod.read_any_file_to_text
    mod.read_any_file_to_text = lambda _p, _n: "内容" * 100
    plugin._embed = _fake_embed
    try:
        payload = {"name": "a.txt", "file_id": "A1", "bytes": b"payload"}
        asyncio.run(plugin._handle_file("s", "c", "", dict(payload)))
        assert plugin._hash_index

        asyncio.run(plugin.cmd_clear_file(session_id="s", stream_id=""))
        assert not plugin._hash_index, "清文件后重发缓存必须清空"
    finally:
        mod.read_any_file_to_text = orig
        asyncio.run(plugin.on_unload())


def test_resend_cache_pruned_when_store_drops_session():
    """清理循环回收会话后，缓存里指向它的键必须一起消失。"""
    plugin, _ = _new_plugin("resend3")
    try:
        plugin._hash_index[("gone", "gone", "d" * 64)] = "旧文件.txt"
        plugin._cleanup_once()
        assert ("gone", "gone", "d" * 64) not in plugin._hash_index, plugin._hash_index
    finally:
        asyncio.run(plugin.on_unload())


# ─── A3：默认分块参数 ───────────────────────────────────────────────


def test_default_chunking_is_1024_128():
    """v1.4.0 默认 1024/128（D3）：块数腰斩 ⇒ embedding 批次数腰斩。"""
    plugin, _ = _new_plugin("chunk")
    try:
        assert plugin.config.reader.chunk_size == 1024
        assert plugin.config.reader.chunk_overlap == 128
        assert plugin.config.reader.pdf_max_pages == 500
    finally:
        asyncio.run(plugin.on_unload())


def test_chunk_count_roughly_halves_on_log_sample():
    """同一份 52 万字日志：1024/128 的块数应显著少于 512/100（约一半）。"""
    from chunker import RecursiveCharacterChunker

    text = ("2026-11-03 15:21:47 INFO 处理文件成功，耗时 0.42s，块数 182。\n" * 12000)[:520000]
    old = RecursiveCharacterChunker(512, 100)
    new = RecursiveCharacterChunker(1024, 128)
    n_old, n_new = len(old.chunk(text)), len(new.chunk(text))
    assert n_new < n_old * 0.65, f"新分块块数应明显下降：{n_old} → {n_new}"
