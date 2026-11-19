# -*- coding: utf-8 -*-
"""v1.7.0 SnowLuma 适配器取文件通道 + size 预检 + 失败文案 + css 支持。

背景
----
官方 napcat-adapter 已合并进 snowluma-adapter（Host 1.3.0 起），结构化 file 段
只有 {name, size, file_id, busid}，不含文件本体。v1.6.0 及以前只能靠
NapCat OneBot HTTP 服务（127.0.0.1:3001）的 get_file 拿本地路径——真机没开
HTTP 服务时整条链路断掉。

v1.7.0 新增首选通道：ctx.api.call 调适配器 public API
（adapter.napcat.file.get_private_file_url / get_group_file_url）换短期下载
URL，再走插件自带的 SSRF 防护下载落盘。任何失败回退 NapCat 兜底（若启用）。

源码依据（MaiBot-SnowLuma-Adapter v1.3.1）：
- 私聊/群聊两个 URL API 均 public=True，入参包在 params 字典里；
- busid=0 必须省略（适配器 v1.1.10 修复，传 0 会参数错误）；
- 返回对端原始响应 {status, retcode, data: {url, ...}}。

本文件把以下契约做成门禁能跑到的回归：
1. 私聊/群聊参数形态（params 包装、int 化、busid=0 省略、>0 才传）；
2. URL 提取优先 data.url / 顶层 url，递归兜底不拿错（多 URL 响应）；
3. 能力关闭 / RPC 异常 / 无 URL / 缺上下文 → None 降级，不抛异常；
4. _acquire_source 首选 SL、下载失败回退 NapCat、超限/安全拒绝不回退；
5. size 预检：超大 file 段直接拒绝，零 RPC；
6. 失败原因拆分 no_content_file_id vs napcat_no_content，文案进模型通知；
7. _message_context 不用裸 id 兜底（message_info.id 可能是消息 ID）；
8. css/scss/less 解析支持（真机 11-18：style.css 被拒）。
"""

import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import file_parser  # noqa: E402

QQ_URL = "https://grouptalk.29334.fts.qq.com/download?token=abc"
PRIVATE_API = "adapter.napcat.file.get_private_file_url"
GROUP_API = "adapter.napcat.file.get_group_file_url"


def _new_plugin(tag="v170", api_response=None, api_error=None):
    """构造带可控 api.call 的插件实例。

    fakehost 对 api.* 的默认返回是 {"status":"ok","retcode":0,"data":{}}（无 URL），
    这里用返回 {"success": True, "result": ...} 的桩模拟宿主 cap.call 包装——
    SDK 2.23.0 的 _CAPABILITY_RESULT_KEYS 会把 api.call 解包成 result 值，
    与真机链路（宿主 _cap_api_call → SDK _normalize_capability_result）一致。
    """
    from fakehost import FakeHost, bind_context, build_context, get_default_config, load_plugin_module

    class _SLHost(FakeHost):
        async def rpc_call(self, method, plugin_id="", payload=None, **kwargs):
            kw = dict(payload or {})
            capability = kw.get("capability") or method
            args = kw.get("args") or {}
            self.calls.append((capability, args))
            if capability == "api.call":
                if api_error is not None:
                    raise api_error
                return {"success": True, "result": api_response}
            return await super().rpc_call(method, plugin_id, payload, **kwargs)

    mod = load_plugin_module(PLUGIN_DIR, f"file_reader_{tag}_under_test")
    plugin = mod.create_plugin()
    host = _SLHost("org.mai-mai.file-reader")
    bind_context(plugin, build_context("org.mai-mai.file-reader", rpc_call=host.rpc_call),
                 get_default_config(mod.FileReaderConfig))
    asyncio.run(plugin.on_load())
    return plugin, mod, host


def _api_calls(host):
    return [(args.get("api_name"), args.get("args")) for cap, args in host.calls if cap == "api.call"]


def _ok(url=QQ_URL):
    """适配器原始响应（OneBot 标准形态）。"""
    return {"status": "ok", "retcode": 0, "data": {"url": url, "file_size": 123}}


# ── 1. 私聊 / 群聊参数契约 ─────────────────────────────────────


