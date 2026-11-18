# -*- coding: utf-8 -*-
"""视频文件理解（v1.8.0）：豆包 Files API 上传 → 一次调用音画双收 → 文本回流检索管线。

路线（2026-11-18 方案评审定案，决策 D1~D8）：
- 音画都要：doubao-seed-2.1-lite 全模态，单模型一次调用同时得到画面描述与音轨转写；
- 取文件走既有通道（SnowLuma/NapCat）落盘，本模块只负责「已落盘文件 → 理解文本」；
- 上传用 Files API（multipart 流式读文件），**不做 base64 整读**——绕开
  fr_audio 的 read_bytes+base64 内存放大（方案 2.2 节）；
- 超限/超预算/未配 Key 一律确定性拒绝（VideoUnsupportedError 继承 ValueError，
  走 plugin._reply_error 既有 value_error 失败通知链路：只注入模型、不发群）；
- 理解文本带【视频理解】头部回流 add_file，分块/检索/追问全部复用，不新增检索分支。

官方限制依据（火山方舟《视频理解》，2026-11-18 核实 docs.volcengine.com）：
- Files API 托管存储单文件 ≤ 512MB（TOS 2GB，本插件不用）；上传默认保留 7 天；
- base64 < 50MB 且请求体 ≤ 64MB；公网 URL 直传 ≤ 50MB（P2 优化项，本版不做）；
- 上传时可带 preprocess_configs[video][model]/[fps]，抽帧 token 默认上限 80k；
- seed-2.1-lite-260915：文本/视觉/音频统一理解，1M 上下文，¥0.8/¥2.7 每百万 token。

架构约束（maibot-plugin-dev 铁律 8）：本模块不出现 self.ctx；网络调用自带
httpx 客户端，日志经注入的 log 回调输出，可脱机单测。
"""

import asyncio
import json
import logging
import os
import struct
from pathlib import Path
from typing import Any, Callable, Optional

_LOGGER = logging.getLogger(__name__)

#: 支持的视频扩展名（容器格式）。仅常见开放封装，不引入 ffmpeg 转码依赖。
VIDEO_EXTENSIONS: frozenset = frozenset({"mp4", "mov", "m4v", "avi", "mkv", "webm"})

#: 能识别但明确拒绝的视频格式：给准确原因（沿用 RECOGNIZED_UNSUPPORTED 惯例）。
VIDEO_RECOGNIZED_UNSUPPORTED: dict[str, str] = {
    "flv": "FLV 封装暂不支持，请转成 .mp4 后重发",
    "wmv": "WMV 格式暂不支持，请转成 .mp4 后重发",
    "rmvb": "RMVB 格式暂不支持，请转成 .mp4 后重发",
    "rm": "RM 格式暂不支持，请转成 .mp4 后重发",
    "ts": "TS 流媒体封装暂不支持，请转成 .mp4 后重发",
    "3gp": "3GP 格式暂不支持，请转成 .mp4 后重发",
}

#: Files API 托管存储硬上限（官方 512MB）。API 侧约束，不随用户配置放宽；
#: max_file_mb 配置只能在此之下收紧。本插件面向 <20MB 短视频，默认远小于此。
FILES_API_HARD_LIMIT_BYTES = 512 * 1024 * 1024

#: 上传接口的 purpose（官方示例固定 user_data）。
_UPLOAD_PURPOSE = "user_data"

#: 输出纪律写在代码层（铁律 11）：不编造、听不清/看不清要标注是硬约束，
#: 不提供用户可改的 prompt 配置项，避免存量实例改不动。
VIDEO_SYSTEM_PROMPT = (
    "你是一个视频内容理解助手。用户会提供一段视频（含画面与音轨），请同时理解两者并输出：\n"
    "1. 画面：按时间顺序描述场景、人物/角色、动作与关键事件；能识别的时间点用 [mm:ss] 标注。\n"
    "2. 音轨：有人声时尽量完整转写（对话/解说/歌词大意），按话题分段；无人声则描述音乐或环境声。\n"
    "3. 综合：最后给一段整体摘要（视频在讲什么、亮点是什么）。\n"
    "输出要求：\n"
    "- 第一行固定输出「内容类型：」加一个短语（如 讲解/演示/剧情/音乐PV/生活记录/其他）。\n"
    "- 只描述你确实看到/听到的内容，不要编造；看不清或听不清的地方明确标注（看不清）/（听不清）。\n"
    "- 画面中的文字（字幕/标题/界面）尽量原样保留关键部分。\n"
    "- 歌词受版权保护，最多引用一两句，不要大段复述。\n"
    "- 用简体中文输出（视频本身是外语时，转写保留原文并附中文大意）。"
)


