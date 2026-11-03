# 更新日志

本文件记录 `org.mai-mai.file-reader` 的行为变更。
README 只保留「原理 / 安装 / 配置 / 命令 / 安全策略 / 常见问题」等使用文档；
**版本细节一律记在这里** —— 新版本先改这里，再改 `_manifest.json` 的 `version`。

## 版本速查

| 版本 | 日期 | 主题 |
|---|---|---|
| v1.3.9 | 2026-11-03 | Planner 公告补「正在解析」状态（长解析期间不再被当成读不到） |
| v1.3.8 | 2026-11-03 | 长解析期间空会话壳被清理回收（文件「入库成功」却检索不到）+ 同一文件被处理两遍 |
| v1.3.7 | 2026-11-03 | 入库回执默认关闭（不再直发「已读取《X》，共 N 段」） |
| v1.3.6 | 2026-11-02 | 插件中心提交准备：兼容范围收敛、LICENSE、分类与隐私清理 |
| v1.3.5 | 2026-11-02 | 死代码清理：移除只写不读的落盘与无调用私有方法 |
| v1.3.4 | 2026-11-02 | 全文直注去重叠（`full_text()` 不再重复拼接 overlap） |
| v1.3.3 | 2026-11-02 | `search_file` 接住宿主注入的 stream_id（多会话不再误拒） |
| v1.3.2 | 2026-11-02 | 结构化 file 段直通（file_id）+ Planner 文件状态公告 |
| v1.3.1 | 2026-11-02 | 适配 SnowLuma v1.1.10+ 结构化 file 段：文件名回填 + 新 `[文件]` 文本解析 |
| v1.3.0 | 2026-10-30 | 正确性 + 混合检索（向量+BM25）+ 注入体验 + 格式层重建 |
| v1.2.1 | 2026-10-07 | 市场 review 加固：跨会话兜底收紧 + DNS 重绑定 + verify_ssl 默认值 |
| v1.2.0 | 2026-09-20 | 下载 SSRF 防护 + 依赖声明修正 |
| v1.1.0 | 2026-09-07 | 性能与内存专项优化 |
| v1.0.20 | 2026-09-04 | 错误回执静默 |
| v1.0.19 | 2026-09-03 | 防抖合并消息多文件修复 |
| v1.0.18 | 2026-09-02 | 性能优化专项 |
| v1.0.17 | 2026-09-02 |  |
| v1.0.16 | 2026-09-02 |  |
| v1.0.15 | 2026-09-02 |  |
| v1.0.14 | 2026-09-02 |  |
| v1.0.13 | 2026-09-02 |  |
| v1.0.12 | 2026-09-02 |  |
| v1.0.11 | 2026-09-02 |  |
| v1.0.10 | 2026-09-02 |  |
| v1.0.9 | 2026-09-02 |  |
| v1.0.8 | 2026-09-02 |  |
| v1.0.7 | 2026-09-02 |  |
| v1.0.6 | 2026-09-02 |  |
| v1.0.5 | 2026-09-02 |  |
| v1.0.4 | 2026-09-02 |  |
| v1.0.3 | 2026-09-02 |  |
| v1.0.2 | 2026-09-02 |  |
| v1.0.1 | 2026-09-02 |  |

---

## v1.0.1（2026-09-02）


- 修复真机 `AttributeError: 'ReaderConfig' object has no attribute 'enabled'`：`on_file_message` 中误从 `reader` 段访问 `enabled`，改为正确的 `plugin` 段。
- 新增首次触发一次性诊断日志（`[诊断] file hook 首次触发: ...`），用于采集真机消息载荷的实际字段结构。
- 增强 OneBot 段兼容：`{"type": "file", "data": {...}}` 嵌套形态、`raw_message.message` 数组、扩展字段名（file_id/filename/title 等）。
- 新增 `reader.insecure_download` 配置项，应对 MITM 代理环境下的证书校验失败。
- 测试增强：新增 hook 直调回归用例（`test_file_reader.py`），拦截配置误访问类错误。

## v1.0.2（2026-09-02）


- 诊断日志增强：`[诊断] file hook 首次触发` 移至 hook 入口最前（插件停用/载荷异常时也会留派发证据），新增 `message_type` 字段；`plugin.enabled=false` 时补一行提示。日志里出现该行 = 插件已加载且 hook 已派发；缺失 = 插件未加载或 hook 未派发。
- 排障指引：若文件消息进来后无任何 file-reader 日志，先发 `/file_status` 验证插件是否加载（无响应即未加载，查启动日志）。

## v1.0.3（2026-09-02）


- **适配 napcat 真机载荷**（v1.0.2 诊断日志实测采集）：`raw_message` 本身是消息段数组（list），直接扫描其中的 file 段——此前只处理 dict 形态导致提取不到；`session_id`/`stream_id` 从 `message.session_id` 顶层取（hook kwargs 无 session_id）；`message_info` 的 `user_info`/`group_info` 嵌套结构适配。
- 诊断日志增强：`raw_message` 为 list 时打印每段的 `type` 与 `data` 键名，后续字段适配无需再猜。
- 测试新增 napcat 真机形态回归用例（raw_message=list + message_info 嵌套 + kwargs 无 session_id）。
- 注意：解析 docx 需要 `docx2txt`（`pip install docx2txt`），真机未装时收到文件会提示安装。

## v1.0.4（2026-09-02）


- **新增 `chat.receive.before_process` hook（detect_file_early）**：v1.0.3 诊断证实 after_process 阶段文件已被宿主转成文本描述（`raw=list[text(-)]`）拿不到文件本体；官方事件管线中 before_process 在 `SessionMessage.process()` 之前触发，raw_message 仍可能是原始消息段——文件提取改由该阶段承担。
- 双阶段共用提取逻辑 + **10 秒去重**（key = session_id:文件名），同一文件在 before/after 各被看到一次也只处理一次。
- 诊断日志分阶段输出（`[诊断] file hook[before|after] 首次触发`），一次部署即可对比两阶段载荷结构。

## v1.0.5（2026-09-02）


- 修复诊断采样缺陷：v1.0.4 的「每阶段一次性」诊断会被第一条普通文本消息消耗，导致文件消息的载荷结构采不到（真机 12:36 日志踩到）。改为**疑似文件消息（`[文件]` 标记或含 file 段）每阶段最多打 5 条完整结构**，普通消息仍只打一行。
- 段 `data` 非 dict 时打印类型与值（此前一律显示 `-`，看不出 text 段到底存了什么）；诊断行增加 `plain=` 纯文本预览。
- 日志格式：`[诊断] file hook[before|after][FILE|msg] 触发: ...`。

## v1.0.6（2026-09-02）


- **新增 NapCat HTTP 兜底（文件读取的关键路径）**：真机证实 napcat-adapter 在 `chat.receive.before_process` **之前**就已把 file 段降级成 `[文件] xxx.docx，大小: N` 纯文本，hook 层拿不到任何文件本体。改为从降级文本解析文件名、取 `message_id`，再调 NapCat `get_msg` → `get_file` 拿回原始文件。
- 新增 `napcat` 配置段（enabled / http_url / access_token / timeout / verify_ssl / cache_dir）；`cache_dir` 为 `get_file` 失败时的目录兜底（按文件名+大小精确匹配）。
- 用标准库 `urllib` 实现 HTTP 客户端（不新增依赖），同步调用包 `asyncio.to_thread` 避免阻塞事件循环。
- 诊断增强：诊断行加 `mid=`（message_id，回溯 NapCat 的关键）；`/file_status` 新增「NapCat 兜底」状态行与「最近文件消息」（名称/大小/mid/阶段），便于核对回溯是否对得上。
- 未启用兜底时给出可操作回执（提示去开 `napcat.enabled`），不再静默失败。
- 修复文件名解析正则：非贪婪 `(.+?)` 未锚定会把「文章.txt」截成「文」——已用 `$` 锚定，测试覆盖。
- 回归 14/14 PASS（新增「NapCat 兜底取文件」与「未启用时的可操作提示」两个用例）+ GATE PASS。

## v1.0.7（2026-09-02）


- **Office 三件套零依赖解析**：docx / xlsx / pptx 本质是 zip + XML，改用标准库 `zipfile` + `ElementTree` 直接解析（docx 按段落收集 `w:t` 含页眉页脚；xlsx 解析 sharedStrings + 多 sheet；pptx 按幻灯片顺序），**docx2txt / python-pptx / pandas 不再是 docx/xlsx/pptx/csv 的必要依赖**——真机不用再找插件子进程的 python.exe 装库，本地与真机行为一致。第三方库仅作兜底（结果为空或异常时自动重试）。
- XML 容错：部分 OOXML 写入器引用 `r:` 等前缀却不声明（`unbound prefix`），自动降级为本地名匹配重试。
- csv 改用标准库 `csv` 模块（原依赖 pandas）。
- 回归 15/15 PASS + GATE PASS。