def test_private_params_wrapped_and_numeric():
    plugin, _, host = _new_plugin("slpriv", api_response=_ok())
    try:
        url = asyncio.run(plugin._resolve_via_snowluma(
            {"file_id": "fid-1", "user_id": "1000000001"}))
        assert url == QQ_URL
        calls = _api_calls(host)
        assert len(calls) == 1
        api_name, args = calls[0]
        assert api_name == PRIVATE_API
        # params 包装：适配器 API 签名是 (params: dict)，不是平铺 kwargs
        assert args == {"params": {"file_id": "fid-1", "user_id": 1000000001}}
        assert isinstance(args["params"]["user_id"], int)
    finally:
        asyncio.run(plugin.on_unload())


def test_group_params_busid_zero_omitted():
    """busid=0 必须省略（适配器 v1.1.10 修复：传 0 会参数错误）。"""
    plugin, _, host = _new_plugin("slgrp0", api_response=_ok())
    try:
        url = asyncio.run(plugin._resolve_via_snowluma(
            {"file_id": "fid-2", "group_id": "888888888", "busid": 0}))
        assert url == QQ_URL
        api_name, args = _api_calls(host)[0]
        assert api_name == GROUP_API
        assert args == {"params": {"file_id": "fid-2", "group_id": 888888888}}
        assert "busid" not in args["params"]
    finally:
        asyncio.run(plugin.on_unload())


def test_group_params_busid_positive_passed():
    plugin, _, host = _new_plugin("slgrp102", api_response=_ok())
    try:
        asyncio.run(plugin._resolve_via_snowluma(
            {"file_id": "fid-3", "group_id": "888888888", "busid": 102}))
        _, args = _api_calls(host)[0]
        assert args["params"]["busid"] == 102
        assert isinstance(args["params"]["busid"], int)
    finally:
        asyncio.run(plugin.on_unload())


def test_group_takes_precedence_over_private():
    """同时有 group_id 与 user_id 时按群文件解析（群聊消息两者都有）。"""
    plugin, _, host = _new_plugin("slboth", api_response=_ok())
    try:
        asyncio.run(plugin._resolve_via_snowluma(
            {"file_id": "f", "group_id": "777", "user_id": "1000000001"}))
        api_name, args = _api_calls(host)[0]
        assert api_name == GROUP_API
        assert "user_id" not in args["params"]
    finally:
        asyncio.run(plugin.on_unload())


def test_non_numeric_id_kept_as_string():
    """user_openid 这类非数字标识不能强转 int，原样传给适配器。"""
    plugin, _, host = _new_plugin("slopenid", api_response=_ok())
    try:
        asyncio.run(plugin._resolve_via_snowluma(
            {"file_id": "f", "user_id": "oABC-xyz_123"}))
        _, args = _api_calls(host)[0]
        assert args["params"]["user_id"] == "oABC-xyz_123"
    finally:
        asyncio.run(plugin.on_unload())


def test_configurable_api_names():
    plugin, _, host = _new_plugin("slcustom", api_response=_ok())
    try:
        plugin.config.snowluma.private_file_url_api = "adapter.snowluma.file.get_private_file_url"
        asyncio.run(plugin._resolve_via_snowluma({"file_id": "f", "user_id": "123"}))
        assert _api_calls(host)[0][0] == "adapter.snowluma.file.get_private_file_url"
    finally:
        asyncio.run(plugin.on_unload())


# ── 2. URL 提取：data.url 优先，多 URL 不拿错 ──────────────────


def test_pick_prefers_data_url_over_preview():
    """fork 的缺陷：取响应里第一个 http URL。预览链接排在下载链接前时拿错。"""
    payload = {
        "status": "ok", "retcode": 0,
        "preview": "https://preview.qq.com/view/xxx",
        "data": {"url": QQ_URL, "preview_url": "https://preview.qq.com/view/xxx"},
    }
    from plugin import FileReaderPlugin
    assert FileReaderPlugin._pick_url_from_response(payload) == QQ_URL


def test_pick_top_level_url():
    from plugin import FileReaderPlugin
    assert FileReaderPlugin._pick_url_from_response({"url": QQ_URL}) == QQ_URL


def test_pick_data_url_wins_over_top_level():
    from plugin import FileReaderPlugin
    payload = {"url": "https://other.qq.com/a", "data": {"url": QQ_URL}}
    assert FileReaderPlugin._pick_url_from_response(payload) == QQ_URL


def test_pick_recursive_fallback_for_wrapped_payload():
    from plugin import FileReaderPlugin
    payload = {"result": {"body": {"download": {"link": QQ_URL}}}}
    assert FileReaderPlugin._pick_url_from_response(payload) == QQ_URL


