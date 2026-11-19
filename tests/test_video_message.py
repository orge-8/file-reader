# -*- coding: utf-8 -*-
"""v1.9.1 QQ「视频」消息（video 段）接入 file-reader 链路。

背景（真机 11-19 21:14）
------------------------
QQ 里直接发「视频」走的是 OneBot **video 段**，不是 file 段。SnowLuma 适配器
把 video 段降级成纯文本 `[视频] 文件: 7a76…db9e3.mp4，大小: 3977255`
（官方文档 04 §6：video/file → 纯文本）。旧版三道门全只认 file/[文件]：
  1. `_looks_like_file` 不认 `[视频]`/video 段 → 连 FILE 诊断都不打；
  2. `_RE_HINT_NAME` 只匹配 `[文件]` → 降级文本解析不出 hint；
  3. `_acquire_source` 对只有 message_id 的 hint 直接抛 napcat_disabled。
结果：视频全程零日志、不入库，Planner 空转 7 次 tool_search 后误称
「这视频我这边放不出来」「音频一律听不了」。

v1.9.1 修法（本文件把契约做成回归）：
1. `[视频]` 降级文本解析（含/不含 `文件:` 前缀、多行合并、带链接、file_ref 尾巴）；
2. `_looks_like_file` / `_extract_files` 认 video 段（file_id/url/path/base64 各形态）；
3. `_pick_onebot_file_segs` 取 video 段、`_match_file_seg` 按 file 字段 basename 匹配；
4. `_get_msg_via_adapter`：action.call_data + get_msg 参数契约、负 message_id、
   成功判定（success=False / 无 message / 异常 → None 降级）；
5. `_acquire_source`：napcat_message_id → RPC 回溯 → video 段 URL 可信下载，
   全程不依赖 napcat.enabled；回溯失败才回退 HTTP 兜底；
6. `_resolve_via_napcat`（HTTP 兜底）也能接住 video 段 URL（同步下载 + 内网拒绝）。
"""

import asyncio
import base64
import hashlib
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

VIDEO_NAME = "0123456789abcdef0123456789abcdef.mp4"
VIDEO_SIZE = 3977255
VIDEO_URL = "https://multimedia.nt.qq.com.cn/download?appid=1407&rkey=abc"
BACKTRACK_API = "adapter.napcat.action.call_data"


def _new_plugin(tag="v191", api_response=None, api_error=None):
    """带可控 api.call 的插件实例（与 test_snowluma_channel 同款桩）。"""
    from fakehost import FakeHost, bind_context, build_context, get_default_config, load_plugin_module

    class _VHost(FakeHost):
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
    host = _VHost("org.mai-mai.file-reader")
    bind_context(plugin, build_context("org.mai-mai.file-reader", rpc_call=host.rpc_call),
                 get_default_config(mod.FileReaderConfig))
    asyncio.run(plugin.on_load())
    return plugin, mod, host


def _api_calls(host):
    return [(args.get("api_name"), args.get("args")) for cap, args in host.calls if cap == "api.call"]


def _video_msg_data(url=VIDEO_URL, name=VIDEO_NAME):
    """get_msg 返回的原始 OneBot 事件（video 段形态）。"""
    return {
        "message_id": 1234567890,
        "message": [
            {"type": "video", "data": {"file": name, "url": url}},
        ],
    }


# ── 1. [视频] 降级文本解析 ─────────────────────────────────────


def test_hint_video_with_file_prefix():
    """真机 21:14 原样：`[视频] 文件: xxx.mp4，大小: N`。"""
    from plugin import FileReaderPlugin

    plugin = FileReaderPlugin.__new__(FileReaderPlugin)
    msg = {"processed_plain_text": f"[视频] 文件: {VIDEO_NAME}，大小: {VIDEO_SIZE}"}
    hints = plugin._parse_file_hints(msg)
    assert len(hints) == 1
    assert hints[0]["name"] == VIDEO_NAME
    assert hints[0]["size_hint"] == VIDEO_SIZE


def test_hint_video_without_prefix():
    from plugin import FileReaderPlugin

    plugin = FileReaderPlugin.__new__(FileReaderPlugin)
    hints = plugin._parse_file_hints({"processed_plain_text": f"[视频] {VIDEO_NAME}，大小: {VIDEO_SIZE}"})
    assert len(hints) == 1
    assert hints[0]["name"] == VIDEO_NAME


