# -*- coding: utf-8 -*-
"""注入体验与生命周期测试（v1.3.0 批次 3）。

设计说明：这些约束都能用**未绑定方法 + 轻量 stub** 验证，不必拉起整个插件。
完整链路（检索 → 拼装 → 返回 modified_kwargs）由根套件 `test_file_reader.py`
覆盖；这里守的是那些"改错了不会报错、只会静默失效"的单点约束：

  - item_id 前缀幂等（旧版靠正文 marker，配置被清空就永不注入）
  - 失败通知只投递一次（否则每轮都往上下文里塞同一条失败）
  - 滑动续期与绝对上限（旧版按上传时间算，长文档聊到第 6 轮就消失）
  - 轮数只加给**被注入**的文件、且跨会话不互相抑制
"""

import asyncio
import time
from types import SimpleNamespace

from plugin import FileReaderPlugin
from vector_store import VectorStore

PLUGIN_ID = "org.mai-mai.file-reader"


def _stub(**reader_overrides):
    """构造只带注入相关状态的最小 self。"""
    defaults = {
        "parse_notice_ttl": 600,
        "file_retention_time": 60,
        "file_max_rounds": 20,
        "file_absolute_ttl": 720,
    }
    defaults.update(reader_overrides)
    reader = SimpleNamespace(**defaults)
    stub = SimpleNamespace(
        config=SimpleNamespace(reader=reader),
        _file_notices={},
        _inject_memo={},
    )
    return stub


async def _fake_embed(texts):
    return [[float(len(t) % 7), 1.0] for t in texts]


def _vs(**kwargs):
    return VectorStore(_fake_embed, chunk_size=64, chunk_overlap=0, **kwargs)


# ── item_id 前缀与幂等 ──────────────────────────────────────────


def test_system_item_id_has_prefix():
    item = FileReaderPlugin._make_system_item("内容", kind="context")
    assert item["meta"]["item_id"].startswith("file-reader:context:")
    notice = FileReaderPlugin._make_system_item("内容", kind="notice")
    assert notice["meta"]["item_id"].startswith("file-reader:notice:")


def test_already_injected_by_item_id():
    items = [
        {"item_type": "UserMessageItem", "meta": {"item_id": "u1"}, "parts": []},
        {
            "item_type": "SystemMessageItem",
            "meta": {"item_id": "file-reader:context:abc"},
            "parts": [{"type": "text", "text": "随便什么内容"}],
        },
    ]
    assert FileReaderPlugin._already_injected(items) is True


def test_already_injected_false_for_unrelated_items():
    items = [
        {"item_type": "UserMessageItem", "meta": {"item_id": "u1"},
         "parts": [{"type": "text", "text": "用户的问题"}]},
        {"item_type": "SystemMessageItem", "meta": {"item_id": "other:1"},
         "parts": [{"type": "text", "text": "别的插件注入的"}]},
    ]
    assert FileReaderPlugin._already_injected(items) is False


def test_already_injected_by_tool_marker():
    """search_file 的工具结果不是我们造的 item，只能靠正文标记识别。"""
    items = [
        {"item_type": "ToolResultItem", "meta": {"item_id": "t1"},
         "parts": [{"type": "text", "text": f"{FileReaderPlugin._TOOL_TEXT_MARK} 全文如下…"}]},
    ]
    assert FileReaderPlugin._already_injected(items) is True


def test_inject_items_returns_full_kwargs():
    """modified_kwargs 必须回传完整 kwargs——只回传改动键会把 tool_definitions 等全丢掉。"""
    kwargs = {
        "session_id": "s1",
        "items": [{"item_type": "UserMessageItem", "meta": {"item_id": "u1"}, "parts": []}],
        "item_schema_version": 1,
        "task_name": "replyer",
        "tool_definitions": [{"name": "x"}],
    }
    out = FileReaderPlugin._inject_items(kwargs["items"], kwargs, "注入内容", kind="context")
    modified = out["modified_kwargs"]
    assert set(modified) == set(kwargs)
    assert modified["tool_definitions"] == [{"name": "x"}]
    assert modified["item_schema_version"] == 1
    assert len(modified["items"]) == len(kwargs["items"]) + 1
    assert modified["items"][-1]["meta"]["item_id"].startswith("file-reader:context:")


# ── 失败通知 ────────────────────────────────────────────────────


def test_notice_delivered_once():
    stub = _stub()
    FileReaderPlugin._add_notice(stub, "s1", "报告.epub", "不支持的格式 .epub")
    first = FileReaderPlugin._take_notice(stub, "s1")
    assert "报告.epub" in first and "不支持的格式" in first
    assert FileReaderPlugin._take_notice(stub, "s1") == ""


def test_notice_ttl_expires():
    stub = _stub(parse_notice_ttl=1)
    FileReaderPlugin._add_notice(stub, "s1", "a.doc", "旧版格式")
    stub._file_notices["s1"][0]["ts"] = time.time() - 10
    assert FileReaderPlugin._take_notice(stub, "s1") == ""


def test_notice_replaced_on_repeat_failure():
    stub = _stub()
    FileReaderPlugin._add_notice(stub, "s1", "a.doc", "第一次原因")
    FileReaderPlugin._add_notice(stub, "s1", "a.doc", "第二次原因")
    assert len(stub._file_notices["s1"]) == 1
    assert "第二次原因" in FileReaderPlugin._take_notice(stub, "s1")