def test_pick_ignores_non_http_and_deep_nesting():
    from plugin import FileReaderPlugin
    assert FileReaderPlugin._pick_url_from_response({"data": {"url": "ftp://x/y"}}) is None
    assert FileReaderPlugin._pick_url_from_response({"data": {"url": ""}}) is None
    assert FileReaderPlugin._pick_url_from_response(None) is None
    assert FileReaderPlugin._pick_url_from_response({}) is None
    # 超过递归深度上限（5 层）不再兜底，避免病态响应拖死解析
    deep: Any = QQ_URL
    for _ in range(8):
        deep = {"wrap": deep}
    assert FileReaderPlugin._pick_url_from_response(deep) is None


def test_pick_list_payload():
    from plugin import FileReaderPlugin
    assert FileReaderPlugin._pick_url_from_response([{"x": 1}, {"url": QQ_URL}]) == QQ_URL


# ── 3. 降级路径：任何失败返回 None，不抛异常 ───────────────────


def test_disabled_returns_none_without_rpc():
    plugin, _, host = _new_plugin("sldis")
    try:
        plugin.config.snowluma.enabled = False
        assert asyncio.run(plugin._resolve_via_snowluma(
            {"file_id": "f", "user_id": "123"})) is None
        assert _api_calls(host) == []
    finally:
        asyncio.run(plugin.on_unload())


def test_missing_file_id_returns_none():
    plugin, _, host = _new_plugin("slnoid")
    try:
        assert asyncio.run(plugin._resolve_via_snowluma({"user_id": "123"})) is None
        assert _api_calls(host) == []
    finally:
        asyncio.run(plugin.on_unload())


def test_private_missing_user_id_returns_none(caplog):
    plugin, _, host = _new_plugin("slnouid")
    try:
        assert asyncio.run(plugin._resolve_via_snowluma({"file_id": "f"})) is None
        assert _api_calls(host) == []
        assert any("user_id" in r.message for r in caplog.records)
    finally:
        asyncio.run(plugin.on_unload())


def test_rpc_exception_degrades_to_none(caplog):
    """能力未声明（E_CAPABILITY_DENIED）/ RPC 超时 / 适配器掉线都走这里。"""
    plugin, _, host = _new_plugin("slerr", api_error=RuntimeError("E_CAPABILITY_DENIED: api.call"))
    try:
        assert asyncio.run(plugin._resolve_via_snowluma(
            {"file_id": "f", "user_id": "123"})) is None
        assert any("调用失败" in r.message for r in caplog.records)
    finally:
        asyncio.run(plugin.on_unload())


def test_response_without_url_degrades_to_none():
    plugin, _, host = _new_plugin("slnourl", api_response={"status": "failed", "retcode": 1400, "data": {}})
    try:
        assert asyncio.run(plugin._resolve_via_snowluma(
            {"file_id": "f", "user_id": "123"})) is None
    finally:
        asyncio.run(plugin.on_unload())


def test_empty_data_default_fakehost_response_degrades():
    """fakehost 默认 api.* 返回（data={}）也必须安全降级，不能抛异常。"""
    plugin, _, host = _new_plugin("sldefault", api_response={"status": "ok", "retcode": 0, "data": {}})
    try:
        assert asyncio.run(plugin._resolve_via_snowluma(
            {"file_id": "f", "group_id": "888"})) is None
    finally:
        asyncio.run(plugin.on_unload())


# ── 4. _acquire_source：首选 SL，失败回退，确定性拒绝不回退 ────


def test_acquire_prefers_snowluma_and_downloads(tmp_path):
    """file_id 无本体 → SL 换 URL → SSRF 下载落盘，全程零 NapCat RPC。"""
    plugin, _, host = _new_plugin("slacq", api_response=_ok())
    body = b"body { color: red; }\n"
    seen = {}

    async def _fake_download(url, max_bytes, suffix="", **kwargs):
        seen["url"] = url
        seen["trusted"] = kwargs.get("trusted_source")
        p = tmp_path / "sl.bin"
        p.write_bytes(body)
        return str(p), len(body), hashlib.sha256(body).hexdigest()

    plugin._download_to_temp = _fake_download
    try:
        fdata = {"name": "style.css", "file_id": "fid", "busid": 0,
                 "size_hint": len(body), "user_id": "1000000001"}
        path, napcat_path, size, digest = asyncio.run(
            plugin._acquire_source(fdata, "style.css", 1024 * 1024))
        assert seen["url"] == QQ_URL
        assert seen["trusted"] is True, "SL 通道必须以可信来源下载（v1.8.1）"
        assert napcat_path is None
        assert size == len(body)
        assert digest == hashlib.sha256(body).hexdigest()
        assert Path(path).read_bytes() == body
        assert len(_api_calls(host)) == 1
        Path(path).unlink(missing_ok=True)
    finally:
        asyncio.run(plugin.on_unload())