## v1.0.8（2026-09-02）


- **修复群聊文件消息解析失败**：群聊降级文本带下载链接尾巴（`[文件] x.docx，大小: 9547，链接: https://tjc-download.ftn.qq.com/...`），长 URL 被吞进文件名导致「不支持的文件类型: .(无扩展名)」（真机 13:27 日志踩到）。解析器先摘出链接再匹配文件名，URL 内部逗号保留完整。
- **新增下载兜底**：群聊形态的纯文本自带 QQ 文件下载链接，hint 解析出 url 后 `_handle_file` 优先走 HTTP 下载（受 `reader.insecure_download` 控制），NapCat 回溯降为第二道兜底。
- 回归 16/16 PASS（新增「群聊降级形态解析」用例）+ GATE PASS。

## v1.0.9（2026-09-02）


- **新增 `reader.silent_success`（默认 true）**：文件入库成功后不再发送「📄 已解析…」回执，保持静默（日志仍记录 `已入库文件 xxx：N 块`）；解析失败/依赖缺失等错误提示照常发送。需要恢复成功回执时把 `reader.silent_success` 设为 `false`。
- 回归 17/17 PASS（新增静默模式用例）+ GATE PASS。

## v1.0.10（2026-09-02）


- **embedding 自动重试**：`llm.embed` 失败时自动重试 2 次（退避 2s/4s），对瞬时拥塞（真机 14:05 日志：cap.call 30s 超时，同期 A_Memorix 也故障）基本免疫；重试过程有 WARNING 日志，耗尽后仍失败才报「embedding 返回为空」。`/file_status` 的嵌入模型状态在单次成功后即恢复「正常」。
- 回归 19/19 PASS（新增「重试后成功」「重试耗尽」两用例，用超时注入验证调用次数与状态翻转）+ GATE PASS。

## v1.0.11（2026-09-02）


- **embedding 后台重试队列**：真机 14:27 日志显示 embedding 服务拥塞可持久超过 v1.0.10 的 6s 退避窗口（3 次尝试全撞 30s 超时），文件最终未入库。现在入库失败时把已解析文本放入内存队列，后台任务每 20s 扫描、按 `reader.embed_retry_interval`（默认 2 分钟）节奏自动重试，最多 `reader.embed_max_retries`（默认 10）次；成功后按 `silent_success` 决定是否回执，对用户基本透明。重试期间无需重发文件。
- **失败回执措辞修正**：原「⚠️ embedding 返回为空」会让 bot 之后从空检索结果瞎猜成「链接拉取超时」（真机 14:28 回复踩到）；改为「⚠️ 文件已收到、内容也读出来了，但嵌入模型暂时无响应，我会每隔 N 分钟自动重试入库」，bot 有据可依。
- `/file_status` 新增「⏳ 待重试入库」行（队列数量与文件名/已重试次数）；同名文件重复失败只保留一条队列记录（覆盖）。
- 回归 22/22 PASS（新增「失败入队+准确回执」「重试入库成功」「同名覆盖」三用例）。

## v1.0.12（2026-09-02）


- **`embed_max_retries` 默认 10 → 30**：真机 15:00–15:20 日志显示 embedding 拥塞实际由宿主 LLM（LongCat-2.0）长调用拖垮事件循环引起，持续远超 10 次 × 2 分钟窗口；默认提到 30 次 ≈ 覆盖 1 小时拥塞。
- **max_retries 改为重试时实时读配置**：原先入队时把上限写死进条目，热调配置对已排队文件无效；现在每次重试实时读 `reader.embed_max_retries`，热改配置立即生效（正在排队的文件也能受益）。
- 回归 23/23 PASS（新增「max_retries 实时读配置」用例）。
- 另：真机日志确认 v1.0.11 的准确回执与队列重试节奏（约 3.5 分钟/轮）均按设计工作，bot 未再出现「链接超时」错误归因。

## v1.0.13（2026-09-02）


- **embedding 大批量拆批调用（关键修复）**：真机 15:38 日志对比发现——黄石区 docx（38996 字节 ≈ 78 块）从 14:05 起**每次**都 30s 超时，而同期 A_Memorix / 表达向量的嵌入都成功；根因不只是拥塞，而是**把 78 块塞进一次 `llm.embed` RPC，单次调用本身就撞 cap.call 30s 上限**。现在按 `reader.embed_batch_size`（默认 16）拆批逐个调用再拼回结果，每次 RPC 远低于上限；某批失败则整单返回空（不拼残缺向量），由既有后台队列重试机制兜底。拆批进度有 INFO 日志。
- 回归 25/25 PASS（新增「10 条拆 4+4+2 三批合并」「拆批部分失败整单返回空」两用例）。

## v1.0.14（2026-09-02）


- **小文件全文直注**：新增 `reader.direct_inject_max_chars`（默认 6000 字）——会话内所有文件解析后全文合计不超过该值时，注入 hook 跳过 RAG 检索，**直接把文件全文注入 LLM 上下文**。小文件不再被 Top-K 检索截断丢内容，且完全绕开查询 embedding 调用（拥塞期查询也可能超时，直注路径零 RPC）；`search_file` 工具同步受益，小文件直接返回全文。超过阈值回落原检索路径，`0` 可关闭直注。
- `/file_status` 新增「全文直注阈值」行。
- 回归 30/30 PASS（新增直注命中、超阈值回落、阈值 0 关闭、幂等标记、search_file 全文 5 个用例）。

## v1.0.15（2026-09-02）


- **search_file 会话兜底（真机 18:51 日志修复）**：狸猫私聊场景——文件 18:51:30 已成功入库（81 块），但 Planner 三次调用 `search_file` 全部返回「当前会话还没有已入库的文件」。日志显示第一次调用传了 `session_id=''`、之后干脆不传——不能指望 LLM 每次都传对参数。现在 `_resolve_tool_session` 兜底：显式 session_id 命中直接用；否则自动定位「最近有文件入库的会话」（单会话场景必命中，多会话取最近上传的）；库为空时仍如实报告。工具描述同步注明「session_id 可留空」，返回结果携带实际会话 ID 与文件名清单，帮助 LLM 后续调用对齐。
- 回归 31/31 PASS（新增「空/错 session_id 兜底定位、空库如实报告」用例）。

## v1.0.16（2026-09-02）


- **修复 search_file 装饰器错绑（真机 19:15 日志，v1.0.15 回归）**：v1.0.15 把辅助方法 `_resolve_tool_session` 插在了 `@Tool` 装饰器与 `search_file` 定义之间——装饰器绑定的是紧随其后的函数，导致 search_file 工具被注册到辅助方法上，真机每次调用报 `unexpected keyword argument 'query'`。本地测试是直调方法所以没暴露。已把辅助方法移到装饰器之前，并同类排查出 v1.0.14 的 `@HookHandler` 与 `inject_file_context` 之间也插了 `_full_text_within_limit`（同一错误模式），一并修复。
- **新增装饰器绑定回归测试**：静态检查全部 6 处 `@Tool/@HookHandler/@Command` 装饰器-函数配对，辅助方法误插装饰器后立即 FAIL——本地即可拦截此类「直调测不出、真机必炸」的错误。
- 回归 32/32 PASS，GATE PASS。

## v1.0.17（2026-09-02）


- **大文件 LLM 概要注入**：全文超 `direct_inject_max_chars` 的文件，入库后用 `llm.generate` 生成一段概要（`FileEntry.summary` 缓存），注入上下文时概要附在检索片段之前——LLM 对大文件先有整体认识，再按需引用检索片段，缓解「只看到零散 chunk 不知道文件在讲什么」的问题。
- **概要取样策略**：源文本超 `summary_source_chars`（默认 12000 字）时按头/中/尾三段均匀取样拼接，控制摘要调用的 token 开销。
- **入库后后台预热**：大文件入库成功即 `asyncio.create_task` 预生成概要，不阻塞回执；注入时若发现还没生成会懒生成兜底（`_ensure_summary`）。
- **降级链路**：生成失败/异常一律静默降级，检索与注入不受影响；查询 embedding 失败但有概要时至少注入概要兜底；检索无命中但有概要时也注入概要。
- **manifest 变更**：capabilities 新增 `llm.generate` 声明——**真机部署时 `_manifest.json` 必须同步替换**（capabilities 变更不热更新，需完整重启）。

