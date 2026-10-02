# file-reader 插件性能与内存审计报告

> 只读分析（未修改任何代码），基于 v1.0.18 代码基线。
> 结论速览：**有明确优化空间**。架构本身很轻（embedding 走 host RPC，无本地 torch/transformers），但存在 3 处事件循环阻塞、2 处无界内存增长、1 处瞬时内存放大，以及 1 个顺带发现的正确性问题（httpx 未声明依赖）。优先级 1~4 均为 10 行内改动，可一次迭代完成。

## 一、项目结构与数据流

| 文件 | 大小 | 角色 |
|---|---|---|
| plugin.py | 86.7 KB（1694 行） | 入口：hook/命令/工具/下载/NapCat 兜底/重试队列/注入 |
| file_parser.py | 16.2 KB（426 行） | 各格式→纯文本（标准库优先 + 可选第三方库兜底） |
| vector_store.py | 13.6 KB（338 行） | 会话级向量库（numpy 余弦 + 矩阵缓存），内存驻留 |
| chunker.py | 4.4 KB（119 行） | 递归字符分块 + 贪心合并 |
| test_file_reader.py | 72.1 KB | 测试（非运行时路径） |
| README.md / _manifest.json | 28.8 KB / 1 KB | 文档 / 依赖声明（仅 numpy 为硬依赖） |

数据流：

```
消息接收（before/after_process 双 hook，_detect_file plugin.py:404）
→ 提取文件段（_extract_files :612 / _parse_file_hints :489 适配器降级文本）
→ 后台 task 处理（_handle_file :750）：bytes / 本地路径 read_bytes(:772)
   / httpx 下载(_download :1014) / NapCat 回溯(_resolve_via_napcat :988)
→ 写临时文件(:816) → to_thread 解析(:821) → 分块 + embedding 入库(vs.add_file, vector_store.py:96)
→ 检索注入（inject_file_context :1240, BLOCKING hook）：直注 / memo 缓存 / 查询 embedding + vs.retrieve(vector_store.py:207)
→ 后台：清理循环(:1551)、embedding 重试队列(:1612)、概要生成(:1178)
```

架构评价：v1.0.18 已做分块合并、矩阵检索、注入 memo、概要异步化，主要热点已解决。以下为剩余问题。

## 二、详细问题清单（按收益/改动成本排序）

### P0-1【高/低成本】本地路径读文件阻塞事件循环
- **位置**：plugin.py:772 `raw_bytes = Path(str(fdata["path"])).read_bytes()`；同 :787（napcat_path 分支）
- **根因**：`_handle_file` 是协程，`read_bytes` 同步读；上限 100MB 的文件读取会卡住整个事件循环（所有聊天、hook、其他文件处理全停顿）。
- **佐证**：下载路径用了 await，解析用了 `asyncio.to_thread`(:821)，唯独本地读两条路径裸奔。
- **建议**：包 `await asyncio.to_thread(Path(...).read_bytes)`。

### P0-2【高/中成本】下载非流式、无前置大小检查，瞬时内存 2~3 倍文件大小
- **位置**：plugin.py:1027-1030 `httpx.AsyncClient ... resp.content` 一次性读入；:806 大小检查发生在**下载完成之后**。
- **根因**：超限文件也先全量下载到内存；raw_bytes + base64 中间态 + 临时文件写盘 + 解析文本同时存活，100MB 上限下瞬时内存可达数百 MB。
- **建议**：① `client.stream("GET", url)` + `aiter_bytes` 边下边写临时文件，累计超 max_bytes 立即中断；②先 HEAD/Content-Length 预检；③ AsyncClient 提为插件级单例（连接复用），现在每次下载新建 client 与新 SSL 上下文。

