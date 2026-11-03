# -*- coding: utf-8 -*-
"""v1.3.9：向 Planner 公告「文件正在解析」。

真机 2026-11-03 15:21 群「鸟の小窝」：19MB PDF 解析了 26 分钟，这期间 store 里
没有任何文件，`announce_file_context` 直接 return，Planner 对文件一无所知 ——
它先判定「19MB 可能是视频」→ stop，20 分钟后岚追问观后感，它才去试 search_file，
拿到「还没有已入库的文件」后绕去联网搜梗，用网页内容代替文件正文。

这条状态不解决「解析慢」，但让 Planner 知道该说「还在读」而不是「读不到」。
"""

import asyncio
import time
from pathlib import Path

from plugin import FileReaderPlugin


def _new_plugin():
    from fakehost import bind_context, build_context, get_default_config, load_plugin_module

    plugin_dir = Path(__file__).resolve().parent.parent
    mod = load_plugin_module(plugin_dir, "file_reader_notice_under_test")
    plugin = mod.create_plugin()
    bind_context(plugin, build_context("org.mai-mai.file-reader"), get_default_config(mod.FileReaderConfig))
    asyncio.run(plugin.on_load())
    return plugin, mod


def _notice_text(result) -> str:
    items = result.get("modified_kwargs", {}).get("items") or []
    assert items, f"应注入一条公告: {result}"
    return str(items[-1]["parts"][0]["text"])


# ── 文案 ─────────────────────────────────────────────────────


def test_notice_seconds_for_short_wait():
    text = FileReaderPlugin._parsing_notice(
        [{"name": "a.pdf", "started_at": time.time() - 12}]
    )
    assert "《a.pdf》" in text
    assert "秒" in text and "分钟" not in text.split("已等待约")[1].split("）")[0], text
    assert "不要断言内容缺失" in text


def test_notice_minutes_for_long_wait():
    """26 分钟的解析说「1560 秒」没有信息量。"""
    text = FileReaderPlugin._parsing_notice(
        [{"name": "奶蛙学.pdf", "started_at": time.time() - 1560}]
    )
    assert "已等待约 26 分钟" in text, text


def test_notice_lists_multiple_and_caps_at_three():
    records = [{"name": f"f{i}.pdf", "started_at": time.time()} for i in range(5)]
    text = FileReaderPlugin._parsing_notice(records)
    assert "《f0.pdf》" in text and "《f1.pdf》" in text and "《f2.pdf》" in text
    assert "《f3.pdf》" not in text, "超过 3 个应收敛成计数"
    assert "等 5 个" in text, text


# ── 注入条件 ──────────────────────────────────────────────────


def test_injects_when_only_parsing():
    """store 空、但有解析中的文件 → 必须注入（真机正踩在这个空档）。"""
    plugin, _ = _new_plugin()
    try:
        plugin._parsing[1] = {"session_id": "s1", "name": "大文件.pdf", "started_at": time.time() - 120}
        res = asyncio.run(plugin.announce_file_context(session_id="s1", items=[]))
        text = _notice_text(res)
        assert "正在解析" in text and "《大文件.pdf》" in text
        assert "已读入文件" not in text, "此时没有任何文件入库，不能说已读入"
    finally:
        asyncio.run(plugin.on_unload())


def test_no_injection_when_nothing_happening():
    plugin, _ = _new_plugin()
    try:
        res = asyncio.run(plugin.announce_file_context(session_id="s1", items=[]))
        assert res == {"action": "continue"}, f"既无文件也无解析任务时不应注入: {res}"
    finally:
        asyncio.run(plugin.on_unload())


def test_other_session_parsing_does_not_leak():
    """会话隔离：A 群的解析状态不能注入到 B 群的 Planner。"""
    plugin, _ = _new_plugin()
    try:
        plugin._parsing[1] = {"session_id": "sA", "name": "私有.pdf", "started_at": time.time()}
        res = asyncio.run(plugin.announce_file_context(session_id="sB", items=[]))
        assert res == {"action": "continue"}, "其他会话的解析状态不得跨会话注入"
    finally:
        asyncio.run(plugin.on_unload())


# ── 生命周期 ──────────────────────────────────────────────────


def test_parsing_state_cleared_after_failure():
    """解析失败也必须撤状态，否则该会话永远被告知「正在解析」。"""
    plugin, _ = _new_plugin()
    try:
        asyncio.run(
            plugin._handle_file(
                session_id="f1",
                conversation_id="f1",
                stream_id="f1",
                fdata={"name": "x.zzz", "bytes": b"unsupported"},
            )
        )
        assert not plugin._parsing, f"失败后不得残留: {plugin._parsing}"
    finally:
        asyncio.run(plugin.on_unload())


def test_parsing_state_visible_while_working():
    """处理期间状态必须在，且用的是回填后的真名。"""
    plugin, plugin_mod = _new_plugin()
    orig = plugin_mod.read_any_file_to_text

    def _slow(_path, _name):
        time.sleep(0.4)
        return "内容" * 10

    async def _embed(texts):
        return [[float(len(t) % 7), 1.0] for t in texts]

    plugin_mod.read_any_file_to_text = _slow
    plugin._embed = _embed
    try:

        async def _drive():
            task = asyncio.create_task(
                plugin._handle_file("p1", "p1", "p1", {"name": "慢文件.txt", "bytes": b"abc"})
            )
            await asyncio.sleep(0.1)
            recs = [r for r in plugin._parsing.values() if r["session_id"] == "p1"]
            assert recs and recs[0]["name"] == "慢文件.txt", f"处理中应可见: {plugin._parsing}"
            await task
            assert not plugin._parsing, "完成后应清空"

        asyncio.run(_drive())
    finally:
        plugin_mod.read_any_file_to_text = orig
        asyncio.run(plugin.on_unload())


def test_stale_parsing_record_is_pruned():
    """任务泄漏（只在异常取消路径才可能）不能变成永久「正在解析」。"""
    plugin, _ = _new_plugin()
    try:
        plugin._parsing[1] = {"session_id": "s1", "name": "僵尸.pdf", "started_at": time.time() - 4 * 3600}
        assert plugin._parsing_for("s1") == []
        assert not plugin._parsing, "超过 3 小时的记录应被裁掉"
    finally:
        asyncio.run(plugin.on_unload())