## v1.0.18（2026-09-02）性能优化专项


五项优化（C1–C5），全部带独立开关可回滚旧行为；回归 49/49 PASS + GATE PASS。

- **C1 分块合并**（`chunk_merge_enabled`，默认开）：递归切分出的相邻短碎片合并到接近 `chunk_size` 再成块，块数降至约 1/3，入库向量化耗时同步下降；`FileEntry` 新增 `source_chars` 记录原文长度，直注/概要阈值判定不再被 overlap 膨胀误导。
- **C2 embedding 并发提速**（`embed_concurrency` + `embed_batch_size`）：入库拆批后多批并发调用 `llm.embed`（默认并发 2），`embed_batch_size` 默认 16 → 64；新增 `query_embed_timeout`（默认 8s）——提问时查询 embedding 仅单次调用、超时立即降级概要兜底不重试，避免拥塞期把用户卡在请求模型之前。
- **C3 注入 memo 去重**（`inject_memo_enabled` / `inject_memo_ttl`）：同一会话同一问题在 TTL（默认 90s）内直接复用上次注入文本，零 embedding、零检索；hook 检索路径与 search_file 工具输出共享幂等标记，互不重复注入；文件入库、清文件、会话清理时自动失效。
- **C3 轮数去重**：`_tick_rounds_once` 按（问题，120s 窗口）计数，同问题跨 hook/tool/attempt 多次触发只算 1 轮——修复 5 轮配额被双触发减半的问题。
- **C4 概要异步化**（`summary_await_on_inject`，默认 false）：提问注入不再同步等待概要生成（旧行为会把 BLOCKING hook 卡住数秒），概要在后台生成、下次提问自然带上；`id(entry)` in-flight 去重防止重复起任务；`summary_retry_interval`（默认 600s）失败冷却，避免每次提问都撞一次超时；入库预热与懒生成共用同一入口。
- **C5 矩阵化检索**（`retrieve_use_matrix`，默认开）：缓存 L2 归一化向量矩阵，查询时一次点积算全库相似度；入库/清理/删文件自动置脏重建；块数多时检索开销显著下降，结果与逐条余弦一致，异常自动回退旧路径。
- **测试**：42 → 49 用例，新增异步概要（不阻塞/in-flight 去重/失败冷却）、注入 memo（去重/失效/轮数去重/tool 标记）、矩阵检索（一致性/置脏/回退）等；新增 2b5 前排空后台任务等时序隔离措施。

## v1.0.19（2026-09-03）防抖合并消息多文件修复


- **修复：message-debounce-cn 防抖合并消息里只有第一个文件被处理**（真机 09-03 10:49 日志）：防抖插件会把连续多条文件消息合并成一条（最多观测到 14 个文件合并成 1 条），文本含多行 `[文件] xxx，大小: N，链接: https://...`；旧版 `re.search` 只取第一个 `[文件]`，其余文件全部静默丢弃——批量发 20 个文件只有 3~4 个入库，且无任何报错。
- **修复方案**：新增 `_parse_file_hints`（复数）逐行解析全部 `[文件]` 行，每行独立摘出自己的下载链接（URL 各自归属，不再只认第一个）；`_detect_file` 对所有解析出的文件各自入库。单文件旧语义经 `_parse_file_hint` 兼容保留。
- **测试**：新增防抖合并 3 行解析（文件名/大小/URL 逐行正确）+ 多文件端到端入库（各自走 URL 下载）2 个用例，52/52 PASS + GATE PASS。

## v1.0.20（2026-09-04）错误回执静默


- **新增 `silent_errors` 配置（默认 true）**：文件处理失败（不支持类型、下载失败、超限、embedding 失败/重试耗尽、NapCat 未启用等）不再发送 ⚠️ 错误回执到群，只写 `logger.warning` 日志；需要恢复错误回执时设为 `false`。
- **收口方法**：新增 `_reply_error(stream_id, text, *, reason)` 统一处理所有错误回执点，所有错误路径替换为 `_reply_error`，受 `silent_errors` 开关控制。
- **测试**：新增 silent_errors 静默测试（默认静默不回执，开关关闭恢复 ⚠️ 提示）+ 6 处既有测试适配（临时 `silent_errors = False` 验证错误回执可见性），53/53 PASS + GATE PASS。

## v1.1.0（2026-09-07）性能与内存专项优化


审计报告见 `PERF_AUDIT.md`（11 项），本版全部修复：

**事件循环阻塞（P0）**
- **本地路径读文件进线程**：`_handle_file` 里 `Path.read_bytes()`（含 NapCat 落盘路径分支）是同步 IO，100MB 级文件会冻结整个事件循环（所有聊天/hook 全停顿）；现包 `asyncio.to_thread`，与解析路径行为对齐。
- **分块进线程**：递归分块是 CPU 密集任务（百万字文本可达秒级），`VectorStore.add_file` 的 `chunker.chunk()` 同样包 `to_thread`。
- **下载流式化 + 双重预检**：Content-Length 超限直接中断（不开始下载）；流式边下边累计，超限立即抛 `_DownloadTooLargeError`——旧版 `resp.content` 全量入内存且大小检查在下载完成之后，超限文件也先全下（瞬时内存 2~3 倍文件大小）。超限回执措辞不变。
- **httpx AsyncClient 单例**：连接池复用，免每次下载新建 client + SSL 上下文；`insecure_download` 配置变化时自动重建。

**内存无界增长（P1）**
- **空会话回收**：文件全过期后，(session_id, conversation_id) → 空 VectorStore 壳永久驻留（每群/每人一个）；清理循环现 `drop` 空库。
- **后台任务句柄注册表**：`create_task` 裸调只靠事件循环弱引用持有（有 GC 提前回收风险），且进行中的下载/解析/概要任务在 `on_unload` 后泄漏、插件重载后仍写旧 store；现统一走 `_spawn_bg()` 注册，卸载时全部取消并等待收尾。
- **重试队列不再持有全文**：条目从「解析全文」（可达数 MB × 最长 1 小时驻留）改为「临时文件路径」，重试到期时重新读取解析；成功/放弃/覆盖/on_unload 时均删除临时文件；临时文件丢失（系统清 temp）时明确提示重发。

**内存占用与计算效率（P2）**
- **向量统一 float32 ndarray**：入库后 `List[List[float]]`（Python float 每维 ~32B）转 `np.float32` 单块数组，内存约降 8 倍；矩阵构建/余弦直接复用；无 numpy 时自动回退列表。
- **检索 Top-K 用 argpartition**：O(N log N) 全排序 → O(N) 选 K 再排序；库大时有感。
- **注入幂等检查早退**：逐项检查 marker 即停，不再把全部上下文 items 拼成大字符串。
- **直注/概要全文缓存**：`FileEntry.full_text()` 缓存 join 结果，memo 未命中时不再每次 hook 重新拼接。
- **概要 pending 键修正**：`id(entry)` → `file_name`——CPython 下 entry 释放后 id 可能被新对象复用，理论上误判 in-flight 导致某文件概要永不生成。
- **概要冷却表裁剪**：`_summary_failed_ts` 24h 过期 + 总量封顶 256，随清理周期执行。
- **正则预编译**：降级文本解析 2 个正则、OOXML 前缀修正 3 个正则提到模块级。
- **NapCat SSL 上下文缓存**：`ssl.create_default_context()` 是 ms 级 CPU 调用，旧版每次 `_napcat_post` 新建；现实例级缓存（verify_ssl 变化时重建）。

**依赖与配置**
- **manifest 补声明 `httpx>=0.24`**：URL 下载路径的必需依赖，旧版漏声明——真机未装 httpx 时下载功能直接废掉。
- **`max_file_size` 默认 100 → 30MB**：与流式预检配合，内存峰值可控；大内存机器可调回。
- **`cleanup_interval` 默认 15 → 5 分钟**：过期文件最长多驻留时间同步下降；空会话回收/冷却表裁剪也挂在这个周期上。

## v1.2.0（2026-09-20）下载 SSRF 防护 + 依赖声明修正


针对插件市场 review 的三条阻断项与一条提醒：