### P0-3【高/中成本】分块 CPU 密集任务跑在事件循环里
- **位置**：vector_store.py:100 `chunks = self._chunker.chunk(content)`（add_file 是 async 但 chunk 是同步 CPU）；chunker.py:42-56 递归 `_split_text` 对超长文本 O(n×13 个分隔符) 扫描。
- **根因**：大文本（如 100 万字日志）递归分块可达秒级，期间事件循环冻结。
- **佐证**：调用处 _handle_file:825 直接 `await vs.add_file(...)`，无 to_thread。
- **建议**：chunk 调用包 `asyncio.to_thread`；或 chunker 对超大文本先按 "\n" 预切再递归。

### P1-4【中/低成本】空会话 VectorStore 永不回收
- **位置**：plugin.py:1559-1566 清理循环只调 `vs.cleanup_expired`（删文件），vector_store.py:235-248 只删 entry 不删空库；SessionStore._sessions(vector_store.py:264) 无上限。
- **根因**：文件全过期后，(session_id, conversation_id) → 空 VectorStore 壳永久驻留；长期运行的 bot 会话数无界增长（每群/每人一个）。
- **建议**：清理循环里 `if not vs.files: self._store.drop(sid, cid)`；成本一行。

### P1-5【中/低成本】后台 task 句柄全部丢弃，卸载时不取消，有 GC 风险
- **位置**：plugin.py:472 `asyncio.create_task(self._handle_file(...))`、:1202 `asyncio.create_task(_run())`（概要）。
- **根因**：①Python 官方警告：仅事件循环弱引用持有任务，可能被 GC 提前回收；②on_unload(:189) 只取消 cleanup/retry 两个 loop，进行中的下载/解析/概要任务泄漏，插件重载后旧任务仍跑并写旧 store。
- **建议**：维护 `self._bg_tasks: set`，create_task 后 add + done_callback discard；on_unload 统一 cancel。

### P1-6【中/低成本】重试队列持有完整解析文本最长约 1 小时
- **位置**：plugin.py:1593-1603 `_embed_retry_queue.append({..., "text": text, ...})`，embed_max_retries=30 × embed_retry_interval=2min。
- **根因**：embedding 拥塞期，每个失败文件的全文（可达数 MB）驻留最长 1 小时；多文件并发失败时内存叠加。入库成功后 `still_queued` 重建(:1682)，失败文件文本不被释放。
- **建议**：①重试条目改存临时文件路径而非全文；②或给队列设总字符数上限（如 20MB），超出放弃最早条目。

### P1-7【中/中成本】向量内存翻倍 + 原文双份驻留
- **位置**：vector_store.py:29 vectors（List[List[float]]，Python float 每维 ~32B）+ :178 `_mat_cache`（float32 矩阵，全库第二份向量）。
- **根因**：FileEntry.vectors 用 list-of-list 存 float，内存是 float32 的 ~8 倍；矩阵缓存再复制一份。
- **建议**：入库后转 `np.asarray(dtype=float32)` 单块数组，或只存 _mat_cache、entry 只留行范围；改动中等。

### P2-8【低/低成本】检索与会话查找的小复杂度问题
- vector_store.py:196 `np.argsort(scores)[::-1][:k]` 全排序 O(N log N)，改 `np.argpartition(-scores, k)[:k]` 再对 k 个排序 → O(N)。
- vector_store.py:222-233 逐条回退路径 `_cosine` 每个向量都 `np.asarray` 重建（矩阵开关默认开，影响小）。
- plugin.py:1254 / :1471-1489 `_resolve_tool_session` 每次线性扫全部会话 dict；可给 _sessions 加 sid→keys 二级索引。

### P2-9【低/低成本】正则与文本拼装
- plugin.py:508-515 `_parse_file_hints` 每次 hook 现编译 2 个正则，建议 `re.compile` 提模块级。
- file_parser.py:208-210 `_parse_xml` 兜底分支三个 `_re.sub` 未预编译（仅异常路径）。
- plugin.py:1264 `existing = " ".join(self._item_text(it) for it in items)` 每次 BLOCKING hook 拼接全部上下文 items 文本只为找 marker；建议只扫 SystemMessageItem 或从后往前找到即停。
- plugin.py:1120 直注路径 `''.join(entry.chunks)` 每次重新拼接全文（memo 未命中时）；可在 FileEntry 缓存 full_text。