def test_acquire_download_failure_falls_back_to_napcat(tmp_path, caplog):
    """SL 换到 URL 但下载失败（非确定性）→ 回退 NapCat get_file 本地路径。"""
    plugin, _, host = _new_plugin("slfallback", api_response=_ok())
    src = tmp_path / "napcat_cache" / "doc.txt"
    src.parent.mkdir()
    src.write_bytes(b"napcat local content")

    async def _broken_download(url, max_bytes, suffix="", **kwargs):
        raise ConnectionError("simulated network failure")

    plugin._download_to_temp = _broken_download
    plugin.config.napcat.enabled = True
    plugin._napcat_resolve_path = lambda seg: str(src)
    try:
        fdata = {"name": "doc.txt", "file_id": "fid", "user_id": "123"}
        path, napcat_path, size, _ = asyncio.run(
            plugin._acquire_source(fdata, "doc.txt", 1024 * 1024))
        assert napcat_path == str(src)
        assert Path(path).read_bytes() == b"napcat local content"
        assert any("回退 NapCat" in r.message for r in caplog.records)
        Path(path).unlink(missing_ok=True)
    finally:
        asyncio.run(plugin.on_unload())


def test_acquire_url_rejected_does_not_fallback():
    """安全拒绝是确定性结论：回退旧路径也不会变好，直接上抛。

    v1.8.1 起适配器 URL 是可信来源（首跳按公网地址校验），域外公网域名不再拒；
    确定性拒绝的判据改为**内网地址**（IP 层校验对可信来源同样生效）。
    """
    plugin, _, host = _new_plugin("slreject",
                                  api_response=_ok("http://192.168.1.1/file.bin"))
    called = {"napcat": 0}

    def _napcat(seg):
        called["napcat"] += 1
        return None

    plugin.config.napcat.enabled = True
    plugin._napcat_resolve_path = _napcat
    try:
        with pytest.raises(Exception) as ei:
            asyncio.run(plugin._acquire_source(
                {"name": "x.bin", "file_id": "fid", "user_id": "123"}, "x.bin", 1024 * 1024))
        assert type(ei.value).__name__ == "_UrlRejectedError"
        assert ei.value.reason == "non_public_address"
        assert called["napcat"] == 0, "安全拒绝后不得再走 NapCat 兜底"
    finally:
        asyncio.run(plugin.on_unload())


def test_acquire_snowluma_disabled_skips_channel():
    plugin, _, host = _new_plugin("sloff")
    try:
        plugin.config.snowluma.enabled = False
        with pytest.raises(Exception) as ei:
            asyncio.run(plugin._acquire_source(
                {"name": "x.txt", "file_id": "fid", "user_id": "123"}, "x.txt", 1024))
        assert type(ei.value).__name__ == "_SourceUnavailableError"
        assert ei.value.reason == "no_content_file_id"
        assert _api_calls(host) == [], "通道关闭时不得发起 api.call"
    finally:
        asyncio.run(plugin.on_unload())


def test_acquire_bytes_and_path_still_take_precedence(tmp_path):
    """有真实内容（bytes/path）时不走 SL 通道——内容优先于 file_id。"""
    plugin, _, host = _new_plugin("slprec", api_response=_ok())
    try:
        path, _, size, _ = asyncio.run(plugin._acquire_source(
            {"name": "a.txt", "file_id": "fid", "bytes": b"abc", "user_id": "123"},
            "a.txt", 1024))
        assert size == 3 and _api_calls(host) == []
        Path(path).unlink(missing_ok=True)

        src = tmp_path / "local.txt"
        src.write_bytes(b"local")
        path, _, size, _ = asyncio.run(plugin._acquire_source(
            {"name": "a.txt", "file_id": "fid", "path": str(src), "user_id": "123"},
            "a.txt", 1024))
        assert size == 5 and _api_calls(host) == []
        Path(path).unlink(missing_ok=True)
    finally:
        asyncio.run(plugin.on_unload())


# ── 5. size 预检：超大 file 段零 RPC 直接拒绝 ──────────────────


