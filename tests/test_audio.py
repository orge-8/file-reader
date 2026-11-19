# -*- coding: utf-8 -*-
"""音频理解通道测试（v1.5.0）。

覆盖面：
- 扩展名/嗅探判定（含 silk/amr 明确拒绝、无扩展名兜底）
- 预检 fail-closed：超大小/base64 硬上限/超时长/时长读不出 → 全部拒绝，绝不截断
- 未配 Key → 配置指引（AudioUnsupportedError，value_error 链路）
- Omni 请求形态：input_audio + base64 data URI + stream=true + format 字段
- SSE 拼接：多 delta、[DONE]、心跳/坏 JSON 行跳过、HTTP 错误带 body
- 产出文本元信息头部 + 入库检索（追问免费的前提）
- 同步解析入口对音频给准确原因（不落入笼统"不支持的文件类型"）
- 重试队列：音频确定性拒绝立即放弃，不空转

全程离线：Omni 网络层用桩替换，不发真实请求。
"""

import asyncio
import base64
import json
import wave
from pathlib import Path

import pytest

import fr_audio
from file_parser import (
    RECOGNIZED_UNSUPPORTED,
    get_extension,
    read_any_file_to_text,
)


def _wav(path: Path, seconds: float = 1.0, rate: int = 8000) -> Path:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


# ─── 判定 ────────────────────────────────────────────────────────


def test_audio_extensions():
    for name in ("a.mp3", "b.WAV", "c.flac", "d.ogg", "e.m4a", "f.aac"):
        assert fr_audio.is_audio_file(name), name
    for name in ("voice.silk", "v.amr", "x.wma", "movie.mp4", "doc.pdf", "noext"):
        assert not fr_audio.is_audio_file(name), name