### P2-10【低/低成本】缓慢泄漏的小字典
- plugin.py:165 `_summary_failed_ts`：失败文件名→时间戳，永不清理，无界增长（慢）。
- plugin.py:1184/1186 `_summary_pending` 用 `id(entry)`：entry 释放后 id 可被新对象复用，理论上误判 in-flight 导致某文件概要永远不生成；建议改用 (session_id, file_name) 或弱引用。
- plugin.py:153 `_recent_file_keys`：>64 才清理，有界 OK。

### P2-11【低/中成本】NapCat HTTP 每次新建 SSL 上下文
- plugin.py:882 `ssl.create_default_context()` 每次 `_napcat_post` 新建（~ms 级 × 每文件 2~3 次请求）。
- 建议：实例级缓存一个 context（verify_ssl 变化时重建）。

### 配置层现状（已较好，建议微调）
- **max_file_size 默认 100MB 偏大**：与 P0-2 叠加时内存风险主要来自这里，建议默认 20~30MB 或文档明示内存占用关系。
- cleanup_interval 默认 15min：过期文件最长多驻留 15min，可降到 5min。
- inject_memo 上限 128 条 × 单条 6000+ 字 ≈ 1MB 有界，OK。
- retrieve matrix / chunk_merge / embed_batch_size=64 / concurrency=2 默认合理。

### 依赖层
- 硬依赖仅 numpy，embedding 走 host RPC——已是最优架构，无需换。
- **pandas**（file_parser.py:42、251-257）：重依赖（~100MB+ 磁盘/内存），仅用于 xls/ods 与 xlsx 兜底；建议文档标注「pandas 非必需」，或 xlsx 兜底换 openpyxl 只读模式。
- **chardet**（:27）：可换 charset-normalizer，优先级低。
- **⚠️ httpx 未在 manifest dependencies 声明但 _download 必需（:1021）**——真机未装时下载路径直接废掉，建议补进 manifest（正确性问题）。

## 三、优化清单排序（收益/改动成本）

| 序 | 项 | 位置 | 根因 | 改法 | 影响 |
|---|---|---|---|---|---|
| 1 | 本地读文件阻塞 | plugin.py:772,787 | 协程内同步 read_bytes | 包 to_thread | 高 |
| 2 | 空会话不回收 | plugin.py:1559 + vector_store.py:235 | 只清文件不清空库 | 循环里 drop 空 vs | 高（长期运行） |
| 3 | 后台 task 无句柄 | plugin.py:472,1202 | create_task 裸调 | task set + unload cancel | 高（正确性+泄漏） |
| 4 | 流式下载+预检大小 | plugin.py:1014-1035,806 | 全量 content、后查大小 | stream + Content-Length + 单例 client | 高（大文件场景） |
| 5 | 分块进线程 | vector_store.py:100 | 同步 CPU 在事件循环 | to_thread | 中~高 |
| 6 | 重试队列存全文 | plugin.py:1599 | text 驻留最长 1h | 存临时文件/总量上限 | 中 |
| 7 | 向量内存翻倍 | vector_store.py:29,178 | list-of-float + 矩阵副本 | 统一 float32 ndarray | 中 |
| 8 | marker 全量扫 items | plugin.py:1264 | 每次 hook 拼全部文本 | 只扫 system items | 中（长上下文） |
| 9 | argpartition | vector_store.py:196 | 全排序 | argpartition top-k | 低~中 |
| 10 | SSL context/正则/小字典 | plugin.py:882,508,165,1184 | 重复构建/无界/id 复用 | 缓存/预编译/定期清理/弱引用 | 低 |
| 11 | pandas/chardet 减重；httpx 补声明 | file_parser.py:42,27；manifest | 重依赖兜底/漏声明 | 文档标注或 openpyxl；补 dependencies | 低（部署体积） |

**建议**：优先级 1~4 均为 10 行内改动，一次迭代即可完成；5~7 视真机负载决定；8~11 为顺手优化。
