# -*- coding: utf-8 -*-
"""v1.8.0 视频理解通道测试。

路线（2026-11-18 方案评审定案 D1~D8）：豆包 seed-2.1-lite + Files API，
一次调用音画双收 → 带【视频理解】头部文本回流 add_file。

覆盖面：
1. 扩展名/嗅探判定（mp4 家族 ftyp 归视频、M4A 品牌归音频——分派边界）；
2. 预检 fail-closed：超大小/超时长/时长读不出/空文件 → 全部拒绝，绝不截断；
3. 时长探测：MP4 mvhd（v0/v1）、AVI avih、损坏文件返回 None；
4. 未配 Key → 配置指引（VideoUnsupportedError，value_error 链路）；
5. Files API 上传契约：multipart purpose/preprocess_configs、流式文件对象（非 base64）；
6. 理解调用契约：video_url.file_id 引用 + system 输出纪律 + max_tokens；
7. 产出文本【视频理解】头部 + 入库 is_video 标记 + planner 公告「已看过」语义；
8. 分派优先级：先视频后音频（改名 mp4 不被误喂 Omni）；
9. 日预算：超限只登记不解析、跨天滚动清零、0=不限；
10. 超限/超预算拒绝 → 模型通知（不发群），与音频同链路；
11. 重试包携带 is_video，重试不重新上传/理解；
12. 同步解析入口对视频给准确原因；describe 口径含视频。

全程离线：httpx 网络层用桩替换，不发真实请求。
"""

import asyncio
import struct
import sys
import types
from pathlib import Path

import pytest

import fr_video
from file_parser import (
    RECOGNIZED_UNSUPPORTED,
    VIDEO_ASYNC_EXTENSIONS,
    describe_supported_types,
    read_any_file_to_text,
)

PLUGIN_DIR = Path(__file__).resolve().parent.parent


# ─── 合成视频文件工具 ────────────────────────────────────────────


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def _mp4(path: Path, seconds: float = 5.0, timescale: int = 1000, version: int = 0) -> Path:
    """合成最小可探测 MP4：ftyp + moov(mvhd)。"""
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    if version == 0:
        mvhd_body = bytes(4) + bytes(8) + struct.pack(">I", timescale) + struct.pack(">I", int(seconds * timescale)) + bytes(80)
    else:
        # version=1（首字节必须是 1，不能 bytes(4) 全零——那是 v0 布局）
        mvhd_body = b"\x01\x00\x00\x00" + bytes(16) + struct.pack(">I", timescale) + struct.pack(">Q", int(seconds * timescale)) + bytes(80)
    mvhd = _box(b"mvhd", mvhd_body)
    moov = _box(b"moov", mvhd)
    mdat = _box(b"mdat", b"\x00" * 64)
    path.write_bytes(ftyp + moov + mdat)
    return path


def _avi(path: Path, seconds: float = 3.0) -> Path:
    us_per_frame = 40000  # 25fps
    total_frames = int(seconds * 25)
    # AVI avih：us@+0 total_frames@+12（中间是 flags/width/height 三个 u32）
    avih_data = struct.pack("<I", us_per_frame) + bytes(12) + struct.pack("<I", total_frames) + bytes(28)
    avih = b"avih" + struct.pack("<I", len(avih_data)) + avih_data
    hdrl = b"hdrl" + avih
    riff = b"RIFF" + struct.pack("<I", 4 + len(hdrl)) + b"AVI " + hdrl
    path.write_bytes(riff + b"\x00" * 64)
    return path


_KW = dict(
    base_url="https://ark.cn-beijing.volces.com/api/v3",
    api_key="test-key",
    model="doubao-seed-2-1-lite-260915",
    max_duration_seconds=600,
    max_file_mb=20.0,
    max_output_tokens=4000,
    fps=1.0,
    request_timeout=300.0,
)


def _new_plugin(tag="v180"):
    from fakehost import bind_context, build_context, get_default_config, load_plugin_module

    mod = load_plugin_module(PLUGIN_DIR, f"file_reader_{tag}_under_test")
    plugin = mod.create_plugin()
    bind_context(plugin, build_context("org.mai-mai.file-reader"), get_default_config(mod.FileReaderConfig))
    asyncio.run(plugin.on_load())
    return plugin, mod


