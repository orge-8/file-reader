# 文件读取（File Reader）

MaiBot 插件：解析 QQ 群/私聊上传的文件，分块后做「向量 + BM25 词法」混合检索（RRF 融合），把最相关内容注入 LLM 上下文，实现低开销的文件问答。

> 参考 [astrbot_plugin_file_reader_pro](https://github.com/zz6zz666/astrbot_plugin_file_reader_pro) 移植。AstrBot 版依赖其内置的 `FaissVecDB` / `RerankProvider`，MaiBot 无这些组件，故本插件改用 **numpy 余弦相似度自实现向量库** + MaiBot 原生 **`llm.embed`** 能力 + **`before_model_request` hook** 注入。

## 工作原理

```
[文件] → 解析(file_parser) → 递归分块(chunker) → 向量化(llm.embed) + BM25 索引(lexical_index) → 会话级库(vector_store)
[提问] → 追问改写 → 向量检索 + 词法检索 → RRF 融合 → 相关度闸门 → 注入 LLM 上下文(before_model_request hook)
```

- 按 `(session_id, conversation_id)` 隔离，不同会话/对话的文件互不串扰。
- **混合检索**（v1.3.0）：向量负责「意思相近」，BM25 负责「字面命中」（函数名、报错码、编号、专有名词）。
  两路分数量纲不同（余弦 0~1 vs BM25 无界），用 RRF（倒数排名融合，k=60）合并排名而不是加权求和。
  一路不可用时自动降级：embedding 挂了还能走纯词法，词法超限就退回纯向量。
- **追问改写**（v1.3.0）：「那第三章呢」这类短问/指代问会自动拼上上一问再检索，纯规则实现不调 LLM
  （注入 hook 是 BLOCKING，只有 11s 预算）。
- **滑动续期**（v1.3.0）：有效期从**最后一次被注入**起算，且只给实际被问到的文件计轮；
  另设绝对上限（默认 12 小时）防止文件永久驻留。长文档不再聊到第 6 轮就消失。
- **没读到就说没读到**：解析失败不刷群，只把原因注入给模型（由 persona 决定说不说、怎么说）；
  检索无命中时不注入任何内容（旧版会拿概要顶上，反而诱导模型编）。
- 注入用 `item_id` 前缀判幂等，重试不叠加；hook 全部 `ErrorPolicy.SKIP`，插件出错不阻断聊天。

### 读取耗时（v1.4.0）

真机「读文件慢」实测归因为三件事，逐条落地（完整证据与决策见 `docs/perf-plan.md`）：

| 项 | 手段 | 本机实测 |
|---|---|---|
| PDF 解析慢、拖垮事件循环 | pypdfium2 优先 + pdfminer 兜底 + 页数上限 | 18.4MB/141 页教材 4.01s → **1.12s**；main loop 滞后 59ms → **21ms** |
| embedding 批次数随块数线性增长 | 默认分块 512/100 → **1024/128** | 52 万字日志 1351 块/22 批 → **591 块/10 批** |
| 同一文件被双 hook 处理两遍 | in-flight 注册表（按 file_id / url / 真实路径去重） | 并发双触发只解析 1 次、embedding 只跑 1 次 |
| 同会话重发同一文件全部重跑 | 会话内 sha256 缓存（不落盘，清文件/清理自动失效） | 回执秒回，零解析、零 embedding |
| 大文本编码探测吃满 CPU | UTF-8 快路径 + chardet 只探前 64KB | 31MB 日志探测 0.22s → **0.03s** |

另外新增一条**可归因的耗时日志**（真机排障地基），入库后直接打出每段耗时：

```
文件处理耗时 奶蛙学 必修第一册.pdf：取字节=1.83s 解析=1.12s 分块=0.01s embedding=28.4s 词法索引=0.42s（词法构建=0.35s 矩阵=0.07s） 合计=31.8s（81 块 / 18.4MB）
```

### 内存与首次查询（v1.6.0）

v1.6.0 面向 4GB 真机，按「峰值 + 稳态 + 故障重试」三条链路治理：

| 项 | 手段 | 行为变化 |
|---|---|---|
| 原始字节同时驻留 | bytes/路径/URL/NapCat 统一限长流式落临时文件，哈希在落盘时同步计算 | 解析前释放 `fdata["bytes"]`，不再保留整份文件副本 |
| 首次提问卡顿 | BM25 与向量矩阵入库预建；查询 CPU 在 worker 线程、同一会话锁下执行 | 首次查询不阻塞 main loop；索引只构建一次 |
| 多文件峰值叠加 | 默认只激活 1 个文件处理，队列最多 3 个；全局数据预算 192MB | 队列满或预算不足时**拒绝新文件并保留旧文件** |
| 长期驻留 | 同名 BM25 替换、批量清理、通知/摘要任务/全文缓存回收 | 同名覆盖不再残留旧文本，历史会话键不再增长 |
| 故障重试重复解析 | embedding 失败保存 chunks 磁盘重试包，重试只做 embedding | 30 次重试中解析次数与 Omni 调用次数均为 1 |
| 解压/解析放大 | GZ 实际解压上限、ZIP 实际读取成员累计上限、非 PDF 文本上限 | 超限明确拒绝；PDF 仍按页数截断并提示 |

BM25 postings 采用 `array('I')` 扁平结构，并在构建中逐块检查 token/posting 上限，超限立即停止并退回纯向量；不会再出现“完整建完几十万 posting 才发现超限”的峰值。`/file_status` 会显示插件 PID、数据占用估算、队列拒绝次数、重试包磁盘量以及词法 tokens/postings。

> 存量实例注意：`config.toml` 只在缺失时初始化，已有字段不会被新默认值覆盖。新增容量字段需按 README 配置表手动补齐；活跃/队列槽位修改后需完整重启。默认 `max_file_size` 对**新实例**为 20MB，老实例保留原值。

## 安装

1. 将本目录放入 MaiBot 的 `plugins/` 文件夹，重启 MaiBot 或通过 WebUI 启用。
2. 安装依赖（见下）。

### 依赖

**核心格式零依赖**：docx / xlsx / pptx / csv / txt 及全部代码/文本类文件用 Python 标准库（zipfile + ElementTree / csv）直接解析，**无需安装任何第三方库**——插件跑在 MaiBot 的独立子进程里，这样本地与真机行为完全一致，也省去了找对 python.exe 装库的麻烦。

**由 manifest `dependencies` 声明（插件管理器会安装，无需手工处理）**：

| 包 | 用途 | 缺失后果 |
|---|---|---|
| `numpy` >=1.24 | 向量库余弦相似度（`vector_store.py`） | 插件无法加载 |
| `httpx` >=0.24 | URL 来源文件的流式下载 | URL 下载路径不可用 |
| `pdfminer.six` >=20221105 | PDF 解析**兜底**引擎 | pypdfium2 也缺失时读不到 PDF |
| `pypdfium2` >=4.30 | PDF 解析**首选**引擎（v1.4.0） | 自动降级 pdfminer：功能不缺，但慢约 3.6× 且解析期间抢 GIL（真机会出现 main loop 秒级卡顿） |
| `mutagen` >=1.47 | 音频时长元数据（v1.5.0，超限预检用） | wav 仍可用标准库读时长；mp3/flac 等读不出时长会被 fail-closed 拒绝 |

> ⚠️ **PDF 是必需能力**：PDF 没有标准库解法。v1.4.0 起按 `pypdfium2 → pdfminer` 两级降级，
> 两个引擎都不可用时才报错（报错信息里会带上两边各自的失败原因）。
> 本机实测 18.4MB / 141 页教材：`pypdfium2` **1.12s**，`pdfminer` **4.01s**，且前者解析期间
> main loop 最大滞后 21ms（后者 59ms）——C 扩展释放 GIL，不再拖被动事件循环。
> 若插件管理器不自动装依赖，请手动执行：`pip install pypdfium2 pdfminer.six`

**可选手工安装**（缺失时全部有降级路径，不影响插件加载与其它格式）：

```bash
pip install chardet          # txt 编码探测；缺失时退回多编码轮询猜测（utf-8 → gb18030 → utf-16）
pip install docx2txt         # docx 兜底：标准库 zip+XML 是主路径，它只在主路径解析为空时补位
pip install python-pptx      # pptx 兜底：同上
pip install pandas openpyxl  # 仅 .xls / .ods 老表格格式（xlsx/csv 已零依赖）
```

> 这些包**刻意不写进 manifest 的 `dependencies`**：`pandas` 随宿主基线安装（MaiBot 的
> `pyproject.toml` 已含 `pandas>=2.3.1`），其余三个都是「标准库主路径失败后才补位」的兜底，
> 写进 dependencies 会让每次部署多装若干重依赖，而收益只体现在边缘格式上。
> 全部按 try-import 处理，缺失时只在对应格式退化，不阻断加载。
>
> `.doc` / `.xls` / `.ppt` 旧版二进制格式不做解析（`file_parser.RECOGNIZED_UNSUPPORTED`），
> 收到时会给模型明确原因并建议另存为新格式，不会静默失败。

## 下载安全策略（v1.2.0）

插件的 URL 下载路径**不接受任意地址**。原因是一个真实的攻击面：适配器会把 file 段降级成纯文本
`[文件] x.docx，大小: 9547，链接: <URL>`，插件从这段文本里抠出 URL 去下载。
也就是说 **URL 完全由发消息的人控制**——任何群成员发一句带内网地址的"文件消息"，
旧版本就会去 GET 它并把响应体解析进 LLM 上下文（SSRF）。

因此 v1.2.0 起，下载前必须过四道校验：

1. **协议**：只允许 `http` / `https`（挡 `file://` 等）
2. **凭据**：拒绝 URL 内嵌用户名密码（`http://user:pass@host/`，常用于混淆真实主机）
3. **域名白名单**：默认只允许 QQ 文件域，见 `reader.download_allowed_hosts`
4. **解析后地址**：把主机名解析成 IP，**每个 IP 都必须是公网地址**——
   拒绝回环 `127.0.0.0/8`、私网 `10/8`·`172.16/12`·`192.168/16`、
   链路本地 `169.254/16`（含云元数据 `169.254.169.254`）、IPv6 回环/链路本地等

**为什么必须做第 4 步**：只比对域名字符串挡不住这些写法，它们都能表达 `127.0.0.1`——

| 写法 | 说明 |
|---|---|
| `http://2130706433/` | 十进制的 127.0.0.1 |
| `http://0x7f000001/` | 十六进制的 127.0.0.1 |
| `http://127.1/` | 短写 |
| `http://127.0.0.1.nip.io/` | 通配 DNS 解析到回环 |
| `http://attacker.com/` | 自己的 A 记录指向 `10.0.0.1` |

所以流程是「**白名单 → 解析 → 逐个 IP 判公网**」，两层都过才放行。

**重定向**：httpx 的自动跟随被显式关闭，改为手动逐跳处理（`reader.download_max_redirects`，默认 3 跳）。
**每一跳都重新做上面四道校验**——否则一个白名单域名只要 302 到 `http://169.254.169.254/` 就能绕过。

**关于 TLS 校验**（回应当前 review 里 `insecure_download` 的提醒）：
运行机器存在 TLS MITM 代理时不必关掉证书校验，把代理根证书路径填进
`reader.download_ca_bundle` 即可**保持校验开启**。只有万不得已才用 `reader.insecure_download`
（它只对该域名关校验、不会放开白名单之外的地址，但毕竟丢了证书这道关）。

**日志脱敏**：QQ 文件链接的 query 里带 `fname`/签名等凭据，所以下载日志只记录
`scheme + host + path`，query 一律打码为 `?<已隐去>`。

## 配置

配置项在 `plugin.py` 的 `config_model` 中声明，首次加载后 Runner 自动生成 `config.toml`，可通过 WebUI 编辑。

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| plugin.enabled | bool | true | 是否启用插件 |
| reader.max_file_size | int | **20**（v1.6.0，旧版 30） | 单文件大小上限（MB）。v1.6.0 起原始文件直接限长流式落临时文件，不再常驻 bytes；存量实例保留旧值 |
| reader.active_file_limit | int | 1 | 同时激活的文件处理数（v1.6.0）。4GB 真机建议保持 1；修改后需完整重启 |
| reader.pending_file_limit | int | 3 | 文件处理队列总上限（含活跃任务）。队列满时拒绝新文件并保留已有文件；修改后需完整重启 |
| reader.data_budget_mb | int | 192 | 全部会话的近似数据预算：chunks + 向量 + 矩阵 + BM25。超限拒绝新文件，不淘汰旧文件；0 = 不限制。不包含 MaiBot/Python 基础运行内存 |
| reader.retry_disk_budget_mb | int | 128 | embedding 失败重试包与临时文件总磁盘上限；超限拒绝新重试并提示重发；0 = 不限制 |
| reader.max_uncompressed_mb | int | 20 | GZ 解压后与 ZIP 实际读取成员的累计上限；超限明确拒绝，防压缩炸弹 |
| reader.parsed_text_max_chars | int | 2000000 | 非 PDF 文档解析文本上限（字符数），超限明确拒绝；PDF 仍按页数截断；0 = 不限制 |
| reader.chunk_size | int | **1024**（v1.4.0，旧版 512） | 分块大小（字符数）。块数直接决定 embedding 批次数：52 万字日志实测 1351 块/22 批 → 591 块/10 批。检索粒度变粗由 BM25 词法检索兜底 |
| reader.chunk_overlap | int | **128**（v1.4.0，旧版 100） | 分块重叠（字符数），随 chunk_size 同比放大（约 12%） |
| reader.pdf_max_pages | int | 500 | PDF 最多解析页数（v1.4.0）：超过即截断，并在日志与回执中提示「共 N 页、只读前 M 页」；0 = 不限制 |
| reader.chunk_merge_enabled | bool | true | 短碎片合并（v1.0.18）：相邻短碎片合并到接近 chunk_size 再成块，块数降至约 1/3，入库向量化耗时同步下降；关闭回退旧逐碎片成块 |
| reader.retrieve_top_k | int | 6 | 检索返回的相关块数量 |
| reader.retrieve_use_matrix | bool | true | 矩阵化检索（v1.0.18）：缓存 L2 归一化向量矩阵，查询时一次点积算全库相似度（入库/清理自动置脏重建）；块数多时检索开销显著下降；异常自动回退逐条余弦 |
| reader.file_retention_time | int | 60 | 文件有效时间（分钟）。**v1.3.0 起为滑动续期**：从最后一次被注入起算，没人问就会正常过期 |
| reader.file_max_rounds | int | **20**（v1.3.0，旧版 5） | 文件最大参与轮数，**只给实际被注入的文件计轮**。⚠️ 已部署实例的 config.toml 会保留旧值 5，需手动改或删掉重新生成 |
| reader.file_absolute_ttl | int | 720 | 文件绝对存活上限（分钟，自入库起算）：一直被问到的文件也不能永久驻留；0 = 不限制 |
| reader.cleanup_interval | int | 5 | 后台清理间隔（分钟）；同时负责回收空会话向量库与裁剪概要冷却表（v1.1.0） |
| reader.enable_group | bool | true | 是否处理群聊文件 |
| reader.download_url_policy | str | whitelist | URL 下载校验策略（v1.2.0）：`whitelist` = 只允许白名单域名；`public` = 允许任意主机但仍必须是公网 IP；`off` = 不校验（仅排障，危险）。三种策略**都**拒绝回环/私网/链路本地地址并逐跳校验重定向；写错的值退回 `whitelist`。v1.2.1 起校验通过的 IP 会**钉定到实际连接**（Host/SNI/证书校验仍按原主机名），DNS 重绑定窗口关闭 |
| reader.download_allowed_hosts | str | ftn.qq.com,qfile.qq.com,nt.qq.com.cn,qq.com,weixin.qq.com | URL 下载域名白名单，逗号分隔，后缀匹配（含子域，边界安全：`eviltfn.qq.com` 不匹配 `ftn.qq.com`）。留空 = `whitelist` 下拒绝一切 URL 下载 |
| reader.download_max_redirects | int | 3 | URL 下载最大重定向跳数，**每一跳重新做域名+地址校验**；0 = 不跟随重定向 |
| reader.download_allow_private_hosts | bool | false | 允许下载目标解析到内网/回环（v1.2.0）。仅当适配器用本机 HTTP 服务提供文件时才需要，且该主机还必须同时加进白名单；开启等于重新放开对内网的访问能力，默认关闭 |
| reader.download_ca_bundle | str | （空） | 自定义 CA 证书路径（PEM）。运行机器有 TLS MITM 代理时指向代理根证书即可**保持证书校验开启**，优先用这个而不是 insecure_download |
| reader.insecure_download | bool | false | 下载文件时跳过 SSL 证书校验（内网 MITM 代理环境用，见常见问题）。优先改用 `download_ca_bundle`；开启后白名单之外仍是拒绝的，但对该域名不再把关证书 |
| reader.injection_marker | str | 【文件检索】 | **已废弃（v1.3.0，保留字段仅为兼容旧 config.toml）**。幂等判定改看注入项的 `item_id` 前缀，与正文内容解耦——旧版靠正文 marker 判重，marker 被清空就变成永不注入 |
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
| reader.hybrid_retrieve_enabled | bool | true | BM25 词法检索总开关（v1.3.0）。开启后检索走「向量 + 词法 RRF 融合」，对函数名/报错码/编号这类字面查询显著更准；体感变差可关闭退回纯向量，**或直接用 `/file_bm25 off`** |
| reader.hybrid_candidate_k | int | 20 | 每路（向量/词法）参与融合的候选数 |
| reader.rrf_k | int | 60 | RRF 融合常数（越大排名差异越平缓） |
| reader.lexical_max_tokens | int | 2000000 | 单会话词法索引 token 软上限（旧字段，保留兼容）；实际生效值还会受全局额度约束 |
| reader.lexical_max_postings | int | 200000 | 单会话 posting 上限（v1.6.0）：超限停用该会话 BM25 并退回纯向量。postings 是索引内存主项，200k 为 4GB 真机保守起点 |
| reader.lexical_global_max_tokens | int | 400000 | 全部会话 BM25 token 总上限（v1.6.0）：新文件只使用剩余额度；超限仅降级词法，不拒绝文件 |
| reader.retrieve_min_score | float | 0.15 | 向量相关度绝对下限：余弦低于此值的候选在融合前丢弃。只作用于**有向量分**的候选，纯词法命中不受影响；0 = 关闭 |
| reader.retrieve_min_ratio | float | 0.5 | 融合后相对下限：低于 Top1 分数该比例的结果丢弃；0 = 关闭 |
| reader.query_rewrite_enabled | bool | true | 追问改写（v1.3.0）：短问/指代问自动拼上上一问再检索；纯规则，不调 LLM |
| reader.ack_min_chars | int | 0 | 入库回执阈值（原文字符数），**默认 0 = 不发**：仅在超大文件确实需要露头时 >0，达到该值（或处理耗时 ≥ 30s）会额外发一条「已读取《X》，共 N 段」。⚠️ 这条是插件直发的独立消息，会与人格化回复并列出现 |
| reader.parse_notice_ttl | int | 600 | 解析失败通知的存活秒数（v1.3.0）：失败原因注入给模型（不发群消息），TTL 内只注入一次 |
| **snowluma.enabled** | bool | **true**（v1.7.0） | **适配器取文件通道**：经插件 RPC 调当前适配器（SnowLuma）的 public API 把 file_id 换成短期下载 URL，再走本插件的 SSRF 防护下载。**无需 NapCat OneBot HTTP 服务**；失败自动回退 NapCat 兜底（若启用） |
| snowluma.private_file_url_api | str | adapter.napcat.file.get_private_file_url | 私聊文件 URL API 名（SnowLuma 复用 adapter.napcat.* 命名空间；一般不用改） |
| snowluma.group_file_url_api | str | adapter.napcat.file.get_group_file_url | 群文件 URL API 名 |
| napcat.enabled | bool | false | NapCat HTTP 兜底取文件（v1.7.0 起为**第二道兜底**，首选通道是 snowluma；仅当适配器通道不可用且你开了 OneBot HTTP 服务时才需要） |
| napcat.http_url | str | http://127.0.0.1:3001 | NapCat OneBot HTTP 服务地址 |
| napcat.access_token | str | 空 | NapCat HTTP 的 access_token（未设置则留空） |
| napcat.timeout | float | 15 | HTTP 请求超时（秒） |
| napcat.verify_ssl | bool | **true**（v1.2.1 起） | HTTPS 时校验证书与主机名。默认从 false 翻转为 true：配 https 地址时关闭它等于允许中间人窃听/篡改（含 access_token）。**存量实例的 config.toml 不会自动跟随新默认值**——若你此前配了 https 地址且没动过这项，请手动改回 true 或删掉 config.toml 重新生成；本地 http 地址不受影响 |
| napcat.cache_dir | str | 空 | 可选：NapCat 文件缓存目录，`get_file` 失败时按文件名+大小在此搜索 |

### 适配器取文件（v1.7.0，首选通道）

官方 napcat-adapter 已合并进 **snowluma-adapter**（Host 1.3.0 起）。结构化 file 段只有
`{name, size, file_id, busid}`，不含文件本体。v1.7.0 的取文件顺序：

```
结构化 file 段（file_id）
  → ① ctx.api.call 适配器 public API 换短期下载 URL
       私聊 get_private_file_url(file_id, user_id)
       群聊 get_group_file_url(file_id, group_id[, busid])   ← busid=0 省略
  → ② 本插件 SSRF 防护下载（白名单 + DNS 钉定 + 逐跳重定向校验）落临时文件
  → ③ 失败时回退 NapCat HTTP 兜底（napcat.enabled=true 才走）
```

- **默认开箱即用**：`snowluma.enabled=true`，不需要 NapCat OneBot HTTP 服务（127.0.0.1:3001）。
  前提只有两个：宿主已加载 SnowLuma 适配器、manifest 声明了 `api.call`（本版已声明）。
- 下载 URL 提取优先 `data.url`（OneBot 标准形态），多 URL 响应不会拿错预览链接。
- 超大文件（段内 size > `reader.max_file_size`）**零 RPC 直接拒绝**，不浪费一轮解析。
- 日志关键字：`[snowluma] 文件下载 URL 解析成功` / `[snowluma] ... 调用失败` /
  `[snowluma] 下载失败（...），回退 NapCat 兜底`；加载时打 `适配器取文件(SnowLuma): 启用`。
- 失败回执精确化：file_id 链路失败报「适配器只提供了 file_id，经适配器解析下载地址后仍未取回
  文件内容」（reason=`no_content_file_id`），不再误报「NapCat 未返回文件内容」。
- **v1.8.1 离线文件修复**：QQ 离线文件服务器返回**裸 IP + HTTP** 链接（如
  `http://183.60.230.81/qqdownloadftnv5`），域名白名单覆盖不了，v1.7.0~v1.8.0 会误拒。
  现在适配器返回的 URL 按**可信来源**处理（`snowluma.trust_adapter_urls`，默认开）：
  首跳只做公网地址校验（内网/回环/云元数据仍拒绝），重定向跳回退域名白名单。
  **消息正文里的 URL 不受影响**，始终走域名白名单。设 `trust_adapter_urls=false`
  可回到旧行为（离线文件会再次被拒）。

### NapCat 兜底（第二道，可选）

旧版适配器（≤ SnowLuma v1.1.9）会把文件消息降级成纯文本 `[文件] 文章.docx，大小: 9547`，
hook 层拿不到文件本体；或首选通道解析/下载失败时，可启用 NapCat HTTP API 回溯：

1. 在 NapCat 里启用 OneBot **HTTP 服务端**（默认 `http://127.0.0.1:3001`），记下 access_token。
2. 插件配置里把 `napcat.enabled` 设为 `true`，填 `http_url` 与 `access_token`。
3. 完整重启 MaiBot（WebUI 改配置不会推送给运行中的插件）。
4. 发一个文件，看日志 `[napcat] get_msg 成功` → `[napcat] get_file 命中` → 回执「已解析」。

回溯链路：`[文件] xxx，大小: N` 文本 → 解析文件名 + 取 `message_id` → `get_msg` 拿原始事件 →
file 段的 `file_id` → `get_file` 换本地路径 → 读盘解析。任一步失败会在日志里留下 `[napcat] ...` 行。

## 音频文件（v1.5.0）

上传音频文件（`mp3` `wav` `flac` `ogg` `m4a` `aac`）后，插件调用 **Omni 音频理解模型**「听一次」，把产出文本（语音→转写+要点；音乐→赏析）当作普通文档解析结果入库，之后的追问走现有混合检索，**免费、不重发音频**。参考 [music_appreciation_plugin](https://github.com/SengokuCola/music_appreciation_plugin) 的调用方式（OpenAI 兼容 `input_audio` + base64 + `stream=true`）。

### 开通与配置（默认阿里云百炼）

宿主 `llm.generate` 不支持音频输入，音频理解需要**独立的接口与密钥**：

1. 开通[阿里云百炼](https://bailian.console.aliyun.com/)，在控制台创建 API-KEY（新用户各模态有免费额度）。
2. WebUI → 插件管理 → 文件读取 → **音频理解** 节，把 Key 填进 `api_key`，保存。
3. 直接发音频文件即可。默认模型 `qwen3.8-omni-flash`（v1.9.0 起；旧默认 `qwen3-omni-flash` 仍可用，但时长上限仅 20 分钟），也可换任何支持音频输入的 OpenAI 兼容接口（改 `base_url` + `model`）。

**未配置 Key 时不报错**：音频文件会收到一条配置指引（经解析失败通知注入给模型），其他文件类型完全不受影响。

### 限制与拒绝策略（不截断、不分段）

| 项 | 默认 | 说明 |
|---|---|---|
| 时长 | ≤ 1 小时（`max_duration_seconds=3600`，v1.9.0 起） | qwen3.8-omni-flash 官方上限 3 小时；base64 通道下 7MB 大小闸门通常先命中（128kbps mp3 约 7 分钟、32kbps 语音约 30 分钟）。超限**直接拒绝**并告知实际时长 |
| 大小 | ≤ 7MB（`max_file_mb`） | Base64 编码后约 9.3MB；**10MB 是 base64 传输通道通用硬上限（不区分模型，qwen3.8 也一样），不可配置放宽**。想吃模型 2GB/3 小时红利须走公网 URL 直传，当前未实现 |
| 格式 | 六种开放格式直发 | 不引入 ffmpeg 转换依赖；QQ 语音 `silk`/`amr`/`wma` 明确拒绝并提示转 mp3/wav |
| 时长读不出 | 拒绝 | fail-closed：无法证明在限内就不发（元数据损坏的 mp3 会命中） |

时长/大小预检在**发送前**完成（mutagen 读元数据），超限不产生任何模型调用费用。同会话重发同一音频命中 sha256 缓存，直接复用不重听。

**思考模式（v1.9.0）**：qwen3.8-omni-flash 默认开启深度思考且思考 token 计入输出预算，会挤占转写篇幅。插件在请求里显式 `enable_thinking=false`（转写/赏析是直给任务，不需要思考），输出预算全部留给理解文本，延迟与成本也更低。

### 成本

Omni 按音频时长计费（百炼 qwen3.8-omni-flash 约 7 token/秒音频输入）。一首 4 分钟的歌 ≈ 1680 token 输入，单次成本很低；**追问零成本**（文本已入库）。注意成本随时长线性增长：放宽时长闸门等于放宽单次花费上限。上传即自动解析，若群内音频量大可关 `audio.enabled`。

## 视频文件（v1.8.0）

上传视频文件（`mp4` `mov` `m4v` `avi` `mkv` `webm`）后，插件调用**豆包全模态模型**「看+听一次」——**画面描述与音轨转写一次调用同时得到**——产出文本带【视频理解】头部入库，之后的追问走现有混合检索，**免费、不重发视频**。

上传走火山方舟 **Files API（multipart 流式读文件）**，不做 base64 整读，20MB 视频不会撑爆内存；理解调用以 `file_id` 引用已上传文件。

**上传成功 ≠ 立即可用（v1.8.2）**：方舟服务端要先对视频做异步抽帧预处理（`status=processing`），插件会自动轮询文件状态直到 `active` 再发起理解（2s 起步、退避封顶 8s，总等待上限 = `request_timeout`，默认 300s）。因此发视频后「正在解析」会比普通文件多几秒到几十秒，属正常现象；预处理失败/超时会给出明确原因（经失败通知注入模型，不发群）。

### 开通与配置（火山方舟）

宿主 `llm.generate` 不支持视频输入，视频理解需要**独立的接口与密钥**：

1. 开通[火山方舟](https://console.volcengine.com/ark)，在「API Key 管理」创建 Key；在「开通管理」确认 `doubao-seed-2-1-lite` 系列模型已开通。
2. WebUI → 插件管理 → 文件读取 → **视频理解** 节，把 Key 填进 `api_key`，保存。
3. 直接发视频文件即可。默认模型 `doubao-seed-2-1-lite-260915`（¥0.8/¥2.7 每百万 token），也可换其他支持视频输入的方舟模型（改 `model`）。

**未配置 Key 时不报错**：视频文件会收到一条配置指引（经解析失败通知注入给模型，不发群消息），其他文件类型完全不受影响。

### 限制与拒绝策略（不截断、不分段）

| 项 | 默认 | 说明 |
|---|---|---|
| 大小 | ≤ 20MB（`max_file_mb`） | 面向 QQ 短视频；Files API 硬上限 512MB 不可放宽，配置只能收紧 |
| 时长 | ≤ 10 分钟（`max_duration_seconds=600`） | 超限**直接拒绝**并告知实际时长，提示剪辑后重发 |
| 日预算 | 20 个/天（`daily_budget`） | 只计真正发起上传的次数；跨天自动清零；0=不限 |
| 抽帧 | 1 fps（`fps`） | 上传时指定，控制 token 消耗 |
| 格式 | 六种开放封装直发 | `flv` `wmv` `rmvb` `rm` `3gp` 明确拒绝并提示转 mp4 |
| 时长读不出 | 拒绝 | fail-closed：MP4/MOV/AVI 读元数据（无 ffmpeg 依赖）；**mkv/webm 当前读不出时长，会被拒绝** |

预检在上传前完成，超限不产生任何费用。超限/超预算的拒绝原因经失败通知注入给模型（模型可向用户转述「剪短点再发」），**不直接发群消息**。同会话重发同一视频命中 sha256 缓存，直接复用不重看。

### 与音频的分派边界

改名的 mp4 / 无扩展名附件按文件头嗅探，**先判视频后判音频**（mp4 家族与 m4a 共享 ftyp 头，先判音频会把视频误喂给 Omni）；`M4A `/`M4B ` 品牌是纯音频，仍走音频通道。

## 命令

| 命令 | 说明 |
|---|---|
| `/clear_file` 或 `/clean_file` | 清除当前会话所有已入库文件 |
| `/file_status` | 查看插件状态（库规模/嵌入模型可用性/配置/**最近一次检索诊断**：模式、候选数、Top1 分数、耗时） |
| `/file_bm25 on\|off` | **v1.3.0 回滚开关**：运行期开关 BM25 词法检索，立刻生效、无需重启；不加参数则查看当前状态。重启后回到配置文件的值 |

## 使用

1. 在群里或私聊直接发送文件，插件自动完成：解析 → 分块 → 向量化 → 入库，并回执确认。
2. 直接提问（如「总结一下这个 PDF 的主要观点」「这份代码里有没有调用外部 API」），插件检索最相关片段注入上下文。

## 支持的文件类型

**真支持（零依赖）**

- 文档：`pdf`（需 pdfminer.six，已在 manifest 声明）`docx` `odt` `epub` `eml`
- 表格：`xlsx` `ods` `csv`
- 演示：`pptx` `odp`
- 网页/笔记本：`html` `htm`（去标签，跳过 script/style）`ipynb`（取 cells，跳过 outputs）
- 压缩：`gz`（解压后按内层扩展名递归解析）
- 代码：`py` `java` `cpp` `c` `h` `hpp` `cs` `js` `ts` `css` `scss` `less`（v1.7.0）`php` `rb` `go` `rs` `swift` `kt` `scala` `sh` `bash` `ps1` `bat` `cmd`
- 标记：`md` `markdown` `xml` `json` `yaml` `yml`
- 配置：`ini` `cfg` `conf` `properties` `env` `toml`
- 其他：`sql` `txt` `log` `lock` `gitignore` `url` `webloc` `srt` `vtt` `ass` `tex` `rst` `bib`

**条件支持**

- `xls`：旧版二进制需 `pandas` + `xlrd`；扩展名写错（其实是 xlsx）时按内容嗅探正常解析。

**音频（v1.5.0，需配置 `[audio]`，见上方「音频文件」章节）**

- `mp3` `wav` `flac` `ogg` `m4a` `aac`：Omni 模型听一次 → 文本入库 → 免费追问。

**视频（v1.8.0，需配置 `[video]`，见上方「视频文件」章节）**

- `mp4` `mov` `m4v` `avi`：豆包全模态看+听一次 → 文本入库 → 免费追问。
- `mkv` `webm`：模型支持，但当前读不出时长会被 fail-closed 拒绝（P2 待办）。

**可识别但不支持**（会给出明确原因，不再输出乱码）

- `doc` `ppt`：旧版二进制 Office → 提示另存为 docx/pptx。
- `rtf`：读出来是控制字乱码 → 提示另存为 docx/txt。
- `silk` `slk` `amr` `wma`（v1.5.0）：QQ 语音/旧音频格式 → 提示转 mp3/wav 后重发。
- `flv` `wmv` `rmvb` `rm` `3gp`（v1.8.0）：旧视频封装 → 提示转 mp4 后重发。

**扩展名不可信时按内容判断**：插件会嗅探文件内容（PDF/OLE/RTF 魔数、ZIP 内的 `mimetype` 与 OOXML 目录结构）。
改名的 docx（`.bin`）、转发后丢失扩展名的文件都能正确识别——只读中央目录，不解压正文。

## 自检 / 测试

```bash
# 结构自检
python tools/check_plugin.py plugins/file-reader

# 冒烟测试（FakeHost 完整生命周期，12 项：组件清单 / manifest 一致性 / SSRF 守卫 / 卸载收尾 / 音频通道）
python plugins/file-reader/tests/smoke_test.py

# pytest（262 项：下载安全守卫 + 分块 / 词法 / 注入 / 格式解析 / 提速专项 / 音频 / v1.6.0 容量与重试包，门禁里的 pytest 步骤跑的就是它）
python -m pytest -q plugins/file-reader/tests

# 宿主级加载探针（官方 MaiBot 源码 PluginLoader，严格版本区间 + SDK 契约 + 组件数）
python plugins/file-reader/tests/host_load_probe.py <MaiBot 源码根目录>

# 功能自测（解析→分块→向量化→检索→清理 + hook 直调，全量，用伪 embedding 不依赖真模型）
python plugins/file-reader/test_file_reader.py
```

> ⚠️ **`run_gates.py` 的 pytest 步骤只收集 `tests/` 目录**。综合功能自测
> `test_file_reader.py`位于**插件根目录**，门禁**看不到它**——改完功能必须手工跑一次，
> 并确认退出码为 0。v1.1.0 就是因为只信了门禁的"全绿"，把已经变红的自测和 SSRF 问题
> 一起带进了提交（当时门禁只跑 `check_plugin`，冒烟与 pytest 都是缺席状态）。
> 现在 `tests/` 下已有冒烟与 pytest 用例，但**根目录那 71 项仍不在门禁范围内**。

## 故障排查

排查顺序：`/file_status`（库规模/嵌入模型/配置/检索诊断）→ 插件日志（`[自检]` / `[下载拒绝]` / `[snowluma]` / `[napcat]` / `文件处理耗时` 开头）→ 下方常见问题对照。音频问题先看 `[自检] 音频通道` 与 `音频预检通过` 日志行。

## 常见问题

- **插件未加载**：检查 `_manifest.json` 是否合法、`numpy` 是否安装、三个生命周期方法是否齐全。
- **embedding 不可用**：`/file_status` 查看；需在 MaiBot 配置里启用一个支持 embedding 的模型（`llm.embed` 能力）。
- **某类文件解析失败**：docx/xlsx/pptx/txt/代码已零依赖；PDF 需 `pdfminer.six`（**已由 manifest 声明**，插件管理器会装；若手装环境请 `pip install pdfminer.six`）；xls/ods 需 `pandas openpyxl`（建议转存 xlsx/csv），按需安装。
- **回执「适配器只提供了 file_id，经适配器解析下载地址后仍未取回文件内容」**（v1.7.0）：首选通道与可选兜底都没取到字节。看日志 `[snowluma]` 行定位：`调用失败` = 适配器 RPC 异常（检查适配器是否在线、`api.call` 能力是否被宿主授权）；`未返回下载 URL` = 适配器动作失败（file_id 过期、user_id/group_id 对不上）；`下载失败` = URL 拿到了但下载被拒/超时（对照下一条白名单问题）。仍读不到时可临时启用 `napcat.enabled` 走 HTTP 兜底。
- **发出文件后回执「适配器把文件降级成了纯文本」**：旧版文本降级形态且 `napcat.enabled` 未开——按上方「NapCat 兜底」章节配置 HTTP 与 token（v1.7.0 的结构化 file 段走首选通道，不需要它）。
- **回执「文件回溯未返回内容」**：看日志里 `[napcat]` 开头的行；常见原因是 HTTP 服务未启用、token 错、`message_id` 对不上（可在 `/file_status` 里看「最近文件消息」的 mid），此时可配 `napcat.cache_dir` 走目录兜底。
- **下载报 CERTIFICATE_VERIFY_FAILED**：运行环境存在 TLS MITM 代理（如安全软件的 proxy-root-ca.cer）时，文件下载 URL 的证书链校验会失败。**首选**把代理根证书路径填进 `reader.download_ca_bundle`（校验保持开启）；仅在内网可控环境下才考虑 `reader.insecure_download = true`（跳过校验）。
- **文件「没有下载：文件链接不在允许下载的域名白名单内」**（v1.2.0）：URL 来源的文件被安全策略拦下了，日志里会有 `[下载拒绝] URL 未通过安全校验（<原因>）`。若你的环境用的是非 QQ 的适配器/CDN，把它的域名加进 `reader.download_allowed_hosts`（逗号分隔，后缀匹配）；确需放开任意公网主机可改 `download_url_policy = public`（仍会拒绝内网地址）。**v1.8.1 起适配器返回的 URL（含 QQ 离线文件裸 IP）不再命中此拒绝**；若仍出现，检查 `snowluma.trust_adapter_urls` 是否被关。
- **想让本机适配器的 HTTP 文件服务被读**（如文件 URL 指向 `127.0.0.1`）：需**同时**满足两个条件——把该主机加进 `download_allowed_hosts`，并设 `download_allow_private_hosts = true`。注意这等于重新放开对内网的访问能力，请确认你真的需要。
- **Hook 报 `'ReaderConfig' object has no attribute 'xxx'`**：配置字段按 section 分层，`enabled` 在 `plugin` 段（`self.config.plugin.enabled`），读取参数在 `reader` 段；跨段误访问会直接 AttributeError（v1.0.0 真机踩过，v1.0.1 已修）。
- **旧版 `.doc` / `.ppt` 为什么读不了**（v1.3.0）：它们是 OLE 二进制格式，解析需要 LibreOffice/antiword 级外部依赖，不适合塞进插件子进程。现在插件会识别 OLE 魔数并**明确提示**「请另存为 docx/pptx」，而不是丢一个 `缺少 python-docx` 的莫名错误或一堆乱码。
- **`.rtf` 为什么被移出支持列表**（v1.3.0）：旧版按纯文本读，解析出来全是 `\rtf1\ansi` 控制字——既占 token 又毫无信息。现在直接提示另存为 docx/txt。
- **`.odt` / `.ods` / `.odp` 现在能读了**（v1.3.0）：旧版分别走纯文本 / pandas / pptx 解析器（后者结构完全不同，必然失败）。现在用 `zipfile` + `ElementTree` 直接解 `content.xml`，零依赖。
- **检索感觉不如以前**（v1.3.0 混合检索）：先试 `/file_bm25 off` 退回纯向量——立刻生效、不用重启、不用改配置。确认是配置问题再动 `reader.hybrid_retrieve_enabled`。想调松紧用 `retrieve_min_score`（绝对下限）/ `retrieve_min_ratio`（相对 Top1）。
- **升级后 `/file_status` 里看不到新配置项**：宿主的 `config.toml` 只在文件**不存在**时生成，之后只会补新增字段、**已存在字段的值一律保留**。所以 `file_max_rounds` 若想用新的 20，需要手动改 `config.toml` 或删掉它重新生成（删前记得抄下 `admin_ids` 之类的现值）。v1.6.0 新增的容量/预算字段同样需要手动补齐，活跃/队列槽位修改后要完整重启。
- **文件被拒「当前处理队列已满」**（v1.6.0）：默认只激活 1 个、队列最多 3 个文件；等前序完成后重发即可。不要盲目调大 `active_file_limit`，4GB 机器上会线性放大峰值。
- **文件被拒「数据预算不足」**（v1.6.0）：`/file_status` 查看数据占用估算与预算。先 `/clear_file` 释放当前会话，或按机器内存调整 `data_budget_mb`；系统**不会**为接收新文件而静默淘汰旧文件。
- **文件被拒「解析文本/解压后超过上限」**（v1.6.0）：GZ/ZIP 与非 PDF 文本有硬上限，PDF 仍按页数截断。拆分文件或按需调高 `max_uncompressed_mb` / `parsed_text_max_chars`。
- **文件聊着聊着就没了**：v1.3.0 起有效期按「最后一次被注入」滑计算，并新增绝对上限 `file_absolute_ttl`（默认 720 分钟）。若确实需要更久，调大这两项；`file_max_rounds` 也要一起看（默认 20 轮，只给实际被问到的文件计轮）。
- **音频文件收到「需要音频理解模型…未配置 API Key」**（v1.5.0）：按「音频文件」章节开通百炼并把 Key 填进 `[audio].api_key`，配置后重发即可。
- **视频文件收到「需要视频理解模型…未配置 API Key」**（v1.8.0）：按「视频文件」章节开通火山方舟并把 Key 填进 `[video].api_key`，配置后重发即可。
- **视频收到「预处理超时」**（v1.8.2）：服务端抽帧预处理超过了 `video.request_timeout`（默认 300s）。稍后重发通常即可；大文件反复出现可调大该配置。若收到「预处理失败（服务端返回 failed）」并带错误码，多为视频损坏/编码异常，转成标准 mp4 后重发。
- **mkv/webm 视频收到「无法读取时长」**（v1.8.0）：当前版本不解析 EBML 时长元数据，fail-closed 拒绝；转成 mp4 后重发即可（P2 待办）。
- **音频被拒「时长/大小超过上限」**（v1.5.0）：这是刻意的 fail-closed 设计（不截断偷发）。剪辑或压码率后重发；确认模型支持更长音频时可调 `max_duration_seconds`（base64 10MB 硬上限不可放宽）。
- **音频被拒「无法读取时长」**（v1.5.0）：元数据损坏或假扩展名。转成标准 mp3/wav 后重发；确认 `mutagen` 已安装（manifest 已声明）。
- **音频理解报 HTTP 401/403**：Key 无效或额度用尽，去百炼控制台核对；报 HTTP 4xx 带 `model not exist` 时核对 `audio.model` 名称与 `base_url` 是否匹配。

> **版本细节见 [CHANGELOG.md](CHANGELOG.md)** —— 本文件只写「怎么装、怎么配、怎么排障」。

## 与 AstrBot 原版的差异

| 能力 | AstrBot 原版 | 本 MaiBot 版 |
|---|---|---|
| 向量库 | FaissVecDB | numpy 余弦相似度（自实现） |
| 重排序 | RerankProvider | 无（宿主只提供 `llm.embed`，无 rerank 能力）；改用 **BM25 词法 + RRF 融合** 补字面召回 |
| 嵌入 | embedding_provider | `llm.embed` 能力 |
| 注入 | on_llm_request | `before_model_request` hook（items） |
| 文件获取 | `item.get_file()` | 入站消息 file/attachment 段；v1.7.0 首选适配器 public API 换 URL 下载，失败回退 NapCat HTTP `get_msg`/`get_file` 回溯 |