def test_notice_cleared_after_success():
    stub = _stub()
    FileReaderPlugin._add_notice(stub, "s1", "a.doc", "读不了")
    FileReaderPlugin._clear_notices(stub, "s1", "a.doc")
    assert FileReaderPlugin._take_notice(stub, "s1") == ""


def test_notice_capacity_bounded():
    stub = _stub()
    for i in range(15):
        FileReaderPlugin._add_notice(stub, "s1", f"f{i}.doc", "失败")
    assert len(stub._file_notices["s1"]) == 10


def test_notice_cleared_with_session():
    stub = _stub()
    FileReaderPlugin._add_notice(stub, "s1", "a.doc", "读不了")
    FileReaderPlugin._clear_notices(stub, "s1")
    assert "s1" not in stub._file_notices


def test_notice_isolated_between_sessions():
    stub = _stub()
    FileReaderPlugin._add_notice(stub, "s1", "a.doc", "读不了")
    assert FileReaderPlugin._take_notice(stub, "s2") == ""


# ── 滑动续期与绝对上限 ──────────────────────────────────────────


def test_touch_updates_last_used():
    vs = _vs()
    entry = asyncio.run(vs.add_file("a.txt", "内容" * 50))
    entry.last_used = time.time() - 10_000
    vs.touch_files(["a.txt"])
    assert time.time() - entry.last_used < 5


def test_cleanup_uses_last_used_not_upload_time():
    """刚上传但从没被问过：按 last_used（= upload_time）过期；被注入过则续期。"""
    vs = _vs()
    e1 = asyncio.run(vs.add_file("old.txt", "内容" * 20))
    e1.upload_time = time.time() - 7200
    e1.last_used = time.time() - 7200
    e2 = asyncio.run(vs.add_file("fresh.txt", "内容" * 20))
    e2.upload_time = time.time() - 7200
    e2.last_used = time.time()  # 刚刚被注入过
    removed = vs.cleanup_expired(retention_seconds=60, max_rounds=99)
    assert "old.txt" in removed
    assert "fresh.txt" not in removed


def test_absolute_ttl_overrides_sliding_renewal():
    """一直被问到的文件也不能永久驻留。"""
    vs = _vs()
    e = asyncio.run(vs.add_file("hot.txt", "内容" * 20))
    e.upload_time = time.time() - 800 * 60  # 入库超过 800 分钟
    e.last_used = time.time()  # 但刚刚被注入
    removed = vs.cleanup_expired(retention_seconds=3600, max_rounds=99, absolute_ttl_seconds=720 * 60)
    assert "hot.txt" in removed


def test_absolute_ttl_zero_disables_limit():
    vs = _vs()
    e = asyncio.run(vs.add_file("hot.txt", "内容" * 20))
    e.upload_time = time.time() - 800 * 60
    e.last_used = time.time()
    removed = vs.cleanup_expired(retention_seconds=3600, max_rounds=99, absolute_ttl_seconds=0)
    assert "hot.txt" not in removed


# ── 轮数 ────────────────────────────────────────────────────────


def test_rounds_only_counted_for_injected_files():
    stub = _stub()
    vs = _vs()
    asyncio.run(vs.add_file("a.txt", "甲" * 40))
    asyncio.run(vs.add_file("b.txt", "乙" * 40))
    FileReaderPlugin._tick_rounds_once(stub, vs, "问题", session_id="s1", file_names=["a.txt"])
    assert vs.files["a.txt"].rounds == 1
    assert vs.files["b.txt"].rounds == 0


def test_tick_rounds_same_question_within_window_only_once():
    stub = _stub()
    vs = _vs()
    asyncio.run(vs.add_file("a.txt", "甲" * 40))
    assert FileReaderPlugin._tick_rounds_once(stub, vs, "同一个问题", session_id="s1", file_names=["a.txt"]) is True
    assert FileReaderPlugin._tick_rounds_once(stub, vs, "同一个问题", session_id="s1", file_names=["a.txt"]) is False
    assert vs.files["a.txt"].rounds == 1


def test_tick_rounds_not_suppressed_across_sessions():
    """A 会话问过「总结一下」，B 会话问同样的话不能被误判成"本轮已计数"。"""
    stub = _stub()
    vs = _vs()
    asyncio.run(vs.add_file("a.txt", "甲" * 40))
    FileReaderPlugin._tick_rounds_once(stub, vs, "总结一下", session_id="s1", file_names=["a.txt"])
    counted = FileReaderPlugin._tick_rounds_once(stub, vs, "总结一下", session_id="s2", file_names=["a.txt"])
    assert counted is True
    assert vs.files["a.txt"].rounds == 2


# ── 词法索引随文件增删同步 ──────────────────────────────────────


def test_lexical_index_tracks_added_and_removed_files():
    vs = _vs()
    asyncio.run(vs.add_file("代码.txt", "def read_any_file_to_text(): pass"))
    assert vs.search_lexical("read_any_file_to_text", 3)
    vs.cleanup_expired(retention_seconds=0, max_rounds=99)
    assert vs.search_lexical("read_any_file_to_text", 3) == []