async def _embed_stub(texts):
    return [[float(len(t) % 7 + 1), 0.5, 0.25] for t in texts]


# ── 1. 判定与分派边界 ─────────────────────────────────────────


def test_video_extensions():
    for name in ("a.mp4", "b.MOV", "c.m4v", "d.avi", "e.mkv", "f.webm"):
        assert fr_video.is_video_file(name), name
    for name in ("song.mp3", "v.m4a", "x.flv", "doc.pdf", "noext"):
        assert not fr_video.is_video_file(name), name


def test_sniff_video_format():
    assert fr_video.sniff_video_format(b"\x00\x00\x00\x20ftypisom\x00\x00") == "mp4"
    assert fr_video.sniff_video_format(b"\x00\x00\x00\x20ftypmp42\x00\x00") == "mp4"
    assert fr_video.sniff_video_format(b"\x00\x00\x00\x20ftypqt  \x00\x00") == "mov"
    assert fr_video.sniff_video_format(b"RIFF\x00\x00\x00\x00AVI LIST") == "avi"
    assert fr_video.sniff_video_format(b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1f") == "mkv"
    # M4A/M4B 是纯音频：视频嗅探必须让位（fr_audio 会认成 m4a）
    assert fr_video.sniff_video_format(b"\x00\x00\x00\x20ftypM4A \x00\x00") == ""
    assert fr_video.sniff_video_format(b"\x00\x00\x00\x20ftypM4B \x00\x00") == ""
    assert fr_video.sniff_video_format(b"%PDF-1.4\n\x00\x00\x00") == ""
    assert fr_video.sniff_video_format(b"short") == ""


def test_video_rejected_formats_single_source():
    for ext in ("flv", "wmv", "rmvb", "rm", "3gp"):
        assert ext in RECOGNIZED_UNSUPPORTED
        assert RECOGNIZED_UNSUPPORTED[ext] == fr_video.VIDEO_RECOGNIZED_UNSUPPORTED[ext]
    # ts 与 TypeScript 冲突：不进同步拒绝表（同步入口 ts 仍按文本解析）
    assert "ts" not in RECOGNIZED_UNSUPPORTED


def test_sync_entry_rejects_video_with_accurate_reason(tmp_path):
    mp4 = _mp4(tmp_path / "a.mp4")
    with pytest.raises(ValueError, match="视频"):
        read_any_file_to_text(str(mp4), "a.mp4")
    flv = tmp_path / "v.flv"
    flv.write_bytes(b"FLV\x01\x05" + b"\x00" * 32)
    with pytest.raises(ValueError, match="FLV"):
        read_any_file_to_text(str(flv), "v.flv")


def test_describe_mentions_video():
    assert "视频" in describe_supported_types()
    assert "mp4" in describe_supported_types()
    assert VIDEO_ASYNC_EXTENSIONS == frozenset({"mp4", "mov", "m4v", "avi", "mkv", "webm"})


# ── 2. 时长探测 ───────────────────────────────────────────────


def test_probe_mp4_v0(tmp_path):
    p = _mp4(tmp_path / "a.mp4", seconds=12.5)
    assert fr_video.probe_video_duration(str(p)) == pytest.approx(12.5, abs=0.01)


def test_probe_mp4_v1(tmp_path):
    p = _mp4(tmp_path / "a.mp4", seconds=90.0, version=1)
    assert fr_video.probe_video_duration(str(p)) == pytest.approx(90.0, abs=0.01)


def test_probe_avi(tmp_path):
    p = _avi(tmp_path / "a.avi", seconds=3.0)
    assert fr_video.probe_video_duration(str(p)) == pytest.approx(3.0, abs=0.05)


def test_probe_corrupt_returns_none(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00" * 1024)
    assert fr_video.probe_video_duration(str(bad)) is None
    mkv = tmp_path / "v.mkv"
    mkv.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 64)
    assert fr_video.probe_video_duration(str(mkv)) is None  # EBML 不解析，fail-closed


def test_probe_no_extension_mp4_content(tmp_path):
    p = _mp4(tmp_path / "noext", seconds=7.0)
    assert fr_video.probe_video_duration(str(p)) == pytest.approx(7.0, abs=0.01)


# ── 3. 预检 fail-closed ───────────────────────────────────────


def test_precheck_ok(tmp_path):
    p = _mp4(tmp_path / "a.mp4", seconds=5.0)
    duration, size = fr_video.precheck_video(str(p), "a.mp4", max_duration_seconds=600, max_file_mb=20.0)
    assert duration == pytest.approx(5.0, abs=0.01)
    assert size == p.stat().st_size


def test_precheck_rejects_oversize(tmp_path):
    p = _mp4(tmp_path / "a.mp4")
    with pytest.raises(fr_video.VideoUnsupportedError, match="超过上限"):
        fr_video.precheck_video(str(p), "a.mp4", max_duration_seconds=600, max_file_mb=0.0001)


def test_precheck_rejects_over_duration(tmp_path):
    p = _mp4(tmp_path / "a.mp4", seconds=120.0)
    with pytest.raises(fr_video.VideoUnsupportedError, match="时长"):
        fr_video.precheck_video(str(p), "a.mp4", max_duration_seconds=60, max_file_mb=20.0)


def test_precheck_rejects_unknown_duration(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00" * 1024)
    with pytest.raises(fr_video.VideoUnsupportedError, match="时长"):
        fr_video.precheck_video(str(bad), "bad.mp4", max_duration_seconds=600, max_file_mb=20.0)


def test_precheck_rejects_empty_file(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(fr_video.VideoUnsupportedError, match="空文件"):
        fr_video.precheck_video(str(empty), "empty.mp4", max_duration_seconds=600, max_file_mb=20.0)


def test_precheck_rejects_unsupported_ext(tmp_path):
    p = tmp_path / "v.flv"
    p.write_bytes(b"FLV" + b"\x00" * 32)
    with pytest.raises(fr_video.VideoUnsupportedError, match="FLV"):
        fr_video.precheck_video(str(p), "v.flv", max_duration_seconds=600, max_file_mb=20.0)


def test_precheck_files_api_hard_limit(tmp_path, monkeypatch):
    p = _mp4(tmp_path / "a.mp4")
    monkeypatch.setattr(fr_video, "FILES_API_HARD_LIMIT_BYTES", 10)
    with pytest.raises(fr_video.VideoUnsupportedError, match="512MB"):
        fr_video.precheck_video(str(p), "a.mp4", max_duration_seconds=600, max_file_mb=0)


# ── 4. read_video_to_text：Key 指引 / 桩 / 头部 ─────────────────


def test_missing_key_gives_config_guidance(tmp_path):
    p = _mp4(tmp_path / "a.mp4")
    with pytest.raises(fr_video.VideoUnsupportedError, match="api_key"):
        asyncio.run(fr_video.read_video_to_text(str(p), "a.mp4", **{**_KW, "api_key": ""}))


def test_read_video_text_header_and_body(tmp_path, monkeypatch):
    p = _mp4(tmp_path / "clip.mp4", seconds=8.0)

    async def fake_upload(path, file_name, **kw):
        assert Path(path).exists()
        return "file-test-123"

    async def fake_ask(file_id, **kw):
        assert file_id == "file-test-123"
        return "内容类型：演示\n[00:01] 画面里一只狸猫在敲键盘。音轨：「今天写插件」。"

    monkeypatch.setattr(fr_video, "_upload_to_files_api", fake_upload)
    monkeypatch.setattr(fr_video, "_ask_video_model", fake_ask)
    text = asyncio.run(fr_video.read_video_to_text(str(p), "clip.mp4", **_KW))
    assert text.startswith("【视频理解】clip.mp4")
    assert "8 秒" in text
    assert "doubao-seed-2-1-lite-260915" in text
    assert "画面描述 + 音轨转写" in text
    assert "狸猫" in text


def test_read_video_empty_model_output_raises(tmp_path, monkeypatch):
    p = _mp4(tmp_path / "a.mp4")

    async def fake_upload(path, file_name, **kw):
        return "file-x"

    async def fake_ask(file_id, **kw):
        return "   "

    monkeypatch.setattr(fr_video, "_upload_to_files_api", fake_upload)
    monkeypatch.setattr(fr_video, "_ask_video_model", fake_ask)
    with pytest.raises(RuntimeError, match="空内容"):
        asyncio.run(fr_video.read_video_to_text(str(p), "a.mp4", **_KW))


# ── 5/6. httpx 桩：上传与理解请求契约 ─────────────────────────


class _FakeResp:
    def __init__(self, status, payload=None, body=b""):
        self.status_code = status
        self._payload = payload
        self.text = body.decode("utf-8", errors="replace") if body else (
            __import__("json").dumps(payload or {})
        )

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _install_fake_httpx(monkeypatch, capture, upload_resp, chat_resp):
    """替换 sys.modules['httpx']：AsyncClient.post 按 URL 分流记录请求。"""
    mod = types.ModuleType("httpx")

    class _AsyncClient:
        def __init__(self, timeout=None):
            capture["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, data=None, files=None, json=None):
            capture.setdefault("requests", []).append(
                {"url": url, "headers": headers, "data": data, "files": files, "json": json}
            )
            if url.endswith("/files"):
                # 验证流式：files 里的文件对象必须是可读文件句柄，不是 bytes/str
                if files is not None:
                    fobj = files["file"][1]
                    capture["upload_file_obj"] = fobj
                    assert hasattr(fobj, "read"), "上传必须传文件对象（流式），不得 base64 整读"
                return upload_resp
            return chat_resp

    mod.AsyncClient = _AsyncClient
    monkeypatch.setitem(sys.modules, "httpx", mod)


def test_upload_contract_multipart_and_preprocess(tmp_path, monkeypatch):
    p = _mp4(tmp_path / "a.mp4")
    capture = {}
    _install_fake_httpx(
        monkeypatch, capture,
        _FakeResp(200, {"id": "file-abc", "bytes": 123}),
        _FakeResp(200, {"choices": [{"message": {"content": "ok"}}]}),
    )
    file_id = asyncio.run(fr_video._upload_to_files_api(
        str(p), "a.mp4", base_url=_KW["base_url"], api_key="k", model=_KW["model"],
        fps=1.0, request_timeout=300.0,
    ))
    assert file_id == "file-abc"
    req = capture["requests"][0]
    assert req["url"] == "https://ark.cn-beijing.volces.com/api/v3/files"
    assert req["headers"]["Authorization"] == "Bearer k"
    assert req["data"]["purpose"] == "user_data"
    assert req["data"]["preprocess_configs[video][model]"] == _KW["model"]
    assert req["data"]["preprocess_configs[video][fps]"] == "1.0"
    assert req["files"]["file"][0] == "a.mp4"
    assert req["json"] is None, "上传是 multipart，不是 JSON body"


def test_upload_http_error_carries_body(tmp_path, monkeypatch):
    p = _mp4(tmp_path / "a.mp4")
    _install_fake_httpx(
        monkeypatch, {},
        _FakeResp(401, body=b'{"error":"invalid key"}'),
        _FakeResp(200, {"choices": []}),
    )
    with pytest.raises(RuntimeError, match="401"):
        asyncio.run(fr_video._upload_to_files_api(
            str(p), "a.mp4", base_url=_KW["base_url"], api_key="bad", model=_KW["model"],
            fps=1.0, request_timeout=300.0,
        ))


def test_upload_missing_id_raises(tmp_path, monkeypatch):
    p = _mp4(tmp_path / "a.mp4")
    _install_fake_httpx(monkeypatch, {}, _FakeResp(200, {"object": "file"}), _FakeResp(200, {}))
    with pytest.raises(RuntimeError, match="file_id"):
        asyncio.run(fr_video._upload_to_files_api(
            str(p), "a.mp4", base_url=_KW["base_url"], api_key="k", model=_KW["model"],
            fps=1.0, request_timeout=300.0,
        ))


def test_chat_contract_video_url_file_id(monkeypatch):
    capture = {}
    _install_fake_httpx(
        monkeypatch, capture,
        _FakeResp(200, {"id": "file-x"}),
        _FakeResp(200, {"choices": [{"message": {"content": "内容类型：其他\n描述"}}]}),
    )
    text = asyncio.run(fr_video._ask_video_model(
        "file-x", base_url=_KW["base_url"], api_key="k", model=_KW["model"],
        max_output_tokens=4000, request_timeout=300.0, user_text="请理解",
    ))
    assert text == "内容类型：其他\n描述"
    req = capture["requests"][0]
    assert req["url"].endswith("/chat/completions")
    payload = req["json"]
    assert payload["model"] == _KW["model"]
    assert payload["max_tokens"] == 4000
    assert payload["messages"][0]["role"] == "system"
    assert "不要编造" in payload["messages"][0]["content"]
    content = payload["messages"][1]["content"]
    assert content[0] == {"type": "video_url", "video_url": {"file_id": "file-x"}}
    assert content[1]["type"] == "text"
    # 不得出现 base64 data URI（Files API 路线的核心约束）
    assert "base64" not in __import__("json").dumps(payload)


def test_chat_content_list_segments_joined(monkeypatch):
    _install_fake_httpx(
        monkeypatch, {},
        _FakeResp(200, {"id": "f"}),
        _FakeResp(200, {"choices": [{"message": {"content": [{"text": "甲"}, {"text": "乙"}]}}]}),
    )
    text = asyncio.run(fr_video._ask_video_model(
        "f", base_url=_KW["base_url"], api_key="k", model=_KW["model"],
        max_output_tokens=100, request_timeout=10.0, user_text="x",
    ))
    assert text == "甲乙"


def test_chat_http_error_and_empty_choices(monkeypatch):
    _install_fake_httpx(
        monkeypatch, {},
        _FakeResp(200, {"id": "f"}),
        _FakeResp(500, body=b"upstream error"),
    )
    with pytest.raises(RuntimeError, match="500"):
        asyncio.run(fr_video._ask_video_model(
            "f", base_url=_KW["base_url"], api_key="k", model=_KW["model"],
            max_output_tokens=100, request_timeout=10.0, user_text="x",
        ))
    _install_fake_httpx(
        monkeypatch, {},
        _FakeResp(200, {"id": "f"}),
        _FakeResp(200, {"choices": []}),
    )
    with pytest.raises(RuntimeError, match="choices"):
        asyncio.run(fr_video._ask_video_model(
            "f", base_url=_KW["base_url"], api_key="k", model=_KW["model"],
            max_output_tokens=100, request_timeout=10.0, user_text="x",
        ))


# ── 7. 入库标记 + planner 公告 ────────────────────────────────


def test_add_file_marks_is_video_and_announces():
    plugin, _ = _new_plugin("vidmark")
    try:
        plugin._embed = _embed_stub

        async def _run():
            vs = plugin._store.get_or_create("sV", "sV", plugin._embed, 1024, 128, 6)
            entry = await vs.add_file(
                "clip.mp4", "【视频理解】clip.mp4\n画面里一只狸猫。", is_video=True,
            )
            assert entry.is_video is True
            assert entry.is_audio is False
            res = await plugin.announce_file_context(session_id="sV", items=[])
            items = res.get("modified_kwargs", {}).get("items") or []
            assert items, f"应注入视频公告: {res}"
            text = str(items[-1]["parts"][0]["text"])
            assert "视频文件" in text and "完整看过" in text and "画面+音轨" in text
            assert "看不了视频" in text  # 明确禁止 planner 自我否定
            assert "已读入文件" not in text, "视频不应混进文档公告"

        asyncio.run(_run())
    finally:
        asyncio.run(plugin.on_unload())


def test_audio_and_video_announced_separately():
    plugin, _ = _new_plugin("vidmix")
    try:
        plugin._embed = _embed_stub

        async def _run():
            vs = plugin._store.get_or_create("sM", "sM", plugin._embed, 1024, 128, 6)
            await vs.add_file("song.mp3", "【音频理解】song.mp3\n赏析。", is_audio=True)
            await vs.add_file("clip.mp4", "【视频理解】clip.mp4\n描述。", is_video=True)
            await vs.add_file("note.txt", "普通文档内容。")
            res = await plugin.announce_file_context(session_id="sM", items=[])
            text = str(res["modified_kwargs"]["items"][-1]["parts"][0]["text"])
            assert "音频文件" in text and "视频文件" in text and "已读入文件" in text
            assert text.index("已读入文件") < text.index("音频文件") < text.index("视频文件")

        asyncio.run(_run())
    finally:
        asyncio.run(plugin.on_unload())


# ── 8. 分派优先级：先视频后音频 ───────────────────────────────


def test_dispatch_mp4_goes_video_not_audio(tmp_path, monkeypatch):
    """改名/无扩展名的 mp4：fr_audio 嗅探会认成 m4a，必须先被视频拦截。"""
    plugin, mod = _new_plugin("viddisp")
    p = _mp4(tmp_path / "clip.mp4", seconds=4.0)
    seen = {}

    async def fake_video(path, name, **kw):
        seen["video"] = name
        return "【视频理解】" + name + "\n描述"

    async def fake_audio(path, name, **kw):
        seen["audio"] = name
        return "【音频理解】" + name

    monkeypatch.setattr(mod.fr_video, "read_video_to_text", fake_video)
    monkeypatch.setattr(mod.fr_audio, "read_audio_to_text", fake_audio)
    try:
        text = asyncio.run(plugin._parse_to_text(str(p), "clip.mp4", []))
        assert "video" in seen and "audio" not in seen
        assert text.startswith("【视频理解】")
    finally:
        asyncio.run(plugin.on_unload())


def test_dispatch_no_extension_sniffs_video_first(tmp_path, monkeypatch):
    plugin, mod = _new_plugin("vidnoext")
    p = _mp4(tmp_path / "noext", seconds=4.0)
    seen = {}

    async def fake_video(path, name, **kw):
        seen["video"] = name
        return "【视频理解】" + name

    async def fake_audio(path, name, **kw):
        seen["audio"] = name
        return "【音频理解】" + name

    monkeypatch.setattr(mod.fr_video, "read_video_to_text", fake_video)
    monkeypatch.setattr(mod.fr_audio, "read_audio_to_text", fake_audio)
    try:
        asyncio.run(plugin._parse_to_text(str(p), "附件", []))
        assert seen.get("video") == "附件.mp4"
        assert "audio" not in seen
    finally:
        asyncio.run(plugin.on_unload())


def test_dispatch_m4a_still_audio(tmp_path, monkeypatch):
    """M4A 品牌纯音频不受影响：视频嗅探让位，仍走 fr_audio。"""
    import wave

    plugin, mod = _new_plugin("vidm4a")
    wav = tmp_path / "song.m4a"
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x00\x00" * 8000)
    seen = {}

    async def fake_video(path, name, **kw):
        seen["video"] = name
        return "【视频理解】" + name

    async def fake_audio(path, name, **kw):
        seen["audio"] = name
        return "【音频理解】" + name

    monkeypatch.setattr(mod.fr_video, "read_video_to_text", fake_video)
    monkeypatch.setattr(mod.fr_audio, "read_audio_to_text", fake_audio)
    try:
        asyncio.run(plugin._parse_to_text(str(wav), "song.m4a", []))
        assert "audio" in seen and "video" not in seen
    finally:
        asyncio.run(plugin.on_unload())


def test_dispatch_video_disabled_gives_reason(tmp_path):
    plugin, _ = _new_plugin("vidoff")
    p = _mp4(tmp_path / "a.mp4")
    try:
        plugin.config.video.enabled = False
        with pytest.raises(fr_video.VideoUnsupportedError, match="video.enabled"):
            asyncio.run(plugin._parse_to_text(str(p), "a.mp4", []))
    finally:
        asyncio.run(plugin.on_unload())


# ── 9. 日预算 ─────────────────────────────────────────────────


def test_daily_budget_take_and_rollover():
    plugin, _ = _new_plugin("vidbudget")
    try:
        plugin.config.video.daily_budget = 2
        assert plugin._video_budget_take() is True
        assert plugin._video_budget_take() is True
        assert plugin._video_budget_take() is False, "第 3 个应被拒"
        # 跨天滚动清零
        plugin._video_budget["day"] = "2000-01-01"
        assert plugin._video_budget_take() is True
        assert plugin._video_budget["used"] == 1
    finally:
        asyncio.run(plugin.on_unload())


def test_daily_budget_zero_means_unlimited():
    plugin, _ = _new_plugin("vidnolimit")
    try:
        plugin.config.video.daily_budget = 0
        for _ in range(50):
            assert plugin._video_budget_take() is True
    finally:
        asyncio.run(plugin.on_unload())


def test_budget_exhausted_rejects_before_probe(tmp_path, monkeypatch):
    """预算用完：连时长探测/上传都不做，直接确定性拒绝。"""
    plugin, mod = _new_plugin("videxhaust")
    p = _mp4(tmp_path / "a.mp4")
    called = []

    def spy_probe(path):
        called.append(path)
        return 5.0

    monkeypatch.setattr(mod.fr_video, "probe_video_duration", spy_probe)
    try:
        plugin.config.video.daily_budget = 1
        plugin.config.video.api_key = "k"
        assert plugin._video_budget_take() is True  # 用掉唯一名额
        with pytest.raises(fr_video.VideoUnsupportedError, match="名额已用完"):
            asyncio.run(plugin._parse_to_text(str(p), "a.mp4", []))
        assert called == [], "超预算不得再做时长探测"
    finally:
        asyncio.run(plugin.on_unload())


# ── 10. 端到端：_handle_file → 通知/入库 ──────────────────────


def _fdata_bytes(name: str, body: bytes) -> dict:
    import base64

    return {"name": name, "bytes": body, "size_hint": len(body)}


def test_handle_video_end_to_end_ingest(tmp_path, monkeypatch):
    plugin, mod = _new_plugin("vide2e")
    p = _mp4(tmp_path / "ok.mp4", seconds=6.0)
    body = p.read_bytes()
    plugin._embed = _embed_stub
    plugin.config.video.api_key = "k"

    async def fake_video(path, name, **kw):
        return f"【视频理解】{name}\n时长：6 秒\n内容类型：演示\n狸猫敲键盘。"

    monkeypatch.setattr(mod.fr_video, "read_video_to_text", fake_video)
    try:
        asyncio.run(plugin._handle_file("sE", "sE", "streamE", _fdata_bytes("ok.mp4", body)))
        vs = plugin._store.get("sE", "sE")
        assert vs is not None and "ok.mp4" in vs.files
        assert vs.files["ok.mp4"].is_video is True
        # silent_errors/silent_success 默认 true：不发群
        assert plugin._file_notices.get("sE") in (None, [])
    finally:
        asyncio.run(plugin.on_unload())


def test_handle_oversize_video_notice_not_group(tmp_path):
    """超 video.max_file_mb：确定性拒绝 → 模型通知（含剪辑指引），不发群。"""
    plugin, _ = _new_plugin("vidbig")
    p = _mp4(tmp_path / "big.mp4", seconds=5.0)
    body = p.read_bytes()
    try:
        plugin.config.video.api_key = "k"
        plugin.config.video.max_file_mb = 0.0001  # 远小于文件
        asyncio.run(plugin._handle_file("sB", "sB", "", _fdata_bytes("big.mp4", body)))
        notices = plugin._file_notices.get("sB") or []
        assert len(notices) == 1
        assert "剪辑" in notices[0]["reason"] or "超过上限" in notices[0]["reason"]
    finally:
        asyncio.run(plugin.on_unload())


def test_handle_video_budget_exhausted_notice(tmp_path):
    plugin, _ = _new_plugin("vidbnote")
    p = _mp4(tmp_path / "b.mp4", seconds=5.0)
    body = p.read_bytes()
    try:
        plugin.config.video.api_key = "k"
        plugin.config.video.daily_budget = 1
        plugin._video_budget_take()  # 用掉名额
        asyncio.run(plugin._handle_file("sN", "sN", "", _fdata_bytes("b.mp4", body)))
        notices = plugin._file_notices.get("sN") or []
        assert len(notices) == 1
        assert "名额" in notices[0]["reason"]
        vs = plugin._store.get("sN", "sN")
        assert vs is None or "b.mp4" not in vs.files, "超预算不得入库"
    finally:
        asyncio.run(plugin.on_unload())


def test_handle_video_missing_key_notice(tmp_path):
    plugin, _ = _new_plugin("vidnokey")
    p = _mp4(tmp_path / "k.mp4", seconds=5.0)
    try:
        plugin.config.video.api_key = ""
        asyncio.run(plugin._handle_file("sK", "sK", "", _fdata_bytes("k.mp4", p.read_bytes())))
        notices = plugin._file_notices.get("sK") or []
        assert len(notices) == 1
        assert "api_key" in notices[0]["reason"]
    finally:
        asyncio.run(plugin.on_unload())


# ── 11. 重试包携带 is_video ───────────────────────────────────


def test_retry_bundle_roundtrip_is_video(tmp_path):
    plugin, _ = _new_plugin("vidretry")
    try:
        path, size = plugin._persist_retry_bundle(
            "clip.mp4", ["块一", "块二"], 100, False, "digest123", is_video=True,
        )
        bundle = plugin._read_retry_bundle(path)
        assert bundle["is_video"] is True
        assert bundle["is_audio"] is False
        plugin._discard_retry_bundle({"bundle_path": path, "bundle_bytes": size})
        assert not Path(path).exists()
    finally:
        asyncio.run(plugin.on_unload())


def test_embedding_failed_carries_is_video():
    from vector_store import EmbeddingFailed

    e = EmbeddingFailed(RuntimeError("x"), ["c"], 10, False, True)
    assert e.is_video is True
    # 旧签名（4 参）仍兼容
    e2 = EmbeddingFailed(RuntimeError("x"), ["c"], 10, True)
    assert e2.is_video is False and e2.is_audio is True


# ── 12. 配置与状态 ────────────────────────────────────────────


def test_config_defaults_and_version():
    plugin, mod = _new_plugin("vidcfg")
    try:
        v = plugin.config.video
        assert v.enabled is True
        assert v.model == "doubao-seed-2-1-lite-260915"
        assert v.max_file_mb == 20.0
        assert v.max_duration_seconds == 600
        assert v.fps == 1.0
        assert v.daily_budget == 20
        assert plugin.config.plugin.config_version == "1.6.0"
    finally:
        asyncio.run(plugin.on_unload())


def test_file_status_shows_video_line():
    plugin, _ = _new_plugin("vidstatus")
    try:
        ok, text, _ = asyncio.run(plugin.cmd_status())
        assert ok
        assert "视频理解" in text
        assert "今日预算" in text
        assert "未配置" in text  # 默认无 Key
    finally:
        asyncio.run(plugin.on_unload())


def test_webui_video_section_labels():
    plugin, mod = _new_plugin("vidwebui")
    try:
        schema = plugin.get_webui_config_schema(
            plugin_id="org.mai-mai.file-reader", plugin_name="file-reader",
            plugin_version="", plugin_description="", plugin_author="",
        )
        sections = schema["sections"]
        assert "video" in sections
        assert sections["video"].get("collapsed") is True
        vfields = sections["video"]["fields"]
        assert vfields["model"]["label"] == "视频理解模型"
        afields = sections["audio"]["fields"]
        assert afields["model"]["label"] == "音频理解模型", "section 维度 label 不得串节"
        assert vfields["daily_budget"]["label"] == "每日理解数上限"
        assert vfields["api_key"].get("hint"), "说明必须抄进 hint"
    finally:
        asyncio.run(plugin.on_unload())