- **【安全·阻断】URL 下载加白名单与地址校验（SSRF）**。旧版 `_download` 对 URL 不做任何域名或目标地址校验：
  而该 URL 来自消息正文（`_parse_file_hints` 从 `链接: ...` 抠出来），等于任何群成员都能
  让插件去 GET `http://127.0.0.1:...` 或 `http://169.254.169.254/latest/meta-data` 并把响应解析进上下文。
  现新增模块级校验层，下载前必过：协议（仅 http/https）→ 拒绝内嵌凭据 → 域名后缀白名单
  → **解析后每个 IP 必须为公网**。默认白名单为 QQ 文件域，**只比域名字符串挡不住**
  `http://2130706433/`、`http://0x7f000001/`、`http://127.0.0.1.nip.io/` 这类写法，所以校验落在解析结果上。
  新增配置：`download_url_policy` / `download_allowed_hosts` / `download_max_redirects` /
  `download_allow_private_hosts`（详见「下载安全策略」一节）。
- **【安全】重定向逐跳校验**。httpx 自动跟随关闭，改为手动逐跳：每一跳重新过完整校验，
  跳数受 `download_max_redirects` 限制——否则白名单域名 302 到内网即可绕过。
  副作用是修复了旧版「QQ 链接 302 时下载直接失败」的行为。
- **【安全】`insecure_download` 不再是绕开 TLS 的唯一手段**。新增 `download_ca_bundle`：
  指向 MITM 代理根证书即可保持证书校验开启（更推荐）。启动自检会把
  `insecure_download` / `download_allow_private_hosts` / `policy=off` 这些风险配置
  以 `[自检][安全] 风险配置：...` 显式打日志。
- **【安全】下载日志脱敏**：QQ 下载链接 query 带凭据，日志只留 scheme+host+path。
- **【阻断】manifest `urls.repository` 修正**为实际仓库 `github.com/orge-8/file-reader`
  （旧值误写为 `maibot-file-reader`）。
- **【阻断】manifest 补声明 `pdfminer.six`**：PDF 无标准库解析路径，缺库时只报「缺少依赖」，
  而描述里 PDF 是核心能力——属功能与声明不符，现作为必需依赖由 manifest 声明。
- **【修复】两处测试用例缺陷**（v1.1.0 自测里就存在，是它掩盖了上面的问题）：
  - `1j2 拆批部分失败` 用例引用了未定义的 `batch_seq`，首次调用即抛 `NameError`，
    于是"第 1 批失败 → 整单返回空"也满足断言 `result == {}`，**用例本身完全没验证到目标场景**。
    已改为按批内容区分失败批，并反查模拟调用序列 `["a","d","d"]` 锁死"第 2 批重试全失败"。
  - `1g3 多文件端到端` 用固定 `await asyncio.sleep(0.5)` 等并发后台任务，属竞态（偶发误报少文件）。
    已改为轮询等待条件成立。
- **【修复】`is_public_ip` 不能只依赖 `is_global`**：CPython 的 IPv4 `is_global` 不把组播
  `224.0.0.0/4` 算作非公网（实测返回 True），已显式叠加 `is_multicast`/`is_reserved`/
  `is_link_local`/`is_loopback`/`is_private`/`is_unspecified` 判定（由新增测试抓出）。
- 新增 15 条 SSRF 回归用例，含"白名单域名被 302 到云元数据后 **169.254.169.254 从未被请求**"
  与"首跳即被拒时零请求发出"两条以"实际发包记录"为证据的断言。
- **补齐测试基建，让门禁不再是假绿**（v1.1.0 门禁只跑了 `check_plugin` 一项，
  冒烟与 pytest 都是缺席状态，所以"全绿"毫无意义）：
  - 新增 `tests/smoke_test.py`（9 项）与 `tests/fakehost.py`：装入插件跑完整生命周期，
    覆盖组件清单（数量/类型/处理器名，专抓装饰器绑错人）、manifest 一致性、
    SSRF 守卫、风险配置告警、卸载收尾、hook 只读性、命令回执。
  - 新增 `tests/test_download_guard.py`（60 项参数化用例）：把下载安全契约做成
    门禁能跑到的一份，含"解析结果而非域名字符串""混合 DNS 结果也拒""非法策略退回 whitelist"。
  - 门禁结果从 `PASS 1 SKIP 0 FAIL 0`（假绿）变为 `PASS 3 SKIP 0 FAIL 0`。
  - 另修：`tests/smoke_test.py` 里日志断言的 `_LogCapture` 曾把 `getMessage()` 二次格式化，
    退化成未填参的模板文本，导致告警断言永远不匹配（自测写错，非产品缺陷）。

## v1.2.1（2026-10-07）市场 review 加固：跨会话兜底收紧 + DNS 重绑定 + verify_ssl 默认值


三条加固全部来自插件中心 AI review（均非阻断项，本轮主动收掉）：

- **search_file 跨会话兜底收紧（隐私向，review 第 1 条）**：旧版 LLM 不传/传错 session_id 时回退「最近有文件入库的会话」——多人群场景下等于把 A 会话上传的文件正文塞进 B 会话的 LLM 上下文，且真机日志证明 Planner 确实经常不传该参数，不是纸上风险。现在 `_resolve_tool_session` 规则改为：显式 session_id 命中直接用；**仅当全局只有一个会话存有文件时**才自动定位（v1.0.15 的单会话场景不回退）；多会话歧义时**拒绝兜底**（fail-closed），返回可执行的指引（提示补传 session_id）并打 WARNING 日志留痕，拒绝路径不泄露任何会话信息。hook 侧 `inject_file_context` 本就是精确匹配注入，不受影响。工具描述同步更新，帮助 LLM 在多会话场景主动带上 session_id。
- **下载连接钉定，关闭 DNS 重绑定窗口（review 第 2 条）**：旧版 `validate_download_target` 校验时解析一次 DNS，httpx 连接时**再解析一次**——两次解析之间域名可以换 IP；`download_url_policy = public`（README 明写的逃生口）下，攻击者自持 DNS 的域名可以先解析到公网 IP 过检、连接时再解析回内网 IP。现在校验通过的解析结果经 `collect_ips` 带出，实际请求直接连这些已校验 IP（`build_pinned_request`：Host 头/SNI/证书校验仍按原主机名，https 用 httpcore 的 `sni_hostname` 扩展，证书校验不降级），重定向每一跳独立「校验 → 钉定」，多个已校验 IP 只有连接类错误才依次尝试、绝不重新解析。`policy = off` 不解析也就无从钉定，维持旧行为。顺带把阻塞的 `getaddrinfo` 挪进 `asyncio.to_thread`，不再卡事件循环。
- **napcat.verify_ssl 默认 false → true（review 第 3 条）**：旧默认在 https 地址下会连主机名校验一起关掉（CERT_NONE）。新实例默认开启校验；**存量实例的 config.toml 不会跟随新默认值**，因此启动自检新增一条风险告警：`napcat.verify_ssl=false` 且 `napcat.http_url` 为 https 地址时打 WARNING（与 download 风险配置告警同款机制），提示改用系统信任的证书而不是关校验。
- 测试：pytest 门禁用例 72 项（新增连接钉定 6 项：`build_pinned_request` 单元契约、collect_ips 拒绝路径不带出 IP、端到端「出站请求打到已校验 IP」、重定向逐跳钉定）；smoke 新增「下载连接钉定」「search_file 跨会话兜底护栏」两项防回归，并把 verify_ssl 告警纳入风险配置告警检查；根套件新增 2a6b（多会话歧义拒绝兜底、拒绝不泄露内容、显式 session_id 各自命中）与 3h/4h/4i。两项修复均做了**反向验证**：还原旧实现后新用例确实 FAIL（钉定还原 → 域名参与连接；旧兜底还原 → LLM 幻觉 session_id 时拿到其他会话内容）。

## v1.3.9（2026-11-03）Planner 公告补「正在解析」状态

### 问题

真机 11-03 15:21 群的完整时间线：

```
15:21:43  希然发文件（19MB PDF）
15:22:00  Planner：19MB 可能是视频，文件内容未知 → stop
15:48:13  岚 @我「来个观后感」
15:48:55  search_file → 还没有已入库的文件
15:50~54  绕去 web_search / moegirl / web_fetch，用 GitHub README 写观后感
15:47:46  （文件此时才入库，26 分钟）
```

`announce_file_context` 在 store 为空时直接 `return`，所以**整个解析窗口内 Planner
对文件一无所知**，只能猜「可能是视频」，或者断言「群文件我读不到」。

### 改动

- `_parsing: dict[token, {session_id, name, started_at}]`：`_handle_file` 一进来登记，
  `finally` 撤销；NapCat 回填真名时同步更新（公告显示真名而不是「附件」）。