def test_oversized_structured_file_rejected_before_any_rpc():
    plugin, _, host = _new_plugin("slbig")
    try:
        plugin.config.reader.silent_errors = False
        plugin.config.reader.max_file_size = 20  # MB
        fdata = {"name": "huge.zip", "file_id": "fid", "size_hint": 21 * 1024 * 1024,
                 "user_id": "123"}
        asyncio.run(plugin._handle_file("s-big", "s-big", "stream-big", fdata))
        assert _api_calls(host) == [], "超大文件不得先走 SL 解析"
        sent = [args.get("text", "") for cap, args in host.calls if cap == "send.text"]
        assert any("超过上限" in t for t in sent), sent
        assert plugin._embed_retry_queue == []
    finally:
        asyncio.run(plugin.on_unload())


def test_size_hint_at_limit_not_rejected():
    """恰好等于上限不拒绝（> 才拒），预检不能误伤边界文件。"""
    plugin, _, host = _new_plugin("sledgesize", api_response=_ok())
    body = b"x" * 64

    async def _fake_download(url, max_bytes, suffix="", **kwargs):
        import tempfile
        fd, p = tempfile.mkstemp(suffix=suffix)
        import os
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        return p, len(body), hashlib.sha256(body).hexdigest()

    plugin._download_to_temp = _fake_download
    try:
        plugin.config.reader.max_file_size = 1  # 1MB
        fdata = {"name": "edge.txt", "file_id": "fid", "size_hint": 1024 * 1024,
                 "user_id": "123"}
        path, _, size, _ = asyncio.run(plugin._acquire_source(fdata, "edge.txt", 1024 * 1024))
        assert size == len(body)
        assert len(_api_calls(host)) == 1, "等于上限应正常走 SL 通道"
        Path(path).unlink(missing_ok=True)
    finally:
        asyncio.run(plugin.on_unload())


# ── 6. 失败原因拆分与模型通知 ──────────────────────────────────


def test_no_content_file_id_reason_and_notice():
    """file_id 链路失败 → no_content_file_id（不再误报 napcat_no_content）。"""
    plugin, _, host = _new_plugin("slreason", api_response={"status": "ok", "retcode": 0, "data": {}})
    try:
        asyncio.run(plugin._handle_file(
            "s-r", "s-r", "", {"name": "lost.pdf", "file_id": "fid", "user_id": "123"}))
        notices = plugin._file_notices.get("s-r") or []
        assert len(notices) == 1
        assert "file_id" in notices[0]["reason"]
        assert "适配器" in notices[0]["reason"]
        # silent_errors=True：不发群消息，只登记模型通知
        assert not any(cap == "send.text" for cap, _ in host.calls)
    finally:
        asyncio.run(plugin.on_unload())


def test_napcat_no_content_reason_unchanged_for_text_path():
    """文本降级 + message_id 回溯链路保持 napcat_no_content 口径。"""
    plugin, _, host = _new_plugin("slmid")
    try:
        plugin.config.napcat.enabled = True
        plugin._resolve_via_napcat = lambda seg: (None, None)
        asyncio.run(plugin._handle_file(
            "s-m", "s-m", "", {"name": "old.txt", "napcat_message_id": 12345}))
        notices = plugin._file_notices.get("s-m") or []
        assert len(notices) == 1
        assert notices[0]["reason"] == "文件回溯未返回内容"
    finally:
        asyncio.run(plugin.on_unload())


def test_reason_text_mapping_covers_new_reason():
    from plugin import FileReaderPlugin
    assert "no_content_file_id" in FileReaderPlugin._REASON_TEXT
    assert "file_id" in FileReaderPlugin._REASON_TEXT["no_content_file_id"]


# ── 7. _message_context：不用裸 id 兜底 ────────────────────────


def test_message_context_nested_user_and_group():
    from plugin import FileReaderPlugin
    msg = {"message_info": {"user_info": {"user_id": 1000000001},
                            "group_info": {"group_id": 888888888},
                            "platform": "qq"}}
    ctx = FileReaderPlugin._message_context(msg, {})
    assert ctx["user_id"] == "1000000001"
    assert ctx["group_id"] == "888888888"
    assert ctx["platform"] == "qq"


def test_message_context_flat_and_kwargs_priority():
    from plugin import FileReaderPlugin
    msg = {"message_info": {"user_id": "111", "group_id": "222"}}
    ctx = FileReaderPlugin._message_context(msg, {"user_id": "999"})
    assert ctx["user_id"] == "999", "kwargs 顶层优先"
    assert ctx["group_id"] == "222"


