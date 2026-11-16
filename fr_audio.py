# -*- coding: utf-8 -*-
"""音频文件理解（v1.5.0）：Omni 模型听一次 → 产出文本 → 回流现有检索管线。

参考 SengokuCola/music_appreciation_plugin 的音频调用方式（OpenAI 兼容
``input_audio`` + base64 data URI + ``stream=true`` SSE），但定位不同：
- 该仓库是「网易云搜歌 → 赏析」一次性 Tool；本模块是 file-reader 的一种
  **文件解析器**：上传音频文件自动触发，产出文本视同文档解析结果，进入
  分块 + 向量/BM25 混合检索，后续追问免费、不重发音频。
- 长音频不截断不分段：超限直接拒绝并告知实际时长/大小（用户明确决策）。

架构约束（maibot-plugin-dev 铁律 8）：本模块不出现 self.ctx；网络调用
自带 httpx 客户端，日志经注入的 log 回调输出，可脱机单测。

官方限制依据（阿里云百炼 Qwen-Omni 文档，2026-11-16 核实）：
- Qwen3-Omni-Flash：单文件；Base64 编码后字符串必须 < 10MB；时长最长 20 分钟。
- 本插件默认 max_file_mb=7.0（7MB 原始 ≈ 9.34MB base64，留余量）、
  max_duration_seconds=1200（官方上限）。换用其他 Omni 模型时可自行放宽。
"""

import asyncio
import base64
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Callable, Optional

_LOGGER = logging.getLogger(__name__)

#: 支持直发的音频扩展名 → Omni 请求 format 字段。
#: 仅常见开放格式，不引入 ffmpeg/pydub 转换依赖（用户明确决策）。
AUDIO_EXTENSIONS: dict[str, str] = {
    "mp3": "mp3",
    "wav": "wav",
    "flac": "flac",
    "ogg": "ogg",
    "m4a": "m4a",
    "aac": "aac",
}

#: 能识别但明确拒绝的音频格式：给**准确原因**（沿用 RECOGNIZED_UNSUPPORTED 惯例）。
AUDIO_RECOGNIZED_UNSUPPORTED: dict[str, str] = {
    "silk": "QQ 语音消息原始格式（silk）暂不支持，请转成 .mp3 / .wav 后重发",
    "slk": "QQ 语音消息原始格式（silk）暂不支持，请转成 .mp3 / .wav 后重发",
    "amr": "AMR 语音格式暂不支持，请转成 .mp3 / .wav 后重发",
    "wma": "WMA 格式暂不支持，请转成 .mp3 / .wav 后重发",
}

#: Base64 编码后的硬上限（百炼 Qwen3-Omni-Flash 官方限制：< 10MB）。
#: 这是 API 侧约束、不随用户配置放宽；max_file_mb 配置只能在此之下收紧。
BASE64_HARD_LIMIT_BYTES = 10 * 1024 * 1024

#: 自适应产出提示词：语音→尽量完整转写+要点；音乐→赏析。
#: 「输出纪律」写在代码层（铁律 11）：不编造、不复述大段歌词是硬约束，
#: 不提供用户可改的 prompt 配置项，避免存量实例改不动。
AUDIO_SYSTEM_PROMPT = (
    "你是一个音频内容理解助手。用户会提供一段音频文件，请先判断内容类型，再按对应方式输出：\n"
    "1. 语音类（对话/会议/课程/讲座/播客等）：尽量完整地转写语音内容，按话题或说话人分段，"
    "每段给出要点；关键的时间点、数字、人名、结论必须保留原文表述。\n"
    "2. 音乐类（歌曲/纯音乐）：写一份赏析笔记，涵盖整体风格与情绪、编曲与配器、人声表现（如有）、"
    "节奏与段落结构、歌词主题与意象（如能听清歌词）。\n"
    "3. 混合或其他内容：先转写可辨识的语音，再描述其余声音内容。\n"
    "输出要求：\n"
    "- 第一行固定输出「内容类型：语音」或「内容类型：音乐」或「内容类型：混合/其他」。\n"
    "- 只描述你确实听到的内容，不要编造；听不清的地方明确标注（听不清），不要猜。\n"
    "- 歌词受版权保护，最多引用一两句，不要大段复述。\n"
    "- 用简体中文输出（音频本身是外语时，转写保留原文并附中文大意）。"
)


