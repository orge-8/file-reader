# -*- coding: utf-8 -*-
"""v1.3.8：长解析期间「空会话壳」被清理循环回收 —— 入库写进脱管对象。

真机 2026-11-03 15:21 群「鸟の小窝」的完整链路：

    15:21:47  [napcat] get_file 命中: ...奶蛙学 必修第一册.pdf
    15:47:46  已入库文件 奶蛙学 必修第一册.pdf：182 块（session=a9ce37…）
    15:48:55  search_file → 当前会话还没有已入库的文件，请先上传文件。

19 MB PDF 解析了 26 分钟，而 `cleanup_interval=5` 分钟的 `_cleanup_once` 在
这期间看到 `not vs.files` 就把这个**空** VectorStore 当壳回收了。`_handle_file`
仍持有引用，`add_file` 顺利写进没人引用的对象：日志打「已入库 182 块」，
store 里却空空如也；又因为 `total_removed == 0`，「后台清理」那条日志也不会打，
整条故障链**零日志**，从日志看像是文件凭空消失。

修法三条：
  1. 向量库延迟到 `add` 之前才创建（`_get_vs`），消除「解析期间挂空壳」的窗口；
  2. `_inflight` 集合登记处理中的会话键，`_cleanup_once` 跳过它们；
  3. `_reassert_vs` 兜底：入库后发现实例已脱管就挂回去。
另附：同一文件会以「真实 file 段 + 群文件上传通知」两条消息出现，去重键改按
`file_id`（旧版按 name，两条消息一个叫「附件」一个叫真名 ⇒ 双双通过，2× 解析）。
"""

import asyncio
import logging
from types import SimpleNamespace

from plugin import FileReaderPlugin
from vector_store import VectorStore


async def _fake_embed(texts):
    return [[float(len(t) % 7), 1.0] for t in texts]


def _empty_vs() -> VectorStore:
    return VectorStore(_fake_embed, chunk_size=64, chunk_overlap=0, retrieve_top_k=3)


def _cleanup_stub(store, inflight):
    """只带 `_cleanup_once` 所需状态的最小 self。"""
    reader = SimpleNamespace(file_retention_time=60, file_max_rounds=20, file_absolute_ttl=720)
    return SimpleNamespace(
        _store=store,
        _inflight=inflight,
        _hash_index={},
        config=SimpleNamespace(reader=reader),
        _memo_invalidate_session=lambda _sid: None,
        _prune_summary_failed=lambda: None,
        # v1.4.0：清理时顺带裁重发缓存；桩里做空实现（真实实现见 test_perf_v140.py）
        _prune_hash_index=lambda: None,
        ctx=SimpleNamespace(logger=logging.getLogger("test.inflight")),
    )


# ── 清理循环：空壳会被回收（旧行为必须保留，否则会话数无界增长） ──────────


def test_cleanup_drops_idle_empty_shell():
    from vector_store import SessionStore

    store = SessionStore.__new__(SessionStore)
    store._sessions = {("sid", "cid"): _empty_vs()}
    stub = _cleanup_stub(store, set())

    assert FileReaderPlugin._cleanup_once(stub) == 0
    assert ("sid", "cid") not in store._sessions, "空闲的空壳应被回收"


# ── 清理循环：in-flight 会话必须被跳过（v1.3.8 核心） ────────────────────


def test_cleanup_skips_inflight_empty_shell():
    from vector_store import SessionStore

    store = SessionStore.__new__(SessionStore)
    store._sessions = {("sid", "cid"): _empty_vs()}
    stub = _cleanup_stub(store, {("sid", "cid"): 1})

    FileReaderPlugin._cleanup_once(stub)
    assert ("sid", "cid") in store._sessions, (
        "处理中的会话不得被当作空壳回收——真机 19MB PDF 解析 26 分钟就是这样丢的"
    )


def test_cleanup_skips_only_inflight_keys():
    """保护必须精确到会话键，不能因为某一会话在处理就冻结全部清理。"""
    from vector_store import SessionStore

    store = SessionStore.__new__(SessionStore)
    store._sessions = {("busy", "c1"): _empty_vs(), ("idle", "c2"): _empty_vs()}
    stub = _cleanup_stub(store, {("busy", "c1"): 2})

    FileReaderPlugin._cleanup_once(stub)
    assert ("busy", "c1") in store._sessions
    assert ("idle", "c2") not in store._sessions


# ── 兜底：脱管后挂回 ─────────────────────────────────────────────────


def test_reassert_vs_puts_back_dropped_store():
    from vector_store import SessionStore

    store = SessionStore.__new__(SessionStore)
    store._sessions = {}
    stub = SimpleNamespace(
        _store=store,
        ctx=SimpleNamespace(logger=logging.getLogger("test.reassert")),
    )
    vs = _empty_vs()

    FileReaderPlugin._reassert_vs(stub, "sid", "cid", vs)
    assert store._sessions.get(("sid", "cid")) is vs


def test_reassert_does_not_clobber_populated_store():
    """已被别的任务写入文件的实例不能被空壳覆盖。"""
    from vector_store import SessionStore, FileEntry

    store = SessionStore.__new__(SessionStore)
    populated = _empty_vs()
    populated._files = {"已有.txt": FileEntry(file_name="已有.txt", chunks=["x"])}
    store._sessions = {("sid", "cid"): populated}
    stub = SimpleNamespace(
        _store=store,
        ctx=SimpleNamespace(logger=logging.getLogger("test.reassert2")),
    )

    FileReaderPlugin._reassert_vs(stub, "sid", "cid", _empty_vs())
    assert store._sessions[("sid", "cid")] is populated, "不得用空实例覆盖已有文件"