- `_parsing_notice()`：拼一句公告，秒级说「约 N 秒」、超过 90 秒说「约 N 分钟」，
  最多列 3 个再收敛成计数。
- `announce_file_context`：文件已入库 → 原有那句；有解析中的 → 追加一句；
  两者都没有才不注入。会话隔离：只取 `session_id` 相同的记录。
- `_parsing_for()`：超过 3 小时的记录视为泄漏（只在异常取消路径才可能留下），
  裁掉并告警，避免永久说「正在解析」。
- `/file_status` 增加一行 `⏳ 解析中: N 个 — 《x》(已等 Ns)`：长解析期间
  「已入库文件 = 0」很容易被误读成文件丢了，这行直接回答「是不是还在处理」。

### 公告文案（不到 100 字，不注正文）

```
《奶蛙学 必修第一册.pdf》 正在解析入库（已等待约 26 分钟），当前还检索不到内容。
这不等于文件读不到：不要断言内容缺失，也不要重复调用文件读取工具；
需要回应时说明还在读、稍后再问即可。
```

### 新增测试

`tests/test_parsing_notice.py`（9 例）：秒/分钟两种量级、多文件收敛、仅解析时注入、
无状态不注入、跨会话不串、失败后不残留、处理中可见且用真名、陈旧记录被裁剪。

### 兼容性

无配置变更。

## v1.3.8（2026-11-03）长解析期间会话壳被回收 + 同一文件处理两遍

### 现象（真机 11-03 15:21 群「鸟の小窝」）

```
15:21:47  [napcat] get_file 命中: ...奶蛙学 必修第一册.pdf
15:47:46  已入库文件 奶蛙学 必修第一册.pdf：182 块（session=a9ce37…）
15:48:55  search_file → 当前会话还没有已入库的文件，请先上传文件。
15:54:20  replyer 注入 → 也没有「文件全文直注」日志
```

日志说入库成功，检索说库里空的。两者都真：写进去的是一个**没人引用的孤儿对象**。

### 根因

`_handle_file` 在**函数开头**就 `get_or_create`，于是 store 里立刻挂上一个 0 文件的
`VectorStore`；随后 19 MB PDF 解析了 26 分钟。而清理循环（`cleanup_interval=5` 分钟）
里有：

```python
if not vs.files:
    self._store.drop(sid, cid)      # 空壳回收（v1.1.0 防会话数无界增长）
```

26 分钟里它跑了 5 次，早已把这个壳 drop 掉。`_handle_file` 仍持有引用，`add_file`
照常写进孤儿对象 —— 日志「已入库 182 块」，store 里什么都没有。

**而且全程零日志**：空壳被回收时 `cleanup_expired` 返回 `[]`，`total_removed == 0`，
「后台清理：移除 N 个过期文件」这条根本不会打。所以从日志看像文件凭空消失。

小文件几秒入库，永远踩不到这个窗口——这就是为什么私聊的 docx 一直正常。

### 改动

1. **向量库延迟创建**：`get_or_create` 从函数开头移到 `add_file` 之前（抽成 `_get_vs`），
   解析期间不再存在空壳；
2. **in-flight 保护**：`_handle_file` 把 `(session_id, conversation_id)` 登记进
   `self._inflight`（**计数**，不是集合——防抖合并会把同一条消息里的 N 个文件并发丢进来，
   用集合时先完成的会抹掉 key），`_cleanup_once` 跳过这些键，`finally` 里减计数；
3. **兜底挂回**：`_reassert_vs` 在入库成功后检查实例是否还挂在 store 上，脱管则挂回
   （不覆盖已有文件的实例）；
4. **同一文件只处理一次**：去重键从 `name` 改为 `file_id`。同一个群文件会以两条消息
   出现——真实 `file` 段（name 常是占位符「附件」）与 `[事件-群文件上传]` 通知（带真名），
   按 name 去重时两者不同名，双双通过 ⇒ 2× 解析 + 2× embedding + 群里连收两条
   「已读取《…》」。`file_id` 才是稳定标识（文本降级形态没有 file_id，回退到 name）。

### 新增测试

`tests/test_inflight_race.py`（8 例）：空闲空壳仍会被回收（守住 v1.1.0 行为）、
in-flight 空壳不被回收、保护精确到会话键、脱管挂回、不覆盖已有实例、
两文件并发时计数为 2 且解析期间无空壳、解析失败不留壳、
`file_id` 相同的两条消息只处理一次。

### 兼容性

无配置变更，`_manifest.json` 仅版本号。

## v1.3.7（2026-11-03）入库回执默认关闭

### 问题

真机 22:10 私聊日志：

```
[消息发送服务] 鸣澜: 已读取《VoidMeleeAura.cs》，共 60 段
[消息发送服务] 鸣澜: 能看到，Python代码主体完整
```

两条消息并列发出。第一条是插件在 `_handle_file` 末尾直发的**独立消息**，与模型自己的人格化回复
完全无关——用户看到的是「机器人一句机械播报 + 一句正常回答」。

### 根因

`reader.ack_min_chars` 默认 20000，且判定是「原文字符数 ≥ 阈值 **或** 处理耗时 ≥ 10s」。
21KB 的 `.cs` 原文约 21551 字，命中第一个条件；60 块向量化又跑了 11s，命中第二个条件。
两条路都能触发，等于默认几乎必发。

### 改动

- `reader.ack_min_chars` 默认 `20000` → **`0`（关闭）**，语义从「大文件才发」改为「可选露头」
- 耗时兜底 `10s` → **`30s`**：10s 太容易踩到（21KB 文件因 embedding 就超了）
- README 配置表与字段描述同步说明「这是插件直发的独立消息，会与人格化回复并列」

### 升级注意

**已部署实例的 `config.toml` 不会自动补/改键**（宿主只在文件不存在时初始化），
所以升级后仍需手工把下面这行改成 0，否则回执照旧：

```toml
[reader]
ack_min_chars = 0
```

### 保留而非删除的理由

超大文件（几百页 PDF、几十 MB 代码库）真实入库要几十秒，全程静默会让人以为插件卡死。
所以保留开关，只是不再默认开启；`silent_success`（另一条「📄 已解析…」）仍为 `True`。

## v1.3.6（2026-11-02）插件中心提交准备

按 plugin-repo 现行规则（`CONTRIBUTING.md` + 2026-10 新增审核项）做的提交前整改，**无功能变更**。

### 兼容范围收敛（2026-10 新增的审核阻断项）

`1.99.99` / `2.99.99` 这类「永远兼容」写法已被列为阻断项，要求按**实际验证过**的范围填写：

| 字段 | v1.3.5 | v1.3.6 | 依据 |
|---|---|---|---|
| `host_application` | `1.0.0 – 1.99.99` | `1.2.0 – 1.3.99` | 真机 1.3.19；插件系统 v2 自 1.2.0 起；`1.0.x` 从未验证 |
| `sdk` | `2.5.1 – 2.99.99` | `2.5.1 – 2.21.99` | 本地 2.8.0 跑通全量测试；devkit 门禁 2.21.0；真机 ≥2.19.0（SnowLuma 要求） |

### 补齐仓库必需项

- **新增 `LICENSE`**（MIT，与 `_manifest.json` 的 `license: MIT` 一致）——通道 A 表单必勾项。
- **新增 `plugin_type: "data"`**：核心行为是「文件解析 → 知识入库 → 检索注入」，对应「数据与知识」类。

### README 依赖章节修正

- 删掉过时的 `pip install python-docx # 仅 .doc 老格式`（v1.3.5 已移除 `_read_doc`，`.doc` 现在走 `RECOGNIZED_UNSUPPORTED` 明确报错）。
- 补上 `docx2txt` / `python-pptx`（标准库主路径失败后的兜底）。
- 说明这些**刻意不写进 `dependencies`** 的理由：`pandas` 随宿主基线安装，其余是边缘格式兜底，写进 dependencies 会让每次部署多装重依赖。

### 隐私清理（提交前必做）

- 测试夹具里的真实 QQ 号 `3816023959`（与作者 Windows 用户名同源）→ 替换为 `1000000001`，昵称 `狸猫` → `测试用户`，共 2 处。
- 复查旧值**零残留**；179 项测试全绿。

### 关于 TLS 校验（审核会问，此处留档）

`reader.insecure_download` 与 `napcat.verify_ssl` 都**默认安全**：前者默认 `False`（即默认校验证书），后者默认 `True`；开启前者或关闭后者时都会打 warning 日志，README 有专章说明「优先用 `download_ca_bundle` 保持校验开启」。