def test_hint_video_with_url_and_file_ref_tail():
    """带链接 + Host 1.3.16 尾巴（file_ref）不得吞进文件名。

    注：URL 提取正则 `https?://\\S+` 会把紧随其后的 `，file_ref:` 一并吃进
    （\\S 含全角逗号）——这是 v1.1.0 起的既有行为，[文件] 形态同样如此，
    非本次回归。本用例只守「文件名不被尾巴污染」这条 v1.3.1 契约。
    """
    from plugin import FileReaderPlugin

    plugin = FileReaderPlugin.__new__(FileReaderPlugin)
    line = f"[视频] 文件: {VIDEO_NAME}，大小: {VIDEO_SIZE}，链接: {VIDEO_URL}，file_ref: 5a1b"
    hints = plugin._parse_file_hints({"processed_plain_text": line})
    assert len(hints) == 1
    assert hints[0]["name"] == VIDEO_NAME
    assert hints[0]["size_hint"] == VIDEO_SIZE
    assert hints[0]["url"].startswith(VIDEO_URL)


def test_hint_mixed_file_and_video_lines():
    """防抖合并：一条消息里 [文件] 与 [视频] 混排，逐行都要解析出来。"""
    from plugin import FileReaderPlugin

    plugin = FileReaderPlugin.__new__(FileReaderPlugin)
    text = f"[文件] 笔记.docx，大小: 9547\n[视频] 文件: {VIDEO_NAME}，大小: {VIDEO_SIZE}"
    hints = plugin._parse_file_hints({"processed_plain_text": text})
    assert [h["name"] for h in hints] == ["笔记.docx", VIDEO_NAME]


def test_hint_plain_chat_still_empty():
    from plugin import FileReaderPlugin

    plugin = FileReaderPlugin.__new__(FileReaderPlugin)
    assert plugin._parse_file_hints({"processed_plain_text": "今天天气不错"}) == []
    assert plugin._parse_file_hints({"processed_plain_text": "[图片] 看看这个"}) == []


def test_looks_like_file_accepts_video():
    from plugin import FileReaderPlugin

    plugin = FileReaderPlugin.__new__(FileReaderPlugin)
    assert plugin._looks_like_file({"processed_plain_text": f"[视频] 文件: {VIDEO_NAME}，大小: {VIDEO_SIZE}"})
    assert plugin._looks_like_file({"raw_message": [{"type": "video", "data": {"file": VIDEO_NAME}}]})
    assert not plugin._looks_like_file({"processed_plain_text": "普通消息"})


# ── 2. video 段提取（_extract_files / _seg_to_file） ───────────


def test_extract_video_seg_with_url():
    from plugin import FileReaderPlugin

    plugin = FileReaderPlugin.__new__(FileReaderPlugin)
    msg = {"raw_message": [{"type": "video", "data": {"file": VIDEO_NAME, "url": VIDEO_URL}}]}
    files = plugin._extract_files(msg)
    assert len(files) == 1
    assert files[0]["name"] == VIDEO_NAME
    assert files[0]["url"] == VIDEO_URL


def test_extract_video_seg_file_id_only():
    """仅 file_id 的 video 段也要交出去（取字节层走回溯/SL）。"""
    from plugin import FileReaderPlugin

    plugin = FileReaderPlugin.__new__(FileReaderPlugin)
    msg = {"raw_message": [{"type": "video", "data": {"file_id": "vid-1", "file": VIDEO_NAME, "size": VIDEO_SIZE}}]}
    files = plugin._extract_files(msg)
    assert len(files) == 1
    assert files[0]["file_id"] == "vid-1"
    assert files[0]["size_hint"] == VIDEO_SIZE


def test_extract_video_seg_base64():
    from plugin import FileReaderPlugin

    plugin = FileReaderPlugin.__new__(FileReaderPlugin)
    payload = base64.b64encode(b"\x00\x00\x00\x20ftypisom").decode()
    msg = {"raw_message": [{"type": "video", "data": {"file": VIDEO_NAME, "base64": payload}}]}
    files = plugin._extract_files(msg)
    assert files[0]["bytes"] == b"\x00\x00\x00\x20ftypisom"


def test_extract_file_seg_behavior_unchanged():
    """回归守卫：file 段提取行为不受 video 支持影响。"""
    from plugin import FileReaderPlugin

    plugin = FileReaderPlugin.__new__(FileReaderPlugin)
    msg = {"raw_message": [{"type": "file", "data": {"file_id": "f1", "name": "a.pdf", "size": 100}}]}
    files = plugin._extract_files(msg)
    assert files == [{"name": "a.pdf", "file_id": "f1", "busid": 0, "size_hint": 100}]