def test_message_context_bare_id_not_used():
    """fork 的缺陷：message_info.id 兜底当 user_id——它可能是消息/会话 ID，
    误用会让适配器解析到别人的文件。语义不明的 id 一律不取。"""
    from plugin import FileReaderPlugin
    msg = {"message_info": {"id": 123456789}}
    ctx = FileReaderPlugin._message_context(msg, {})
    assert ctx["user_id"] == ""
    assert ctx["group_id"] == ""


def test_message_context_sender_id_and_qq_aliases():
    from plugin import FileReaderPlugin
    ctx = FileReaderPlugin._message_context({"message_info": {"sender_id": "555"}}, {})
    assert ctx["user_id"] == "555"
    ctx = FileReaderPlugin._message_context({"message_info": {"user_info": {"qq": 666}}}, {})
    assert ctx["user_id"] == "666"


def test_detect_file_injects_context_into_fdata():
    """端到端：结构化 file 段经 hook 后，fdata 必须带上 user_id/group_id。"""
    plugin, _, host = _new_plugin("slinject", api_response=_ok())
    captured = {}

    async def _capture(session_id, conversation_id, stream_id, fdata):
        captured.update(fdata)

    plugin._handle_file = _capture
    try:
        message = {
            "session_id": "sess-1",
            "raw_message": [{"type": "file", "data": {
                "name": "附件", "file_id": "fid-x", "size": 100, "busid": 0}}],
            "message_info": {"user_info": {"user_id": 1000000001},
                             "group_info": {"group_id": 888888888}},
        }
        asyncio.run(plugin._detect_file("before", message=message))
        # 后台任务需要让出事件循环才执行
        async def _drain():
            for _ in range(20):
                if captured:
                    return
                await asyncio.sleep(0.01)
        asyncio.run(_drain())
        assert captured.get("file_id") == "fid-x"
        assert captured.get("user_id") == "1000000001"
        assert captured.get("group_id") == "888888888"
    finally:
        asyncio.run(plugin.on_unload())


# ── 8. css / scss / less 解析支持 ──────────────────────────────


@pytest.mark.parametrize("ext", ["css", "scss", "less"])
def test_stylesheet_extensions_supported(tmp_path, ext):
    content = ".btn { color: #333; }\n/* 注释 */\n"
    p = tmp_path / f"style.{ext}"
    p.write_text(content, encoding="utf-8")
    text = file_parser.read_any_file_to_text(str(p), f"style.{ext}")
    assert ".btn" in text and "color" in text


def test_describe_supported_types_mentions_css():
    assert "css" in file_parser.describe_supported_types()


def test_css_end_to_end_via_snowluma_channel(tmp_path):
    """真机场景复现：style.css 经 SL 通道下载 → 解析入库 → 可检索。"""
    plugin, mod, host = _new_plugin("slcss", api_response=_ok())
    css = "body { margin: 0; }\n.header { display: flex; }\n"

    async def _fake_download(url, max_bytes, suffix="", **kwargs):
        import os
        import tempfile
        fd, p = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(css.encode("utf-8"))
        return p, len(css.encode()), hashlib.sha256(css.encode()).hexdigest()

    async def _embed(texts):
        return [[float(len(t) % 7), 1.0] for t in texts]

    plugin._download_to_temp = _fake_download
    plugin._embed = _embed
    try:
        asyncio.run(plugin._handle_file(
            "s-css", "s-css", "",
            {"name": "style.css", "file_id": "fid-css", "size_hint": len(css),
             "user_id": "1000000001"}))
        vs = plugin._store.get("s-css", "s-css")
        assert vs is not None and vs.files, "css 应成功入库"
        entry = list(vs.files.values())[0]
        assert "display: flex" in entry.full_text()
        assert plugin._file_notices.get("s-css") in (None, []), "不得有失败通知"
    finally:
        asyncio.run(plugin.on_unload())


# ── 9. 配置与 manifest 契约 ────────────────────────────────────


def test_snowluma_config_defaults():
    from plugin import FileReaderConfig
    cfg = FileReaderConfig()
    assert cfg.snowluma.enabled is True
    assert cfg.snowluma.private_file_url_api == PRIVATE_API
    assert cfg.snowluma.group_file_url_api == GROUP_API
    # 版本随功能演进（1.5.0=snowluma 节 → 1.6.0=video 节）；断言不低于引入节时的版本
    from packaging.version import Version
    assert Version(cfg.plugin.config_version) >= Version("1.5.0"), "新增 [snowluma] 节必须 bump config_version"


def test_manifest_declares_api_call():
    """api.call 必须声明：Host 1.4.6 只有 api.replace_dynamic 免声明。"""
    import json
    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    assert "api.call" in manifest.get("capabilities", [])
    from packaging.version import Version
    assert Version(manifest["version"]) >= Version("1.7.0")