class VideoUnsupportedError(ValueError):
    """格式不支持 / 超限 / 超预算 / 未配置等**确定性拒绝**。

    继承 ValueError：走 plugin._handle_file 既有 value_error 分支，
    失败原因经解析失败通知注入给模型（不发群消息），与音频拒绝同链路。
    """


def is_video_file(file_name: str) -> bool:
    """扩展名是否属于支持的视频格式。"""
    from file_parser import get_extension

    return get_extension(file_name) in VIDEO_EXTENSIONS


def sniff_video_format(head: bytes) -> str:
    """按文件头嗅探视频封装（无扩展名/改名文件兜底）；识别不了返回空串。

    只看头部字节，不解析全文件。注意与 fr_audio.sniff_audio_format 的边界：
    ftyp 品牌 M4A /M4B 是纯音频（归 fr_audio），mp42/isom/mp41 等视频品牌归这里；
    plugin._parse_to_text 的分派顺序是**先视频后音频**，防止 mp4 被误当 m4a。
    """
    if len(head) < 12:
        return ""
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B "):
            return ""  # 纯音频，交给 fr_audio
        if brand in (b"mp42", b"isom", b"mp41", b"iso2", b"avc1", b"M4V ", b"qt  ", b"3gp4", b"3gp5"):
            return "mov" if brand == b"qt  " else "mp4"
        return ""
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "avi"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "mkv"  # EBML：mkv/webm 同头，统一按 mkv 处理（模型侧都收）
    if head.startswith(b"FLV"):
        return "flv"
    return ""


# ─── 时长探测（纯标准库，不引入 ffmpeg/ffprobe 依赖） ─────────────


def _probe_mp4_duration(path: str) -> Optional[float]:
    """读 MP4/MOV 的 moov/mvhd box 取时长（秒）。读不出返回 None。

    只顺序扫描顶层 box 找 moov，再在 moov 内找 mvhd；不整读文件。
    mvhd version 0：timescale@+12(u32) duration@+16(u32)；
    version 1：timescale@+20(u32) duration@+24(u64)。
    """
    try:
        with open(path, "rb") as f:
            file_size = os.fstat(f.fileno()).st_size
            pos = 0
            moov: Optional[bytes] = None
            while pos + 8 <= file_size:
                f.seek(pos)
                header = f.read(8)
                if len(header) < 8:
                    break
                size = struct.unpack(">I", header[:4])[0]
                box_type = header[4:8]
                header_len = 8
                if size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    size = struct.unpack(">Q", ext)[0]
                    header_len = 16
                elif size == 0:
                    size = file_size - pos  # 延伸到文件尾
                if size < header_len:
                    break
                if box_type == b"moov":
                    payload_size = size - header_len
                    # moov 元数据通常 <1MB；异常大（>16MB）视为损坏，不整读
                    if payload_size > 16 * 1024 * 1024:
                        return None
                    moov = f.read(payload_size)
                    break
                pos += size
            if not moov:
                return None
            # 在 moov 内顺序找 mvhd（嵌套一层 box）
            i = 0
            while i + 8 <= len(moov):
                size = struct.unpack(">I", moov[i : i + 4])[0]
                box_type = moov[i + 4 : i + 8]
                if size < 8:
                    break
                if box_type == b"mvhd":
                    body = moov[i + 8 : i + size]
                    if len(body) < 4:
                        return None
                    version = body[0]
                    if version == 1:
                        if len(body) < 32:
                            return None
                        timescale = struct.unpack(">I", body[20:24])[0]
                        duration = struct.unpack(">Q", body[24:32])[0]
                    else:
                        if len(body) < 20:
                            return None
                        timescale = struct.unpack(">I", body[12:16])[0]
                        duration = struct.unpack(">I", body[16:20])[0]
                    if timescale > 0 and duration > 0:
                        return duration / float(timescale)
                    return None
                i += size
    except (OSError, struct.error) as e:
        _LOGGER.warning("MP4 时长探测失败（%s）: %s", path, e)
    return None