# ── 3. get_msg 原始事件里的 video 段匹配 ───────────────────────


def test_pick_onebot_segs_includes_video():
    from plugin import FileReaderPlugin

    msg = _video_msg_data()
    msg["message"].insert(0, {"type": "text", "data": {"text": "hi"}})
    segs = FileReaderPlugin._pick_onebot_file_segs(msg)
    assert len(segs) == 1
    assert segs[0]["file"] == VIDEO_NAME


def test_match_video_seg_by_file_basename():
    """video 段没有 file_name/name，靠 file 字段 basename 匹配。"""
    from plugin import FileReaderPlugin

    segs = [{"file": VIDEO_NAME, "url": VIDEO_URL}]
    seg, mode = FileReaderPlugin._match_file_seg(segs, VIDEO_NAME, VIDEO_SIZE)
    assert seg is segs[0]
    assert mode == "name"


def test_match_video_seg_size_disambiguation():
    """占位名场景：两个 video 段靠 size_hint 消歧（video 段 size 字段）。"""
    from plugin import FileReaderPlugin

    segs = [
        {"file": "a.mp4", "url": "https://x/1", "size": 100},
        {"file": "b.mp4", "url": "https://x/2", "size": VIDEO_SIZE},
    ]
    seg, mode = FileReaderPlugin._match_file_seg(segs, "附件", VIDEO_SIZE)
    assert seg["file"] == "b.mp4"
    assert mode == "size"


# ── 4. _get_msg_via_adapter：RPC 回溯契约 ──────────────────────


def test_backtrack_params_contract():
    """action.call_data + action_name=get_msg + params 包装；message_id 原样（负数合法）。"""
    plugin, _, host = _new_plugin("vbt", api_response=_video_msg_data())
    try:
        data = asyncio.run(plugin._get_msg_via_adapter(-822208709))
        assert data is not None and "message" in data
        calls = _api_calls(host)
        assert len(calls) == 1
        api_name, args = calls[0]
        assert api_name == BACKTRACK_API
        assert args == {"action_name": "get_msg", "params": {"message_id": -822208709}}
    finally:
        asyncio.run(plugin.on_unload())


def test_backtrack_unwraps_onebot_envelope():
    """个别形态仍带 {status, retcode, data} 外壳时要剥到 data。"""
    plugin, _, _ = _new_plugin("vbenv", api_response={"status": "ok", "retcode": 0, "data": _video_msg_data()})
    try:
        data = asyncio.run(plugin._get_msg_via_adapter(123))
        assert data is not None
        assert data["message"][0]["type"] == "video"
    finally:
        asyncio.run(plugin.on_unload())


def test_backtrack_host_failure_returns_none():
    plugin, _, _ = _new_plugin("vbfail", api_response={"success": False, "error": "timeout"})
    try:
        assert asyncio.run(plugin._get_msg_via_adapter(123)) is None
    finally:
        asyncio.run(plugin.on_unload())


def test_backtrack_rpc_exception_returns_none():
    plugin, _, _ = _new_plugin("vbexc", api_error=RuntimeError("E_CAPABILITY_DENIED"))
    try:
        assert asyncio.run(plugin._get_msg_via_adapter(123)) is None
    finally:
        asyncio.run(plugin.on_unload())


def test_backtrack_no_message_returns_none():
    plugin, _, _ = _new_plugin("vbempty", api_response={"message_id": 1})
    try:
        assert asyncio.run(plugin._get_msg_via_adapter(123)) is None
    finally:
        asyncio.run(plugin.on_unload())


def test_backtrack_disabled_by_empty_api():
    plugin, _, host = _new_plugin("vboff", api_response=_video_msg_data())
    try:
        plugin.config.snowluma.msg_backtrack_api = ""
        assert asyncio.run(plugin._get_msg_via_adapter(123)) is None
        assert _api_calls(host) == []
    finally:
        asyncio.run(plugin.on_unload())


def test_backtrack_no_message_id_no_rpc():
    plugin, _, host = _new_plugin("vbnomid", api_response=_video_msg_data())
    try:
        assert asyncio.run(plugin._get_msg_via_adapter(None)) is None
        assert _api_calls(host) == []
    finally:
        asyncio.run(plugin.on_unload())


# ── 5. _acquire_source：视频 hint 端到端（不依赖 napcat.enabled） ──