保留该开关的原因：运行环境存在 TLS 中间人代理（如安全软件的 `proxy-root-ca.cer`）时，硬关校验会让**全部文件下载不可用**；把它做成默认关闭、显式开启、有替代方案的配置项，比直接删除更符合实际部署需要。

## v1.3.5（2026-11-02）死代码清理

用 AST 扫全仓库（含测试）逐个核实「定义了但没有调用点」与「只写不读」的符号，清理如下。

### 移除：只写不读的会话元数据落盘

- `SessionStore.save_meta()` 有两处调用（`on_unload`、后台清理循环），但 **`load_meta()` 从未被调用** —— 写了没人读。
- 更关键：`save_meta()` 只记文件名 / 轮数 / 上传时间，**不存向量也不存正文**，即使读回来也无法恢复检索能力（重启后文件必须重传）。
- 于是它唯一的实际效果是：每次文件入库、每次清理周期、每次卸载都做一次全量 JSON 序列化并写盘。
- 一并移除 `save_meta()` / `load_meta()` / `_meta_path()` 及 `plugin.py` 两处调用。`data_dir` 保留（启动日志用，便于排查部署路径）。
- 影响：实例 `data/plugins/org.mai-mai.file-reader/file_reader/sessions_meta.json` 不再生成，已有的可以直接删。

### 移除：无调用点的方法

| 位置 | 符号 | 说明 |
|---|---|---|
| `vector_store.py` | `FileEntry.touch()` | 与 `touch_files()` 重复，后者直接写 `entry.last_used` |
| `vector_store.py` | `VectorStore.list_files()` | 无调用（`files` property 已覆盖） |
| `vector_store.py` | `SessionStore.list_sessions()` | 无调用 |
| `lexical_index.py` | `LexicalIndex.mark_dirty()` | `add()` / `remove_file()` 内部已直接置 `_dirty` |
| `lexical_index.py` | `LexicalIndex.reset()` | 无调用；会话级释放走 `LexicalIndex(...)` 重建 |

### 移除：孤立函数与导入

- `file_parser.py::_pdf_from_stdlib()` —— 无人调用；`SUPPORTED_EXTENSIONS["pdf"]` 注册的是 `_read_pdf`（pdfminer）。
- `file_parser.py::_read_doc()` + 配套 `from docx import Document` 可选导入 —— `.doc` 不在支持列表（走 `RECOGNIZED_UNSUPPORTED` 提示另存为 docx），该函数永远不会被调用。
- 未使用的 `import json`（`vector_store.py`）、`Optional`（`lexical_index.py`）。

### 保留说明

`on_config_update()` 虽然没有本仓库内的调用点，但它是 SDK `Plugin` 基类的生命周期方法（`maibot_sdk/plugin.py`），由宿主调用，必须保留。

## v1.3.4（2026-11-02）全文直注去重叠

### 现象

真机 22:04 日志，`search_file` 明文路径返回的文件内容里有成段重复：

```text
content: """  ... eference:2]。本代码的软件限制是第二道防线...
""" from __future__ import annotations
import asyncio
eference:2]。
本代码的软件限制是第二道防线...
""" from __future__ import annotations
import asyncio
import time
```

模型在 Reply 思考里据此写下 “The extracted document has repetition and some corruption”，并在候选回复里准备说「有些段落重复」。

### 根因

`FileEntry.full_text()` 是 `"".join(self.chunks)`。而 chunks 由 `RecursiveCharacterChunker(chunk_size, chunk_overlap)` 切出，**相邻块带 `chunk_overlap` 长度的重叠**。直接拼接 = 每个重叠区被重复一次。

15 块 × overlap 100 ≈ 1400 字重复（原文 5875 字、实际注入约 7300 字，多出 24%）。块数越多膨胀越狠。

注：阈值判定用的是 `source_chars`（原文长度，v1.0.18 已修对），所以「5875 字 ≤ 6000」的判定是对的——**膨胀只影响注入给模型的内容本身**。

### 修复

- `FileEntry` 新增 `chunk_overlap` 字段，`add_file` 写入实际配置值；旧数据默认 0 → 退化为原行为。
- `full_text()` 拼接时**按最长重叠搜索**去掉相邻块重复：上限取 `chunk_overlap`，找不到重叠就不切。
  为什么不用「固定切掉 N 字符」：`chunk_merge_enabled` 开启时块边界并非等距，固定切会切掉正文。
- 没有真实重叠的块（短块 / 合并块）不会被误切。

### 影响

- 注入给模型的全文恢复为原文本身，不再出现「看似损坏」的重复段。
- 直注 token 消耗下降约 overlap×(块数-1)；块数多的长文档收益更明显。
- 检索路径（单块返回）不受影响。

## v1.3.3（2026-11-02）`search_file` 接住宿主注入的会话标识

### 现象

真机 21:53 日志：

```text
[推理中工具调用] 工具=search_file
search_file 未收到（有效）session_id，回退到唯一存有文件的会话 79588bdc971fac73433f5ec062aa1576
```

这次能回退成功，**只是因为当时全局只有一个会话存有文件**。一旦群聊（如「知夏的图书室」）也上传过文件，就会命中 v1.2.1 的 fail-closed 分支直接拒绝检索。

### 根因

`search_file` 第一行是 `del kwargs`，把宿主注入的上下文字段一并丢弃。而 Host 1.3.x 的 `_build_tool_context_payload`（`src/plugin_runtime/component_query.py:740-773`）本来就会补：

```python
stream_id = str(context.stream_id or context.session_id or "").strip()
if stream_id:
    payload["stream_id"] = stream_id
    payload["chat_id"] = stream_id
```

合并规则是「模型没传才补」。所以宿主一直在给，插件一直在扔。

### 修复

- `search_file` 先从 `kwargs` 取 `stream_id` / `chat_id`，仅在模型没显式传 `session_id` 时使用；
- 解析优先级：**显式 `session_id` > 宿主注入 `stream_id` > 全局唯一有文件的会话 > 拒绝**；
- 这**不是猜测**：值由宿主按当前执行上下文注入，与 `_store` 的 `session_id` 同口径，属已知归属。v1.2.1 收紧时「工具链路拿不到当前会话」的前提，在 Host 1.3.x 已不成立。
- 显式 `session_id` 与注入值冲突时以显式为准（尊重模型的显式意图）。
- 工具描述同步改为「会话归属由宿主自动带入，正常情况下不需要传 session_id」，减少模型无谓填参。

### 未改动

- 多会话**且宿主未注入**时仍 fail-closed，隐私护栏不变。
- 新增用例覆盖：注入 stream_id 命中各自会话、不串会话、显式优先。

## v1.3.2（2026-11-02）结构化 file 段直通 + Planner 文件状态公告

v1.3.1 真机验证通过后（`文件段匹配 mode=size` → `get_file 命中` → `文件名回填` → `已入库 6 块` → `全文直注 2264 字` → 回复正确），日志暴露两个遗留问题。

### 修复 1：结构化 file 段不再被丢弃

- 现象：`文件段无法提取内容: payload_keys=['file_id', 'name', 'size']` 每文件打两次（before/after）。
- 原因：SnowLuma v1.1.10+ 的 file 段只有 `file_id/name/size/busid`，`_seg_to_file` 只认 `base64/path/url` → 返回 `None` → 整段丢弃，只能退到 `[文件]` 文本 + `get_msg` 回溯。
- v1.3.2：`_seg_to_file` 在无内容字段但有 `file_id` 时返回 `{name, file_id, busid, size_hint}`，`_handle_file` 直接 `get_file` 取本地路径。
- 收益：省一次 `get_msg` RPC；不再依赖 `size` 消歧定位段（同名同大小也能对上）；warning 消失。
- 失败回执分支同步覆盖 `file_id`（`reason=napcat_no_content`），不再误报「没有可用于回溯的 message_id」。

### 修复 2：向 Planner 公告文件状态（信息对称）

- 现象（真机 11-02 21:39）：全文已直注 replyer 且答得正确，但 Planner 上下文没有这份内容 → 轮次 6 判定「我根本没读到文件内容，却编出了一个标题」→ 触发 `recall_last_message` 撤回重答。
- 根因：注入只注册在 `maisaka.replyer.before_model_request`，Planner 侧完全看不见 ⇒ 两侧信息不对称，**Planner 的自我校对反而制造故障**。
- v1.3.2：新增 `maisaka.planner.before_request` hook，只注入一句公告（文件名 + 总字数 + 「全文/片段会在生成回复时附上」+ 不必再调文件工具），**不注入正文**（Planner 逐轮 attempt 灌全文会把 token 打爆）。
- 文案按 `direct_inject_max_chars` 区分「全文」与「片段」，与实际供给一致，避免 Planner 依据错误前提决策。