def test_sniff_audio_format():
    assert fr_audio.sniff_audio_format(b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00") == "mp3"
    assert fr_audio.sniff_audio_format(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "wav"
    assert fr_audio.sniff_audio_format(b"fLaC\x00\x00\x00\x22\x00\x00\x00\x00") == "flac"
    assert fr_audio.sniff_audio_format(b"OggS\x00\x02\x00\x00\x00\x00\x00\x00") == "ogg"
    assert fr_audio.sniff_audio_format(b"\x00\x00\x00\x20ftypM4A \x00\x00") == "m4a"
    assert fr_audio.sniff_audio_format(b"\xff\xfb\x90\x00" + b"\x00" * 8) == "mp3"
    assert fr_audio.sniff_audio_format(b"\xff\xf1\x50\x80" + b"\x00" * 8) == "aac"
    assert fr_audio.sniff_audio_format(b"#!SILK_V3\x00\x00\x00") == "silk"
    assert fr_audio.sniff_audio_format(b"%PDF-1.4\n\x00\x00\x00") == ""
    assert fr_audio.sniff_audio_format(b"short") == ""


def test_silk_amr_in_recognized_unsupported():
    # 拒绝原因单一来源：file_parser 与 fr_audio 一致
    for ext in ("silk", "slk", "amr", "wma"):
        assert ext in RECOGNIZED_UNSUPPORTED
        assert RECOGNIZED_UNSUPPORTED[ext] == fr_audio.AUDIO_RECOGNIZED_UNSUPPORTED[ext]


def test_sync_entry_rejects_audio_with_accurate_reason(tmp_path):
    wav = _wav(tmp_path / "a.wav")
    with pytest.raises(ValueError, match="音频"):
        read_any_file_to_text(str(wav), "a.wav")
    silk = tmp_path / "v.silk"
    silk.write_bytes(b"#!SILK_V3" + b"\x00" * 32)
    with pytest.raises(ValueError, match="silk"):
        read_any_file_to_text(str(silk), "v.silk")


# ─── 预检 fail-closed ────────────────────────────────────────────


def test_precheck_ok(tmp_path):
    wav = _wav(tmp_path / "a.wav", seconds=2.0)
    fmt, duration, size = fr_audio.precheck_audio(
        str(wav), "a.wav", max_duration_seconds=1200, max_file_mb=7.0
    )
    assert fmt == "wav"
    assert duration == pytest.approx(2.0, abs=0.01)
    assert size == wav.stat().st_size


def test_precheck_rejects_oversize(tmp_path):
    wav = _wav(tmp_path / "a.wav")
    with pytest.raises(fr_audio.AudioUnsupportedError, match="超过上限"):
        fr_audio.precheck_audio(str(wav), "a.wav", max_duration_seconds=1200, max_file_mb=0.001)


def test_precheck_rejects_over_duration(tmp_path):
    wav = _wav(tmp_path / "a.wav", seconds=3.0)
    with pytest.raises(fr_audio.AudioUnsupportedError, match="时长"):
        fr_audio.precheck_audio(str(wav), "a.wav", max_duration_seconds=1, max_file_mb=7.0)


def test_precheck_rejects_unknown_duration(tmp_path):
    # 扩展名是 mp3 但内容是垃圾：mutagen 读不出时长 → fail-closed 拒绝
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"\x00" * 1024)
    with pytest.raises(fr_audio.AudioUnsupportedError, match="时长"):
        fr_audio.precheck_audio(str(bad), "bad.mp3", max_duration_seconds=1200, max_file_mb=7.0)


def test_precheck_rejects_empty_file(tmp_path):
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    with pytest.raises(fr_audio.AudioUnsupportedError, match="空文件"):
        fr_audio.precheck_audio(str(empty), "empty.wav", max_duration_seconds=1200, max_file_mb=7.0)


def test_precheck_base64_hard_limit(tmp_path, monkeypatch):
    # 配置放得再宽，base64 硬上限（接口 10MB）也不可越过
    wav = _wav(tmp_path / "a.wav", seconds=1.0)
    monkeypatch.setattr(fr_audio, "BASE64_HARD_LIMIT_BYTES", 100)
    with pytest.raises(fr_audio.AudioUnsupportedError, match="硬上限"):
        fr_audio.precheck_audio(str(wav), "a.wav", max_duration_seconds=0, max_file_mb=0)


def test_precheck_unsupported_format(tmp_path):
    f = tmp_path / "a.wma"
    f.write_bytes(b"\x00" * 64)
    with pytest.raises(fr_audio.AudioUnsupportedError, match="WMA"):
        fr_audio.precheck_audio(str(f), "a.wma", max_duration_seconds=1200, max_file_mb=7.0)


# ─── read_audio_to_text：Key 指引 / Omni 桩 / 头部 ────────────────


_KW = dict(
    base_url="https://example.invalid/v1",
    api_key="sk-test",
    model="qwen3.8-omni-flash",
    max_duration_seconds=3600,
    max_file_mb=7.0,
    max_output_tokens=3000,
    request_timeout=30.0,
)


def test_no_api_key_gives_config_guidance(tmp_path):
    wav = _wav(tmp_path / "a.wav")
    with pytest.raises(fr_audio.AudioUnsupportedError, match="api_key"):
        asyncio.run(fr_audio.read_audio_to_text(str(wav), "a.wav", **{**_KW, "api_key": ""}))


def test_read_audio_text_header_and_body(tmp_path, monkeypatch):
    wav = _wav(tmp_path / "meeting.wav", seconds=2.0)
    captured = {}

    async def fake_omni(audio_bytes, omni_format, **kwargs):
        captured["bytes"] = audio_bytes
        captured["format"] = omni_format
        captured["kwargs"] = kwargs
        return "内容类型：语音\n讨论了发布计划。"

    monkeypatch.setattr(fr_audio, "_ask_omni", fake_omni)
    text = asyncio.run(fr_audio.read_audio_to_text(str(wav), "meeting.wav", **_KW))
    assert text.startswith("【音频理解】meeting.wav")
    assert "时长：2 秒" in text
    assert "qwen3.8-omni-flash" in text
    assert "讨论了发布计划" in text
    assert captured["format"] == "wav"
    assert captured["bytes"] == wav.read_bytes()
    assert captured["kwargs"]["model"] == "qwen3.8-omni-flash"


def test_read_audio_empty_model_output_raises(tmp_path, monkeypatch):
    wav = _wav(tmp_path / "a.wav")

    async def fake_omni(audio_bytes, omni_format, **kwargs):
        # 真实 _ask_omni 返回前已 strip；空输出的契约是空串
        return ""

    monkeypatch.setattr(fr_audio, "_ask_omni", fake_omni)
    with pytest.raises(RuntimeError, match="空内容"):
        asyncio.run(fr_audio.read_audio_to_text(str(wav), "a.wav", **_KW))


# ─── _ask_omni 请求形态与 SSE 解析（桩 httpx） ────────────────────


class _FakeResp:
    def __init__(self, status, lines, body=b""):
        self.status_code = status
        self._lines = lines
        self._body = body

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _install_fake_httpx(monkeypatch, resp, capture):
    import sys
    import types

    mod = types.ModuleType("httpx")

    class _Client:
        def __init__(self, **kwargs):
            capture["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, **kwargs):
            capture["method"] = method
            capture["url"] = url
            capture["payload"] = kwargs.get("json")
            capture["headers"] = kwargs.get("headers")
            return _FakeStreamCtx(resp)

    mod.AsyncClient = _Client
    monkeypatch.setitem(sys.modules, "httpx", mod)


def _sse_lines(*texts):
    lines = []
    for t in texts:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": t}}]}))
    lines.append("data: [DONE]")
    return lines


def test_ask_omni_payload_shape(monkeypatch):
    capture = {}
    _install_fake_httpx(monkeypatch, _FakeResp(200, _sse_lines("你好", "世界")), capture)
    out = asyncio.run(
        fr_audio._ask_omni(
            b"AUDIO", "mp3",
            base_url="https://example.invalid/v1/",
            api_key="sk-test",
            model="qwen3.8-omni-flash",
            max_output_tokens=3000,
            request_timeout=30.0,
            user_text="理解这段音频",
        )
    )
    assert out == "你好世界"
    assert capture["method"] == "POST"
    assert capture["url"] == "https://example.invalid/v1/chat/completions"
    payload = capture["payload"]
    assert payload["stream"] is True
    assert payload["modalities"] == ["text"]
    assert payload["model"] == "qwen3.8-omni-flash"
    assert payload["max_tokens"] == 3000
    # v1.9.0：qwen3.8 思考默认开启会挤占输出预算，必须显式关思考；
    # 官方 OpenAI 兼容文档：raw HTTP 放 body 顶层（不是 extra_body 嵌套）
    assert payload["enable_thinking"] is False
    content = payload["messages"][1]["content"]
    audio_part = content[0]
    assert audio_part["type"] == "input_audio"
    assert audio_part["input_audio"]["format"] == "mp3"
    assert audio_part["input_audio"]["data"] == "data:;base64," + base64.b64encode(b"AUDIO").decode()
    assert content[1]["text"] == "理解这段音频"
    assert capture["headers"]["Authorization"] == "Bearer sk-test"
    # 系统提示的输出纪律必须在代码层（不可被存量配置改掉）
    assert "不要编造" in payload["messages"][0]["content"]


def test_ask_omni_skips_heartbeat_and_bad_json(monkeypatch):
    lines = [
        ": ping",
        "data: {broken json",
        *_sse_lines("片段"),
    ]
    _install_fake_httpx(monkeypatch, _FakeResp(200, lines), {})
    out = asyncio.run(
        fr_audio._ask_omni(
            b"A", "wav", base_url="https://x/v1", api_key="k", model="m",
            max_output_tokens=100, request_timeout=5.0, user_text="t",
        )
    )
    assert out == "片段"


def test_ask_omni_http_error_carries_body(monkeypatch):
    _install_fake_httpx(monkeypatch, _FakeResp(401, [], body=b'{"error":"invalid key"}'), {})
    with pytest.raises(RuntimeError, match="HTTP 401.*invalid key"):
        asyncio.run(
            fr_audio._ask_omni(
                b"A", "wav", base_url="https://x/v1", api_key="k", model="m",
                max_output_tokens=100, request_timeout=5.0, user_text="t",
            )
        )


# ─── v1.5.1：Planner 公告的音频语义 ──────────────────────────────
#
# 真机 2026-11-16 22:01（LongCat-2.5 planner）：公告只说「已读入 mp3（817 字）」，
# planner reasoning 反复纠结「mp3 哪来文本 / 我没有耳朵」，首轮回复只围绕文件名，
# 靠自发调 search_file 拿到赏析正文后才答到点上。公告必须明说「已经被听过」。


def _new_plugin():
    from fakehost import bind_context, build_context, get_default_config, load_plugin_module

    plugin_dir = Path(__file__).resolve().parent.parent
    mod = load_plugin_module(plugin_dir, "file_reader_audio_notice_under_test")
    plugin = mod.create_plugin()
    bind_context(plugin, build_context("org.mai-mai.file-reader"), get_default_config(mod.FileReaderConfig))
    asyncio.run(plugin.on_load())
    return plugin, mod


def _notice_text(result) -> str:
    items = result.get("modified_kwargs", {}).get("items") or []
    assert items, f"应注入一条公告: {result}"
    return str(items[-1]["parts"][0]["text"])


async def _embed_stub(texts):
    return [[float(len(t) % 7), 1.0] for t in texts]


def test_planner_notice_audio_says_listened():
    plugin, _ = _new_plugin()
    try:
        async def _drive():
            plugin._embed = _embed_stub
            vs = plugin._get_vs("sA", "sA")
            await vs.add_file(
                "歌.mp3",
                "【音频理解】歌.mp3\n格式：mp3｜时长：3 分 1 秒\n\n内容类型：音乐\n赏析笔记：钢琴开头。",
                is_audio=True,
            )
            res = await plugin.announce_file_context(session_id="sA", items=[])
            text = _notice_text(res)
            assert "已由音频理解模型完整听过" in text, text
            assert "不要声称自己听不了音频" in text, text
            assert "《歌.mp3》" in text
            assert "全文会在生成回复时自动附上" in text, "小音频应走全文直注供给"
            assert "已读入文件" not in text, "纯音频会话不应再用文档口径公告"

        asyncio.run(_drive())
    finally:
        asyncio.run(plugin.on_unload())


def test_planner_notice_doc_wording_unchanged():
    """文档路径公告保持原口径（v1.3.2 起的既有契约，测试 test_file_reader 也依赖）。"""
    plugin, _ = _new_plugin()
    try:
        async def _drive():
            plugin._embed = _embed_stub
            vs = plugin._get_vs("sD", "sD")
            await vs.add_file("笔记.txt", "苹果富含维生素C。" * 5)
            res = await plugin.announce_file_context(session_id="sD", items=[])
            text = _notice_text(res)
            assert "本会话已读入文件" in text and "《笔记.txt》" in text
            assert "音频理解模型" not in text

        asyncio.run(_drive())
    finally:
        asyncio.run(plugin.on_unload())


def test_planner_notice_mixed_audio_and_doc():
    """混合会话：文档与音频各一段公告，字数分开统计。"""
    plugin, _ = _new_plugin()
    try:
        async def _drive():
            plugin._embed = _embed_stub
            vs = plugin._get_vs("sM", "sM")
            await vs.add_file("笔记.txt", "文档内容。" * 10)
            await vs.add_file("歌.mp3", "【音频理解】歌.mp3\n赏析。", is_audio=True)
            res = await plugin.announce_file_context(session_id="sM", items=[])
            text = _notice_text(res)
            assert "本会话已读入文件：《笔记.txt》" in text, text
            assert "本会话的音频文件：《歌.mp3》" in text, text
            assert "完整听过" in text

        asyncio.run(_drive())
    finally:
        asyncio.run(plugin.on_unload())


def test_handle_file_marks_audio_entry(tmp_path, monkeypatch):
    """端到端：_handle_file 走音频通道后，入库条目必须带 is_audio 标记。"""
    plugin, plugin_mod = _new_plugin()
    wav = _wav(tmp_path / "song.wav", seconds=1.0)

    async def fake_read(path, name, **kwargs):
        return f"【音频理解】{name}\n格式：wav｜时长：1 秒\n\n内容类型：音乐\n赏析。"

    monkeypatch.setattr(plugin_mod.fr_audio, "read_audio_to_text", fake_read)
    plugin._embed = _embed_stub
    try:
        asyncio.run(
            plugin._handle_file(
                session_id="sH",
                conversation_id="sH",
                stream_id="sH",
                fdata={"name": "song.wav", "bytes": wav.read_bytes()},
            )
        )
        vs = next(v for (sid, _c), v in plugin._store._sessions.items() if sid == "sH")
        entry = vs.files["song.wav"]
        assert entry.is_audio is True, "音频通道入库必须打 is_audio 标记"
        assert entry.full_text().startswith("【音频理解】")
    finally:
        asyncio.run(plugin.on_unload())


def test_add_file_default_not_audio():
    """默认路径（文档）不受影响：is_audio 缺省 False。"""
    from vector_store import VectorStore

    async def _drive():
        vs = VectorStore(_embed_stub)
        entry = await vs.add_file("a.txt", "普通文档内容")
        assert entry.is_audio is False

    asyncio.run(_drive())


# ─── v1.8.2：日志回调契约（与 fr_video 同款修复） ────────────────


def test_normalize_log_strips_level_for_logger_info():
    received = []

    def logger_info(msg, *args):
        received.append((msg, args))

    emit = fr_audio._normalize_log(logger_info)
    emit("info", "音频预检通过 %s：format=%s", "a.wav", "wav")
    assert received == [("音频预检通过 %s：format=%s", ("a.wav", "wav"))]


def test_read_audio_with_real_logger_no_logging_error(tmp_path, monkeypatch, caplog):
    """真机口径：log=logger.info 不得产生 Logging error（fr_video 已复现同病）。"""
    import logging

    wav = _wav(tmp_path / "log.wav", seconds=1.0)

    async def fake_omni(audio_bytes, omni_format, **kwargs):
        return "内容类型：语音\n转写。"

    monkeypatch.setattr(fr_audio, "_ask_omni", fake_omni)
    logger = logging.getLogger("fr_test.audio_logger_contract")
    with caplog.at_level(logging.INFO, logger="fr_test.audio_logger_contract"):
        text = asyncio.run(fr_audio.read_audio_to_text(
            str(wav), "log.wav", log=logger.info, **_KW,
        ))
    assert text.startswith("【音频理解】")
    assert "音频预检通过 log.wav" in caplog.text
    assert "Logging error" not in caplog.text
    assert "not all arguments converted" not in caplog.text


# ─── v1.9.0：默认模型升级 qwen3.8-omni-flash + 闸门放宽 ───────────


def test_audio_config_defaults_v190():
    """默认值契约：模型/时长/超时升级，大小闸门不随模型放宽（base64 通用硬上限）。"""
    plugin, _ = _new_plugin()
    try:
        a = plugin.config.audio
        assert a.model == "qwen3.8-omni-flash"
        assert a.max_duration_seconds == 3600
        assert a.max_file_mb == 7.0, "base64 10MB 是传输通道通用约束，不随模型升级放宽"
        assert a.max_output_tokens == 3000
        assert a.request_timeout == 300.0
    finally:
        asyncio.run(plugin.on_unload())


def test_precheck_duration_gate_uses_config_value(tmp_path, monkeypatch):
    """时长闸门按配置值判定：30 分钟音频在新默认 3600 下放行、旧默认 1200 下拒绝。

    时长探测桩成 1800s（探测本身另有真文件用例）；大小闸门设 0 跳过，
    避免合成大 wav 触发 base64 硬上限干扰本用例焦点。
    """
    wav = _wav(tmp_path / "long.wav", seconds=1.0)
    monkeypatch.setattr(fr_audio, "probe_audio_duration", lambda path: 1800.0)
    fmt, duration, _ = fr_audio.precheck_audio(
        str(wav), "long.wav", max_duration_seconds=3600, max_file_mb=0,
    )
    assert fmt == "wav" and duration == 1800.0
    with pytest.raises(fr_audio.AudioUnsupportedError, match="超过上限"):
        fr_audio.precheck_audio(str(wav), "long.wav", max_duration_seconds=1200, max_file_mb=0)