def test_status_reports_snowluma_channel():
    plugin, _, host = _new_plugin("slstatus")
    try:
        ok, text, _ = asyncio.run(plugin.cmd_status(stream_id=""))
        assert ok
        assert "适配器取文件" in text
    finally:
        asyncio.run(plugin.on_unload())


# ── 10. v1.8.1 可信适配器 URL（真机 11-19 09:19 裸 IP 被误拒） ──
#
# 真机日志：私聊离线文件 → SL 换 URL 成功 → http://183.60.230.81:80/qqdownloadftnv5
# 被 host_not_allowed 拒绝。QQ 离线文件服务器给裸 IP+HTTP，域名白名单覆盖不了。
# 修法：适配器 URL 是可信来源（RPC 返回，非消息正文），首跳按公网地址校验；
# 内网仍拒、重定向回退白名单、消息正文 URL 不受影响。


class _FakeStreamResponse:
    def __init__(self, status_code, headers=None, body=b""):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._body = body

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "location" in self.headers

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aiter_bytes(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


class _FakeClient:
    """记录出站 (url, headers) 的 httpx 替身。"""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def stream(self, method, url, **kwargs):
        self.requests.append((url, dict(kwargs.get("headers") or {})))
        status, headers, body = self.routes.get(url, (404, {}, b""))
        resp = _FakeStreamResponse(status, headers, body)

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def aclose(self):
        return None


def _patch_resolver(plugin, resolver):
    """在被测模块命名空间替换 resolve_host_ips（load_plugin_module 每次出新模块对象）。"""
    g = type(plugin)._download.__globals__
    original = g["resolve_host_ips"]
    g["resolve_host_ips"] = resolver
    return lambda: g.__setitem__("resolve_host_ips", original)


def _install_client(plugin, client):
    async def factory():
        return client

    plugin._get_http_client = factory


def test_trusted_bare_ip_downloads_end_to_end(tmp_path):
    """真机场景复现：SL 返回裸 IP+HTTP → 下载成功落盘（v1.8.0 这里会 host_not_allowed）。"""
    bare = "http://183.60.230.81:80/qqdownloadftnv5?sig=xxx"
    plugin, _, host = _new_plugin("slbareip", api_response=_ok(bare))
    body = b"offline file content"
    client = _FakeClient({"http://183.60.230.81:80/qqdownloadftnv5?sig=xxx": (200, {}, body)})
    restore = _patch_resolver(plugin, lambda h, p: [h])
    _install_client(plugin, client)
    try:
        fdata = {"name": "附件", "file_id": "fid", "size_hint": len(body), "user_id": "1000000001"}
        path, napcat_path, size, digest = asyncio.run(
            plugin._acquire_source(fdata, "附件", 1024 * 1024))
        assert napcat_path is None
        assert Path(path).read_bytes() == body
        assert size == len(body)
        assert digest == hashlib.sha256(body).hexdigest()
        assert client.requests, "应当发出一次请求"
        Path(path).unlink(missing_ok=True)
    finally:
        restore()
        asyncio.run(plugin.on_unload())


def test_trusted_source_still_rejects_private_ip():
    """可信来源只放宽域名层：内网/回环/云元数据 IP 一律仍拒（零出站请求）。"""
    for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254"):
        plugin, _, _ = _new_plugin(f"slpriv{ip.replace('.', '')}",
                                   api_response=_ok(f"http://{ip}/x"))
        client = _FakeClient({})
        restore = _patch_resolver(plugin, lambda h, p: [h])
        _install_client(plugin, client)
        try:
            with pytest.raises(Exception) as ei:
                asyncio.run(plugin._acquire_source(
                    {"name": "x.bin", "file_id": "fid", "user_id": "123"}, "x.bin", 4096))
            assert type(ei.value).__name__ == "_UrlRejectedError", ip
            assert ei.value.reason == "non_public_address", ip
            assert client.requests == [], f"{ip} 不应发出任何请求"
        finally:
            restore()
            asyncio.run(plugin.on_unload())


def test_trust_disabled_restores_whitelist_rejection():
    """trust_adapter_urls=False 回滚开关：裸 IP 回到 host_not_allowed（旧行为）。"""
    plugin, _, _ = _new_plugin("sltrustoff",
                               api_response=_ok("http://183.60.230.81/qqdownloadftnv5"))
    client = _FakeClient({})
    restore = _patch_resolver(plugin, lambda h, p: [h])
    _install_client(plugin, client)
    try:
        plugin.config.snowluma.trust_adapter_urls = False
        with pytest.raises(Exception) as ei:
            asyncio.run(plugin._acquire_source(
                {"name": "x.bin", "file_id": "fid", "user_id": "123"}, "x.bin", 4096))
        assert type(ei.value).__name__ == "_UrlRejectedError"
        assert ei.value.reason == "host_not_allowed"
        assert client.requests == []
    finally:
        restore()
        asyncio.run(plugin.on_unload())


def test_message_body_url_not_trusted():
    """威胁模型边界：消息正文里的 URL（fdata.url）不享受信任，裸 IP 仍拒。"""
    plugin, _, host = _new_plugin("slmsgurl")
    client = _FakeClient({})
    restore = _patch_resolver(plugin, lambda h, p: [h])
    _install_client(plugin, client)
    try:
        with pytest.raises(Exception) as ei:
            asyncio.run(plugin._acquire_source(
                {"name": "x.bin", "url": "http://183.60.230.81/qqdownloadftnv5"}, "x.bin", 4096))
        assert type(ei.value).__name__ == "_UrlRejectedError"
        assert ei.value.reason == "host_not_allowed"
        assert client.requests == []
        assert _api_calls(host) == [], "正文 URL 路径不应触发适配器 RPC"
    finally:
        restore()
        asyncio.run(plugin.on_unload())


def test_trusted_redirect_hop_falls_back_to_whitelist():
    """信任只给首跳：302 到域外主机 → 第二跳按用户白名单拒绝。"""
    plugin, _, _ = _new_plugin("slredir",
                               api_response=_ok("http://183.60.230.81/qqdownloadftnv5"))
    client = _FakeClient({
        "http://183.60.230.81/qqdownloadftnv5": (
            302, {"location": "http://evil.example.com/payload"}, b""),
    })
    restore = _patch_resolver(plugin, lambda h, p: {"183.60.230.81": ["183.60.230.81"],
                                                    "evil.example.com": ["93.184.216.34"]}[h])
    _install_client(plugin, client)
    try:
        with pytest.raises(Exception) as ei:
            asyncio.run(plugin._acquire_source(
                {"name": "x.bin", "file_id": "fid", "user_id": "123"}, "x.bin", 4096))
        assert type(ei.value).__name__ == "_UrlRejectedError"
        assert ei.value.reason == "host_not_allowed"
        assert len(client.requests) == 1, "第二跳被拒前只应有首跳请求"
    finally:
        restore()
        asyncio.run(plugin.on_unload())


def test_trusted_redirect_to_whitelisted_host_succeeds(tmp_path):
    """302 到白名单内主机（QQ 域）→ 正常跟随下载（信任不破坏合法重定向）。"""
    plugin, _, _ = _new_plugin("slredirok",
                               api_response=_ok("http://183.60.230.81/qqdownloadftnv5"))
    body = b"REDIR_OK"
    client = _FakeClient({
        "http://183.60.230.81/qqdownloadftnv5": (
            302, {"location": "https://ftn.qq.com/realfile"}, b""),
        "https://93.184.216.34/realfile": (200, {}, body),
    })
    restore = _patch_resolver(plugin, lambda h, p: {"183.60.230.81": ["183.60.230.81"],
                                                    "ftn.qq.com": ["93.184.216.34"]}[h])
    _install_client(plugin, client)
    try:
        path, _, size, _ = asyncio.run(plugin._acquire_source(
            {"name": "x.bin", "file_id": "fid", "user_id": "123"}, "x.bin", 4096))
        assert Path(path).read_bytes() == body
        assert size == len(body)
        urls = [u for u, _h in client.requests]
        assert urls == ["http://183.60.230.81/qqdownloadftnv5", "https://93.184.216.34/realfile"], (
            f"第二跳应钉到它自己校验过的 IP: {client.requests}")
        Path(path).unlink(missing_ok=True)
    finally:
        restore()
        asyncio.run(plugin.on_unload())


def test_trust_adapter_urls_default_and_config_version():
    from plugin import FileReaderConfig
    cfg = FileReaderConfig()
    assert cfg.snowluma.trust_adapter_urls is True, "默认信任适配器 URL（修复开箱即用）"
    from packaging.version import Version
    assert Version(cfg.plugin.config_version) >= Version("1.7.0"), (
        "新增 trust_adapter_urls 字段必须 bump config_version，否则存量实例不补键")