### 兼容性

- 无 `file_id` 的旧形态（≤ v1.1.9 文本降级）走原路径，行为不变。
- `napcat.enabled=false` 时 file_id 分支不启用，仍按原逻辑报 `napcat_disabled`。

## v1.3.1（2026-11-02）适配 SnowLuma v1.1.10+ 结构化 file 段

真机环境：MaiBot Host **1.3.19** + SnowLuma Adapter **v1.1.13**（接 NapCat）+ `capabilities.files = true`。
故障现象（11-02 21:26 私聊实测）：文件字节其实已经拿到，最后一步被解析器拒收。

```text
[napcat] get_msg 成功: message_id='1556341576'
[napcat] get_file 命中: ...\NapCat\temp\飞天显卡呜呜转神教_白话版.docx   ← 真实文件名在这里
文件处理失败（silent_errors=True）: value_error | ⚠️ 不支持的文件类型: .(无扩展名)
```

### 背景：入站 `file` 段的两种形态

| SnowLuma Adapter | 入站 `file` 段 | `[文件]` 文本由谁生成 |
|---|---|---|
| ≤ v1.1.9 | 降级为 **text 段** | 适配器 `_build_file_text_segment()`，尾部带 `链接: {url}` |
| ≥ v1.1.10 | 结构化 `{"type":"file","data":{name,size,file_id,busid}}` | Host `FileComponent.to_plain_text()` |

`v1.1.10` 起（manifest `host min 1.3.18`）结构化段**不含 url**，且 NapCat 私聊 file 段的真实文件名在 `file` 字段而非 `name`，适配器的 `or "附件"` 兜底于是产出占位名。Host 1.3.16 又把 `to_plain_text()` 改成 `[文件] name，大小: N[，类型: mime][，file_ref: ref]`（删掉了 `链接:` 与 `文件ID:`）。

### 修复 1：`[文件]` 解析不再吞整行

- 旧正则 `\[文件\]\s*(?P<name>.+?)(?:\s*[，,]\s*大小[:：]\s*(?P<size>\d+))?\s*[，,]?\s*$` 用 `$` 锚定行尾，`.+?` 会一路吃到末尾：`附件，大小: 15397，file_ref: 11a7…` 整串被当成文件名 ⇒ `suffix` 为空 ⇒ 解析器按扩展名分派直接拒收。
- 新正则 `\[文件\]\s*(?P<name>[^，,]+?)(?:\s*[，,]\s*大小[:：]\s*(?P<size>\d+))?(?:\s*[，,].*)?$`：名字不含逗号、`类型:`/`file_ref:` 尾巴一律忽略。
- 代价：文件名本身含中英文逗号时会被截断——历史版本同样处理不了，非回归。

### 修复 2：用 NapCat 返回的真实文件名回填

- `_handle_file` 在拿到 `napcat_path` 后判断：占位/降级名**没有可用扩展名**而路径 basename 有时，用 basename 替换 `name`，并在写临时文件**之前**生效。
- 收益不止于解析成功：入库索引、注入上下文、回执里显示的都是真实文件名，`_search_cache_dir` 等按名回溯的兜底也能命中。
- 只做「无扩展名 → 有扩展名」单向回填，不覆盖适配器给的正确名字。

### 修复 3：占位名改用 size 消歧，不再 fallback

- `_match_file_seg` 新增 `size` 匹配方式：名字全未命中、但 `size_hint` 唯一命中一个 file 段时直接采用。
- 为什么需要：占位名「附件」永远匹配不上段内真实文件名，旧逻辑退到「取第一个」并打 WARNING —— 单文件消息每次文件都刷一条噪音，多文件消息则可能串档（v1.3.0 专门修过的静默故障）。
- `size_hint=0` 或 size 也不唯一时保持旧 `fallback` 语义，不改既有行为。

### 兼容性

- 文本降级路径（≤ v1.1.9）行为不变；`链接:` URL 下载路径不受影响。
- 已实测的 SFF（silent fallback）链路：`get_msg → file 段 → get_file → 本地路径` 在 Host 1.3.19 上仍然可用，无需改动。
- 新增回归用例：新 `[文件]` 文本形态解析、占位名回填入库。`pytest` 168 passed。

## v1.3.0（2026-10-30）正确性 + 混合检索 + 注入体验 + 格式层重建

按 `docs/IMPLEMENTATION_PLAN.md` 的批次 1–4 一次成型。四条主线：**答得准**（分块不再丢结构、补关键词召回）、**更像人类**（注入去机器味、没读到就说没读到）、**更耐用**（文件不再 5 轮就消失、长文档可持续追问）、**格式可信**（声明的格式真能读，读不了给准确原因）。

### 修复：分块器把文本结构吃掉了

- `chunker.py` 的 `_split_text` 旧实现 `text.split(sep)` 切完**直接丢弃分隔符**，递归也不拼回：`"你好。世界"` 变成 `"你好世界"`，段落换行、代码缩进全部消失。注入给模型的文本没有标点与分段，代码/表格最严重。这个 bug 不报错、不少块，只能靠 `join(chunks) == 原文` 的等价性断言发现。
- 现在分隔符**附着在前一片段末尾**，且递归只传剩余分隔符（`seps[i+1:]`，否则会在「片段+分隔符」上再次命中同一分隔符而无限递归）。
- 连续分隔符（`\n\n\n`、代码缩进空格）产生的空片段，其分隔符会**并进相邻片段**而不是被丢弃；`chunk()` 里原本的 `if c.strip()` 也会把 `"\n\n"` 整段扔掉——两处都改了，否则段落照样丢。

### 修复：一次发多个文件会静默串档

- 防抖合并后 N 条 `[文件]` 提示共用同一个 `message_id`（消息体里是 N 个 file 段），旧 `_pick_onebot_file_seg` 永远返回**第一个** file 段 → 14 个文件全被读成第一个文件的内容，再按各自文件名分别入库：静默串档，并且污染检索（库里出现 N 份「同一份内容 + N 个不同文件名」）。
- 新增 `_pick_onebot_file_segs`（取全部段）+ `_match_file_seg`（按 `Path(x).name` 精确匹配，`size_hint` 与段内 `file_size` 不等时排除，候选字段兼容 `file_name`/`name`/`file`/`title`）。
- 匹配方式进日志：`[napcat] 文件段匹配: name=xx.docx mode=name|fallback|ambiguous segs=N`，`fallback`/`ambiguous` 走 WARNING（串档是静默故障，不把匹配方式打出来真机无从排查）。

### 新增：BM25 词法检索 + RRF 融合（`lexical_index.py`）

- 纯向量对「查具体符号」近乎无能：函数名、报错码、编号、表格专名——语义相近但字面不同的块常排前面。
- 分词零依赖：中文按二元组（「第三章」→ 第三/三章），英文标识符**同时保留整体与子词**（查 `read_any_file_to_text` 整体命中，查 `file` 也能命中），`. _ -` 连接的写法（`deploy.sh`、`报告.pdf`）作为整体命中。
- 两路分数量纲不同（余弦 0~1 vs BM25 无界），用 **RRF（k=60）融合排名**而不是加权求和。
- 降级矩阵：向量可用+词法可用 → 融合；向量可用+词法停用 → 纯向量；**向量失败+词法可用 → 纯词法**（本次额外收益：embedding 拥塞时代码/报错码类问题仍能答）；两者都不可用 → 概要兜底。
- 内存护栏 `reader.lexical_max_tokens`（默认 200 万 token）：超限即停用该会话词法索引、释放倒排表、退回纯向量，**不抛错**，原因在 `/file_status` 可见。
- **回滚开关**：`reader.hybrid_retrieve_enabled` + 运行期命令 `/file_bm25 on|off`。为什么两者都要：宿主只在 `config.toml` **不存在**时才初始化，`config_version` 相同时完全不写回，即使 bump 也是「新默认值为骨架 + 旧值覆盖」——只改 `Field(default)` 对已部署实例永远无效。体感变差时必须有立刻可用的代码层退路。

### 新增：追问改写、相关度阈值、检索诊断

