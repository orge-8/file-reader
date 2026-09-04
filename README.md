# 文件读取（File Reader）

MaiBot 插件：解析 QQ 群/私聊上传的文件，分块向量化后按语义检索，把最相关内容注入 LLM 上下文，实现低开销的文件问答。

> 参考 [astrbot_plugin_file_reader_pro](https://github.com/zz6zz666/astrbot_plugin_file_reader_pro) 移植。AstrBot 版依赖其内置的 `FaissVecDB` / `RerankProvider`，MaiBot 无这些组件，故本插件改用 **numpy 余弦相似度自实现向量库** + MaiBot 原生 **`llm.embed`** 能力 + **`before_model_request` hook** 注入。

## 工作原理

```
[文件] → 解析(file_parser) → 递归分块(chunker) → 向量化(llm.embed) → 会话级向量库(vector_store)
[提问] → 语义检索 Top-K → 注入 LLM 上下文(before_model_request hook)
```

- 按 `(session_id, conversation_id)` 隔离，不同会话/对话的文件互不串扰。
- 文件有效时间（默认 60 分钟）+ 最大参与轮数（默认 5 轮）双条件过期，后台自动清理。
- 注入带幂等标记，重试不叠加；hook 全部 `ErrorPolicy.SKIP`，插件出错不阻断聊天。

## 安装

1. 将本目录放入 MaiBot 的 `plugins/` 文件夹，重启 MaiBot 或通过 WebUI 启用。
2. 安装依赖（见下）。

### 依赖

**核心格式零依赖**：docx / xlsx / pptx / csv / txt 及全部代码/文本类文件用 Python 标准库（zipfile + ElementTree / csv）直接解析，**无需安装任何第三方库**——插件跑在 MaiBot 的独立子进程里，这样本地与真机行为完全一致，也省去了找对 python.exe 装库的麻烦。

`numpy`（必需，已写入 manifest dependencies）。

第三方库仅作兜底或覆盖老格式，可按需安装：

```bash
pip install numpy            # 必需
pip install pdfminer.six     # PDF（无标准库解法，必装才能读 PDF）
pip install pandas openpyxl  # xls/ods 老表格格式（xlsx/csv 已零依赖）
pip install python-docx      # .doc 老格式（建议转存 .docx）
pip install chardet          # txt 编码检测（建议装，缺失时多编码轮询兜底）
```

## 配置

配置项在 `plugin.py` 的 `config_model` 中声明，首次加载后 Runner 自动生成 `config.toml`，可通过 WebUI 编辑。

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| plugin.enabled | bool | true | 是否启用插件 |
| reader.max_file_size | int | 100 | 单文件大小上限（MB） |
| reader.chunk_size | int | 512 | 分块大小（字符数） |
| reader.chunk_overlap | int | 100 | 分块重叠（字符数） |
| reader.chunk_merge_enabled | bool | true | 短碎片合并（v1.0.18）：相邻短碎片合并到接近 chunk_size 再成块，块数降至约 1/3，入库向量化耗时同步下降；关闭回退旧逐碎片成块 |
| reader.retrieve_top_k | int | 6 | 检索返回的相关块数量 |
| reader.retrieve_use_matrix | bool | true | 矩阵化检索（v1.0.18）：缓存 L2 归一化向量矩阵，查询时一次点积算全库相似度（入库/清理自动置脏重建）；块数多时检索开销显著下降；异常自动回退逐条余弦 |
| reader.file_retention_time | int | 60 | 文件有效时间（分钟） |
| reader.file_max_rounds | int | 5 | 文件最大参与轮数 |
| reader.cleanup_interval | int | 15 | 后台清理间隔（分钟） |
| reader.enable_group | bool | true | 是否处理群聊文件 |
| reader.insecure_download | bool | false | 下载文件时跳过 SSL 证书校验（内网 MITM 代理环境用，见常见问题） |
| reader.injection_marker | str | 【文件检索】 | 注入文本的幂等标记 |
| reader.silent_success | bool | true | 文件入库成功后保持静默（不发回执，日志仍记录）；设为 false 恢复「已解析 N 块」回执 |
| reader.silent_errors | bool | true | 错误回执静默（v1.0.20）：文件处理失败（不支持的类型/下载失败/超限/embedding 失败/重试耗尽等）只写日志、不发群消息；设为 false 恢复 ⚠️ 错误回执 |
| reader.embed_retry_interval | float | 2.0 | 嵌入失败后台重试间隔（分钟）；入库时 embedding 超时会先进后台队列定时重试 |
| reader.embed_max_retries | int | 30 | 后台重试最大次数（超过后放弃并提示重发文件；默认 ≈ 覆盖 1 小时拥塞，重试时实时读取、热改立即生效） |
| reader.embed_batch_size | int | 64 | 单次 llm.embed RPC 的最大文本条数；v1.0.18 配合分块合并从 16 提到 64（合并后单批约 8K 字仍远低于 30s RPC 上限），批数下降进一步缩短入库耗时；host 限流可调回 16 |
| reader.embed_concurrency | int | 2 | 入库拆批后的并发批数：多批同时调用 llm.embed（1 = 旧串行行为）；host 出现限流/批量报错时调回 1 |
| reader.query_embed_timeout | float | 8.0 | 提问时查询 embedding 的超时秒数：仅单次调用、超时立即放弃并降级概要兜底，不重试（避免拥塞期把用户卡在请求模型之前）；0 = 关闭超时（旧行为） |
| reader.direct_inject_max_chars | int | 6000 | 全文直注阈值（解析后总字符数）：会话内所有文件全文合计 ≤ 该值时，跳过 RAG 检索、直接把全文注入上下文（小文件全量可见且省一次查询 embedding）；0 = 关闭，始终检索 |
| reader.inject_memo_enabled | bool | true | 注入去重缓存（v1.0.18）：同一会话同一问题在 TTL 内直接复用上次注入文本（零 embedding、零检索），覆盖 hook 每次尝试重跑与 Planner/回复双触发；文件入库、清文件、会话清理时自动失效 |
| reader.inject_memo_ttl | int | 90 | 注入去重缓存的存活秒数 |
| reader.summary_enabled | bool | true | 大文件 LLM 概要：全文超 direct_inject_max_chars 的文件在入库后用 llm.generate 生成一段概要，注入上下文时附在检索片段前，让 LLM 对大文件先有整体认识；生成失败静默降级（不影响检索） |
| reader.summary_source_chars | int | 12000 | 生成概要时截取的源文本长度（字符数）：从文件头、中、尾三段均匀取样拼接，控制摘要调用的 token 开销 |
| reader.summary_max_chars | int | 500 | 概要文本的最大长度（字符数） |
| reader.summary_await_on_inject | bool | false | 提问注入时是否同步等待概要生成（v1.0.17 旧行为）；默认 false = 概要后台生成、不阻塞注入，下次提问自然带上 |
| reader.summary_retry_interval | float | 600.0 | 概要生成失败后的冷却秒数：冷却期内不再尝试生成（避免每次提问都撞一次超时）；0 = 不冷却 |
| **napcat.enabled** | bool | false | **启用 NapCat HTTP 兜底取文件**（见下，QQ 发文件必须开） |
| napcat.http_url | str | http://127.0.0.1:3001 | NapCat OneBot HTTP 服务地址 |
| napcat.access_token | str | 空 | NapCat HTTP 的 access_token（未设置则留空） |
| napcat.timeout | float | 15 | HTTP 请求超时（秒） |
| napcat.verify_ssl | bool | false | HTTPS 时校验证书（本地 http 无需开） |
| napcat.cache_dir | str | 空 | 可选：NapCat 文件缓存目录，`get_file` 失败时按文件名+大小在此搜索 |

### NapCat 兜底（重要）

真机实测（napcat-adapter）：适配器会把文件消息降级成纯文本 `[文件] 文章.docx，大小: 9547`，
**连 `chat.receive.before_process`（`SessionMessage.process()` 之前）拿到的也已经是 text 段**，
hook 层无论如何都拿不到文件本体。因此文件读取必须走 NapCat HTTP API 回溯：

1. 在 NapCat 里启用 OneBot **HTTP 服务端**（默认 `http://127.0.0.1:3001`），记下 access_token。
2. 插件配置里把 `napcat.enabled` 设为 `true`，填 `http_url` 与 `access_token`。
3. 完整重启 MaiBot（WebUI 改配置不会推送给运行中的插件）。
4. 发一个文件，看日志 `[napcat] get_msg 成功` → `[napcat] get_file 命中` → 回执「已解析」。

回溯链路：`[文件] xxx，大小: N` 文本 → 解析文件名 + 取 `message_id` → `get_msg` 拿原始事件 →
file 段的 `file_id` → `get_file` 换本地路径 → 读盘解析。任一步失败会在日志里留下 `[napcat] ...` 行。

## 命令

| 命令 | 说明 |
|---|---|
| `/clear_file` 或 `/clean_file` | 清除当前会话所有已入库文件 |
| `/file_status` | 查看插件状态（库规模/嵌入模型可用性/配置） |

## 使用

1. 在群里或私聊直接发送文件，插件自动完成：解析 → 分块 → 向量化 → 入库，并回执确认。
2. 直接提问（如「总结一下这个 PDF 的主要观点」「这份代码里有没有调用外部 API」），插件检索最相关片段注入上下文。

## 支持的文件类型

- 文档：`pdf` `docx` `doc` `rtf` `odt`
- 表格：`xlsx` `xls` `ods` `csv`
- 演示：`pptx` `ppt` `odp`
- 代码：`py` `java` `cpp` `c` `h` `hpp` `cs` `js` `ts` `php` `rb` `go` `rs` `swift` `kt` `scala` `sh` `bash` `ps1` `bat` `cmd`
- 标记：`md` `markdown` `html` `htm` `xml` `json` `yaml` `yml`
- 配置：`ini` `cfg` `conf` `properties` `env` `toml`
- 其他：`sql` `txt` `log` `lock` `gitignore` `url` `webloc`

## 自检 / 测试

```bash
# 结构自检
python tools/check_plugin.py plugins/file-reader

# 冒烟测试（完整生命周期）
python tools/smoke_test.py plugins/file-reader

# 功能自测（解析→分块→向量化→检索→清理，用伪 embedding 不依赖真模型）
python plugins/file-reader/test_file_reader.py
```

## 常见问题

- **插件未加载**：检查 `_manifest.json` 是否合法、`numpy` 是否安装、三个生命周期方法是否齐全。
- **embedding 不可用**：`/file_status` 查看；需在 MaiBot 配置里启用一个支持 embedding 的模型（`llm.embed` 能力）。
- **某类文件解析失败**：docx/xlsx/pptx/txt/代码已零依赖；PDF 需 `pdfminer.six`，xls/ods 需 `pandas openpyxl`（建议转存 xlsx/csv），按需安装。
- **发出文件后回执「适配器把文件降级成了纯文本」**：说明 `napcat.enabled` 未开——按上方「NapCat 兜底」章节配置 HTTP 与 token。
- **回执「NapCat 未返回文件内容」**：看日志里 `[napcat]` 开头的行；常见原因是 HTTP 服务未启用、token 错、`message_id` 对不上（可在 `/file_status` 里看「最近文件消息」的 mid），此时可配 `napcat.cache_dir` 走目录兜底。
- **下载报 CERTIFICATE_VERIFY_FAILED**：运行环境存在 TLS MITM 代理（如安全软件的 proxy-root-ca.cer）时，文件下载 URL 的证书链校验会失败；将 `reader.insecure_download` 设为 `true` 可跳过校验（仅建议在内网可控环境使用）。
- **Hook 报 `'ReaderConfig' object has no attribute 'xxx'`**：配置字段按 section 分层，`enabled` 在 `plugin` 段（`self.config.plugin.enabled`），读取参数在 `reader` 段；跨段误访问会直接 AttributeError（v1.0.0 真机踩过，v1.0.1 已修）。

## 更新日志

### v1.0.20（2026-09-04）错误回执静默

- **新增 `silent_errors` 配置（默认 true）**：文件处理失败（不支持的类型、下载失败、超限、embedding 失败/重试耗尽、NapCat 未启用等）不再发送 ⚠️ 错误回执到群，只写 `logger.warning` 日志；需要恢复错误回执时设为 `false`。
- **收口方法**：新增 `_reply_error(stream_id, text, *, reason)` 统一处理所有错误回执点，所有错误路径替换为 `_reply_error`，受 `silent_errors` 开关控制。
- **测试**：新增 silent_errors 静默测试（默认静默不回执，开关关闭恢复 ⚠️ 提示）+ 6 处既有测试适配（临时 `silent_errors = False` 验证错误回执可见性），53/53 PASS + GATE PASS。

### v1.0.19（2026-09-03）防抖合并消息多文件修复

- **修复：message-debounce-cn 防抖合并消息里只有第一个文件被处理**（真机 09-03 10:49 日志）：防抖插件会把连续多条文件消息合并成一条（最多观测到 14 个文件合并成 1 条），文本含多行 `[文件] xxx，大小: N，链接: https://...`；旧版 `re.search` 只取第一个 `[文件]`，其余文件全部静默丢弃——批量发 20 个文件只有 3~4 个入库，且无任何报错。
- **修复方案**：新增 `_parse_file_hints`（复数）逐行解析全部 `[文件]` 行，每行独立摘出自己的下载链接（URL 各自归属，不再只认第一个）；`_detect_file` 对所有解析出的文件各自入库。单文件旧语义经 `_parse_file_hint` 兼容保留。
- **测试**：新增防抖合并 3 行解析（文件名/大小/URL 逐行正确）+ 多文件端到端入库（各自走 URL 下载）2 个用例，52/52 PASS + GATE PASS。

### v1.0.18（2026-09-02）性能优化专项

五项优化（C1–C5），全部带独立开关可回滚旧行为；回归 49/49 PASS + GATE PASS。

- **C1 分块合并**（`chunk_merge_enabled`，默认开）：递归切分出的相邻短碎片合并到接近 `chunk_size` 再成块，块数降至约 1/3，入库向量化耗时同步下降；`FileEntry` 新增 `source_chars` 记录原文长度，直注/概要阈值判定不再被 overlap 膨胀误导。
- **C2 embedding 并发提速**（`embed_concurrency` + `embed_batch_size`）：入库拆批后多批并发调用 `llm.embed`（默认并发 2），`embed_batch_size` 默认 16 → 64；新增 `query_embed_timeout`（默认 8s）——提问时查询 embedding 仅单次调用、超时立即降级概要兜底不重试，避免拥塞期把用户卡在请求模型之前。
- **C3 注入 memo 去重**（`inject_memo_enabled` / `inject_memo_ttl`）：同一会话同一问题在 TTL（默认 90s）内直接复用上次注入文本，零 embedding、零检索；hook 检索路径与 search_file 工具输出共享幂等标记，互不重复注入；文件入库、清文件、会话清理时自动失效。
- **C3 轮数去重**：`_tick_rounds_once` 按（问题，120s 窗口）计数，同问题跨 hook/tool/attempt 多次触发只算 1 轮——修复 5 轮配额被双触发减半的问题。
- **C4 概要异步化**（`summary_await_on_inject`，默认 false）：提问注入不再同步等待概要生成（旧行为会把 BLOCKING hook 卡住数秒），概要在后台生成、下次提问自然带上；`id(entry)` in-flight 去重防止重复起任务；`summary_retry_interval`（默认 600s）失败冷却，避免每次提问都撞一次超时；入库预热与懒生成共用同一入口。
- **C5 矩阵化检索**（`retrieve_use_matrix`，默认开）：缓存 L2 归一化向量矩阵，查询时一次点积算全库相似度；入库/清理/删文件自动置脏重建；块数多时检索开销显著下降，结果与逐条余弦一致，异常自动回退旧路径。
- **测试**：42 → 49 用例，新增异步概要（不阻塞/in-flight 去重/失败冷却）、注入 memo（去重/失效/轮数去重/tool 标记）、矩阵检索（一致性/置脏/回退）等；新增 2b5 前排空后台任务等时序隔离措施。

### v1.0.17（2026-09-02）

- **大文件 LLM 概要注入**：全文超 `direct_inject_max_chars` 的文件，入库后用 `llm.generate` 生成一段概要（`FileEntry.summary` 缓存），注入上下文时概要附在检索片段之前——LLM 对大文件先有整体认识，再按需引用检索片段，缓解「只看到零散 chunk 不知道文件在讲什么」的问题。
- **概要取样策略**：源文本超 `summary_source_chars`（默认 12000 字）时按头/中/尾三段均匀取样拼接，控制摘要调用的 token 开销。
- **入库后后台预热**：大文件入库成功即 `asyncio.create_task` 预生成概要，不阻塞回执；注入时若发现还没生成会懒生成兜底（`_ensure_summary`）。
- **降级链路**：生成失败/异常一律静默降级，检索与注入不受影响；查询 embedding 失败但有概要时至少注入概要兜底；检索无命中但有概要时也注入概要。
- **manifest 变更**：capabilities 新增 `llm.generate` 声明——**真机部署时 `_manifest.json` 必须同步替换**（capabilities 变更不热更新，需完整重启）。

### v1.0.16（2026-09-02）

- **修复 search_file 装饰器错绑（真机 19:15 日志，v1.0.15 回归）**：v1.0.15 把辅助方法 `_resolve_tool_session` 插在了 `@Tool` 装饰器与 `search_file` 定义之间——装饰器绑定的是紧随其后的函数，导致 search_file 工具被注册到辅助方法上，真机每次调用报 `unexpected keyword argument 'query'`。本地测试是直调方法所以没暴露。已把辅助方法移到装饰器之前，并同类排查出 v1.0.14 的 `@HookHandler` 与 `inject_file_context` 之间也插了 `_full_text_within_limit`（同一错误模式），一并修复。
- **新增装饰器绑定回归测试**：静态检查全部 6 处 `@Tool/@HookHandler/@Command` 装饰器-函数配对，辅助方法误插装饰器后立即 FAIL——本地即可拦截此类「直调测不出、真机必炸」的错误。
- 回归 32/32 PASS，GATE PASS。

### v1.0.15（2026-09-02）

- **search_file 会话兜底（真机 18:51 日志修复）**：狸猫私聊场景——文件 18:51:30 已成功入库（81 块），但 Planner 三次调用 `search_file` 全部返回「当前会话还没有已入库的文件」。日志显示第一次调用传了 `session_id=''`、之后干脆不传——不能指望 LLM 每次都传对参数。现在 `_resolve_tool_session` 兜底：显式 session_id 命中直接用；否则自动定位「最近有文件入库的会话」（单会话场景必命中，多会话取最近上传的）；库为空时仍如实报告。工具描述同步注明「session_id 可留空」，返回结果携带实际会话 ID 与文件名清单，帮助 LLM 后续调用对齐。
- 回归 31/31 PASS（新增「空/错 session_id 兜底定位、空库如实报告」用例）。

### v1.0.14（2026-09-02）

- **小文件全文直注**：新增 `reader.direct_inject_max_chars`（默认 6000 字）——会话内所有文件解析后全文合计不超过该值时，注入 hook 跳过 RAG 检索，**直接把文件全文注入 LLM 上下文**。小文件不再被 Top-K 检索截断丢内容，且完全绕开查询 embedding 调用（拥塞期查询也可能超时，直注路径零 RPC）；`search_file` 工具同步受益，小文件直接返回全文。超过阈值回落原检索路径，`0` 可关闭直注。
- `/file_status` 新增「全文直注阈值」行。
- 回归 30/30 PASS（新增直注命中、超阈值回落、阈值 0 关闭、幂等标记、search_file 全文 5 个用例）。

### v1.0.13（2026-09-02）

- **embedding 大批量拆批调用（关键修复）**：真机 15:38 日志对比发现——黄石区 docx（38996 字节 ≈ 78 块）从 14:05 起**每次**都 30s 超时，而同期 A_Memorix / 表达向量的嵌入都成功；根因不只是拥塞，而是**把 78 块塞进一次 `llm.embed` RPC，单次调用本身就撞 cap.call 30s 上限**。现在按 `reader.embed_batch_size`（默认 16）拆批逐个调用再拼回结果，每次 RPC 远低于上限；某批失败则整单返回空（不拼残缺向量），由既有后台队列重试机制兜底。拆批进度有 INFO 日志。
- 回归 25/25 PASS（新增「10 条拆 4+4+2 三批合并」「拆批部分失败整单返回空」两用例）。

### v1.0.12（2026-09-02）

- **`embed_max_retries` 默认 10 → 30**：真机 15:00–15:20 日志显示 embedding 拥塞实际由宿主 LLM（LongCat-2.0）长调用拖垮事件循环引起，持续远超 10 次 × 2 分钟窗口；默认提到 30 次 ≈ 覆盖 1 小时拥塞。
- **max_retries 改为重试时实时读配置**：原先入队时把上限写死进条目，热调配置对已排队文件无效；现在每次重试实时读 `reader.embed_max_retries`，热改配置立即生效（正在排队的文件也能受益）。
- 回归 23/23 PASS（新增「max_retries 实时读配置」用例）。
- 另：真机日志确认 v1.0.11 的准确回执与队列重试节奏（约 3.5 分钟/轮）均按设计工作，bot 未再出现「链接超时」错误归因。

### v1.0.11（2026-09-02）

- **embedding 后台重试队列**：真机 14:27 日志显示 embedding 服务拥塞可持久超过 v1.0.10 的 6s 退避窗口（3 次尝试全撞 30s 超时），文件最终未入库。现在入库失败时把已解析文本放入内存队列，后台任务每 20s 扫描、按 `reader.embed_retry_interval`（默认 2 分钟）节奏自动重试，最多 `reader.embed_max_retries`（默认 10）次；成功后按 `silent_success` 决定是否回执，对用户基本透明。重试期间无需重发文件。
- **失败回执措辞修正**：原「⚠️ embedding 返回为空」会让 bot 之后从空检索结果瞎猜成「链接拉取超时」（真机 14:28 回复踩到）；改为「⚠️ 文件已收到、内容也读出来了，但嵌入模型暂时无响应，我会每隔 N 分钟自动重试入库」，bot 有据可依。
- `/file_status` 新增「⏳ 待重试入库」行（队列数量与文件名/已重试次数）；同名文件重复失败只保留一条队列记录（覆盖）。
- 回归 22/22 PASS（新增「失败入队+准确回执」「重试入库成功」「同名覆盖」三用例）。

### v1.0.10（2026-09-02）

- **embedding 自动重试**：`llm.embed` 失败时自动重试 2 次（退避 2s/4s），对瞬时拥塞（真机 14:05 日志：cap.call 30s 超时，同期 A_Memorix 也故障）基本免疫；重试过程有 WARNING 日志，耗尽后仍失败才报「embedding 返回为空」。`/file_status` 的嵌入模型状态在单次成功后即恢复「正常」。
- 回归 19/19 PASS（新增「重试后成功」「重试耗尽」两用例，用超时注入验证调用次数与状态翻转）+ GATE PASS。

### v1.0.9（2026-09-02）

- **新增 `reader.silent_success`（默认 true）**：文件入库成功后不再发送「📄 已解析…」回执，保持静默（日志仍记录 `已入库文件 xxx：N 块`）；解析失败/依赖缺失等错误提示照常发送。需要恢复成功回执时把 `reader.silent_success` 设为 `false`。
- 回归 17/17 PASS（新增静默模式用例）+ GATE PASS。

### v1.0.8（2026-09-02）

- **修复群聊文件消息解析失败**：群聊降级文本带下载链接尾巴（`[文件] x.docx，大小: 9547，链接: https://tjc-download.ftn.qq.com/...`），长 URL 被吞进文件名导致「不支持的文件类型: .(无扩展名)」（真机 13:27 日志踩到）。解析器先摘出链接再匹配文件名，URL 内部逗号保留完整。
- **新增下载兜底**：群聊形态的纯文本自带 QQ 文件下载链接，hint 解析出 url 后 `_handle_file` 优先走 HTTP 下载（受 `reader.insecure_download` 控制），NapCat 回溯降为第二道兜底。
- 回归 16/16 PASS（新增「群聊降级形态解析」用例）+ GATE PASS。

### v1.0.7（2026-09-02）

- **Office 三件套零依赖解析**：docx / xlsx / pptx 本质是 zip + XML，改用标准库 `zipfile` + `ElementTree` 直接解析（docx 按段落收集 `w:t` 含页眉页脚；xlsx 解析 sharedStrings + 多 sheet；pptx 按幻灯片顺序），**docx2txt / python-pptx / pandas 不再是 docx/xlsx/pptx/csv 的必要依赖**——真机不用再找插件子进程的 python.exe 装库，本地与真机行为一致。第三方库仅作兜底（结果为空或异常时自动重试）。
- XML 容错：部分 OOXML 写入器引用 `r:` 等前缀却不声明（`unbound prefix`），自动降级为本地名匹配重试。
- csv 改用标准库 `csv` 模块（原依赖 pandas）。
- 回归 15/15 PASS + GATE PASS。

### v1.0.6（2026-09-02）

- **新增 NapCat HTTP 兜底（文件读取的关键路径）**：真机证实 napcat-adapter 在 `chat.receive.before_process` **之前**就已把 file 段降级成 `[文件] xxx.docx，大小: N` 纯文本，hook 层拿不到任何文件本体。改为从降级文本解析文件名、取 `message_id`，再调 NapCat `get_msg` → `get_file` 拿回原始文件。
- 新增 `napcat` 配置段（enabled / http_url / access_token / timeout / verify_ssl / cache_dir）；`cache_dir` 为 `get_file` 失败时的目录兜底（按文件名+大小精确匹配）。
- 用标准库 `urllib` 实现 HTTP 客户端（不新增依赖），同步调用包 `asyncio.to_thread` 避免阻塞事件循环。
- 诊断增强：诊断行加 `mid=`（message_id，回溯 NapCat 的关键）；`/file_status` 新增「NapCat 兜底」状态行与「最近文件消息」（名称/大小/mid/阶段），便于核对回溯是否对得上。
- 未启用兜底时给出可操作回执（提示去开 `napcat.enabled`），不再静默失败。
- 修复文件名解析正则：非贪婪 `(.+?)` 未锚定会把「文章.txt」截成「文」——已用 `$` 锚定，测试覆盖。
- 回归 14/14 PASS（新增「NapCat 兜底取文件」与「未启用时的可操作提示」两个用例）+ GATE PASS。

### v1.0.5（2026-09-02）

- 修复诊断采样缺陷：v1.0.4 的「每阶段一次性」诊断会被第一条普通文本消息消耗，导致文件消息的载荷结构采不到（真机 12:36 日志踩到）。改为**疑似文件消息（`[文件]` 标记或含 file 段）每阶段最多打 5 条完整结构**，普通消息仍只打一行。
- 段 `data` 非 dict 时打印类型与值（此前一律显示 `-`，看不出 text 段到底存了什么）；诊断行增加 `plain=` 纯文本预览。
- 日志格式：`[诊断] file hook[before|after][FILE|msg] 触发: ...`。

### v1.0.4（2026-09-02）

- **新增 `chat.receive.before_process` hook（detect_file_early）**：v1.0.3 诊断证实 after_process 阶段文件已被宿主转成文本描述（`raw=list[text(-)]`）拿不到文件本体；官方事件管线中 before_process 在 `SessionMessage.process()` 之前触发，raw_message 仍可能是原始消息段——文件提取改由该阶段承担。
- 双阶段共用提取逻辑 + **10 秒去重**（key = session_id:文件名），同一文件在 before/after 各被看到一次也只处理一次。
- 诊断日志分阶段输出（`[诊断] file hook[before|after] 首次触发`），一次部署即可对比两阶段载荷结构。

### v1.0.3（2026-09-02）

- **适配 napcat 真机载荷**（v1.0.2 诊断日志实测采集）：`raw_message` 本身是消息段数组（list），直接扫描其中的 file 段——此前只处理 dict 形态导致提取不到；`session_id`/`stream_id` 从 `message.session_id` 顶层取（hook kwargs 无 session_id）；`message_info` 的 `user_info`/`group_info` 嵌套结构适配。
- 诊断日志增强：`raw_message` 为 list 时打印每段的 `type` 与 `data` 键名，后续字段适配无需再猜。
- 测试新增 napcat 真机形态回归用例（raw_message=list + message_info 嵌套 + kwargs 无 session_id）。
- 注意：解析 docx 需要 `docx2txt`（`pip install docx2txt`），真机未装时收到文件会提示安装。

### v1.0.2（2026-09-02）

- 诊断日志增强：`[诊断] file hook 首次触发` 移至 hook 入口最前（插件停用/载荷异常时也会留派发证据），新增 `message_type` 字段；`plugin.enabled=false` 时补一行提示。日志里出现该行 = 插件已加载且 hook 已派发；缺失 = 插件未加载或 hook 未派发。
- 排障指引：若文件消息进来后无任何 file-reader 日志，先发 `/file_status` 验证插件是否加载（无响应即未加载，查启动日志）。

### v1.0.1（2026-09-02）

- 修复真机 `AttributeError: 'ReaderConfig' object has no attribute 'enabled'`：`on_file_message` 中误从 `reader` 段访问 `enabled`，改为正确的 `plugin` 段。
- 新增首次触发一次性诊断日志（`[诊断] file hook 首次触发: ...`），用于采集真机消息载荷的实际字段结构。
- 增强 OneBot 段兼容：`{"type": "file", "data": {...}}` 嵌套形态、`raw_message.message` 数组、扩展字段名（file_id/filename/title 等）。
- 新增 `reader.insecure_download` 配置项，应对 MITM 代理环境下的证书校验失败。
- 测试增强：新增 hook 直调回归用例（`test_file_reader.py`），拦截配置误访问类错误。

## 与 AstrBot 原版的差异

| 能力 | AstrBot 原版 | 本 MaiBot 版 |
|---|---|---|
| 向量库 | FaissVecDB | numpy 余弦相似度（自实现） |
| 重排序 | RerankProvider | 无（仅余弦 Top-K） |
| 嵌入 | embedding_provider | `llm.embed` 能力 |
| 注入 | on_llm_request | `before_model_request` hook（items） |
| 文件获取 | `item.get_file()` | 入站消息 file/attachment 段；适配器降级时走 NapCat HTTP `get_msg`/`get_file` 回溯 |
