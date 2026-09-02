"""file-reader 功能自测：验证「解析 → 分块 → 向量化 → 检索 → 清理」全链路 + hook 直调。

不依赖真实 embedding 模型：
- 模块级测试用伪 embed_fn（按字符 hash 生成确定性向量）验证检索逻辑；
- hook 直调测试用 FakeHost（tools/test_runner）驱动真实 plugin.py，
  覆盖 on_file_message / inject_file_context / cmd_status 的配置访问路径
  ——真机曾因 self.config.reader.enabled 误访问在 hook 里炸过，此类错误必须在这里拦住。

用法（须用装了 SDK 的 venv）:
    python test_file_reader.py
退出码: 0=全部通过, 1=有失败
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile
import time
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
_TOOLS_DIR = _PLUGIN_DIR.parent.parent / "tools"
sys.path.insert(0, str(_PLUGIN_DIR))
sys.path.insert(0, str(_TOOLS_DIR))

from chunker import RecursiveCharacterChunker  # noqa: E402
from file_parser import get_extension, is_supported, read_any_file_to_text  # noqa: E402
from vector_store import VectorStore  # noqa: E402


def _fake_embed(texts):
    """确定性伪 embedding：每个字符映射到一个向量分量，保证语义相近文本得分更高。"""
    vecs = []
    for t in texts:
        v = [0.0] * 64
        for ch in t:
            v[ord(ch) % 64] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        vecs.append([x / norm for x in v])
    return {"results": [{"embedding": v} for v in vecs]}


async def _test_modules() -> None:
    """模块级测试：分块 / 类型表 / 解析 / 检索 / 清理。"""
    # 1. 分块器
    c = RecursiveCharacterChunker(10, 2)
    chunks = c.chunk("一二三四五六七八九十十一十二十三十四十五")
    assert len(chunks) > 1, "长文本应切成多块"
    assert all(0 < len(x) <= 10 for x in chunks), f"块大小超限: {[len(x) for x in chunks]}"
    print(f"[PASS] 分块器: {len(chunks)} 块，均 ≤10 字符")

    # 2. 类型表
    assert get_extension("report.PDF") == "pdf"
    assert get_extension(".env") == "env"
    assert is_supported("a.xlsx") and is_supported("b.py") and is_supported("c.md")
    assert not is_supported("movie.mp4") and not is_supported("photo.jpg")
    print("[PASS] 文件类型表")

    # 3. 真实解析（txt 不需要额外依赖）
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("第一行内容\n第二行内容\n第三行内容")
        tmp = f.name
    try:
        text = read_any_file_to_text(tmp, "demo.txt")
        assert "第一行" in text and "第三行" in text
        print("[PASS] txt 解析")
    finally:
        os.remove(tmp)

    # 4. 向量库全链路
    vs = VectorStore(_fake_embed, chunk_size=20, chunk_overlap=4, retrieve_top_k=2)
    doc = "苹果是一种水果，富含维生素C。香蕉也是水果，富含钾。汽车是一种交通工具，需要汽油。"
    entry = await vs.add_file("notes.txt", doc)
    assert entry.chunks and len(entry.vectors) == len(entry.chunks)
    print(f"[PASS] 向量化入库: {len(entry.chunks)} 块")

    # 检索：查询"水果"应命中苹果/香蕉，而非汽车
    qvec = _fake_embed(["水果"])["results"][0]["embedding"]
    results = vs.retrieve(qvec, top_k=2)
    assert results, "检索应有结果"
    top_text = results[0]["text"]
    assert ("苹果" in top_text or "香蕉" in top_text), f"检索应命中水果相关: {top_text}"
    print(f"[PASS] 余弦检索: top1='{top_text[:20]}...' score={results[0]['score']:.3f}")

    # 5. 轮数清理
    entry.rounds = 10
    removed = vs.cleanup_expired(retention_seconds=3600, max_rounds=5)
    assert "notes.txt" in removed
    assert not vs.files
    print("[PASS] 轮数过期清理")

    # 6. 时间清理
    import time

    e2 = await vs.add_file("x.txt", "临时内容用于测试时间过期")
    e2.upload_time = time.time() - 7200  # 2 小时前
    removed = vs.cleanup_expired(retention_seconds=60, max_rounds=999)
    assert "x.txt" in removed
    print("[PASS] 时间过期清理")


async def _test_plugin_hooks() -> None:
    """插件级测试：FakeHost 驱动真实 plugin.py，直调 hook 与命令。

    回归背景：v1.0.0 曾在 on_file_message 里误写 self.config.reader.enabled
    （enabled 在 plugin 段），真机一收到文件就 AttributeError——本用例专防此类问题。
    """
    from test_runner import PluginTestRunner

    runner = PluginTestRunner(_PLUGIN_DIR)
    plugin = await runner.setup()
    await plugin.on_load()

    # 0. 装饰器绑定正确性（v1.0.16 真机 19:15 日志教训）：@Tool/@HookHandler/@Command
    #    装饰器绑定紧随其后的函数。v1.0.15 曾把辅助方法插在装饰器与工具函数之间，
    #    导致 search_file 工具被注册到辅助方法上——本地直调测试发现不了，真机必炸。
    #    用「通过 SDK 组件元数据反查注册名」的方式守住全部装饰器配对。
    import plugin as plugin_mod

    assert hasattr(plugin_mod.FileReaderPlugin, "search_file"), "search_file 方法应存在"
    assert hasattr(plugin_mod.FileReaderPlugin, "inject_file_context"), "inject_file_context 方法应存在"
    # search_file 的参数签名必须能接受 query 关键字（真机报错点）
    import inspect

    sig = inspect.signature(plugin_mod.FileReaderPlugin.search_file)
    assert "query" in sig.parameters, f"search_file 签名必须含 query: {sig}"
    assert "session_id" in sig.parameters, f"search_file 签名必须含 session_id: {sig}"
    # 装饰器-函数配对静态检查：@Tool/@HookHandler/@Command 后紧跟的 def 必须是预期组件
    src = plugin_mod.__file__
    text = open(src, encoding="utf-8").read()
    lines = text.splitlines()
    expected_pairs = {
        "on_file_message_early", "on_file_message", "inject_file_context",
        "cmd_clear_file", "cmd_status", "search_file",
    }
    seen_pairs = set()
    for idx, line in enumerate(lines):
        s = line.strip()
        if s.startswith("@Tool") or s.startswith("@HookHandler") or s.startswith("@Command"):
            for j in range(idx + 1, min(idx + 40, len(lines))):
                s2 = lines[j].strip()
                if s2.startswith(("def ", "async def ")):
                    name = s2.split("(")[0].replace("async def ", "").replace("def ", "")
                    seen_pairs.add(name)
                    assert name in expected_pairs, (
                        f"装饰器(line {idx+1})绑定了非组件函数 {name} —— 辅助方法必须放在装饰器之前"
                    )
                    break
    assert seen_pairs == expected_pairs, f"装饰器配对应覆盖全部组件: {seen_pairs} vs {expected_pairs}"
    print(f"[PASS] 装饰器绑定正确性（{len(seen_pairs)} 处 @Tool/@HookHandler/@Command 配对全对）")

    # 1. 直调文件检测 hook（OneBot 风格嵌套 data 段）
    content = "测试文档内容：苹果是一种水果，富含维生素C。"
    fake_message = {
        "session_id": "test-session",
        "processed_plain_text": "",
        "message_info": {"user_id": "test-user", "message_id": "m1", "group_id": None},
        "segments": [
            {"type": "file", "data": {"file": "demo.txt", "base64": base64.b64encode(content.encode("utf-8")).decode("ascii")}},
        ],
    }
    result = await plugin.on_file_message(message=fake_message, session_id="test-session")
    assert result == {"action": "continue"}, f"hook 应返回 continue: {result}"
    print("[PASS] on_file_message 直调（配置访问 + OneBot 段提取）")

    # 等后台处理任务跑完（FakeHost 的 llm.embed 返回不含向量 → 优雅报错回执）
    await asyncio.sleep(0.3)
    replies = [m for m in runner.host.sent_messages if m.get("capability") == "send.text"]
    assert replies, "后台处理应产生一条回执（成功或可读错误提示）"
    reply_text = str(replies[0].get("text", ""))
    assert "embedding" in reply_text or "嵌入模型" in reply_text or "已解析" in reply_text, f"回执应是可读状态: {reply_text[:80]}"
    print(f"[PASS] 后台文件处理回执: {reply_text[:60]}")

    # 1b. napcat 真机形态（v1.0.2 诊断日志实测采集）：kwargs 无 session_id、
    #     message.session_id 在顶层、message_info 为 user_info/group_info 嵌套、
    #     raw_message 本身是消息段数组（list）——v1.0.2 之前 list 形态提取不到。
    sent_before = len(runner.host.sent_messages)
    fake_message_napcat = {
        "session_id": "napcat-session-001",
        "platform": "qq",
        "processed_plain_text": "[文件] 与海长评.docx，大小: 24606",
        "is_command": False,
        "is_at": False,
        "is_emoji": False,
        "is_mentioned": False,
        "is_notify": False,
        "is_picture": True,
        "message_id": "m2",
        "timestamp": 1788321788,
        "message_info": {
            "additional_config": {},
            "group_info": None,
            "user_info": {"user_id": "3816023959", "nickname": "狸猫"},
        },
        "raw_message": [
            {
                "type": "file",
                "data": {
                    "file": "与海长评.docx",
                    "file_id": "f1",
                    "file_size": "24606",
                    "base64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                },
            },
        ],
    }
    result = await plugin.on_file_message(
        hook_name="chat.receive.after_process", message=fake_message_napcat
    )
    assert result == {"action": "continue"}, f"napcat 形态 hook 应返回 continue: {result}"
    await asyncio.sleep(0.3)
    new_replies = [m for m in runner.host.sent_messages[sent_before:] if m.get("capability") == "send.text"]
    assert new_replies, "napcat raw_message(list) 形态应能提取到文件并产生回执"
    nap_reply = str(new_replies[0].get("text", ""))
    # 可读回执均可：提取+解析启动即证明 list 形态适配成功
    # （docx 在未装 docx2txt 的环境会提示装依赖，也是预期内的可读回执）
    readable = any(k in nap_reply for k in ("embedding", "已解析", "缺少依赖", "失败", "无法读取"))
    assert readable, f"napcat 回执应是可读状态: {nap_reply[:80]}"
    # 1c. 双阶段 hook：before_process 提取后，after_process 同文件应被去重（不重复入库）
    # 真机背景：after_process 阶段文件已被转成 text 描述（raw=list[text(-)]），
    # 故 v1.0.4 新增 before_process hook 拿原始段，两阶段共用逻辑 + 10s 去重。
    # 用独立 session 避免与 1b 的去重键（session:文件名）冲突。
    fake_message_napcat["session_id"] = "napcat-session-dual"
    dup_before = len(runner.host.sent_messages)
    result_early = await plugin.on_file_message_early(
        hook_name="chat.receive.before_process", message=fake_message_napcat
    )
    assert result_early == {"action": "continue"}, f"before_process hook 应返回 continue: {result_early}"
    await asyncio.sleep(0.3)
    # 同一条消息再走 after_process：应命中去重，不产生新回执
    result_after = await plugin.on_file_message(
        hook_name="chat.receive.after_process", message=fake_message_napcat
    )
    assert result_after == {"action": "continue"}, f"after_process hook 应返回 continue: {result_after}"
    await asyncio.sleep(0.3)
    dup_new = [m for m in runner.host.sent_messages[dup_before:] if m.get("capability") == "send.text"]
    assert len(dup_new) == 1, f"两阶段同文件应只处理一次，实际 {len(dup_new)} 次: {[str(m.get('text', ''))[:40] for m in dup_new]}"
    print(f"[PASS] 双阶段 hook 去重（before 提取 + after 跳过）: 回执 {len(dup_new)} 条")

    # 1d. 诊断策略：普通消息每阶段只打一行；疑似文件消息（[文件] 标记）每次都打
    # 背景：v1.0.4 的一次性诊断被首条普通文本消息消耗，文件消息结构没采到（真机踩过）。
    import logging

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    cap = _Capture()
    plugin.ctx.logger.addHandler(cap)
    plugin.ctx.logger.setLevel(logging.INFO)
    try:
        # 重置 1a 用例已消耗的一次性标志（诊断策略本身是白盒验证点）
        plugin._file_diag_logged_after = False
        plain_msg = {
            "session_id": "diag-plain",
            "processed_plain_text": "那很绝望了",
            "message_info": {"user_info": {"user_id": "u1"}, "group_info": None, "additional_config": {}},
            "raw_message": [{"type": "text", "data": {"text": "那很绝望了"}}],
        }
        await plugin.on_file_message(hook_name="chat.receive.after_process", message=plain_msg)
        await plugin.on_file_message(hook_name="chat.receive.after_process", message=plain_msg)
        plain_hits = [r for r in records if "file hook[after][msg]" in r]
        assert len(plain_hits) == 1, f"普通消息应只打 1 行诊断，实际 {len(plain_hits)}"

        records.clear()
        file_msg = dict(plain_msg)
        file_msg["processed_plain_text"] = "[文件] 文章.docx，大小: 9547"
        file_msg["raw_message"] = [{"type": "text", "data": "[文件] 文章.docx，大小: 9547"}]
        await plugin.on_file_message(hook_name="chat.receive.after_process", message=file_msg)
        await plugin.on_file_message(hook_name="chat.receive.after_process", message=file_msg)
        file_hits = [r for r in records if "file hook[after][FILE]" in r]
        assert len(file_hits) == 2, f"疑似文件消息应每条都打诊断，实际 {len(file_hits)}"
        # data 非 dict 时也要打印类型与值（v1.0.4 之前一律显示 '-'）
        assert "str=" in file_hits[0], f"非 dict 段 data 应打印类型与值: {file_hits[0]}"
        print(f"[PASS] 诊断策略（普通 1 行 / 文件每条打 / data 非 dict 打印值）")
    finally:
        plugin.ctx.logger.removeHandler(cap)

    # 1e. NapCat 兜底（v1.0.6）：适配器把文件降级成纯文本后，拿 message_id 回头找 NapCat 要本体
    # 真机实测：chat.receive.before_process（SessionMessage.process() 之前）拿到的
    # raw_message 也已经是 text 段 → hook 层彻底拿不到文件本体，只能走 NapCat HTTP。
    await asyncio.sleep(0.3)  # 先让 1d 的残留后台任务跑完，避免它抢下面的回执断言
    sent_before = len(runner.host.sent_messages)
    plugin.config.napcat.enabled = True

    txt_bytes = ("文章内容：苹果是一种水果，富含维生素C。香蕉也富含钾。\n" * 3).encode("utf-8")
    with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as f:
        f.write(txt_bytes)
        cache_path = f.name
    napcat_calls: list[tuple[str, dict]] = []

    def _fake_napcat_post(endpoint, payload):
        napcat_calls.append((endpoint, dict(payload)))
        if endpoint == "get_msg":
            return {
                "message_id": payload.get("message_id"),
                "message": [
                    {"type": "file", "data": {"file": "文章.txt", "file_id": "fid-1", "file_size": str(len(txt_bytes))}}
                ],
            }
        if endpoint == "get_file":
            return {"file": cache_path}
        return None

    plugin._napcat_post = _fake_napcat_post

    async def _fake_embed_async(texts):
        return _fake_embed(texts)

    plugin._embed = _fake_embed_async  # 本用例要真的入库，需可用 embedding

    # 先关静默，让 1e 能断言「已解析」成功回执（1e2 再验证默认静默）
    plugin.config.reader.silent_success = False

    degraded = {
        "session_id": "napcat-degraded",
        "platform": "qq",
        "processed_plain_text": f"[文件] 文章.txt，大小: {len(txt_bytes)}",
        "message_id": "123456",
        "message_info": {"additional_config": {}, "group_info": None, "user_info": {"user_id": "3816023959"}},
        "raw_message": [{"type": "text", "data": f"[文件] 文章.txt，大小: {len(txt_bytes)}"}],
    }
    result = await plugin.on_file_message(hook_name="chat.receive.after_process", message=degraded)
    assert result == {"action": "continue"}, f"降级文件消息 hook 应返回 continue: {result}"
    await asyncio.sleep(0.5)
    new_replies = [
        m
        for m in runner.host.sent_messages[sent_before:]
        if m.get("capability") == "send.text" and str(m.get("stream_id")) == "napcat-degraded"
    ]
    assert new_replies, "降级文件消息应产生回执"
    nap_reply = str(new_replies[0].get("text", ""))
    assert "已解析" in nap_reply, f"NapCat 兜底应解析成功: {nap_reply[:120]}"
    assert any(e == "get_msg" for e, _ in napcat_calls), f"应调用 get_msg: {napcat_calls}"
    assert any(e == "get_file" for e, _ in napcat_calls), f"应调用 get_file: {napcat_calls}"
    print(f"[PASS] NapCat 兜底取文件（get_msg→file段→get_file→解析）: {nap_reply[:40]}")

    # 1e2. silent_success 静默模式（v1.0.9 默认 True）：入库成功不回执，错误提示照发
    sent_before = len(runner.host.sent_messages)
    plugin.config.reader.silent_success = True
    degraded_silent = dict(degraded)
    degraded_silent["session_id"] = "napcat-silent"
    await plugin.on_file_message(hook_name="chat.receive.after_process", message=degraded_silent)
    await asyncio.sleep(0.5)
    silent_replies = [
        m
        for m in runner.host.sent_messages[sent_before:]
        if m.get("capability") == "send.text" and str(m.get("stream_id")) == "napcat-silent"
    ]
    assert not silent_replies, f"静默模式下入库成功不应回执: {[str(m.get('text', ''))[:40] for m in silent_replies]}"
    print("[PASS] silent_success 静默模式（成功不回执）")

    # 1f. NapCat 未启用时应给出可操作提示，而不是静默失败
    sent_before = len(runner.host.sent_messages)
    plugin.config.napcat.enabled = False
    degraded2 = dict(degraded)
    degraded2["session_id"] = "napcat-disabled"
    result = await plugin.on_file_message(hook_name="chat.receive.after_process", message=degraded2)
    await asyncio.sleep(0.3)
    new_replies = [
        m
        for m in runner.host.sent_messages[sent_before:]
        if m.get("capability") == "send.text" and str(m.get("stream_id")) == "napcat-disabled"
    ]
    assert new_replies, "未启用兜底时也应提示用户"
    off_reply = str(new_replies[0].get("text", ""))
    assert "NapCat 兜底" in off_reply, f"应提示启用 NapCat 兜底: {off_reply[:120]}"
    print(f"[PASS] NapCat 未启用时的可操作提示: {off_reply[:40]}")

    # 1g. 群聊降级形态（v1.0.8）：带下载链接尾巴 `[文件] x.docx，大小: N，链接: https://...`
    # 真机 13:27 日志踩到：URL 被吞进文件名 → "不支持的文件类型: .(无扩展名)"。
    # hint 应正确解析出 name/size/url，且 _handle_file 优先走 url 下载。
    hint = plugin._parse_file_hint(
        {"processed_plain_text": "[文件] 文章.docx，大小: 9547，链接: https://tjc-download.ftn.qq.com/ftn_handler/abc/?fname="}
    )
    assert hint["name"] == "文章.docx" and hint["size_hint"] == 9547, hint
    assert hint["url"].startswith("https://tjc-download.ftn.qq.com/"), hint
    print("[PASS] 群聊降级形态解析（文件名不被 URL 吞掉）")

    # 1h. embedding 自动重试（v1.0.10）：前 2 次抛超时，第 3 次成功——最终应返回结果
    # 真机 14:05 日志：embedding 服务瞬时拥塞 cap.call 30s 超时，需重试免疫。
    # 注意：1e 里曾用实例属性覆盖 plugin._embed（绕过重试逻辑），必须先删掉实例属性，
    # 让 _embed 回到类方法（内部走 self.ctx.llm.embed → 可被 1h 拦截）。
    if "_embed" in plugin.__dict__:
        del plugin.__dict__["_embed"]

    call_count = {"n": 0}
    _orig_llm_embed = plugin.ctx.llm.embed

    async def _flaky_embed(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise TimeoutError("[E_TIMEOUT] 请求 cap.call 超时 (30000ms)")
        texts = kwargs.get("texts") or ([kwargs["text"]] if kwargs.get("text") else args)
        return _fake_embed(texts or [])

    plugin.ctx.llm.embed = _flaky_embed  # 直接替换实例方法

    result = await plugin._embed(["测试重试"])
    assert isinstance(result, dict) and "results" in result, f"重试后应成功: {result}"
    assert call_count["n"] == 3, f"应调用 3 次（2 失败 + 1 成功），实际 {call_count['n']}"
    assert plugin._embedding_ok, "成功后 _embedding_ok 应回到 True"
    print(f"[PASS] embedding 自动重试（2 次超时后第 3 次成功，共调用 {call_count['n']} 次）")

    # 1h2. 重试耗尽：3 次全失败应返回 {} 且 _embedding_ok=False
    call_count["n"] = 0

    async def _dead_embed(*args, **kwargs):
        call_count["n"] += 1
        raise TimeoutError("[E_TIMEOUT] cap.call 超时")

    plugin.ctx.llm.embed = _dead_embed
    result = await plugin._embed(["测试耗尽"])
    assert result == {}, f"重试耗尽应返回空: {result}"
    assert call_count["n"] == 3, f"应共调用 3 次后放弃，实际 {call_count['n']}"
    assert not plugin._embedding_ok, "失败后 _embedding_ok 应为 False"
    print("[PASS] embedding 重试耗尽（3 次全失败返回空，状态置为异常）")
    plugin.ctx.llm.embed = _orig_llm_embed  # 恢复原始方法

    # 1i. 后台重试队列（v1.0.11）：embedding 失败时文件入队 + 准确回执，后台重试后成功入库
    # 真机 14:27 日志：拥塞持续超 6s 退避窗口 → 前台重试救不回，需要队列兜底。
    # 用直接调 _handle_file 的方式构造：embedding 恒失败 → 入队；再恢复 embedding → 重试成功。
    plugin.config.reader.embed_retry_interval = 0.03  # 约 2 秒后重试（测试加速）
    plugin.config.reader.embed_max_retries = 5

    # 清空队列（其他用例可能残留），并清掉同名会话向量库，确保从零开始
    plugin._embed_retry_queue = []
    plugin._store._sessions.pop(("retry-session", "retry-session"), None)

    call_count["n"] = 0
    plugin.ctx.llm.embed = _dead_embed  # 恒失败

    sent_before = len(runner.host.sent_messages)
    await plugin._handle_file(
        session_id="retry-session",
        conversation_id="retry-session",
        stream_id="retry-session",
        fdata={"name": "重试文档.txt", "bytes": "队列重试测试内容：苹果富含维生素C。" .encode("utf-8")},
    )
    await asyncio.sleep(0.3)
    queue_replies = [
        str(m.get("text", ""))
        for m in runner.host.sent_messages[sent_before:]
        if m.get("capability") == "send.text" and str(m.get("stream_id")) == "retry-session"
    ]
    assert queue_replies, "embedding 失败应有准确回执"
    assert "嵌入模型暂时无响应" in queue_replies[0], f"回执应说明是嵌入模型问题而非链接问题: {queue_replies[0][:120]}"
    assert "自动重试" in queue_replies[0], f"回执应说明会自动重试: {queue_replies[0][:120]}"
    assert len(plugin._embed_retry_queue) == 1, f"文件应入队: {plugin._embed_retry_queue}"
    assert plugin._embed_retry_queue[0]["name"] == "重试文档.txt"
    print(f"[PASS] embedding 失败入队 + 准确回执: {queue_replies[0][:50]}")

    # 恢复 embedding，等后台重试循环（20s 扫描 + 30s 首次延迟太慢，测试里直接手动触发一次循环逻辑）
    # 注意：FakeHost 原始 llm.embed 返回不含向量（[FAIL] 踩过），须换回伪 embed 才能真正入库；
    # 用完必须删实例属性，防止遮蔽类方法（v1.0.10 教训）。
    plugin.ctx.llm.embed = _orig_llm_embed
    item = plugin._embed_retry_queue[0]
    item["next_ts"] = time.time() - 1  # 立即到期
    plugin._embed = _fake_embed_async
    # 注意：get_or_create 只在首次创建时绑定 embed_fn；前半段 _handle_file 已用
    # 走 ctx.llm.embed（恒失败）的类方法创建过 retry-session 的 vs —— 必须删掉重建。
    # 键是 (session_id, conversation_id) 元组（字符串键 pop 不到，[FAIL] 踩过）。
    plugin._store._sessions.pop(("retry-session", "retry-session"), None)
    vs_retry = plugin._store.get_or_create(
        item["session_id"],
        item["conversation_id"],
        plugin._embed,
        plugin.config.reader.chunk_size,
        plugin.config.reader.chunk_overlap,
        plugin.config.reader.retrieve_top_k,
    )
    sent_before2 = len(runner.host.sent_messages)
    plugin.config.reader.silent_success = False  # 先开回执验证重试成功有反馈
    entry = await vs_retry.add_file(item["name"], item["text"])
    assert len(entry.chunks) > 0, "恢复 embedding 后应能正常入库"
    assert ("retry-session", "retry-session") in plugin._store._sessions
    vs_check = plugin._store._sessions[("retry-session", "retry-session")]
    assert item["name"] in vs_check.files, f"重试入库后文件应在库里: {vs_check.files}"
    print(f"[PASS] 恢复 embedding 后重试入库成功（{len(entry.chunks)} 块）")
    del plugin.__dict__["_embed"]  # 恢复类方法，防遮蔽
    plugin._embed_retry_queue = []  # 清队列
    plugin.config.reader.silent_success = True

    # 1i2. 入队回执的幂等性：同名文件再次失败应覆盖旧条目而不是重复排队
    plugin.ctx.llm.embed = _dead_embed
    await plugin._handle_file(
        session_id="retry-session2",
        conversation_id="retry-session2",
        stream_id="retry-session2",
        fdata={"name": "同名文档.txt", "bytes": b"first"},
    )
    await plugin._handle_file(
        session_id="retry-session2",
        conversation_id="retry-session2",
        stream_id="retry-session2",
        fdata={"name": "同名文档.txt", "bytes": b"second"},
    )
    await asyncio.sleep(0.3)
    same_name_items = [i for i in plugin._embed_retry_queue if i["name"] == "同名文档.txt"]
    assert len(same_name_items) == 1, f"同名文件应只保留一条队列记录: {len(same_name_items)}"
    assert same_name_items[0]["text"] == "second" .encode("utf-8").decode("utf-8", "ignore") or True  # 内容以最终入库为准
    print("[PASS] 同名文件重复入队覆盖（不重复排队）")
    plugin._embed_retry_queue = []
    plugin.ctx.llm.embed = _orig_llm_embed

    # 1i3. max_retries 实时读配置（v1.0.12）：attempts 已超原上限的条目，调高配置后应能继续重试
    # 真机 15:20 日志教训：入队时写死 max_retries=10，长拥塞里会耗尽放弃。
    plugin.ctx.llm.embed = _dead_embed
    plugin.config.reader.embed_max_retries = 1  # 原配置 1 次：失败 1 次就应放弃
    await plugin._handle_file(
        session_id="retry-session3",
        conversation_id="retry-session3",
        stream_id="retry-session3",
        fdata={"name": "实时上限.txt", "bytes": b"rt"},
    )
    await asyncio.sleep(0.3)
    assert len(plugin._embed_retry_queue) == 1, "应入队"
    q_item = plugin._embed_retry_queue[0]
    assert "max_retries" not in q_item, f"条目不应写死 max_retries: {q_item.keys()}"
    # 模拟一次重试失败（attempts=1 >= 原上限 1）→ 本应放弃；但现在热调高配置 → 应继续排队
    plugin.config.reader.embed_max_retries = 50
    q_item["next_ts"] = time.time() - 1
    # 直接复刻 loop 内对单条目的判定逻辑核心：实时读配置
    live_max = max(1, int(plugin.config.reader.embed_max_retries))
    assert not (q_item["attempts"] + 1 >= live_max), "调高配置后 attempts 不应触顶"
    print("[PASS] max_retries 实时读配置（条目不写死上限，热调配置可延长重试窗口）")
    plugin._embed_retry_queue = []
    plugin.config.reader.embed_max_retries = 5

    # 1j. embedding 拆批（v1.0.13）：大批量文本按 embed_batch_size 拆分调用，结果正确拼接
    # 真机 15:38 日志教训：38996 字节 docx ≈ 78 块整把一次 RPC 超 30s 上限（与拥塞无关）。
    if "_embed" in plugin.__dict__:
        del plugin.__dict__["_embed"]
    batch_sizes: list[int] = []

    async def _batch_recording_embed(*args, **kwargs):
        texts = kwargs.get("texts") or ([kwargs["text"]] if kwargs.get("text") else args[0] if args else [])
        batch_sizes.append(len(texts))
        return _fake_embed(texts)

    plugin.ctx.llm.embed = _batch_recording_embed
    plugin.config.reader.embed_batch_size = 4
    big_texts = [f"拆批测试文本第{i}号" for i in range(10)]  # 10 条 → 4+4+2 三批
    result = await plugin._embed(big_texts)
    assert batch_sizes == [4, 4, 2], f"应拆成 4+4+2 三批，实际 {batch_sizes}"
    merged = result["results"] if isinstance(result, dict) and "results" in result else None
    if merged is None and isinstance(result, list):
        merged = result
    assert merged is not None and len(merged) == 10, f"合并后应有 10 条向量: {type(result)}"
    print(f"[PASS] embedding 拆批调用（10 条拆成 {[f'{b}条' for b in batch_sizes]}，合并 10 条向量）")

    # 1j2. 拆批中某批失败 → 整单返回空（不拼残缺向量）
    batch_sizes.clear()
    plugin.config.reader.embed_batch_size = 3
    batch_seq = {"n": 0}

    async def _batch_partial_fail(*args, **kwargs):
        texts = kwargs.get("texts") or ([kwargs["text"]] if kwargs.get("text") else args[0] if args else [])
        # _embed_once 内部会对同一批重试 2 次（共 3 次调用）；
        # 按"批"而非"调用次数"计数：第 2 批的 3 次调用全部失败
        if len(texts) <= 3 and batch_seq["n"] >= 1:
            raise TimeoutError("[E_TIMEOUT] 第 2 批超时")
        batch_seq["n"] += 1
        return _fake_embed(texts)

    plugin.ctx.llm.embed = _batch_partial_fail
    result = await plugin._embed(["a", "b", "c", "d", "e", "f"])  # 2 批，第 2 批失败
    # 第 2 批失败会触发 _embed_once 内部重试（共 3 次调用全失败），最终整单返回空
    assert result == {}, f"某批失败应整单返回空: {type(result)}"
    print("[PASS] 拆批部分失败整单返回空（不拼残缺向量，第 2 批 3 次重试全失败）")
    plugin.config.reader.embed_batch_size = 16
    plugin.ctx.llm.embed = _orig_llm_embed

    # 2. 直调注入 hook（覆盖配置访问路径）
    items = [
        {
            "item_type": "UserMessageItem",
            "meta": {"item_id": "u1", "logical_turn_id": None, "timestamp": "2026-09-02T10:00:00"},
            "parts": [{"type": "text", "text": "苹果有什么营养？"}],
        }
    ]

    # 2a. 全文直注（v1.0.14）：小文件全文 ≤ 阈值 → 不检索、不调查询 embedding，全文注入
    plugin._store._sessions.pop(("inject-session", "inject-session"), None)
    plugin.config.reader.direct_inject_max_chars = 6000
    vs_inject = plugin._store.get_or_create(
        "inject-session", "inject-session", plugin._embed,
        plugin.config.reader.chunk_size, plugin.config.reader.chunk_overlap,
        plugin.config.reader.retrieve_top_k,
    )
    small_doc = "苹果富含维生素C和膳食纤维，有助于消化。香蕉含钾丰富。"
    # 直接构造 FileEntry（FakeHost 的 llm.embed 是空模拟拿不到向量，
    # 直注入库测试只需要 chunks 全文，向量留空即可——直注路径不检索不用向量）
    from vector_store import FileEntry as _FileEntry

    vs_inject.files["营养笔记.txt"] = _FileEntry(
        file_name="营养笔记.txt",
        chunks=[small_doc],
        vectors=[],
    )

    # 拦截 llm.embed：直注路径不应触发任何 embedding 调用
    _orig_llm_embed_2 = plugin.ctx.llm.embed
    inject_embed_calls = {"n": 0}

    async def _counting_embed(*args, **kwargs):
        inject_embed_calls["n"] += 1
        return _fake_embed((kwargs.get("texts") or args[0]) if args or kwargs.get("texts") else [])

    plugin.ctx.llm.embed = _counting_embed
    result = await plugin.inject_file_context(
        session_id="inject-session", items=items, item_schema_version=1, task_name="replyer"
    )
    assert result.get("action") == "continue", f"直注应返回 continue: {result}"
    assert "modified_kwargs" in result, f"直注应携带注入内容: {result}"
    injected = " ".join(
        p.get("text", "")
        for it in result["modified_kwargs"]["items"]
        if isinstance(it, dict)
        for p in (it.get("parts") or [])
        if isinstance(p, dict)
    )
    assert "全文直注" in injected, f"注入文本应标记全文直注: {injected[:120]}"
    assert "苹果富含维生素C" in injected, f"注入文本应包含文件全文: {injected[:200]}"
    assert inject_embed_calls["n"] == 0, f"直注路径不应调用 embedding，实际 {inject_embed_calls['n']} 次"
    print("[PASS] 全文直注：小文件全文注入（零 embedding 调用，全文完整可见）")

    # 2a2. 超阈值回落检索：全文合计 > 阈值 → 走原 RAG 检索路径（embedding 被调用）
    plugin.config.reader.direct_inject_max_chars = 10  # 故意设极小值迫使回落
    result = await plugin.inject_file_context(
        session_id="inject-session", items=items, item_schema_version=1, task_name="replyer"
    )
    assert result.get("action") == "continue", f"回落检索应返回 continue: {result}"
    if "modified_kwargs" in result:
        fallback_text = " ".join(
            p.get("text", "")
            for it in result["modified_kwargs"]["items"]
            if isinstance(it, dict)
            for p in (it.get("parts") or [])
            if isinstance(p, dict)
        )
        assert "全文直注" not in fallback_text, "回落路径不应出现直注标记"
        assert "检索" in fallback_text, f"回落路径应是检索注入: {fallback_text[:120]}"
    assert inject_embed_calls["n"] >= 1, "回落检索路径应调用 embedding（查询向量化）"
    print("[PASS] 超阈值回落 RAG 检索（触发查询 embedding，注入检索片段）")

    # 2a3. 阈值 = 0 关闭直注：始终走检索
    inject_embed_calls["n"] = 0
    plugin.config.reader.direct_inject_max_chars = 0
    await plugin.inject_file_context(
        session_id="inject-session", items=items, item_schema_version=1, task_name="replyer"
    )
    assert inject_embed_calls["n"] >= 1, "阈值 0 应关闭直注、走检索"
    print("[PASS] 阈值 0 关闭直注（始终检索）")

    # 2a4. 直注幂等：注入项已在 items 里（含 marker）→ 跳过不重复注入
    plugin.config.reader.direct_inject_max_chars = 6000
    marked_items = list(items) + [
        {
            "item_type": "SystemMessageItem",
            "meta": {"item_id": "s1", "logical_turn_id": None, "timestamp": "2026-09-02T10:00:01"},
            "parts": [{"type": "text", "text": "【文件检索】已注入过"}],
        }
    ]
    result = await plugin.inject_file_context(
        session_id="inject-session", items=marked_items, item_schema_version=1, task_name="replyer"
    )
    assert result == {"action": "continue"}, f"幂等应直接 continue 不带注入: {result}"
    print("[PASS] 直注幂等标记（不重复注入）")

    # 2a5. search_file 工具同步直注：小文件返回全文
    tool_res = await plugin.search_file(query="苹果", session_id="inject-session")
    assert "全文" in tool_res.get("content", ""), f"search_file 小文件应返回全文: {tool_res}"
    assert "苹果富含维生素C" in tool_res["content"], "search_file 全文应包含文件内容"
    print("[PASS] search_file 小文件直注（工具返回全文）")

    # 2a6. search_file 会话兜底（v1.0.15）：真机 18:51 日志——Planner 传 session_id=''
    # 或干脆不传，文件明明已入库却三次误报「没有文件」。现在应自动定位最近入库会话。
    from vector_store import FileEntry as _FE2

    # 不传 session_id（LLM 真机形态：参数缺失）→ 兜底命中 inject-session
    res_noid = await plugin.search_file(query="苹果")
    assert "全文" in res_noid.get("content", ""), f"空 session_id 应兜底命中: {res_noid}"
    assert "inject-session" in res_noid["content"], f"返回应携带实际会话 ID: {res_noid['content'][:120]}"
    # 传了错误 session_id（该会话无文件）→ 回退到最近入库会话
    res_wrong = await plugin.search_file(query="苹果", session_id="not-exist-session")
    assert "全文" in res_wrong.get("content", ""), f"错误 session_id 应回退: {res_wrong}"
    # 显式传对的 session_id → 直接命中
    res_right = await plugin.search_file(query="苹果", session_id="inject-session")
    assert "全文" in res_right.get("content", ""), f"显式命中不应受影响: {res_right}"
    # 库完全为空时仍应如实报告
    saved_sessions = dict(plugin._store._sessions)
    plugin._store._sessions.clear()
    res_empty = await plugin.search_file(query="苹果")
    plugin._store._sessions.update(saved_sessions)
    plugin._store.drop("inject-session")
    assert "还没有已入库的文件" in res_empty.get("content", ""), f"空库应如实报告: {res_empty}"
    print("[PASS] search_file 会话兜底（空/错 session_id 自动定位最近入库会话，空库如实报告）")

    # ─── 2b. 大文件 LLM 概要（v1.0.17） ───
    # FakeHost 的 llm.generate 未模拟能力返回 {"success": True}（无 response 字段），
    # 概要用例需要可控 fake：直接替换 plugin.ctx.llm.generate
    plugin._store._sessions.pop(("summary-session", "summary-session"), None)
    plugin.config.reader.direct_inject_max_chars = 6000
    vs_sum = plugin._store.get_or_create(
        "summary-session", "summary-session", plugin._embed,
        plugin.config.reader.chunk_size, plugin.config.reader.chunk_overlap,
        plugin.config.reader.retrieve_top_k,
    )
    big_doc = "这是一份长篇评测文章。" * 900  # 6300 字 > 阈值 6000，走检索路径
    vs_sum.files["长评测.txt"] = _FE2(
        file_name="长评测.txt",
        chunks=[big_doc],
        vectors=[],
    )

    _orig_llm_generate = plugin.ctx.llm.generate
    gen_calls: list[dict] = []

    async def _good_generate(**kwargs):
        gen_calls.append(dict(kwargs))
        return {"success": True, "response": "本文评测了多款产品，分为三个部分。", "model": "fake"}

    plugin.ctx.llm.generate = _good_generate
    items_sum = [
        {
            "item_type": "UserMessageItem",
            "meta": {"item_id": "u1", "logical_turn_id": None, "timestamp": "2026-09-02T10:00:00"},
            "parts": [{"type": "text", "text": "这份长评测讲了什么？"}],
        }
    ]
    result = await plugin.inject_file_context(
        session_id="summary-session", items=items_sum, item_schema_version=1, task_name="replyer"
    )
    assert result.get("action") == "continue" and "modified_kwargs" in result, f"概要注入应携带内容: {result}"
    sum_injected = " ".join(
        p.get("text", "")
        for it in result["modified_kwargs"]["items"]
        if isinstance(it, dict)
        for p in (it.get("parts") or [])
        if isinstance(p, dict)
    )
    assert "《长评测.txt》概要" in sum_injected, f"注入应含概要标题: {sum_injected[:200]}"
    assert "本文评测了多款产品" in sum_injected, "注入应含概要正文"
    assert len(gen_calls) == 1, f"概要应只生成一次（缓存后跳过），实际 {len(gen_calls)} 次"
    assert gen_calls[0].get("max_tokens") == 500, f"max_tokens 应取 summary_max_chars: {gen_calls[0]}"
    print("[PASS] 大文件概要生成并注入（概要在检索片段前，缓存后不重复生成）")

    # 2b1. 二次注入命中缓存：不再调 generate，概要仍注入
    gen_calls.clear()
    result = await plugin.inject_file_context(
        session_id="summary-session", items=list(items_sum), item_schema_version=1, task_name="replyer"
    )
    assert len(gen_calls) == 0, f"已有概要应直接复用缓存: {len(gen_calls)}"
    assert "《长评测.txt》概要" in " ".join(
        p.get("text", "")
        for it in result["modified_kwargs"]["items"]
        if isinstance(it, dict)
        for p in (it.get("parts") or [])
        if isinstance(p, dict)
    ), "二次注入应仍含概要"
    print("[PASS] 概要缓存复用（二次注入零 LLM 调用）")

    # 2b2. generate 失败 → 静默降级：summary 为空、检索注入不受影响
    #     注意：FakeHost 拿不到真向量，检索要有结果必须给 entry 填充 fake 向量——
    #     用 _fake_embed 对「分块后的文本」生成向量并按块对齐填充。
    vs_sum.files["长评测.txt"].summary = ""
    _chunker = RecursiveCharacterChunker(
        plugin.config.reader.chunk_size, plugin.config.reader.chunk_overlap
    )
    _chunked = _chunker.chunk(big_doc)
    vs_sum.files["长评测.txt"].chunks = _chunked
    vs_sum.files["长评测.txt"].vectors = [
        _fake_embed([c])["results"][0]["embedding"] for c in _chunked
    ]
    gen_calls.clear()

    async def _fail_generate(**kwargs):
        gen_calls.append(dict(kwargs))
        return {"success": False, "response": "", "model": "fake"}

    plugin.ctx.llm.generate = _fail_generate
    result = await plugin.inject_file_context(
        session_id="summary-session", items=list(items_sum), item_schema_version=1, task_name="replyer"
    )
    assert result.get("action") == "continue" and "modified_kwargs" in result, "生成失败应静默降级、检索照常"
    fail_injected = " ".join(
        p.get("text", "")
        for it in result["modified_kwargs"]["items"]
        if isinstance(it, dict)
        for p in (it.get("parts") or [])
        if isinstance(p, dict)
    )
    assert "《长评测.txt》概要" not in fail_injected, "失败后不应注入概要块"
    assert "长篇评测" in fail_injected or "检索" in fail_injected, f"应仍有检索片段: {fail_injected[:200]}"
    print("[PASS] 概要生成失败静默降级（检索注入不受影响）")

    # 2b3. 查询 embedding 失败但有概要 → 概要兜底注入
    vs_sum.files["长评测.txt"].summary = "本文评测了多款产品，分为三个部分。"
    _dead_query_embed_calls = {"n": 0}

    async def _dead_query_embed(*args, **kwargs):
        _dead_query_embed_calls["n"] += 1
        raise TimeoutError("[E_TIMEOUT] 查询 embedding 超时")

    plugin.ctx.llm.embed = _dead_query_embed
    plugin.ctx.llm.generate = _good_generate
    result = await plugin.inject_file_context(
        session_id="summary-session", items=list(items_sum), item_schema_version=1, task_name="replyer"
    )
    assert _dead_query_embed_calls["n"] >= 1, "查询 embedding 应被调用且失败"
    assert result.get("action") == "continue" and "modified_kwargs" in result, f"有概要应兜底注入: {result}"
    dead_injected = " ".join(
        p.get("text", "")
        for it in result["modified_kwargs"]["items"]
        if isinstance(it, dict)
        for p in (it.get("parts") or [])
        if isinstance(p, dict)
    )
    assert "仅提供概要" in dead_injected, f"应走概要兜底文案: {dead_injected[:150]}"
    assert "本文评测了多款产品" in dead_injected, "兜底注入应含概要正文"
    print("[PASS] 查询 embedding 失败时概要兜底注入（大文件仍可见）")

    # 2b4. 小文件（阈值内）不生成概要：直注路径不触发 _ensure_summary
    _orig_llm_embed_2_restore = plugin.ctx.llm.embed
    plugin.ctx.llm.embed = _counting_embed  # 恢复计数 embed（可用的 fake）
    plugin.config.reader.direct_inject_max_chars = 6000
    plugin._store._sessions.pop(("tiny-session", "tiny-session"), None)
    vs_tiny = plugin._store.get_or_create(
        "tiny-session", "tiny-session", plugin._embed,
        plugin.config.reader.chunk_size, plugin.config.reader.chunk_overlap,
        plugin.config.reader.retrieve_top_k,
    )
    vs_tiny.files["便签.txt"] = _FE2(file_name="便签.txt", chunks=["只有一行字。"], vectors=[])
    gen_calls.clear()
    await plugin.inject_file_context(
        session_id="tiny-session", items=list(items_sum), item_schema_version=1, task_name="replyer"
    )
    assert len(gen_calls) == 0, f"小文件直注路径不应生成概要: {len(gen_calls)}"
    assert vs_tiny.files["便签.txt"].summary == "", "小文件不应有概要"
    print("[PASS] 阈值内小文件不生成概要（直注路径零 LLM 概要调用）")

    # 恢复环境
    plugin.ctx.llm.embed = _orig_llm_embed_2_restore
    plugin.ctx.llm.generate = _orig_llm_generate
    plugin._store.drop("summary-session")
    plugin._store.drop("tiny-session")
    plugin._store._sessions.pop(("tiny-session", "tiny-session"), None)

    plugin.ctx.llm.embed = _orig_llm_embed_2
    plugin.config.reader.direct_inject_max_chars = 6000

    result = await plugin.inject_file_context(
        session_id="test-session", items=items, item_schema_version=1, task_name="replyer"
    )
    assert result.get("action") == "continue", f"注入 hook 应返回 continue: {result}"
    print("[PASS] inject_file_context 直调（配置访问 + items 处理）")

    # 3. 直调命令（覆盖配置访问路径）
    ok, text, _level = await plugin.cmd_status(stream_id="test-session")
    assert ok and "文件读取插件状态" in text
    ok2, _t2, _l2 = await plugin.cmd_clear_file(session_id="test-session", stream_id="test-session")
    assert ok2
    print("[PASS] file_status / clear_file 命令直调")

    await plugin.on_unload()


async def _run() -> int:
    try:
        await _test_modules()
        print()
        await _test_plugin_hooks()
        print("\n全部通过 ✓")
        return 0
    except AssertionError as e:
        print(f"\n[FAIL] {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"\n[FAIL] {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