- 追问改写（纯规则、不调 LLM）：当前问句 ≤12 字 / 含指代词（这|那|它|上面|刚才|第X章…）/ ≤20 字且以「呢|吗」结尾 → 拼成「上一问 + 当前问」再检索。「那第三章呢」不再检索不到东西。不调 LLM 的原因：注入 hook 是 BLOCKING 且只有 11s 预算。
- 相关度阈值 `retrieve_min_score`（向量绝对下限 0.15）+ `retrieve_min_ratio`（相对 Top1 比例 0.5）。前者只作用于**有向量分**的候选——否则纯词法降级通路会被余弦阈值整条掐死。
- **无命中不再注入概要**（旧版拿概要顶上，等于给模型一份无法定位、无法引用的内容，反而诱导它编）。
- `/file_status` 新增环形诊断（最近 20 条）：模式 / 原始提问与改写后查询 / 向量候选数 / 词法候选数 / 融合后与过阈后条数 / Top1 分数 / 耗时 / 来源分布。只出模式与分数，不出正文。
- 工具侧与 hook 侧走**同一套**混合检索，避免「工具查得到、注入查不到」。

### 变更：注入体验（去机器味 + item_id 幂等）

- 注入文案改为事实式：`以下是本会话已读过的文件内容：` + `《文件.pdf》` + 正文；同一文件多块合并到同一标题下。**不再带** `【文件检索】`、`相关度 0.723`、`[1]` 编号、「供参考，仅在相关时引用」——模型会复述这套元信息。
- 幂等判定改看 `item_id` 前缀（`file-reader:{kind}:{uuid}`），与正文彻底解耦。旧版靠正文 marker 判重，marker 被配成空串就变成**永不注入**。`search_file` 的工具结果不是我们构造的 item，拿不到该前缀，所以正文标记降级为**代码层常量**，仅用于识别「工具已经把内容给过模型了」，避免紧接着重复注入。

### 变更：生命周期（滑动续期）

- `FileEntry.last_used`：过期判定从 `upload_time` 改为 `last_used`，**只有实际被注入的文件**才续期。
- 轮数只加给被注入的文件（旧版 `increment_rounds()` 给会话内所有文件 +1，没被问到的也跟着过期）；轮数去重 key 加入 `session_id`（旧 key 不含会话，A 会话问过「总结」后 B 会话问同样的话会被误判成「本轮已计数」，表现为文件永不过期）。
- `file_max_rounds` 5 → 20；新增 `file_absolute_ttl`（默认 720 分钟）：一直被问到的文件也不能永久驻留。
- 大文件回执：原文 ≥ 20000 字或处理耗时 ≥ 10s 时发一条中性的「已读取《X》，共 N 段」，其余保持静默。入库动辄几十秒，全程静默会让用户以为插件坏了。

### 新增：解析失败只告诉模型（不刷群）

- `silent_errors=true` 时旧版失败**只写日志**，用户和模型都不知道发生了什么，追问时模型只能凭空猜。
- 现在所有失败点登记到会话通知队列（单会话上限 10 条、TTL 600s、同名覆盖），注入 hook 在**任何情况下**先检查（包括本会话已经没有文件库时），注入一次即标记 delivered；同名文件成功入库或 `/clear_file` 时清除。
- `silent_errors=false`（旧行为）仍发群消息且不再重复注入，避免双重告知。

### 格式层重建（声明支持的必须真能读）

| 格式 | v1.2.1 实际 | v1.3.0 |
|---|---|---|
| odt | 走 `_read_txt` → zip 乱码（假支持） | zip + ElementTree 真解析 |
| ods | 需 pandas（假支持） | 真解析，数值取 `office:value` |
| odp | 走 pptx 解析器 → 必然失败（假支持） | 真解析，按 `draw:page` 输出幻灯片 |
| rtf | 走 `_read_txt` → 控制字乱码（假支持） | 移出支持列表，明确提示另存为 docx/txt |
| doc / ppt | 报「缺少 python-docx」等莫名错误 | 判 OLE 魔数 → 提示另存为 docx/pptx；不是 OLE 则按内容试 OOXML |
| xls | 缺依赖就失败 | OLE 时需 pandas+xlrd 并说明；扩展名错则按内容解析 |
| html | 原文入库（脚本样式吃 token） | `HTMLParser` 去标签，跳过 script/style/noscript/svg |
| ipynb | 按 JSON 原文入库（base64 图片撑爆分块） | 结构化取 cells，**跳过 outputs** |
| epub / eml / gz | 不支持 | 零依赖新增（epub 走 spine 顺序；gz 解压后按内层扩展名递归解析） |
| srt / vtt / ass / tex / rst / bib | 不支持 | 纯文本映射 |

- 新增 `detect_format()` 内容嗅探：读前 8 字节 + ZIP 中央目录（最多再读 `mimetype` 一个几十字节的小文件，**不解压正文**）。扩展名未知 → 用嗅探结果；扩展名与内容冲突 → 以内容为准并打 INFO（`.bin` 其实是 docx、转发后扩展名丢失都能读）。
- `describe_supported_types()` 重写：只列真能读的，与 README 同一口径。

### 配置变更

| 配置 | 变更 | 说明 |
|---|---|---|
| `plugin.config_version` | 1.0.0 → 1.1.0 | 新增键要落盘必须 bump（否则旧 config.toml 里看不到、也改不了） |
| `reader.file_max_rounds` | 5 → 20 | 已部署实例的 config.toml 保留旧值 5，需手动改或删掉重新生成 |
| `reader.file_absolute_ttl` | 新增 720 | 分钟；0 = 不限制 |
| `reader.hybrid_retrieve_enabled` | 新增 true | BM25 总开关 |
| `reader.hybrid_candidate_k` | 新增 20 | 每路参与融合的候选数 |
| `reader.rrf_k` | 新增 60 | RRF 常数 |
| `reader.lexical_max_tokens` | 新增 2000000 | 词法索引 token 上限，超限停用 |
| `reader.retrieve_min_score` | 新增 0.15 | 向量绝对下限；0 = 关闭 |
| `reader.retrieve_min_ratio` | 新增 0.5 | 相对 Top1 比例；0 = 关闭 |
| `reader.query_rewrite_enabled` | 新增 true | 追问改写开关 |
| `reader.ack_min_chars` | 新增 20000 | 大文件回执阈值；0 = 不发 |
| `reader.parse_notice_ttl` | 新增 600 | 失败通知存活秒数 |
| `reader.injection_marker` | **废弃（保留字段）** | 不再读取；保留是为兼容旧 config.toml，正文判重已改 `item_id` |

### 升级须知

1. **新版配置项不会自动出现在已部署实例的 `config.toml` 里**（宿主只在文件不存在时初始化）。bump 了 `config_version` 后宿主会补**新增**字段，但**已存在字段的值一律保留**——所以 `file_max_rounds` 若想用新的 20，需手动改。
2. 分块语义变了（保留分隔符），已入库的会话不受影响（内存态），重启后新文件按新语义切分。
3. 混合检索出问题先试 `/file_bm25 off`，不要急着改配置文件（改完要重启，且旧实例可能没这个键）。
4. `.doc/.ppt/.rtf` 现在会**明确报不支持**，不再是"读出一堆乱码"。若群里常发这些格式，请在群公告里提示另存为新格式。

### 测试

- `pytest tests`：168 项（新增 `test_chunker.py` 13 项、`test_napcat_multifile.py` 11 项、`test_lexical.py` 26 项、`test_injection.py` 20 项、`test_formats.py` 26 项；新增 `tests/conftest.py` 把插件根目录放进 `sys.path`，否则新测试全部 ImportError）。
- 根套件 `test_file_reader.py`：71 项全过（按新语义更新了分块等价性、注入文案、概要与轮数用例）。
- 冒烟 `tests/smoke_test.py`：11 项（组件清单加入 `file_bm25`）。
- 门禁 `run_gates.py --plugin file-reader`：PASS 3 / SKIP 0 / FAIL 0。
- 分块修复做了**反向验证**：还原旧 `_split_text` 后 6 项新用例立刻变红。

### 已知限制

- 长文档大纲式分段概要、独立 recall@k 评测集、OCR、rerank、老二进制 Office 真正解析——均未做，理由见 `docs/IMPLEMENTATION_PLAN.md` 的「非目标」。
- 批次 5（逐文件管理 `/file_list` `/file_del`、会话配额、zip 递归）未实施。
- 本地门禁覆盖不到宿主的两道硬闸（Host 版本区间校验、`_validate_sdk_plugin_contract`），真机首启仍需看日志确认进「已加载」。