class AudioUnsupportedError(ValueError):
    """格式不支持 / 超限 / 未配置等**确定性拒绝**。

    继承 ValueError：走 plugin._handle_file 既有 value_error 分支，
    失败原因经解析失败通知注入给模型（不发群消息），与文档解析失败同链路。
    """


def is_audio_file(file_name: str) -> bool:
    """扩展名是否属于支持直发的音频格式。"""
    from file_parser import get_extension

    return get_extension(file_name) in AUDIO_EXTENSIONS


def sniff_audio_format(head: bytes) -> str:
    """按文件头嗅探音频格式（无扩展名/改名文件兜底）；识别不了返回空串。

    只看头部字节，不解析全文件。m4a/aac 的 ftyp box 品牌较多，只认常见的。
    """
    if len(head) < 12:
        return ""
    if head.startswith(b"ID3"):
        return "mp3"
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # MPEG frame sync：mp3（Layer III）或 AAC ADTS（layer bits 为 00）
        layer = (head[1] >> 1) & 0x03
        return "aac" if layer == 0 else "mp3"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"OggS"):
        return "ogg"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B ", b"mp42", b"isom", b"mp41"):
            return "m4a"
    if head.startswith(b"#!SILK"):
        return "silk"
    if head.startswith(b"#!AMR"):
        return "amr"
    return ""


def probe_audio_duration(path: str) -> Optional[float]:
    """读音频时长（秒）。优先 mutagen；缺依赖时用标准库 wave/aifc 兜底 wav。

    返回 None = 无法确定时长（不据此放行：调用方按 fail-closed 拒绝）。
    """
    try:
        import mutagen  # type: ignore

        meta = mutagen.File(path)
        if meta is not None and getattr(meta, "info", None) is not None:
            length = float(getattr(meta.info, "length", 0.0) or 0.0)
            if length > 0:
                return length
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001  元数据损坏不致命，走兜底
        _LOGGER.warning("mutagen 读取音频时长失败（%s）: %s", path, e)
    suffix = Path(path).suffix.lower()
    if suffix == ".wav":
        import wave

        try:
            with wave.open(path, "rb") as wf:
                rate = wf.getframerate()
                if rate > 0:
                    return wf.getnframes() / float(rate)
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("wave 读取音频时长失败（%s）: %s", path, e)
    return None


def precheck_audio(
    path: str,
    file_name: str,
    *,
    max_duration_seconds: float,
    max_file_mb: float,
) -> tuple[str, float, int]:
    """发送前的确定性预检。返回 (omni_format, duration_seconds, size_bytes)。

    任一不满足即抛 AudioUnsupportedError（fail-closed，绝不截断后偷发）：
    大小 → base64 硬上限 → 时长。时长读不出来也拒绝——无法证明在限内。
    """
    from file_parser import get_extension

    ext = get_extension(file_name)
    fmt = AUDIO_EXTENSIONS.get(ext)
    if not fmt:
        reason = AUDIO_RECOGNIZED_UNSUPPORTED.get(ext)
        if reason:
            raise AudioUnsupportedError(f"{reason}（{file_name}）")
        raise AudioUnsupportedError(f"不支持的音频格式: .{ext or '(无扩展名)'}（{file_name}）")

    size = os.path.getsize(path)
    if size <= 0:
        raise AudioUnsupportedError(f"音频文件「{file_name}」是空文件，无法理解")
    size_mb = size / 1024 / 1024
    if max_file_mb > 0 and size_mb > max_file_mb:
        raise AudioUnsupportedError(
            f"音频文件「{file_name}」大小 {size_mb:.1f}MB 超过上限 {max_file_mb:.1f}MB"
            "（audio.max_file_mb），已拒绝。请压缩码率或剪辑后重发"
        )
    b64_size = 4 * math.ceil(size / 3)
    if b64_size >= BASE64_HARD_LIMIT_BYTES:
        raise AudioUnsupportedError(
            f"音频文件「{file_name}」大小 {size_mb:.1f}MB，Base64 编码后约 "
            f"{b64_size / 1024 / 1024:.1f}MB，超过模型接口 10MB 硬上限，已拒绝。"
            "请压缩码率或剪辑后重发"
        )

    duration = probe_audio_duration(path)
    if duration is None:
        raise AudioUnsupportedError(
            f"无法读取音频文件「{file_name}」的时长（元数据缺失或格式异常），"
            "为避免超出模型限制已拒绝。请转成标准 .mp3 / .wav 后重发"
        )
    if max_duration_seconds > 0 and duration > max_duration_seconds:
        raise AudioUnsupportedError(
            f"音频文件「{file_name}」时长 {duration / 60:.1f} 分钟超过上限 "
            f"{max_duration_seconds / 60:.0f} 分钟（audio.max_duration_seconds），已拒绝。"
            "请剪辑后重发"
        )
    return fmt, duration, size


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    minutes, sec = divmod(total, 60)
    return f"{minutes} 分 {sec} 秒" if minutes else f"{sec} 秒"