def test_acquire_video_hint_via_backtrack_url(tmp_path):
    """核心场景：`[视频]` hint 只有 message_id → RPC get_msg → video 段 URL → 可信下载。

    napcat.enabled 保持默认 False——真机 21:14 就是这个配置，旧版直接抛
    napcat_disabled，新版必须走通。
    """
    plugin, _, host = _new_plugin("vacq", api_response=_video_msg_data())
    body = b"\x00\x00\x00\x20ftypisom" + b"video-bytes" * 100
    seen = {}

    async def _fake_download(url, max_bytes, suffix="", **kwargs):
        seen["url"] = url
        seen["trusted"] = kwargs.get("trusted_source")
        p = tmp_path / "back.mp4"
        p.write_bytes(body)
        return str(p), len(body), hashlib.sha256(body).hexdigest()

    plugin._download_to_temp = _fake_download
    try:
        assert plugin.config.napcat.enabled is False
        fdata = {"name": VIDEO_NAME, "size_hint": VIDEO_SIZE, "napcat_message_id": 1234567890}
        path, napcat_path, size, digest = asyncio.run(
            plugin._acquire_source(fdata, VIDEO_NAME, 20 * 1024 * 1024))
        assert seen["url"] == VIDEO_URL
        assert seen["trusted"] is True, "回溯 URL 来自适配器 RPC，必须按可信来源下载"
        assert napcat_path is None
        assert size == len(body)
        assert digest == hashlib.sha256(body).hexdigest()
        assert Path(path).read_bytes() == body
        # 只发一次 get_msg 回溯，不发文件 URL API
        calls = _api_calls(host)
        assert len(calls) == 1 and calls[0][0] == BACKTRACK_API
        Path(path).unlink(missing_ok=True)
    finally:
        asyncio.run(plugin.on_unload())


def test_acquire_video_hint_backtrack_base64(tmp_path):
    """video 段带 base64 时直接落盘，零下载。"""
    body = b"video-base64-content"
    msg = _video_msg_data()
    msg["message"][0]["data"] = {"file": VIDEO_NAME, "base64": base64.b64encode(body).decode()}
    plugin, _, host = _new_plugin("vb64", api_response=msg)
    try:
        fdata = {"name": VIDEO_NAME, "size_hint": len(body), "napcat_message_id": 1}
        path, _, size, _ = asyncio.run(plugin._acquire_source(fdata, VIDEO_NAME, 1024 * 1024))
        assert size == len(body)
        assert Path(path).read_bytes() == body
        Path(path).unlink(missing_ok=True)
    finally:
        asyncio.run(plugin.on_unload())


def test_acquire_video_hint_backtrack_fail_then_napcat_disabled():
    """回溯失败 + napcat 未启用 → napcat_disabled（文案含回溯语义）。"""
    plugin, _, _ = _new_plugin("vfail", api_response={"success": False, "error": "no msg"})
    try:
        fdata = {"name": VIDEO_NAME, "size_hint": VIDEO_SIZE, "napcat_message_id": 1}
        with pytest.raises(Exception) as ei:
            asyncio.run(plugin._acquire_source(fdata, VIDEO_NAME, 1024 * 1024))
        assert type(ei.value).__name__ == "_SourceUnavailableError"
        assert ei.value.reason == "napcat_disabled"
        assert "回溯" in str(ei.value)
    finally:
        asyncio.run(plugin.on_unload())


def test_acquire_video_hint_backtrack_download_fail_falls_back(tmp_path):
    """回溯拿到 URL 但下载失败（非确定性）→ 回退 NapCat HTTP 兜底（若启用）。"""
    plugin, _, _ = _new_plugin("vfb", api_response=_video_msg_data())
    src = tmp_path / "cache" / VIDEO_NAME
    src.parent.mkdir()
    src.write_bytes(b"napcat-local-video")

    async def _broken_download(url, max_bytes, suffix="", **kwargs):
        raise ConnectionError("simulated")

    plugin._download_to_temp = _broken_download
    plugin.config.napcat.enabled = True
    plugin._resolve_via_napcat = lambda fdata: (None, str(src))
    try:
        fdata = {"name": VIDEO_NAME, "size_hint": VIDEO_SIZE, "napcat_message_id": 1}
        path, napcat_path, size, _ = asyncio.run(
            plugin._acquire_source(fdata, VIDEO_NAME, 20 * 1024 * 1024))
        assert napcat_path == str(src)
        assert Path(path).read_bytes() == b"napcat-local-video"
        Path(path).unlink(missing_ok=True)
    finally:
        asyncio.run(plugin.on_unload())


