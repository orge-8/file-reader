"""MaiBot 文件读取插件（File Reader）—— 入口文件

参考 astrbot_plugin_file_reader_pro 移植，架构对齐：
  文件 → 解析(file_parser) → 递归分块(chunker) → 向量化(llm.embed) → 会话级向量库(vector_store)
  提问时 → 语义检索 Top-K → 注入 LLM 上下文(before_model_request hook)

与 AstrBot 版的差异（MaiBot 无内置 FaissVecDB / RerankProvider）：
  - 向量库用 numpy 余弦相似度自实现（vector_store.py）
  - 注入走 `maisaka.replyer.before_model_request`，传 items 而非 messages
  - 文件内容从入站消息的 file/attachment 段提取（chat.receive.after_process）

铁律遵循：入口文件不写 `from __future__ import annotations`（见 skill runtime-gotchas 5.1）。
"""
import asyncio
import base64
import json
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, ClassVar, Iterable, Optional

# 补 sys.path，让同目录辅助模块可导入（runtime-gotchas 5.2）
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from maibot_sdk import (  # noqa: E402
    Command,
    EventHandler,
    Field,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import (  # noqa: E402
    ErrorPolicy,
    EventType,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

from chunker import RecursiveCharacterChunker  # noqa: E402
from file_parser import describe_supported_types, read_any_file_to_text  # noqa: E402
from vector_store import SessionStore  # noqa: E402


# ─── 配置模型 ────────────────────────────────────────────────────
class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "file"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class ReaderConfig(PluginConfigBase):
    """文件读取与检索配置。"""

    __ui_label__ = "文件读取"
    __ui_icon__ = "folder"
    __ui_order__ = 1

    max_file_size: int = Field(default=100, description="单文件大小上限（MB）")
    chunk_size: int = Field(default=512, description="分块大小（字符数）")
    chunk_overlap: int = Field(default=100, description="分块重叠（字符数）")
    chunk_merge_enabled: bool = Field(default=True, description="短碎片合并（v1.0.18 性能优化）：递归切分出的相邻短碎片合并到接近 chunk_size 再成块，块数降至约 1/3，入库向量化耗时同步下降；关闭则回退旧的逐碎片成块行为")
    retrieve_top_k: int = Field(default=6, description="检索返回的相关块数量")
    retrieve_use_matrix: bool = Field(default=True, description="矩阵化检索（v1.0.18 性能优化）：缓存 L2 归一化向量矩阵，查询时一次点积算全库相似度（入库/清理/删文件自动置脏重建）；块数多时检索开销显著下降，结果与逐条余弦一致；异常自动回退旧路径")
    file_retention_time: int = Field(default=60, description="文件有效时间（分钟）")
    file_max_rounds: int = Field(default=5, description="文件最大参与轮数")
    cleanup_interval: int = Field(default=15, description="后台清理间隔（分钟）")
    enable_group: bool = Field(default=True, description="是否处理群聊文件")
    insecure_download: bool = Field(default=False, description="下载文件时跳过 SSL 证书校验（运行机器存在 TLS MITM 代理、报 CERTIFICATE_VERIFY_FAILED 时开启）")
    injection_marker: str = Field(default="【文件检索】", description="注入文本的幂等标记")
    inject_memo_enabled: bool = Field(default=True, description="注入去重缓存（v1.0.18）：同一会话同一问题在 TTL 内直接复用上次注入文本（零 embedding、零检索），覆盖 hook 每次尝试重跑与 Planner/回复双触发；文件入库、清文件、会话清理时自动失效")
    inject_memo_ttl: int = Field(default=90, description="注入去重缓存的存活秒数")
    silent_success: bool = Field(default=True, description="文件入库成功后保持静默（不发回执，日志仍记录）；关闭后每次入库都回复「已解析 N 块」")
    embed_retry_interval: float = Field(default=2.0, description="嵌入失败后台重试间隔（分钟）；入库时 embedding 超时会先在后台队列排队，定时重试")
    embed_max_retries: int = Field(default=30, description="后台重试最大次数（超过后放弃并提示重发文件；默认 30 次 × 2 分钟 ≈ 覆盖 1 小时拥塞）")
    embed_batch_size: int = Field(default=64, description="单次 llm.embed RPC 调用的最大文本条数；v1.0.18 配合分块合并把默认从 16 提到 64（合并后单批约 8K 字仍远低于 30s RPC 上限），批数下降进一步缩短入库耗时；若 host 限流可调回 16")
    embed_concurrency: int = Field(default=2, description="入库拆批后的并发批数：多批同时调用 llm.embed（1 = 旧串行行为）；host 出现限流/批量报错时调回 1")
    query_embed_timeout: float = Field(default=8.0, description="提问时查询 embedding 的超时秒数：仅单次调用、超时立即放弃并降级到概要兜底，不重试（避免拥塞期把用户卡在请求模型之前）；0 = 关闭超时（旧行为，最坏可卡 ~96s）")
    direct_inject_max_chars: int = Field(default=6000, description="全文直注阈值（解析后总字符数）：会话内所有文件全文合计不超过该值时，跳过 RAG 检索、直接把文件全文注入上下文（小文件无需检索即可全量可见，还省一次查询 embedding 调用）；设为 0 关闭该行为，始终走检索")
    summary_enabled: bool = Field(default=True, description="大文件 LLM 概要：全文超 direct_inject_max_chars 的文件在入库后用 llm.generate 生成一段概要，注入上下文时附在检索片段前，让 LLM 对大文件先有整体认识；生成失败静默降级（不影响检索）")
    summary_source_chars: int = Field(default=12000, description="生成概要时截取的源文本长度（字符数）：从文件头、中、尾三段均匀取样拼接，控制摘要调用的 token 开销")
    summary_max_chars: int = Field(default=500, description="概要文本的最大长度（字符数）")
    summary_await_on_inject: bool = Field(default=False, description="提问注入时是否同步等待概要生成（v1.0.17 旧行为）：概要缺失时同步调用 llm.generate 会把 BLOCKING hook 卡住数秒；默认 false = 缺失时立即用已有内容检索注入，概要在后台生成、下次提问自然带上")
    summary_retry_interval: float = Field(default=600.0, description="概要生成失败后的冷却秒数：冷却期内不再尝试生成（避免每次提问都撞一次超时）；0 = 不冷却（每次注入都重试）")


class NapcatConfig(PluginConfigBase):
    """NapCat HTTP API 兜底配置。

    真机实测（v1.0.6）：napcat-adapter 会把 file 段降级成纯文本
    `[文件] xxx.docx，大小: 9547`，**连 chat.receive.before_process（SessionMessage.process()
    之前）拿到的也已经是 text 段** —— hook 层拿不到文件本体，只能回头找 NapCat 要原始事件。
    """

    __ui_label__ = "NapCat 兜底"
    __ui_icon__ = "cloud-download"
    __ui_order__ = 2

    enabled: bool = Field(
        default=False,
        description="启用 NapCat HTTP API 兜底取文件（适配器会把文件段转成纯文本，不启用则读不到内容）",
    )
    http_url: str = Field(default="http://127.0.0.1:3001", description="NapCat OneBot HTTP 服务地址")
    access_token: str = Field(default="", description="NapCat HTTP access_token（未设置则留空）")
    timeout: float = Field(default=15.0, description="HTTP 请求超时（秒）")
    verify_ssl: bool = Field(default=False, description="HTTPS 时校验证书（本地 http 无需开启）")
    cache_dir: str = Field(
        default="",
        description="可选：NapCat 文件缓存目录，get_file 失败时按文件名+大小在此目录搜索",
    )


class FileReaderConfig(PluginConfigBase):
    """插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    reader: ReaderConfig = Field(default_factory=ReaderConfig)
    napcat: NapcatConfig = Field(default_factory=NapcatConfig)


class FileReaderPlugin(MaiBotPlugin):
    """文件读取插件。"""

    config_model = FileReaderConfig
    # 订阅全局模型配置热重载（embedding 模型变化时重建）
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("model",)

    # ─── 生命周期 ────────────────────────────────────────────────
    async def on_load(self) -> None:
        """插件加载时初始化。"""
        self._store = SessionStore(Path(self.ctx.paths.data_dir) / "file_reader")
        self._cleanup_task: Optional[asyncio.Task] = None
        self._embedding_ok = True
        self._embedding_err = ""
        # 双 hook（before/after）去重：key = f"{session_id}:{file_name}" -> 上次处理时刻
        self._recent_file_keys: dict[str, float] = {}
        # 最近一次"被降级成纯文本"的文件消息留档（供 /file_status 排查）
        self._last_file_hint: dict[str, Any] = {}
        # embedding 失败重试队列（v1.0.11）：[{session_id, conversation_id, stream_id, name, text, attempts, next_ts}]
        self._embed_retry_queue: list[dict[str, Any]] = []
        self._retry_task: Optional[asyncio.Task] = None
        # 注入去重缓存（v1.0.18 C3）：(session_id, query_key) -> (ts, inject_text)
        # 覆盖 hook 每次尝试重跑与 Planner/回复双触发；入库/清文件/会话清理时必须失效
        self._inject_memo: dict[tuple[str, str], tuple[float, str]] = {}
        # 概要异步化（v1.0.18 C4）：in-flight 去重（同一 entry 的概要任务不重复起）
        # 与失败冷却（file_name -> 上次失败时刻，冷却期内不再撞 llm.generate 超时）
        self._summary_pending: set[int] = set()
        self._summary_failed_ts: dict[str, float] = {}

        # 行为自检：确认 chunker / 解析器在进程内可用（runtime-gotchas 5.1 的对策）
        self._run_self_check()

        self.ctx.logger.info(
            "文件读取插件已加载：data_dir=%s，chunk_size=%d，top_k=%d，有效时间=%dmin，最大轮数=%d",
            self._store.data_dir,
            self.config.reader.chunk_size,
            self.config.reader.retrieve_top_k,
            self.config.reader.file_retention_time,
            self.config.reader.file_max_rounds,
        )
        # NapCat 兜底开关是"能否读到文件"的关键，加载时显式打出来
        self.ctx.logger.info(
            "NapCat 兜底: %s (url=%s, token=%s, cache_dir=%s)",
            "启用" if self.config.napcat.enabled else "停用（适配器降级的文件读不到内容！）",
            self.config.napcat.http_url,
            "已设置" if (self.config.napcat.access_token or "").strip() else "空",
            self.config.napcat.cache_dir or "未设置",
        )
        await self._start_cleanup_loop()
        await self._start_retry_loop()

    async def on_unload(self) -> None:
        """插件卸载时停止后台任务。"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except (asyncio.CancelledError, Exception):
                pass
            self._cleanup_task = None
        if self._retry_task:
            self._retry_task.cancel()
            try:
                await self._retry_task
            except (asyncio.CancelledError, Exception):
                pass
            self._retry_task = None
        self._store.save_meta()
        self.ctx.logger.info("文件读取插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载。"""
        del scope, config_data, version
        # v1.0.18 C5：热重载同步矩阵检索开关到已存在的会话库
        use_matrix = bool(self.config.reader.retrieve_use_matrix)
        for vs in self._store._sessions.values():
            vs.retrieve_use_matrix = use_matrix
        self.ctx.logger.info(
            "配置已热重载：chunk_size=%d, top_k=%d, retention=%dmin, max_rounds=%d, matrix_retrieve=%s",
            self.config.reader.chunk_size,
            self.config.reader.retrieve_top_k,
            self.config.reader.file_retention_time,
            self.config.reader.file_max_rounds,
            use_matrix,
        )

    # ─── 行为自检 ────────────────────────────────────────────────
    def _run_self_check(self) -> None:
        """拿固定样例喂进程内函数对象，验证行为而非读源码文本（runtime-gotchas）。"""
        try:
            c = RecursiveCharacterChunker(10, 2)
            out = c.chunk("一二三四五六七八九十十一十二十三十四")
            assert isinstance(out, list) and out and all(isinstance(x, str) for x in out), "分块结果异常"
            self.ctx.logger.info("[自检] 分块器: PASS (%d 块)", len(out))
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.error("[自检] 分块器: FAIL (%s)", e)

        try:
            from file_parser import get_extension, is_supported

            assert get_extension("a.PDF") == "pdf"
            assert is_supported("report.xlsx")
            assert not is_supported("photo.mp4")
            self.ctx.logger.info("[自检] 解析器类型表: PASS")
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.error("[自检] 解析器类型表: FAIL (%s)", e)

    # ─── embedding 封装 ──────────────────────────────────────────
    async def _embed(self, texts: list[str], *, query_mode: bool = False) -> Any:
        """调用 MaiBot llm.embed，带自动重试（指数退避），最终失败返回空（上层抛可读错误）。

        v1.0.18：拆批后多批并发（embed_concurrency，默认 2）+ 批大小提到 64；
        max_concurrent 一并透传给 host 侧调度。任一批失败仍整单返回 {}（防残缺向量）。
        query_mode=True 时走查询分层：单次调用 + wait_for 超时，不重试（拥塞期不卡用户）。
        """
        batch_size = max(1, int(self.config.reader.embed_batch_size))
        if len(texts) <= batch_size:
            return await self._embed_once(texts, query_mode=query_mode)

        # 拆批
        batches = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]
        total_batches = len(batches)
        concurrency = max(1, int(self.config.reader.embed_concurrency)) if not query_mode else 1

        if concurrency <= 1:
            # 串行路径（embed_concurrency=1 等价旧行为）
            results: list[Any] = []
            for idx, part in enumerate(batches):
                part_result = await self._embed_once(part, query_mode=query_mode)
                if part_result == {} or part_result is None:
                    self.ctx.logger.warning(
                        "embedding 拆批调用第 %d/%d 批失败，整单失败待重试", idx + 1, total_batches
                    )
                    return {}
                results.append(part_result)
                self.ctx.logger.info("embedding 拆批进度: %d/%d 批（%d 块）", idx + 1, total_batches, len(part))
            return self._merge_embed_results(results)

        # 并发路径：Semaphore 限流，return_exceptions 收集失败批
        sem = asyncio.Semaphore(concurrency)
        done_log: dict[int, str] = {}

        async def _run_one(idx: int, part: list[str]) -> Any:
            async with sem:
                return await self._embed_once(part, query_mode=query_mode)

        async def _guarded(idx: int, part: list[str]) -> tuple[int, Any]:
            try:
                r = await _run_one(idx, part)
                done_log[idx] = "ok"
                return (idx, r)
            except Exception as e:  # noqa: BLE001
                done_log[idx] = f"{type(e).__name__}: {e}"
                return (idx, {})

        gathered = await asyncio.gather(*(_guarded(i, b) for i, b in enumerate(batches)))
        gathered.sort(key=lambda x: x[0])  # 按原批序拼回，保证向量与文本对齐

        failed = [i for i, r in gathered if r == {} or r is None]
        if failed:
            self.ctx.logger.warning(
                "embedding 并发拆批 %d/%d 批失败（%s），整单失败待重试",
                len(failed), total_batches, ",".join(str(i + 1) for i in failed),
            )
            return {}
        for i in range(total_batches):
            self.ctx.logger.info("embedding 拆批进度: %d/%d 批（%d 块）", i + 1, total_batches, len(batches[i]))
        return self._merge_embed_results([r for _, r in gathered])

    def _merge_embed_results(self, results: list[Any]) -> Any:
        """把多批 embed 结果拼成一个与单批同构的结果。"""
        # 纯 list 形态：直接拼接
        if all(isinstance(r, list) for r in results):
            merged: list[Any] = []
            for r in results:
                merged.extend(r)
            return merged
        # dict 形态：兼容 results / embeddings 两种键
        if all(isinstance(r, dict) for r in results):
            merged_dict: dict[str, Any] = {}
            for key in ("results", "embeddings"):
                if all(key in r for r in results):
                    items: list[Any] = []
                    for r in results:
                        items.extend(r[key])
                    merged_dict[key] = items
            if merged_dict:
                return merged_dict
            # 单条形态（embedding 键）：每批只有 1 条时退化为列表语义不可行，直接取第一键拼接不了，
            # 保守返回第一批（调用方 _embed_query 只用于单条查询，不会走拆批路径）
            return results[0]
        return results[0]

    async def _embed_once(self, texts: list[str], *, query_mode: bool = False) -> Any:
        """单批 embed 调用 + 自动重试。

        v1.0.18 重试分层：
        - 入库（query_mode=False）：2 次尝试、退避 2s——入库走后台任务，可等；
        - 查询（query_mode=True）：单次调用 + wait_for(query_embed_timeout)——
          BLOCKING hook 卡在请求模型之前是用户感知延迟主因（旧行为最坏 ~96s），
          超时立即返回 {}，上层注入路径会降级到概要兜底。
        """
        if query_mode:
            timeout = float(self.config.reader.query_embed_timeout or 0)
            try:
                if timeout > 0:
                    return await asyncio.wait_for(self.ctx.llm.embed(texts=texts), timeout=timeout)
                return await self.ctx.llm.embed(texts=texts)
            except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                self._embedding_ok = False
                self._embedding_err = f"{type(e).__name__}: {e}"
                self.ctx.logger.warning("查询 embedding 失败（不重试，降级概要兜底）: %s", e)
                return {}

        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                result = await self.ctx.llm.embed(texts=texts)
                self._embedding_ok = True
                self._embedding_err = ""
                return result
            except Exception as e:  # noqa: BLE001
                last_err = e
                self._embedding_ok = False
                self._embedding_err = f"{type(e).__name__}: {e}"
                if attempt < 1:  # 还有重试机会
                    delay = 2.0
                    self.ctx.logger.warning(
                        "embedding 调用失败（第 %d 次），%.0fs 后重试: %s",
                        attempt + 1,
                        delay,
                        e,
                    )
                    await asyncio.sleep(delay)
        self.ctx.logger.error("embedding 调用失败（已重试 1 次）: %s", last_err)
        return {}

    async def _embed_query(self, query: str) -> Optional[list[float]]:
        result = await self._embed([query], query_mode=True)
        if isinstance(result, dict):
            if isinstance(result.get("results"), list) and result["results"]:
                item = result["results"][0]
                if isinstance(item, dict) and "embedding" in item:
                    return list(item["embedding"])
            if result.get("embedding") is not None:
                return list(result["embedding"])
        if isinstance(result, list) and result:
            return list(result[0])
        return None

    # ─── 文件消息检测（双阶段 hook：before_process 拿原始段，after_process 兜底） ───
    @HookHandler("chat.receive.before_process", name="detect_file_early", mode=HookMode.OBSERVE, error_policy=ErrorPolicy.SKIP)
    async def on_file_message_early(self, **kwargs: Any) -> dict[str, Any]:
        """预处理阶段检测文件。

        官方事件管线：before_process 在 SessionMessage.process() 轻量转换**之前**触发，
        raw_message 可能仍是原始消息段（含 file 段的 url/base64/file_id）——
        after_process 阶段实测文件已被转换成 text 描述（raw=list[text(-)]），故加此 hook。
        """
        return await self._detect_file("before", **kwargs)

    @HookHandler("chat.receive.after_process", name="detect_file", mode=HookMode.OBSERVE, error_policy=ErrorPolicy.SKIP)
    async def on_file_message(self, **kwargs: Any) -> dict[str, Any]:
        """处理完成阶段检测文件（兜底：before_process 未提取到时再试一次）。"""
        return await self._detect_file("after", **kwargs)

    async def _detect_file(self, stage: str, **kwargs: Any) -> dict[str, Any]:
        """双阶段共用的文件检测逻辑；同文件 10 秒窗口去重防重复入库。"""
        message = kwargs.get("message")

        # 诊断：疑似文件消息必打（看清载荷），普通消息每阶段只打一次
        self._log_file_hook_diag(stage, kwargs, message)

        if not isinstance(message, dict) or not message:
            return {"action": "continue"}

        if not self.config.plugin.enabled:
            self.ctx.logger.info("[诊断] 插件已停用（plugin.enabled=false），跳过文件检测")
            return {"action": "continue"}

        # 群聊开关（兼容 message_info.group_id 平铺与 group_info 嵌套两种形态）
        if not self.config.reader.enable_group:
            mi = message.get("message_info") or {}
            gi = mi.get("group_info") if isinstance(mi, dict) else None
            is_group = bool(
                (isinstance(mi, dict) and mi.get("group_id"))
                or (isinstance(gi, dict) and (gi.get("group_id") or gi.get("group_name") or gi.get("group_all")))
                or kwargs.get("is_group")
            )
            if is_group:
                return {"action": "continue"}

        # 提取文件段
        files = self._extract_files(message)
        if not files and self._looks_like_file(message):
            # 真机形态：适配器已把 file 段降级成 `[文件] xxx.docx，大小: 9547` 纯文本，
            # 本体只能拿 message_id 回头找 NapCat 要。
            hint = self._parse_file_hint(message)
            if hint:
                hint["napcat_message_id"] = message.get("message_id")
                # 留档：/file_status 里能看到最近一次文件消息的 message_id，便于核对 NapCat 回溯
                self._last_file_hint = {
                    "name": hint["name"],
                    "size": hint["size_hint"],
                    "mid": repr(message.get("message_id")),
                    "stage": stage,
                }
                files = [hint]
        if not files:
            return {"action": "continue"}

        session_id = str(self._pick_session_id(message, kwargs) or "default")
        conversation_id = str(kwargs.get("session_id") or session_id)
        stream_id = str(self._pick_stream_id(message, kwargs) or "")

        # 去重：同一 session 同名文件 10 秒内只处理一次（before/after 双 hook 各看到一次）
        now = time.time()
        to_handle: list[dict[str, Any]] = []
        for fdata in files:
            key = f"{session_id}:{fdata.get('name', '')}"
            last = self._recent_file_keys.get(key, 0.0)
            if now - last < 10.0:
                continue
            self._recent_file_keys[key] = now
            to_handle.append(fdata)
        # 顺带清理过期键
        if len(self._recent_file_keys) > 64:
            self._recent_file_keys = {k: v for k, v in self._recent_file_keys.items() if now - v < 60.0}

        # 后台处理，不阻塞聊天主流程（runtime-gotchas 5.3）
        for fdata in to_handle:
            asyncio.create_task(
                self._handle_file(session_id, conversation_id, stream_id, fdata)
            )
        return {"action": "continue"}

    def _looks_like_file(self, message: dict[str, Any]) -> bool:
        """轻量判断：该消息是否疑似文件消息（不做内容提取，只判标记与段类型）。"""
        plain = str(message.get("processed_plain_text") or "")
        if "[文件]" in plain or "[文件]" in str(message.get("text") or ""):
            return True
        segs = message.get("raw_message")
        if isinstance(segs, list):
            for s in segs:
                if isinstance(s, dict) and str(s.get("type", "")).lower() in ("file", "attachment"):
                    return True
        return False

    def _parse_file_hint(self, message: dict[str, Any]) -> Optional[dict[str, Any]]:
        """从降级文本里解析文件名、大小与可选下载链接。

        两种真机形态：
        - 私聊：`[文件] 文章.docx，大小: 9547`
        - 群聊：`[文件] 文章.docx，大小: 9547，链接: https://tjc-download.ftn.qq.com/...`

        返回 {"name", "size_hint", "url"?}；解析不出返回 None。
        """
        plain = str(message.get("processed_plain_text") or "") or str(message.get("text") or "")
        # 注意：非贪婪 (.+?) 必须锚定，否则只会吃到第一个字符（"文"而非"文章.txt"）；
        # name 后面可以跟 大小 和 链接 两个可选尾巴，链接里含逗号，必须先单独摘出来。
        url_m = re.search(r"链接[:：]\s*(https?://\S+)", plain)
        url = url_m.group(1).rstrip("，,。") if url_m else ""
        # 去掉链接尾巴再匹配文件名，避免长 URL 被吞进 name；
        # 截断处可能残留 "，大小: 9547，" 这类尾巴，让大小组可选+锚定吸收它
        name_part = plain[: url_m.start()] if url_m else plain
        m = re.search(
            r"\[文件\]\s*(?P<name>.+?)(?:\s*[，,]\s*大小[:：]\s*(?P<size>\d+))?\s*[，,]?\s*$",
            name_part,
            re.MULTILINE,
        )
        if not m:
            return None
        name = m.group("name").strip()
        if not name:
            return None
        size_raw = m.group("size")
        size_hint = int(size_raw) if size_raw else 0
        hint: dict[str, Any] = {"name": name, "size_hint": size_hint}
        if url:
            hint["url"] = url
        return hint

    def _log_file_hook_diag(self, stage: str, kwargs: dict[str, Any], message: Any) -> None:
        """诊断载荷结构。

        策略（v1.0.5）：
        - 疑似文件消息：每阶段最多打 5 条完整段结构——排障靠的就是这些行，
          不能让第一条普通文本消息把一次性标志消耗掉（v1.0.4 踩过）。
        - 普通消息：每阶段只打一行，防刷屏。
        """
        if not isinstance(message, dict) or not message:
            flag = f"_file_diag_logged_{stage}"
            if getattr(self, flag, False):
                return
            setattr(self, flag, True)
            self.ctx.logger.info(
                "[诊断] file hook[%s] 载荷非 dict: message_type=%s kwargs_keys=%s",
                stage,
                type(message).__name__,
                sorted(kwargs.keys()),
            )
            return
        looks_file = self._looks_like_file(message)
        if looks_file:
            cnt_attr = f"_file_diag_file_count_{stage}"
            cnt = int(getattr(self, cnt_attr, 0))
            if cnt >= 5:
                return
            setattr(self, cnt_attr, cnt + 1)
            tag = "FILE"
        else:
            flag = f"_file_diag_logged_{stage}"
            if getattr(self, flag, False):
                return
            setattr(self, flag, True)
            tag = "msg"
        self._log_diag_detail(stage, tag, kwargs, message)

    def _log_diag_detail(self, stage: str, tag: str, kwargs: dict[str, Any], message: dict[str, Any]) -> None:
        """打印单条诊断：载荷键结构 + raw_message 段细节 + 纯文本预览。"""
        try:
            mi = message.get("message_info")
            raw = message.get("raw_message")
            raw_desc = self._describe_raw(raw)
            plain = str(message.get("processed_plain_text") or "")
            self.ctx.logger.info(
                "[诊断] file hook[%s][%s] 触发: kwargs_keys=%s message_keys=%s message_info_keys=%s mid=%s raw=%s plain=%s",
                stage,
                tag,
                sorted(kwargs.keys()),
                sorted(message.keys()),
                sorted(mi.keys()) if isinstance(mi, dict) else "-",
                repr(message.get("message_id")),
                raw_desc,
                repr(plain[:80]),
            )
        except Exception:  # noqa: BLE001
            pass

    def _describe_raw(self, raw: Any) -> str:
        """描述 raw_message 结构：list → 各段 type + data 摘要（data 非 dict 时打印值与类型）。"""
        if isinstance(raw, list):
            descs = []
            for s in raw[:8]:
                if not isinstance(s, dict):
                    descs.append(f"{type(s).__name__}={str(s)[:40]!r}")
                    continue
                data = s.get("data")
                if isinstance(data, dict):
                    detail = "|".join(sorted(data.keys()))
                elif data is None:
                    detail = "-"
                else:
                    # data 为非 dict（如纯字符串）：打印类型与值，便于看清 text 段到底存了什么
                    detail = f"{type(data).__name__}={str(data)[:60]!r}"
                descs.append(f"{s.get('type')}({detail})")
            return f"list[{', '.join(descs)}]"
        if isinstance(raw, dict):
            return f"dict keys={sorted(raw.keys())}"
        return type(raw).__name__

    def _extract_files(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """从消息里找出文件段，返回 [{name, bytes|path|url}, ...]。

        兼容多种结构：
        - segments / message_chain / content / message 列表里的 {"type": "file", ...}
        - OneBot 风格 {"type": "file", "data": {...}}（napcat 等适配器）
        - raw_message 里嵌套的 file / attachment / files
        - raw_message.message 数组（OneBot 原始事件）
        """
        found: list[dict[str, Any]] = []

        def _scan_segments(segs: Any) -> None:
            if not isinstance(segs, list):
                return
            for seg in segs:
                if not isinstance(seg, dict):
                    continue
                stype = str(seg.get("type", "")).lower()
                if stype in ("file", "attachment"):
                    # OneBot 风格：实际字段嵌在 data 里
                    payload = seg.get("data") if isinstance(seg.get("data"), dict) else seg
                    fdata = self._seg_to_file(payload)
                    if fdata:
                        found.append(fdata)
                    else:
                        self.ctx.logger.warning(
                            "文件段无法提取内容: payload_keys=%s", sorted(payload.keys())
                        )

        # 1) 消息级段列表
        for key in ("segments", "message_chain", "content", "message"):
            _scan_segments(message.get(key))

        # 2) raw_message：list（napcat 真机形态：消息段数组）或 dict（嵌套容器）
        raw = message.get("raw_message")
        if isinstance(raw, list):
            # 真机确认：raw_message 本身就是 [{"type": "file", "data": {...}}, ...] 段数组
            _scan_segments(raw)
        elif isinstance(raw, dict):
            for key in ("file", "attachment", "files"):
                val = raw.get(key)
                if isinstance(val, dict):
                    fdata = self._seg_to_file(val)
                    if fdata:
                        found.append(fdata)
                else:
                    _scan_segments(val)
            # OneBot 原始事件：message 数组
            if isinstance(raw.get("message"), list):
                _scan_segments(raw["message"])
        return found

    def _seg_to_file(self, seg: dict[str, Any]) -> Optional[dict[str, Any]]:
        """从单个文件段提取 name + 内容（base64 / 本地路径 / URL）。

        兼容字段名：name / file_name / filename / file / file_id / title；
        内容：base64 / data / content_base64 / url / file_url / path / file_path / local_path。
        OneBot 的 data.file 可能是 URL、本地缓存路径或纯文件名，按形态分发。
        """
        raw_file = str(seg.get("file") or "")
        name = (
            seg.get("name")
            or seg.get("file_name")
            or seg.get("filename")
            or seg.get("title")
            or Path(raw_file).name  # OneBot data.file 常带路径，取文件名
            or seg.get("file_id")
            or "unnamed_file"
        )
        content_b64 = seg.get("base64") or seg.get("content_base64")
        url = seg.get("url") or seg.get("file_url")
        path = seg.get("path") or seg.get("file_path") or seg.get("local_path") or seg.get("tmp_path")

        # OneBot data.file 形态分发：URL / 本地路径 / 纯文件名
        if raw_file:
            if raw_file.startswith(("http://", "https://")) and not url:
                url = raw_file
            elif not path and Path(raw_file).exists():
                path = raw_file

        if content_b64:
            try:
                raw_bytes = base64.b64decode(content_b64)
                return {"name": str(name), "bytes": raw_bytes}
            except Exception:  # noqa: BLE001
                pass
        if path and Path(str(path)).exists():
            return {"name": str(name), "path": str(path)}
        if url:
            return {"name": str(name), "url": str(url)}
        # 无可用内容
        return None

    def _pick_session_id(self, message: dict[str, Any], kwargs: dict[str, Any]) -> str:
        for key in ("session_id", "user_id", "sender_id", "user_openid"):
            if kwargs.get(key):
                return str(kwargs[key])
        # 真机形态：message 顶层 session_id（napcat，与 before_model_request 的 session 一致）
        if message.get("session_id"):
            return str(message["session_id"])
        mi = message.get("message_info") or {}
        if not isinstance(mi, dict):
            return ""
        # 真机形态：user_info / group_info 嵌套
        ui = mi.get("user_info")
        if isinstance(ui, dict):
            for key in ("user_id", "id", "qq", "user_openid"):
                if ui.get(key):
                    return str(ui[key])
        gi = mi.get("group_info")
        if isinstance(gi, dict):
            for key in ("group_id", "id", "group_all"):
                if gi.get(key):
                    return str(gi[key])
        for key in ("user_id", "sender_id", "user_openid", "group_id"):
            if mi.get(key):
                return str(mi[key])
        return ""

    def _pick_stream_id(self, message: dict[str, Any], kwargs: dict[str, Any]) -> str:
        for key in ("stream_id", "chat_id", "session_id", "stream"):
            if kwargs.get(key):
                return str(kwargs[key])
        # 真机形态：message 顶层 session_id（napcat，回执发送目标）
        if message.get("session_id"):
            return str(message["session_id"])
        mi = message.get("message_info") or {}
        if isinstance(mi, dict):
            for key in ("stream_id", "chat_id", "group_id"):
                if mi.get(key):
                    return str(mi[key])
            gi = mi.get("group_info")
            if isinstance(gi, dict):
                for key in ("group_id", "id"):
                    if gi.get(key):
                        return str(gi[key])
        return ""

    async def _handle_file(self, session_id: str, conversation_id: str, stream_id: str, fdata: dict[str, Any]) -> None:
        """后台：写临时文件 → 解析 → 向量化 → 落库 → 回执。

        embedding 失败（真机 14:27 日志：服务拥塞持续超 6s 退避窗口）时：
        已解析文本放入后台重试队列，由 _retry_loop 定时重试入库，对用户透明。
        """
        name = str(fdata.get("name", "unnamed_file"))
        vs = self._store.get_or_create(
            session_id,
            conversation_id,
            self._embed,
            self.config.reader.chunk_size,
            self.config.reader.chunk_overlap,
            self.config.reader.retrieve_top_k,
            chunk_merge=bool(self.config.reader.chunk_merge_enabled),
            retrieve_use_matrix=bool(self.config.reader.retrieve_use_matrix),
        )
        tmp_path: Optional[str] = None
        try:
            # 取文件字节
            raw_bytes = fdata.get("bytes")
            if raw_bytes is None and fdata.get("path"):
                raw_bytes = Path(str(fdata["path"])).read_bytes()
            if raw_bytes is None and fdata.get("url"):
                raw_bytes = await self._download(str(fdata["url"]))
            if raw_bytes is None and fdata.get("napcat_message_id"):
                # 适配器降级形态：拿 message_id 回头找 NapCat 要原始文件
                if not self.config.napcat.enabled:
                    await self._reply(
                        stream_id,
                        f"⚠️ 文件「{name}」读不到内容：适配器把文件降级成了纯文本，"
                        "请在插件配置里启用「NapCat 兜底」并填好 HTTP 地址与 token。",
                    )
                    return
                raw_bytes, napcat_path = await asyncio.to_thread(self._resolve_via_napcat, fdata)
                if raw_bytes is None and napcat_path:
                    raw_bytes = Path(str(napcat_path)).read_bytes()
            if raw_bytes is None:
                if fdata.get("napcat_message_id"):
                    await self._reply(
                        stream_id,
                        f"⚠️ 文件「{name}」无法读取：NapCat 未返回文件内容"
                        "（检查 HTTP 服务/token/message_id 是否对得上，或配置 cache_dir）。",
                    )
                else:
                    await self._reply(
                        stream_id,
                        f"⚠️ 文件「{name}」无法读取：消息里没有文件内容，也没有可用于回溯的 message_id。",
                    )
                return

            # 大小检查
            max_bytes = self.config.reader.max_file_size * 1024 * 1024
            if len(raw_bytes) > max_bytes:
                await self._reply(
                    stream_id,
                    f"⚠️ 文件「{name}」大小 {len(raw_bytes) / 1024 / 1024:.1f}MB 超过上限 {self.config.reader.max_file_size}MB，已跳过。",
                )
                return

            # 写临时文件
            suffix = Path(name).suffix or ""
            fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            with os.fdopen(fd, "wb") as f:
                f.write(raw_bytes)

            # 解析
            text = await asyncio.to_thread(read_any_file_to_text, tmp_path, name)

            # 向量化 + 入库
            try:
                entry = await vs.add_file(name, text)
            except RuntimeError as e:
                # embedding 为空/超时：放入后台重试队列（v1.0.11）
                # 注意：ValueError（内容为空）不在此列，走下方通用分支
                await self._enqueue_embed_retry(session_id, conversation_id, stream_id, name, text)
                self.ctx.logger.warning("文件 %s 入库失败（embedding），已入后台重试队列: %s", name, e)
                return
            self.ctx.logger.info(
                "已入库文件 %s：%d 块（session=%s）",
                name,
                len(entry.chunks),
                session_id,
            )
            # v1.0.17：大文件后台预热概要（不阻塞回执；注入时 _ensure_summary 也会懒生成，这里只是让首次提问即命中）
            # v1.0.18 C4：改走 _ensure_summary_async（in-flight 去重 + 失败冷却），注入触发时不会重复起任务
            # v1.0.18 C1：阈值判定用 entry.total_chars()（原文长度），overlap 合并会让 join(chunks) 膨胀
            if (
                bool(self.config.reader.summary_enabled)
                and entry.total_chars() > max(1, int(self.config.reader.direct_inject_max_chars))
                and not entry.summary
            ):
                self._ensure_summary_async(entry)
            # v1.0.18 C3：文件变动必须失效注入缓存，否则同一问题会复用旧文件的注入内容
            self._memo_invalidate_session(session_id)
            if not self.config.reader.silent_success:
                await self._reply(
                    stream_id,
                    f"📄 已解析「{name}」，切成 {len(entry.chunks)} 块并向量化。现在可以直接问我文件内容了。",
                )
        except ValueError as e:
            await self._reply(stream_id, f"⚠️ {e}")
        except RuntimeError as e:
            await self._reply(stream_id, f"⚠️ {e}")
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.error("处理文件 %s 失败: %s", name, e, exc_info=True)
            await self._reply(stream_id, f"⚠️ 处理文件「{name}」失败：{type(e).__name__}")
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ─── NapCat HTTP 兜底（同步实现，调用处包 asyncio.to_thread） ───
    def _napcat_post(self, endpoint: str, payload: dict[str, Any]) -> Optional[Any]:
        """调 NapCat OneBot HTTP API，返回 data 字段；失败返回 None。"""
        cfg = self.config.napcat
        url = f"{str(cfg.http_url).rstrip('/')}/{endpoint}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        token = (cfg.access_token or "").strip()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        ctx = ssl.create_default_context()
        if not cfg.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            self.ctx.logger.error("[napcat] %s HTTP %s: %s", endpoint, e.code, e.read()[:200])
            return None
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.error("[napcat] %s 请求失败: %s: %s", endpoint, type(e).__name__, e)
            return None
        if not isinstance(data, dict):
            return None
        status = str(data.get("status", ""))
        retcode = data.get("retcode")
        if status == "ok" or retcode == 0 or (status == "" and retcode is None and "data" in data):
            return data.get("data")
        self.ctx.logger.error(
            "[napcat] %s 返回异常: status=%s retcode=%s wording=%s",
            endpoint,
            status,
            retcode,
            data.get("wording") or data.get("message"),
        )
        return None

    def _napcat_get_msg(self, message_id: Any) -> Optional[dict[str, Any]]:
        """拿原始 OneBot 事件。message_id 形态未知，先原样试，失败再转 int 重试。"""
        if message_id in (None, ""):
            self.ctx.logger.error("[napcat] 消息没有 message_id，无法回溯原始事件")
            return None
        candidates: list[Any] = [message_id]
        try:
            as_int = int(str(message_id))
            if as_int != message_id:
                candidates.append(as_int)
        except (TypeError, ValueError):
            pass
        for cand in candidates:
            data = self._napcat_post("get_msg", {"message_id": cand})
            if isinstance(data, dict):
                self.ctx.logger.info(
                    "[napcat] get_msg 成功: message_id=%r raw_keys=%s",
                    cand,
                    sorted(data.keys()) if isinstance(data, dict) else "-",
                )
                return data
        return None

    @staticmethod
    def _pick_onebot_file_seg(msg_data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """从原始事件的 message 段数组里找 file 段（OneBot 形态：{type, data:{...}}）。"""
        segs = msg_data.get("message")
        if isinstance(msg_data.get("data"), dict) and "message" in msg_data["data"]:
            segs = msg_data["data"]["message"]
        if not isinstance(segs, list):
            return None
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            if str(seg.get("type", "")).lower() != "file":
                continue
            data = seg.get("data")
            return data if isinstance(data, dict) else seg
        return None

    def _napcat_resolve_path(self, file_seg: dict[str, Any]) -> Optional[str]:
        """file 段 → 本地路径：优先 get_file(file_id)，退化到段内路径。"""
        file_id = file_seg.get("file_id") or file_seg.get("id")
        if file_id:
            data = self._napcat_post("get_file", {"file_id": str(file_id)})
            if isinstance(data, dict):
                p = data.get("file") or data.get("path")
                if p and Path(str(p)).exists():
                    self.ctx.logger.info("[napcat] get_file 命中: %s", p)
                    return str(p)
                self.ctx.logger.warning("[napcat] get_file 返回路径不可用: %r", p)
            elif isinstance(data, str) and Path(data).exists():
                return data
        # 退路：段里的 file 字段本身就是本地路径
        for key in ("path", "file_path", "file"):
            val = file_seg.get(key)
            if val and Path(str(val)).exists():
                return str(val)
        return None

    def _search_cache_dir(self, name: str, size_hint: int) -> Optional[str]:
        """最后兜底：在 NapCat 缓存目录按 文件名+大小 精确匹配。"""
        root = (self.config.napcat.cache_dir or "").strip()
        if not root or not size_hint:
            return None
        base = Path(root)
        if not base.is_dir():
            self.ctx.logger.warning("[napcat] cache_dir 不存在: %s", root)
            return None
        try:
            for f in base.rglob(Path(name).name):
                if f.is_file() and f.stat().st_size == size_hint:
                    self.ctx.logger.info("[napcat] cache_dir 命中: %s", f)
                    return str(f)
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.error("[napcat] cache_dir 扫描失败: %s", e)
        return None

    def _resolve_via_napcat(self, fdata: dict[str, Any]) -> tuple[Optional[bytes], Optional[str]]:
        """同步：拿 NapCat 原始事件 → file 段 → 文件内容。返回 (bytes, path)。"""
        name = str(fdata.get("name", ""))
        size_hint = int(fdata.get("size_hint") or 0)
        msg_data = self._napcat_get_msg(fdata.get("napcat_message_id"))
        file_seg = self._pick_onebot_file_seg(msg_data) if isinstance(msg_data, dict) else None
        if file_seg is None:
            self.ctx.logger.error(
                "[napcat] 原始事件里没有 file 段: message_id=%r data_keys=%s",
                fdata.get("napcat_message_id"),
                sorted(msg_data.keys()) if isinstance(msg_data, dict) else None,
            )
            # 原始事件都翻不到时，退到缓存目录
            path = self._search_cache_dir(name, size_hint)
            return (None, path)
        b64 = file_seg.get("base64")
        if b64:
            try:
                return (base64.b64decode(b64), None)
            except Exception as e:  # noqa: BLE001
                self.ctx.logger.error("[napcat] base64 解码失败: %s", e)
        path = self._napcat_resolve_path(file_seg)
        if not path:
            path = self._search_cache_dir(name, size_hint)
        return (None, path)

    async def _download(self, url: str) -> Optional[bytes]:
        """下载文件（用 httpx，可选依赖）。

        运行机器若存在 TLS MITM 代理（proxy-root-ca.cer），HTTPS 会报
        CERTIFICATE_VERIFY_FAILED；可开 reader.insecure_download 跳过校验。
        """
        try:
            import httpx  # type: ignore
        except ImportError:
            self.ctx.logger.error("下载文件需要 httpx，请安装：pip install httpx")
            return None
        verify = not self.config.reader.insecure_download
        try:
            async with httpx.AsyncClient(timeout=30, verify=verify) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.content
        except Exception as e:  # noqa: BLE001
            if not verify and isinstance(e, Exception) and "CERTIFICATE_VERIFY_FAILED" in str(e):
                pass  # 已跳过校验仍失败，走通用报错
            self.ctx.logger.error("下载 %s 失败: %s", url, e)
            return None

    async def _reply(self, stream_id: str, text: str) -> None:
        if not stream_id:
            self.ctx.logger.warning("无 stream_id，跳过回执: %s", text[:50])
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.error("回执发送失败: %s", e)

    # ─── LLM 上下文注入（before_model_request hook） ─────────────
    # v1.0.16 教训：辅助方法必须放在 @HookHandler/@Tool 装饰器之前——
    # 装饰器绑定的是紧随其后的函数，曾把 _full_text_within_limit 注册成
    # inject_file_context hook 本体（本地直调测试发现不了，真机必炸）。

    def _memo_get(self, session_id: str, query_key: str) -> Optional[str]:
        """查注入去重缓存：TTL 内命中返回上次注入文本，否则 None。"""
        if not bool(self.config.reader.inject_memo_enabled):
            return None
        item = self._inject_memo.get((session_id, query_key))
        if item is None:
            return None
        ts, text = item
        ttl = max(1, int(self.config.reader.inject_memo_ttl))
        if time.time() - ts > ttl:
            self._inject_memo.pop((session_id, query_key), None)
            return None
        return text

    def _memo_put(self, session_id: str, query_key: str, text: str) -> None:
        """写注入去重缓存：LRU 上限 128 条，超限先淘汰最旧。"""
        if not bool(self.config.reader.inject_memo_enabled) or not text:
            return
        if len(self._inject_memo) >= 128:
            oldest_key = min(self._inject_memo, key=lambda k: self._inject_memo[k][0])
            self._inject_memo.pop(oldest_key, None)
        self._inject_memo[(session_id, query_key)] = (time.time(), text)

    def _memo_invalidate_session(self, session_id: str) -> None:
        """文件变动后失效该会话的全部注入缓存（防止注入陈旧内容）。"""
        for key in [k for k in self._inject_memo if k[0] == session_id]:
            self._inject_memo.pop(key, None)

    def _tick_rounds_once(self, vs: Any, query_key: str = "") -> None:
        """轮数去重（v1.0.18 C3）：同一问题文本在 120s 窗口内只给文件 +1 轮。

        旧逻辑 hook 与 tool 各 +1、且每次 attempt 都 +1，5 轮配额实际 2-3 轮就用光，
        文件提前过期 → 用户重发 → 重新 embedding。这里用与 memo 同形的小缓存去重。
        """
        key = ("__round__", query_key.strip() or str(time.time() // 120))
        item = self._inject_memo.get(key)
        now = time.time()
        if item is not None and now - item[0] < 120:
            return  # 同一问题本轮已计数
        if len(self._inject_memo) >= 128:
            oldest_key = min(self._inject_memo, key=lambda k: self._inject_memo[k][0])
            self._inject_memo.pop(oldest_key, None)
        self._inject_memo[key] = (now, "")
        vs.increment_rounds()

    def _full_text_within_limit(self, vs: Any) -> Optional[str]:
        """直注判定：会话内所有文件全文合计 ≤ direct_inject_max_chars 时返回拼接全文，否则 None。

        小文件全文直注（v1.0.14）：检索只取 Top-K 块，小文件场景反而丢内容——
        全文直接可见比「相关度排序」更符合预期；同时省一次查询 embedding 调用
        （拥塞期查询也会超时，直注路径完全绕开）。
        """
        limit = int(self.config.reader.direct_inject_max_chars)
        if limit <= 0 or not vs.files:
            return None
        # v1.0.18：阈值判定用原文长度（source_chars），overlap 合并会让 join(chunks) 膨胀约 20%
        total = sum(entry.total_chars() for entry in vs.files.values())
        if total > limit:
            return None
        parts: list[str] = []
        for fname, entry in vs.files.items():
            parts.append(f"《{fname}》全文：\n{''.join(entry.chunks)}")
        return "\n\n".join(parts)

    @staticmethod
    def _sample_text_for_summary(entry: Any, max_chars: int) -> str:
        """为概要生成取样源文本：头 / 中 / 尾三段均匀取样拼接。

        大文件全文直接喂摘要 LLM token 开销大且易撞上下文限制；
        首尾段保留开头与结尾信息，中段保留主体内容。
        """
        full = "".join(entry.chunks)
        if len(full) <= max_chars:
            return full
        seg = max_chars // 3
        head = full[:seg]
        mid_start = max(0, (len(full) - seg) // 2)
        mid = full[mid_start : mid_start + seg]
        tail = full[-seg:]
        return f"{head}\n……（中略）……\n{mid}\n……（中略）……\n{tail}"

    async def _generate_file_summary(self, entry: Any) -> str:
        """为单个大文件生成 LLM 概要；失败返回空串（静默降级，不影响检索）。"""
        source_chars = max(1000, int(self.config.reader.summary_source_chars))
        max_len = max(100, int(self.config.reader.summary_max_chars))
        source = self._sample_text_for_summary(entry, source_chars)
        if not source.strip():
            return ""
        prompt = (
            "请为以下文档内容写一段简洁的中文概要，概括文档的主题、结构与主要内容，"
            f"不超过 {max_len} 字，不要逐句罗列，直接输出概要正文：\n\n"
            f"文档名：{entry.file_name}\n\n{source}"
        )
        try:
            result = await self.ctx.llm.generate(prompt=prompt, max_tokens=max_len)
            if not isinstance(result, dict) or not result.get("success"):
                self.ctx.logger.warning(
                    "文件 %s 概要生成失败: %s", entry.file_name, result if not isinstance(result, dict) else result.get("reasoning", "unknown")
                )
                return ""
            text = str(result.get("response", "")).strip()
            if len(text) > max_len:
                text = text[:max_len]
            self.ctx.logger.info(
                "已生成文件 %s 的概要（%d 字）", entry.file_name, len(text)
            )
            return text
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.warning("文件 %s 概要生成异常（静默降级）: %s", entry.file_name, e)
            return ""

    def _summary_cooling(self, entry: Any) -> bool:
        """概要失败冷却判断（v1.0.18 C4）：冷却期内不再尝试生成。"""
        interval = float(self.config.reader.summary_retry_interval)
        if interval <= 0:
            return False
        last_fail = self._summary_failed_ts.get(entry.file_name)
        return last_fail is not None and (time.time() - last_fail) < interval

    def _ensure_summary_async(self, entry: Any) -> bool:
        """为单个大文件启动后台概要任务（v1.0.18 C4）。

        in-flight 去重（与入库预热共用）：同一 entry 不重复起任务；
        失败冷却期内跳过。返回是否实际启动了任务。
        """
        if entry.summary or id(entry) in self._summary_pending or self._summary_cooling(entry):
            return False
        self._summary_pending.add(id(entry))

        async def _run() -> None:
            try:
                summary = await self._generate_file_summary(entry)
                if summary:
                    entry.summary = summary
                    self._summary_failed_ts.pop(entry.file_name, None)
                else:
                    self._summary_failed_ts[entry.file_name] = time.time()
            except Exception as e:  # noqa: BLE001
                self._summary_failed_ts[entry.file_name] = time.time()
                self.ctx.logger.warning("文件 %s 概要后台生成异常（已进入冷却）: %s", entry.file_name, e)
            finally:
                self._summary_pending.discard(id(entry))

        asyncio.create_task(_run())
        return True

    async def _ensure_summary(self, vs: Any) -> None:
        """注入时的概要保障（v1.0.18 C4 异步化）。

        summary_await_on_inject=true 时保持 v1.0.17 同步行为（测试/排障用）；
        默认 false：只负责触发后台生成（in-flight + 冷却去重），不阻塞注入，
        概要缺失的本次注入仍可正常检索，下次提问自然带上概要。
        """
        if not bool(self.config.reader.summary_enabled):
            return
        limit = max(1, int(self.config.reader.direct_inject_max_chars))
        await_now = bool(self.config.reader.summary_await_on_inject)
        for entry in vs.files.values():
            # v1.0.18：阈值判定用原文长度
            if entry.total_chars() <= limit or entry.summary:
                continue
            if await_now:
                if self._summary_cooling(entry):
                    continue
                summary = await self._generate_file_summary(entry)
                if summary:
                    entry.summary = summary
                else:
                    self._summary_failed_ts[entry.file_name] = time.time()
            else:
                self._ensure_summary_async(entry)

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="inject_file_context",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
        # v1.0.18：显式超时（查询 embedding 单次 8s + 检索/拼装余量），防止 BLOCKING hook 卡死请求
        timeout_ms=11000,
    )
    async def inject_file_context(self, **kwargs: Any) -> dict[str, Any]:
        """检索相关文件内容，注入 LLM 上下文。

        参考 runtime-gotchas 2：传的是 items 而非 messages；
        只回 modified_kwargs["items"]；带幂等标记避免重试叠加。
        """
        try:
            session_id = str(kwargs.get("session_id") or "")
            items = kwargs.get("items")
            if not session_id or not isinstance(items, list):
                return {"action": "continue"}

            # 找当前会话的向量库
            vs = None
            for (sid, _cid), candidate in self._store._sessions.items():
                if sid == session_id:
                    vs = candidate
                    break
            if vs is None or not vs.files:
                return {"action": "continue"}

            marker = self.config.reader.injection_marker

            # 幂等：已有同样标记则跳过（直注 / 检索两条路径共用）
            existing = " ".join(self._item_text(it) for it in items)
            if marker in existing:
                return {"action": "continue"}

            # 直注路径（v1.0.14）：全文合计 ≤ 阈值 → 不做检索、不调查询 embedding，
            # 直接把文件全文注入。注意 query 为空（如纯表情消息）时也落这里兜底。
            full_text = self._full_text_within_limit(vs)
            if full_text:
                inject_text = (
                    f"{marker} 以下是当前会话文件的完整内容（全文直注，无需检索）：\n\n{full_text}"
                )
                new_item = self._make_system_item(inject_text)
                modified = dict(kwargs)
                modified["items"] = list(items) + [new_item]
                if "item_schema_version" in kwargs:
                    modified["item_schema_version"] = kwargs["item_schema_version"]
                self._tick_rounds_once(vs, "direct-inject")
                self.ctx.logger.info(
                    "文件全文直注（%.0f 字 ≤ 阈值 %d），跳过检索",
                    sum(e.total_chars() for e in vs.files.values()),
                    int(self.config.reader.direct_inject_max_chars),
                )
                return {"action": "continue", "modified_kwargs": modified}

            # 用最近一条用户消息做查询（简化：取最后一条 user item 的文本）
            query = self._extract_last_user_query(items)
            if not query:
                return {"action": "continue"}

            # 注入去重（v1.0.18 C3）：同一会话同一问题 TTL 内直接复用上次注入文本。
            # hook 每次 attempt 都会重跑、Planner 与回复各触发一次——旧幂等只挡 items 内的
            # marker，挡不住跨触发的重复 embedding 与重复检索。
            memo_key = query.strip()
            memo_text = self._memo_get(session_id, memo_key)
            if memo_text:
                new_item = self._make_system_item(memo_text)
                modified = dict(kwargs)
                modified["items"] = list(items) + [new_item]
                if "item_schema_version" in kwargs:
                    modified["item_schema_version"] = kwargs["item_schema_version"]
                self.ctx.logger.info("文件注入命中去重缓存（同会话同问题 TTL 内），跳过检索")
                return {"action": "continue", "modified_kwargs": modified}

            # 大文件概要（v1.0.17）：检索前先确保大文件有概要（异步生成、失败静默），
            # 有概要时即使本次检索无命中也能给 LLM 一个整体认识
            await self._ensure_summary(vs)
            summaries = [
                (fname, entry.summary)
                for fname, entry in vs.files.items()
                if entry.summary
            ]

            qvec = await self._embed_query(query)
            if qvec is None:
                if summaries:
                    # 查询 embedding 失败但概要可用：至少注入概要兜底
                    parts = [f"{marker} 当前会话文件的概要（检索模型暂不可用，仅提供概要）："]
                    for fname, s in summaries:
                        parts.append(f"《{fname}》概要：\n{s}")
                    inject_text = "\n\n".join(parts)
                    self._memo_put(session_id, memo_key, inject_text)
                    new_item = self._make_system_item(inject_text)
                    modified = dict(kwargs)
                    modified["items"] = list(items) + [new_item]
                    if "item_schema_version" in kwargs:
                        modified["item_schema_version"] = kwargs["item_schema_version"]
                    self._tick_rounds_once(vs, memo_key)
                    return {"action": "continue", "modified_kwargs": modified}
                return {"action": "continue"}

            results = vs.retrieve(qvec)
            if not results and not summaries:
                return {"action": "continue"}

            # 拼接注入文本：概要在前（v1.0.17），检索片段在后
            parts = [f"{marker} 以下是当前会话文件的概要与根据用户问题检索到的相关内容（供参考，仅在相关时引用）："]
            for fname, s in summaries:
                parts.append(f"《{fname}》概要：\n{s}")
            for i, r in enumerate(results, 1):
                parts.append(f"[{i}] 来源「{r['file_name']}」 相关度 {r['score']:.3f}：\n{r['text']}")
            inject_text = "\n\n".join(parts)
            self._memo_put(session_id, memo_key, inject_text)

            new_item = self._make_system_item(inject_text)
            modified = dict(kwargs)
            modified["items"] = list(items) + [new_item]
            if "item_schema_version" in kwargs:
                modified["item_schema_version"] = kwargs["item_schema_version"]

            # 增加轮数（同问题每轮只 +1，hook/tool/attempt 共享去重）
            self._tick_rounds_once(vs, memo_key)

            return {"action": "continue", "modified_kwargs": modified}
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.error("注入文件上下文失败: %s", e, exc_info=True)
            return {"action": "continue"}

    @staticmethod
    def _item_text(item: Any) -> str:
        if not isinstance(item, dict):
            return ""
        parts = item.get("parts") or []
        texts = []
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "text":
                texts.append(str(p.get("text", "")))
        return " ".join(texts)

    @staticmethod
    def _extract_last_user_query(items: list[Any]) -> str:
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            if item.get("item_type") in ("UserMessageItem", "user"):
                parts = item.get("parts") or []
                for p in parts:
                    if isinstance(p, dict) and p.get("type") == "text":
                        t = str(p.get("text", "")).strip()
                        if t:
                            return t
        return ""

    @staticmethod
    def _make_system_item(text: str) -> dict[str, Any]:
        import datetime
        import uuid

        return {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": uuid.uuid4().hex,
                "logical_turn_id": None,
                "timestamp": datetime.datetime.now().isoformat(),
            },
            "parts": [{"type": "text", "text": text}],
        }

    # ─── 命令 ────────────────────────────────────────────────────
    @Command("clear_file", description="清除当前会话已入库的文件", pattern=r"^\s*[/／]\s*(?:clear_file|clean_file|清文件|清理文件)\s*$")
    async def cmd_clear_file(self, **kwargs: Any) -> tuple[bool, str, int]:
        """清除当前会话所有文件。"""
        session_id = ""
        for key in ("session_id", "user_id", "chat_id", "stream_id"):
            if kwargs.get(key):
                session_id = str(kwargs[key])
                break
        if not session_id:
            return True, "无法定位当前会话，清除失败。", 0

        removed = self._store.drop(session_id)
        self._memo_invalidate_session(session_id)  # v1.0.18 C3：清文件后失效注入缓存
        text = f"🗑️ 已清除当前会话 {removed} 个文件块。" if removed else "当前会话没有已入库的文件。"
        await self._reply(str(kwargs.get("stream_id", "") or ""), text)
        return True, text, 2 if removed else 0

    @Command("file_status", description="查看文件读取插件状态", pattern=r"^\s*[/／]\s*(?:file_status|文件状态|文件读取状态)\s*$")
    async def cmd_status(self, **kwargs: Any) -> tuple[bool, str, int]:
        """状态命令：库规模 / 嵌入模型可用性 / 关键配置。"""
        total_files = sum(len(vs.files) for vs in self._store._sessions.values())
        total_chunks = sum(vs.total_chunks() for vs in self._store._sessions.values())
        sessions = len(self._store._sessions)
        embed_state = "正常" if self._embedding_ok else f"异常({self._embedding_err})"
        lines = [
            "📊 文件读取插件状态：",
            f"- 会话数: {sessions}",
            f"- 已入库文件: {total_files}",
            f"- 向量块总数: {total_chunks}",
            f"- 嵌入模型: {embed_state}",
            f"- 分块: {self.config.reader.chunk_size}/{self.config.reader.chunk_overlap}",
            f"- 检索 Top-K: {self.config.reader.retrieve_top_k}",
            f"- 全文直注阈值: {self.config.reader.direct_inject_max_chars} 字" + ("（关闭）" if int(self.config.reader.direct_inject_max_chars) <= 0 else ""),
            f"- 有效时间: {self.config.reader.file_retention_time}min / 最大 {self.config.reader.file_max_rounds} 轮",
            f"- 支持类型: {describe_supported_types()}",
            f"- NapCat 兜底: {'启用' if self.config.napcat.enabled else '停用'} ({self.config.napcat.http_url})",
        ]
        if self._embed_retry_queue:
            names = "、".join(f"{item['name']}(第{item['attempts']}次)" for item in self._embed_retry_queue[:5])
            lines.append(f"- ⏳ 待重试入库: {len(self._embed_retry_queue)} 个 — {names}")
        hint = getattr(self, "_last_file_hint", {}) or {}
        if hint:
            lines.append(
                f"- 最近文件消息: {hint.get('name')} 大小={hint.get('size')} mid={hint.get('mid')} 阶段={hint.get('stage')}"
            )
        text = "\n".join(lines)
        stream_id = str(kwargs.get("stream_id", "") or "")
        if stream_id:
            await self._reply(stream_id, text)
        return True, text, 2

    # ─── Tool：手动查询文件内容（供 LLM 主动调用） ────────────────
    def _resolve_tool_session(self, session_id: str) -> tuple[Optional[Any], str]:
        """工具调用的会话兜底解析。

        真机 18:51 日志教训：Planner 调 search_file 时首次传 session_id=''、
        之后干脆不传——文件 18:51:30 已入库，工具却三次误报「没有文件」。
        不能指望 LLM 每次都传对 session_id，规则：
        1) 显式 session_id 且该会话有文件 → 直接用；
        2) 否则回退「最近有文件入库的会话」（单会话场景必命中，
           多会话取最近一次文件上传的）。
        返回 (VectorStore 或 None, 实际采用的 session_id)。

        v1.0.16 教训：本方法曾插在 @Tool 装饰器与 search_file 定义之间，
        导致 @Tool 把 _resolve_tool_session 注册成了工具本体（真机报
        unexpected keyword argument 'query'）。普通辅助方法必须放在
        @Tool 装饰器之前定义。
        """
        if session_id:
            for (sid, _cid), candidate in self._store._sessions.items():
                if sid == session_id and candidate.files:
                    return candidate, session_id
        best: Optional[Any] = None
        best_ts = -1.0
        best_sid = ""
        for (sid, _cid), candidate in self._store._sessions.items():
            if not candidate.files:
                continue
            latest = max(e.upload_time for e in candidate.files.values())
            if latest > best_ts:
                best_ts = latest
                best = candidate
                best_sid = sid
        if best is not None and session_id and session_id != best_sid:
            self.ctx.logger.info(
                "search_file 传入 session_id=%s 无文件，回退到最近入库会话 %s", session_id, best_sid
            )
        return best, best_sid

    @Tool(
        "search_file",
        description=(
            "在当前会话已上传的文件中检索与问题相关的片段。"
            "当用户上传过文件（PDF/Word/Excel/PPT/代码等）或询问文件内容时调用；"
            "session_id 可留空，会自动定位最近上传文件的会话。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="要在文件中检索的问题或关键词",
                required=True,
            ),
            ToolParameterInfo(
                name="session_id",
                param_type=ToolParamType.STRING,
                description="当前会话 ID（可选，留空时自动定位最近上传文件的会话）",
                required=False,
            ),
        ],
    )
    async def search_file(self, query: str, session_id: str = "", **kwargs: Any) -> dict[str, str]:
        """检索并返回相关片段。"""
        del kwargs
        if not query.strip():
            return {"content": "请提供要检索的问题。"}
        vs, resolved_sid = self._resolve_tool_session(str(session_id or ""))
        if vs is None or not vs.files:
            return {"content": "当前会话还没有已入库的文件，请先上传文件。"}

        # 直注路径（v1.0.14）：小文件直接返回全文，不检索
        full_text = self._full_text_within_limit(vs)
        if full_text:
            self._tick_rounds_once(vs, query)  # v1.0.18 C3：同问题每轮只 +1
            names = "、".join(vs.files.keys())
            marker = self.config.reader.injection_marker
            return {
                "content": (
                    # v1.0.18 C3：带幂等标记，Planner 拿到全文后 hook 检测到标记即不再重复注入
                    f"{marker} 当前会话文件较小（≤ {int(self.config.reader.direct_inject_max_chars)} 字），"
                    f"直接给出全文（会话 {resolved_sid}，文件：{names}）：\n\n{full_text}"
                )
            }

        qvec = await self._embed_query(query)
        if qvec is None:
            return {"content": "嵌入模型不可用，无法检索。"}

        results = vs.retrieve(qvec)
        if not results:
            return {"content": "未在文件中检索到相关内容。"}
        marker = self.config.reader.injection_marker
        parts = [f"{marker} 检索到以下相关片段（会话 {resolved_sid}，已入库文件：{'、'.join(vs.files.keys())}）："]
        for i, r in enumerate(results, 1):
            parts.append(f"[{i}]「{r['file_name']}」({r['score']:.3f}):\n{r['text']}")
        self._tick_rounds_once(vs, query)  # v1.0.18 C3：同问题每轮只 +1
        return {"content": "\n\n".join(parts)}

    # ─── 后台清理循环 ────────────────────────────────────────────
    async def _start_cleanup_loop(self) -> None:
        async def loop() -> None:
            while True:
                try:
                    await asyncio.sleep(self.config.reader.cleanup_interval * 60)
                    retention = self.config.reader.file_retention_time * 60
                    max_rounds = self.config.reader.file_max_rounds
                    total_removed = 0
                    for (sid, _cid), vs in list(self._store._sessions.items()):
                        removed = vs.cleanup_expired(retention, max_rounds)
                        if removed:
                            self._memo_invalidate_session(sid)  # v1.0.18 C3：清理后失效该会话注入缓存
                        total_removed += len(removed)
                    if total_removed:
                        self.ctx.logger.info("后台清理：移除 %d 个过期文件", total_removed)
                    self._store.save_meta()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.ctx.logger.error("后台清理异常: %s", e)

        self._cleanup_task = asyncio.create_task(loop())

    # ─── embedding 失败后台重试队列（v1.0.11） ───────────────────
    async def _enqueue_embed_retry(
        self,
        session_id: str,
        conversation_id: str,
        stream_id: str,
        name: str,
        text: str,
    ) -> None:
        """把解析好但 embedding 失败的文件放进后台重试队列，并回复准确措辞。

        措辞设计：明确"文件已收到、内容已读出"，只是嵌入模型暂时无响应——
        避免 bot 之后从空检索结果瞎猜成「链接拉取超时」（真机 14:28 日志踩过）。
        """
        # 同名文件已在队列里（如 10s 去重窗口外的重复发送）：覆盖旧条目即可
        self._embed_retry_queue = [
            item for item in self._embed_retry_queue if not (item["session_id"] == session_id and item["name"] == name)
        ]
        max_retries = max(1, int(self.config.reader.embed_max_retries))
        self._embed_retry_queue.append(
            {
                "session_id": session_id,
                "conversation_id": conversation_id,
                "stream_id": stream_id,
                "name": name,
                "text": text,
                "attempts": 0,
                "next_ts": time.time() + 30.0,  # 首次重试等 30s（短期抖动大概率自愈）
            }
        )
        await self._reply(
            stream_id,
            f"⚠️ 文件「{name}」已收到、内容也读出来了，但嵌入模型暂时无响应。"
            f"我会每隔 {self.config.reader.embed_retry_interval:.0f} 分钟自动重试入库（最多 {max_retries} 次），"
            "成功后无需重新发送。",
        )

    async def _start_retry_loop(self) -> None:
        """后台定时扫描重试队列：到期的条目重新尝试入库。"""

        async def loop() -> None:
            while True:
                try:
                    await asyncio.sleep(20.0)  # 扫描频率 20s；条目自身带 next_ts 控制实际节奏
                    if not self._embed_retry_queue:
                        continue
                    now = time.time()
                    due = [item for item in self._embed_retry_queue if now >= item["next_ts"]]
                    if not due:
                        continue
                    interval = max(0.5, float(self.config.reader.embed_retry_interval)) * 60.0
                    still_queued: list[dict[str, Any]] = [
                        item for item in self._embed_retry_queue if now < item["next_ts"]
                    ]
                    for item in due:
                        item["attempts"] += 1
                        # max_retries 重试时实时读配置（真机 15:20 日志教训：入队时写死会
                        # 在长拥塞里耗尽；热改配置立即生效，正在排队的文件也能受益）
                        max_retries = max(1, int(self.config.reader.embed_max_retries))
                        try:
                            vs = self._store.get_or_create(
                                item["session_id"],
                                item["conversation_id"],
                                self._embed,
                                self.config.reader.chunk_size,
                                self.config.reader.chunk_overlap,
                                self.config.reader.retrieve_top_k,
                                chunk_merge=bool(self.config.reader.chunk_merge_enabled),
                                retrieve_use_matrix=bool(self.config.reader.retrieve_use_matrix),
                            )
                            entry = await vs.add_file(item["name"], item["text"])
                        except Exception as e:  # noqa: BLE001
                            if item["attempts"] >= max_retries:
                                self.ctx.logger.error(
                                    "文件 %s 重试 %d 次仍失败，放弃: %s", item["name"], item["attempts"], e
                                )
                                await self._reply(
                                    item["stream_id"],
                                    f"⚠️ 文件「{item['name']}」自动重试 {item['attempts']} 次仍未入库"
                                    "（嵌入模型持续无响应），麻烦重新发送一次文件。",
                                )
                                continue
                            item["next_ts"] = time.time() + interval
                            still_queued.append(item)
                            self.ctx.logger.warning(
                                "文件 %s 重试第 %d/%d 次失败，%.0f 分钟后再试: %s",
                                item["name"],
                                item["attempts"],
                                max_retries,
                                interval / 60.0,
                                e,
                            )
                            continue
                        # 成功入库
                        self.ctx.logger.info(
                            "重试入库成功 %s：%d 块（第 %d 次尝试，session=%s）",
                            item["name"],
                            len(entry.chunks),
                            item["attempts"],
                            item["session_id"],
                        )
                        if not self.config.reader.silent_success:
                            await self._reply(
                                item["stream_id"],
                                f"📄 文件「{item['name']}」已重试入库成功，切成 {len(entry.chunks)} 块。现在可以直接问我内容了。",
                            )
                    self._embed_retry_queue = still_queued
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.ctx.logger.error("embedding 重试循环异常: %s", e)

        self._retry_task = asyncio.create_task(loop())


def create_plugin() -> FileReaderPlugin:
    """创建插件实例。"""
    return FileReaderPlugin()