def _probe_avi_duration(path: str) -> Optional[float]:
    """读 AVI 的 avih（dwMicroSecPerFrame × dwTotalFrames）取时长。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        idx = head.find(b"avih")
        if idx < 0 or idx + 4 + 40 > len(head):
            return None
        size = struct.unpack("<I", head[idx + 4 : idx + 8])[0]
        if size < 40:
            return None
        # avih 数据布局（相对 fourcc 后第 8 字节起）：
        # dwMicroSecPerFrame@+0 dwMaxBytesPerSec@+4 dwPaddingGranularity@+8
        # dwFlags@+12 dwTotalFrames@+16
        body = head[idx + 8 : idx + 8 + min(size, 40)]
        if len(body) < 20:
            return None
        us_per_frame = struct.unpack("<I", body[0:4])[0]
        total_frames = struct.unpack("<I", body[16:20])[0]
        if us_per_frame > 0 and total_frames > 0:
            return us_per_frame * total_frames / 1e6
    except (OSError, struct.error) as e:
        _LOGGER.warning("AVI 时长探测失败（%s）: %s", path, e)
    return None


def probe_video_duration(path: str) -> Optional[float]:
    """读视频时长（秒）。MP4/MOV/M4V 走 mvhd，AVI 走 avih。

    MKV/WebM 的 EBML 时长解析复杂且本插件主场景是 QQ 短视频（mp4 为主），
    读不出返回 None——调用方按 fail-closed 拒绝（与 fr_audio 同口径：
    无法证明在时长限内就不放行）。
    """
    suffix = Path(path).suffix.lower()
    if suffix in (".mp4", ".mov", ".m4v"):
        return _probe_mp4_duration(path)
    if suffix == ".avi":
        return _probe_avi_duration(path)
    # 无扩展名/其他封装：按内容试 mp4（QQ 视频绝大多数是 mp4 家族）
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return _probe_mp4_duration(path)
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return _probe_avi_duration(path)
    return None


def precheck_video(
    path: str,
    file_name: str,
    *,
    max_duration_seconds: float,
    max_file_mb: float,
) -> tuple[float, int]:
    """上传前的确定性预检。返回 (duration_seconds, size_bytes)。

    任一不满足即抛 VideoUnsupportedError（fail-closed，绝不截断后偷发）：
    格式 → 大小 → Files API 硬上限 → 时长。时长读不出来也拒绝。
    """
    from file_parser import get_extension

    ext = get_extension(file_name)
    if ext not in VIDEO_EXTENSIONS:
        reason = VIDEO_RECOGNIZED_UNSUPPORTED.get(ext)
        if reason:
            raise VideoUnsupportedError(f"{reason}（{file_name}）")
        raise VideoUnsupportedError(f"不支持的视频格式: .{ext or '(无扩展名)'}（{file_name}）")

    size = os.path.getsize(path)
    if size <= 0:
        raise VideoUnsupportedError(f"视频文件「{file_name}」是空文件，无法理解")
    size_mb = size / 1024 / 1024
    if max_file_mb > 0 and size_mb > max_file_mb:
        raise VideoUnsupportedError(
            f"视频文件「{file_name}」大小 {size_mb:.1f}MB 超过上限 {max_file_mb:.1f}MB"
            "（video.max_file_mb），已拒绝。请剪辑或压缩后重发"
        )
    if size > FILES_API_HARD_LIMIT_BYTES:
        raise VideoUnsupportedError(
            f"视频文件「{file_name}」大小 {size_mb:.1f}MB 超过上传接口 512MB 硬上限，已拒绝。"
            "请剪辑或压缩后重发"
        )

    duration = probe_video_duration(path)
    if duration is None:
        raise VideoUnsupportedError(
            f"无法读取视频文件「{file_name}」的时长（元数据缺失或封装异常），"
            "为避免超出模型限制已拒绝。请转成标准 .mp4 后重发"
        )
    if max_duration_seconds > 0 and duration > max_duration_seconds:
        raise VideoUnsupportedError(
            f"视频文件「{file_name}」时长 {duration / 60:.1f} 分钟超过上限 "
            f"{max_duration_seconds / 60:.0f} 分钟（video.max_duration_seconds），已拒绝。"
            "请剪辑后重发"
        )
    return duration, size


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    minutes, sec = divmod(total, 60)
    return f"{minutes} 分 {sec} 秒" if minutes else f"{sec} 秒"


# ─── Files API 上传 + 理解调用 ───────────────────────────────────


async def _upload_to_files_api(
    path: str,
    file_name: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    fps: float,
    request_timeout: float,
) -> str:
    """multipart 上传视频到方舟 Files API，返回 file_id。

    httpx 的 files= 传文件对象是流式读取，不做 base64 整读（方案 2.2）。
    preprocess_configs 在上传时指定抽帧模型与 fps（官方推荐形态）。
    """
    import httpx  # type: ignore

    url = f"{base_url.rstrip('/')}/files"
    data = {
        "purpose": _UPLOAD_PURPOSE,
        "preprocess_configs[video][model]": model,
        "preprocess_configs[video][fps]": str(fps),
    }
    mime = "video/quicktime" if Path(file_name).suffix.lower() == ".mov" else "video/mp4"
    async with httpx.AsyncClient(timeout=float(request_timeout)) as client:
        with open(path, "rb") as f:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key.strip()}"},
                data=data,
                files={"file": (file_name, f, mime)},
            )
    if response.status_code >= 400:
        raise RuntimeError(
            f"视频上传失败 HTTP {response.status_code}: {response.text[:500]}"
        )
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise RuntimeError(f"视频上传响应不是合法 JSON: {response.text[:200]}") from e
    file_id = str(payload.get("id") or "")
    if not file_id:
        raise RuntimeError(f"视频上传未返回 file_id: {str(payload)[:300]}")
    return file_id


async def _ask_video_model(
    file_id: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    max_output_tokens: int,
    request_timeout: float,
    user_text: str,
) -> str:
    """Chat API 引用 file_id 做视频理解（非流式，方舟 Chat API 支持）。

    请求形态依据官方《视频理解》Chat API 示例：
    content 段 {"type": "video_url", "video_url": {"file_id": ...}}。
    """
    import httpx  # type: ignore

    payload = {
        "model": model,
        "max_tokens": int(max_output_tokens),
        "messages": [
            {"role": "system", "content": VIDEO_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"file_id": file_id}},
                    {"type": "text", "text": user_text},
                ],
            },
        ],
    }
    async with httpx.AsyncClient(timeout=float(request_timeout)) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            json=payload,
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"视频理解模型请求失败 HTTP {response.status_code}: {response.text[:500]}"
        )
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise RuntimeError(f"视频理解响应不是合法 JSON: {response.text[:200]}") from e
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"视频理解模型未返回 choices: {str(data)[:300]}")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):
        # 个别网关把 content 拆成段数组，拼文本段
        content = "".join(
            str(seg.get("text") or "") for seg in content if isinstance(seg, dict)
        )
    return str(content or "").strip()


async def read_video_to_text(
    path: str,
    file_name: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    max_duration_seconds: float,
    max_file_mb: float,
    max_output_tokens: int,
    fps: float,
    request_timeout: float,
    log: Optional[Callable[..., None]] = None,
) -> str:
    """视频 → 理解文本（含元信息头部）。供 plugin._parse_to_text 分派。

    未配置 api_key 时给友好指引（用户尚未开通方舟，不能当故障报错）。
    网络/模型异常抛 RuntimeError，走既有 runtime_error 失败通知链路。
    """
    _log = log or (lambda *a, **k: None)
    if not (api_key or "").strip():
        raise VideoUnsupportedError(
            f"视频文件「{file_name}」需要视频理解模型才能读取，当前未配置 API Key。"
            "请管理员在插件配置 [video] 中填写 api_key（默认用火山方舟 doubao-seed-2-1-lite，"
            "开通与获取 Key 见插件 README「视频文件」一节），配置后重发即可"
        )

    duration, size = await asyncio.to_thread(
        precheck_video,
        path,
        file_name,
        max_duration_seconds=float(max_duration_seconds),
        max_file_mb=float(max_file_mb),
    )
    _log(
        "info",
        "视频预检通过 %s：时长=%s 大小=%.2fMB",
        file_name, _format_duration(duration), size / 1024 / 1024,
    )

    file_id = await _upload_to_files_api(
        path,
        file_name,
        base_url=base_url,
        api_key=api_key,
        model=model,
        fps=float(fps),
        request_timeout=request_timeout,
    )
    _log("info", "视频已上传 %s：file_id=%s", file_name, file_id)

    user_text = (
        f"文件名：{file_name}\n时长：{_format_duration(duration)}\n"
        "请按系统提示理解这段视频的画面与音轨并输出。"
    )
    text = await _ask_video_model(
        file_id,
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_output_tokens=max_output_tokens,
        request_timeout=request_timeout,
        user_text=user_text,
    )
    text = text.strip()
    if not text:
        raise RuntimeError(
            f"视频理解模型对「{file_name}」返回了空内容（可能是接口异常或视频无可理解内容），"
            "请稍后重发"
        )
    header = (
        f"【视频理解】{file_name}\n"
        f"时长：{_format_duration(duration)}｜大小：{size / 1024 / 1024:.2f}MB"
        f"｜理解模型：{model}\n"
        "以下内容由视频理解模型生成（画面描述 + 音轨转写），仅覆盖上述完整视频；"
        "追问时以此文本为准。\n\n"
    )
    return header + text