def test_acquire_video_hint_url_rejected_no_fallback():
    """回溯 URL 指向内网 → 确定性安全拒绝，不回退。"""
    plugin, _, _ = _new_plugin("vrej", api_response=_video_msg_data(url="http://192.168.1.1/v.mp4"))
    called = {"napcat": 0}

    def _napcat(fdata):
        called["napcat"] += 1
        return (None, None)

    plugin.config.napcat.enabled = True
    plugin._resolve_via_napcat = _napcat
    try:
        fdata = {"name": VIDEO_NAME, "size_hint": VIDEO_SIZE, "napcat_message_id": 1}
        with pytest.raises(Exception) as ei:
            asyncio.run(plugin._acquire_source(fdata, VIDEO_NAME, 20 * 1024 * 1024))
        assert type(ei.value).__name__ == "_UrlRejectedError"
        assert ei.value.reason == "non_public_address"
        assert called["napcat"] == 0
    finally:
        asyncio.run(plugin.on_unload())


def test_acquire_file_id_prefers_snowluma_over_backtrack(tmp_path):
    """回归守卫：有 file_id 时仍首选 SL 文件 URL 通道，不走 get_msg 回溯。"""
    plugin, _, host = _new_plugin("vprec", api_response={"status": "ok", "retcode": 0, "data": {"url": VIDEO_URL}})
    body = b"x"

    async def _fake_download(url, max_bytes, suffix="", **kwargs):
        p = tmp_path / "sl.mp4"
        p.write_bytes(body)
        return str(p), len(body), hashlib.sha256(body).hexdigest()

    plugin._download_to_temp = _fake_download
    try:
        fdata = {"name": VIDEO_NAME, "file_id": "fid", "user_id": "123", "napcat_message_id": 1}
        path, _, _, _ = asyncio.run(plugin._acquire_source(fdata, VIDEO_NAME, 1024 * 1024))
        calls = _api_calls(host)
        assert len(calls) == 1
        assert calls[0][0] == "adapter.napcat.file.get_private_file_url"
        Path(path).unlink(missing_ok=True)
    finally:
        asyncio.run(plugin.on_unload())


# ── 6. NapCat HTTP 兜底也能接住 video 段 URL ───────────────────


def test_napcat_fallback_downloads_video_url(monkeypatch):
    """_resolve_via_napcat：video 段无本地路径 → 同步下载 URL 返回 bytes。"""
    plugin, _, _ = _new_plugin("vhttp", api_response=None)
    body = b"http-fallback-video"
    msg = _video_msg_data()
    plugin._napcat_get_msg = lambda mid: msg
    plugin._napcat_resolve_path = lambda seg: None

    class _Resp:
        def __init__(self, data):
            self._data = data

        def read(self, n=-1):
            return self._data if n < 0 else self._data[:n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import plugin as plugin_mod

    monkeypatch.setattr(plugin_mod.urllib.request, "urlopen", lambda req, timeout=None: _Resp(body))
    try:
        plugin.config.napcat.enabled = True
        fdata = {"name": VIDEO_NAME, "size_hint": len(body), "napcat_message_id": 1}
        raw, path = plugin._resolve_via_napcat(fdata)
        assert raw == body and path is None
    finally:
        asyncio.run(plugin.on_unload())


def test_napcat_fallback_rejects_private_ip(monkeypatch):
    plugin, _, _ = _new_plugin("vpriv", api_response=None)
    msg = _video_msg_data(url="http://127.0.0.1/v.mp4")
    plugin._napcat_get_msg = lambda mid: msg
    plugin._napcat_resolve_path = lambda seg: None
    called = {"open": 0}

    import plugin as plugin_mod

    def _no_open(req, timeout=None):
        called["open"] += 1
        raise AssertionError("内网 URL 不得发起下载")

    monkeypatch.setattr(plugin_mod.urllib.request, "urlopen", _no_open)
    try:
        plugin.config.napcat.enabled = True
        fdata = {"name": VIDEO_NAME, "size_hint": 10, "napcat_message_id": 1}
        raw, path = plugin._resolve_via_napcat(fdata)
        assert raw is None and path is None
        assert called["open"] == 0
    finally:
        asyncio.run(plugin.on_unload())


# ── 7. 配置默认值 ──────────────────────────────────────────────


def test_config_defaults_v191():
    plugin, mod, _ = _new_plugin("vcfg")
    try:
        assert plugin.config.snowluma.msg_backtrack_api == BACKTRACK_API
        assert plugin.config.plugin.config_version == "1.9.1"
    finally:
        asyncio.run(plugin.on_unload())