# ── 并发：同一会话的多个文件各自计数，先完成的不解除保护 ─────────────


def test_concurrent_files_keep_count_until_last_one():
    import time

    plugin, plugin_mod = _new_plugin()
    orig_parse = plugin_mod.read_any_file_to_text

    def _slow_parse(_path, _name):
        time.sleep(0.4)
        return "情绪识别只作为强度微调的辅助信号。" * 5

    async def _embed(texts):
        return [[float(len(t) % 7), 1.0] for t in texts]

    plugin_mod.read_any_file_to_text = _slow_parse
    plugin._embed = _embed  # 实例属性遮蔽类方法，避免 FakeHost 的 embed 返回空
    try:

        async def _drive():
            t1 = asyncio.create_task(
                plugin._handle_file("k", "k", "k", {"name": "a.txt", "bytes": b"abc"})
            )
            t2 = asyncio.create_task(
                plugin._handle_file("k", "k", "k", {"name": "b.txt", "bytes": b"abc"})
            )
            await asyncio.sleep(0.15)
            # 计数而非集合：两个文件同时处理，计数必须是 2
            assert plugin._inflight.get(("k", "k")) == 2, (
                f"并发文件应各自计数: {plugin._inflight}"
            )
            # 解析中不得创建空壳（旧版在函数开头 get_or_create 就会留下一个）
            assert ("k", "k") not in plugin._store._sessions, "解析期间不应存在空会话壳"
            await asyncio.gather(t1, t2)
            assert not plugin._inflight, "全部完成后必须清空 in-flight"
            vs = plugin._store._sessions.get(("k", "k"))
            assert vs is not None and len(vs.files) == 2, f"两个文件都应入库: {vs and vs.files}"

        asyncio.run(_drive())
    finally:
        plugin_mod.read_any_file_to_text = orig_parse
        asyncio.run(plugin.on_unload())


# ── 真实插件：解析失败不得留下空壳 ───────────────────────────────────


def _new_plugin():
    """返回 (plugin, module)。模块必须用这里加载的那一份——

    conftest 把插件目录塞进 sys.path 后，`import plugin` 与 `load_plugin_module`
    会产生**两个不同的模块对象**；打桩测试若 patch 错了模块，桩不会生效，
    表现为「任务早就跑完了」。第一版测试就踩了这个坑。
    """
    from fakehost import bind_context, build_context, get_default_config, load_plugin_module
    from pathlib import Path

    plugin_dir = Path(__file__).resolve().parent.parent
    mod = load_plugin_module(plugin_dir, "file_reader_inflight_under_test")
    plugin = mod.create_plugin()
    bind_context(plugin, build_context("org.mai-mai.file-reader"), get_default_config(mod.FileReaderConfig))
    asyncio.run(plugin.on_load())
    return plugin, mod


def test_handle_file_creates_no_shell_before_parse():
    plugin, _ = _new_plugin()
    try:
        asyncio.run(
            plugin._handle_file(
                session_id="noshell",
                conversation_id="noshell",
                stream_id="noshell",
                fdata={"name": "x.zzz", "bytes": b"not a supported format"},
            )
        )
        assert ("noshell", "noshell") not in plugin._store._sessions, (
            "解析失败不得留下空会话壳——旧版在函数开头 get_or_create，壳会被清理循环回收"
        )
        assert not plugin._inflight, "in-flight 必须在 finally 里解除，否则该会话永久跳过清理"
    finally:
        asyncio.run(plugin.on_unload())


# ── 去重：两条消息同一个 file_id ─────────────────────────────────────


def test_dedup_merges_file_segment_and_upload_notice():
    plugin, _ = _new_plugin()
    calls: list[dict] = []

    async def _record(session_id, conversation_id, stream_id, fdata):
        calls.append(dict(fdata))

    plugin._handle_file = _record  # 实例属性遮蔽，避免真的去下载/解析
    try:
        real_file_msg = {
            "message_id": "780174956",
            "session_id": "grp",
            "processed_plain_text": "[文件] 附件，大小: 19247475",
            "raw_message": [
                {"type": "file", "data": {"file_id": "F1", "name": "附件", "size": 19247475}}
            ],
        }
        notice_msg = {
            "message_id": "qq-notice-947e3e845f114c019af58cbffbe3659d",
            "session_id": "grp",
            "processed_plain_text": (
                "[事件-群文件上传] 希然 上传了文件 奶蛙学 必修第一册.pdf，大小 19247475 "
                "[文件] 奶蛙学 必修第一册.pdf，大小: 19247475"
            ),
            "raw_message": [
                {"type": "text", "data": {"text": "[事件-群文件上传] 希然 上传了文件 奶蛙学 必修第一册.pdf"}},
                {
                    "type": "file",
                    "data": {
                        "busid": 102,
                        "file_id": "F1",
                        "name": "奶蛙学 必修第一册.pdf",
                        "size": 19247475,
                    },
                },
            ],
        }

        async def _drive():
            await plugin._detect_file("before", message=real_file_msg, session_id="grp")
            await plugin._detect_file("before", message=notice_msg, session_id="grp")
            await asyncio.sleep(0)

        asyncio.run(_drive())
        assert len(calls) == 1, (
            f"同一 file_id 的两条消息只应处理一次（旧版按 name 去重 ⇒ 2 次解析 + 2 次 embedding）: {len(calls)}"
        )
        assert calls[0].get("file_id") == "F1"
    finally:
        asyncio.run(plugin.on_unload())