async def _ask_omni(
    audio_bytes: bytes,
    omni_format: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    max_output_tokens: int,
    request_timeout: float,
    user_text: str,
) -> str:
    """流式调用 Omni（百炼 Omni 仅支持 stream=true）并拼接文本。

    参考 music_appreciation_plugin._ask_omni 的请求形态；错误信息带
    HTTP 状态与 body 前 500 字符，方便真机定位（密钥不回显）。
    """
    import httpx  # type: ignore

    payload = {
        "model": model,
        "stream": True,
        "modalities": ["text"],
        "max_tokens": int(max_output_tokens),
        "messages": [
            {"role": "system", "content": AUDIO_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": f"data:;base64,{base64.b64encode(audio_bytes).decode()}",
                            "format": omni_format,
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            },
        ],
    }
    pieces: list[str] = []
    async with httpx.AsyncClient(timeout=float(request_timeout)) as client:
        async with client.stream(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            json=payload,
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"音频理解模型请求失败 HTTP {response.status_code}: {body[:500]}"
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue  # 个别网关会夹心跳/注释行，跳过不致命
                for choice in chunk.get("choices") or []:
                    content = (choice.get("delta") or {}).get("content")
                    if content:
                        pieces.append(content)
    return "".join(pieces).strip()


async def read_audio_to_text(
    path: str,
    file_name: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    max_duration_seconds: float,
    max_file_mb: float,
    max_output_tokens: int,
    request_timeout: float,
    log: Optional[Callable[..., None]] = None,
) -> str:
    """音频 → 理解文本（含元信息头部）。供 plugin._parse_to_text 分派。

    未配置 api_key 时给友好指引（用户尚未开通百炼，不能当故障报错）。
    网络/模型异常抛 RuntimeError，走既有 runtime_error 失败通知链路。
    """
    _log = log or (lambda *a, **k: None)
    if not (api_key or "").strip():
        raise AudioUnsupportedError(
            f"音频文件「{file_name}」需要音频理解模型才能读取，当前未配置 API Key。"
            "请管理员在插件配置 [audio] 中填写 api_key（默认用阿里云百炼 qwen3-omni-flash，"
            "开通与获取 Key 见插件 README「音频文件」一节），配置后重发即可"
        )

    fmt, duration, size = await asyncio.to_thread(
        precheck_audio,
        path,
        file_name,
        max_duration_seconds=float(max_duration_seconds),
        max_file_mb=float(max_file_mb),
    )
    _log(
        "info",
        "音频预检通过 %s：format=%s 时长=%s 大小=%.2fMB",
        file_name, fmt, _format_duration(duration), size / 1024 / 1024,
    )

    audio_bytes = await asyncio.to_thread(Path(path).read_bytes)
    user_text = (
        f"文件名：{file_name}\n时长：{_format_duration(duration)}\n"
        "请按系统提示理解这段音频并输出。"
    )
    text = await _ask_omni(
        audio_bytes,
        fmt,
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_output_tokens=max_output_tokens,
        request_timeout=request_timeout,
        user_text=user_text,
    )
    if not text:
        raise RuntimeError(
            f"音频理解模型对「{file_name}」返回了空内容（可能是音频无人声且模型未描述，"
            "或接口异常），请稍后重发"
        )
    header = (
        f"【音频理解】{file_name}\n"
        f"格式：{fmt}｜时长：{_format_duration(duration)}｜大小：{size / 1024 / 1024:.2f}MB"
        f"｜理解模型：{model}\n"
        "以下内容由音频理解模型生成，仅覆盖上述完整音频；追问时以此文本为准。\n\n"
    )
    return header + text
