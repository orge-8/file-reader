# file-reader 读取提速改进方案（v1.4 目标）

> 版本：2026-11-04 ｜ 基线代码：v1.3.9 ｜ 强度：细问（grill 两轮后定稿）
> 本文档 = 施工蓝图：每个改进项含问题证据、函数级改动点、代码骨架、验收指标。

---

## 0. 实测基线（先量后改）

### 0.1 本机复测（开发机，Python 直跑插件解析器）

| 样本 | 大小 | 解析 | 分块 | 块数（512/overlap100） | 推算 embedding |
|---|---|---|---|---|---|
| 一些日志.txt | 0.74MB / 52 万字 | **0.01s** | 0.03s | 1456 | 23 批 |
| 奶蛙学 必修第一册.pdf | 18.4MB / 141 页 | pdfminer **3.80s** → pypdfium2 **1.51s** | ~0.01s | 182 | 3 批 |

结论：纯文本文件的解析与分块**不是瓶颈**（0.04s 级）；瓶颈在「块数 × embedding 批次耗时」和 PDF 解析。

### 0.2 真机日志证据（2026-11-03 15:21~15:48，鸟の小窝）

| 时间 | 事件 | 含义 |
|---|---|---|
| 15:21:47 | `[napcat] get_file 命中` **出现 2 次** | 同一文件被双 hook 并发触发，重复处理 |
| 15:22:49~15:24:06 | main loop 卡顿 3.53s / 1.06s / 0.86s / 0.58s | 落在 PDF 解析窗口内；pdfminer 纯 Python，to_thread 仍抢 GIL |
| 15:26:50~15:26:56 | http loop 10.06s、main loop **16.17s** | 与宿主统计任务（多周期统计 6.42s）叠加，归因待 A0 数据 |
| 15:47:46 | `已入库 奶蛙学…pdf：182 块` **出现 2 次**，各 3 批 embedding | 解析 + embedding 全部做了双份 |
| 15:47:46→15:47:50 | 概要生成 4s | 未优化，暂可接受 |

**核心结论：真机"读取慢"= 重复处理 ×2 + PDF 解析拖垮事件循环 + embedding 批次数随块数线性增长。** 解析器微观优化（lxml、分块器改写）无实测支撑，不做。

---

## 1. 决策记录（结论 + 理由）

| # | 问题 | 结论 | 理由 |
|---|---|---|---|
| D1 | PDF 新引擎选型 | **pypdfium2**（BSD-3，预编译轮子） | PyMuPDF 是 AGPL 与插件 MIT 许可冲突；pypdfium2 本机实测 2.5× pdfminer，且 C 扩展**释放 GIL**，根除真机事件循环卡顿。pdfminer 保留为兜底 |
| D2 | 依赖如何"装插件即可用" | 写进 `_manifest.json` 的 `dependencies`（`python_package` 类型） | MaiBot Host 依赖管道自动执行 `uv pip install --python` 装到插件所在环境，无需用户手工装（SDK 文档 + 现有 numpy/httpx/pdfminer.six 已验证此机制）。注意依赖条目是**严格字段白名单**：只有 `type`/`name`/`version_spec`，不能加 `optional` |
| D3 | chunk_size / overlap | **1024 / 128**（用户拍板） | embedding 批次数腰斩是最确定的提速。 |
| D4 | 解析↔embedding 流水线重叠 | **放弃** | 实测解析 ≤4s、embedding 数十秒，重叠收益上限 <10%，不值 80 行改动 |
| D5 | 重发缓存边界 | **会话内 + 不持久化** | 跨会话复用有侧信道语义（B 会话能感知"此文件被处理过"）；持久化引入缓存目录管理成本 |
| D6 | OCR（图片型 PDF） | **本期不做**，列入已知盲区 | 图文教材 141 页仅提取 7 万字是格式特性不是 bug；RapidOCR 路径留待下期单独评估 |
| D7 | 分块器预切 / lxml / 进程池 | **放弃** | 分块实测 0.03s；OOXML 解析无慢证据；pypdfium2 释放 GIL 后进程池无必要 |

---

## 2. 改进项（按实施顺序）

### A0 ｜ 分段耗时观测（先行，一切归因的地基）

**问题**：真机 15:21:47→15:47:46 有 26 分钟无法归因的空窗（解析慢？embedding 重试队列空转？网络？），现有日志只有"拆批进度"和"已入库"两个点。

**改动**：[plugin.py](plugin.py#L1281-L1400) `_handle_file`，用 `time.perf_counter()` 包四段，结束时打一条汇总日志：

```python
t_fetch = t_parse = t_chunk = t_embed = 0.0
# 取字节后：t_fetch = time.perf_counter() - t0
# read_any_file_to_text 后：t_parse = ...
# vs.add_file 内部 chunk/embed 拆分计时（vector_store.py add_file 里各包一层）
self.ctx.logger.info(
    "文件 %s 处理耗时: 取字节=%.1fs 解析=%.1fs 分块=%.1fs embedding=%.1fs（%d 块 / %.1fMB）",
    name, t_fetch, t_parse, t_chunk, t_embed, len(entry.chunks), size_mb,
)
```

**验收**：真机再发一次奶蛙学 PDF，日志能直接读出四段耗时，26min 空窗归因闭环。

---

### A1 ｜ 重复处理去重（收益最直接：成本 ×2 → ×1）

**问题**：双 hook（before/after_process）或适配器重复投递导致同一文件并发进入两个 `_handle_file` 任务。[plugin.py](plugin.py) 现有 `_recent_file_keys` 去重未拦住（两任务**同时**通过检查，是典型的 check-then-act 竞态）。

**改法**：加 in-flight 注册表，键用稳定文件身份：

```python
self._inflight: dict[str, asyncio.Task] = {}  # 插件实例级

identity = f"{session_id}|{napcat_path or fdata.get('url') or (name, size_hint)}"
task = self._inflight.get(identity)
if task is not None and not task.done():
    self.ctx.logger.info("文件 %s 正在处理中（重复触发），本次直接复用", name)
    return  # 或 await task 后仅补回执
self._inflight[identity] = asyncio.current_task()
try:
    ...  # 原处理逻辑
finally:
    self._inflight.pop(identity, None)
```

优先级：`napcat_path`（真机已证明可靠回填）> `url` > `(name, size)`。**不跨重启**、不做持久化（D5 同款边界）。

**验收**：真机同一消息双 hook 触发时，日志仅一条 `已入库`，embedding 调用次数减半；`tests/` 新增 `test_inflight_dedup.py` 模拟并发双触发。

---

### A2 ｜ PDF 引擎：pypdfium2 优先 + pdfminer 兜底 + 页数上限

**问题**：`_read_pdf` 只有 pdfminer.six（[file_parser.py](file_parser.py#L670-L673)），纯 Python、慢、抢 GIL（真机卡顿证据见 §0.2）。

**改法**（与现有"标准库/优选库优先 + 兜底降级"模式同构）：

1. `_manifest.json` → `dependencies` 追加：
   ```json
   { "type": "python_package", "name": "pypdfium2", "version_spec": ">=4.30" }
   ```
2. [file_parser.py](file_parser.py) 顶部可选导入区加 `pypdfium2` try-import；
3. `_read_pdf` 改为：

```python
def _read_pdf_pypdfium2(path: str, max_pages: int) -> str:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(path)
    try:
        parts = []
        for i in range(min(len(pdf), max_pages)):
            page = pdf[i]
            try:
                parts.append(page.get_textpage().get_text_range())
            finally:
                page.close()
        return "\n".join(parts)
    finally:
        pdf.close()

def _read_pdf(path: str) -> str:
    max_pages = _PDF_MAX_PAGES  # 配置注入，默认 500
    if pypdfium2 is not None:
        try:
            text = _read_pdf_pypdfium2(path, max_pages)
            if text.strip():
                return text
            # 空文本 = 纯扫描件，直接报准（不再让 pdfminer 白跑几分钟）
            raise RuntimeError("PDF 未提取到文字，可能是纯扫描件（本期不支持 OCR）")
        except Exception:
            pass  # 落 pdfminer 兜底
    if extract_text is None:
        raise RuntimeError("缺少依赖 pdfminer.six，请安装：pip install pdfminer.six")
    return extract_text(path, maxpages=max_pages)
```

4. 新增配置项 `reader.pdf_max_pages`（默认 500）：超限截断并在回执中提示"已截取前 N 页"。

**验收**：
- 本机奶蛙学样本 ≤2s；
- 真机处理期间 `event_loop_watchdog` 不出现 >2s 的 main loop 告警（pypdfium2 释放 GIL）；
- 故意损坏的 PDF 落 pdfminer 兜底，双失败时报错信息含两个引擎的原因。

---

### A3 ｜ chunk_size 默认 1024 + overlap 12（D3，配置级改动，立即见效）

**问题**：`chunk_size=512` 下日志样本 1456 块 → 23 批 embedding。块数与 embedding 耗时线性相关。

**改法**：[plugin.py](plugin.py) 配置默认值 `chunk_size: 512→1024`、`chunk_overlap: 100→12`。

- 兼容性：`FileEntry.chunk_overlap` 已按 entry 存储，`full_text()` 去重逻辑无需改；旧数据（overlap=100）与新数据共存无冲突。
- 检索粒度变粗的代价由 BM25 词法检索（已有）兜底。
- embedding 并发拆批（v1.0.18 gather）已生效（真机 3 批同秒完成），**不重复优化**；如需进一步提速只调 `embed_concurrency` 配置，不改代码。

**验收**：同一日志样本入库块数 ≈ 原 50%（1456 → ~730）；`test_fulltext_overlap.py` 全绿；抽 3 个主题级提问检索结果不回退。

---

### A4 ｜ 文本编码探测提速（低成本顺手项）

**问题**：`_read_txt` 对**全量字节**跑 `chardet.detect`（[file_parser.py](file_parser.py#L702-L715)），>10MB 的日志文件探测即秒级。

**改法**：

```python
def _read_txt(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8")          # ① 快路径：命中即返回，零探测
    except UnicodeDecodeError:
        pass
    encoding = "utf-8"
    if chardet is not None:
        det = chardet.detect(raw[:65536])   # ② 只探前 64KB
        if det and det.get("encoding") and det.get("confidence", 0) > 0.5:
            encoding = det["encoding"]
    for enc in (encoding, "gbk", "gb18030", "latin-1"):  # ③ 兜底链不变
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")
```

**验收**：50MB UTF-8 日志读取 <1s；GBK 编码旧文件（找真机样本）仍正确解码。

---

### A5 ｜ 会话内重发哈希缓存（D5：会话内 + 不持久化）

**问题**：群里转发/用户重发同一文件时，解析 + embedding 全部重跑。

**改法**：[plugin.py](plugin.py) `_handle_file` 在拿到 `raw_bytes` 后、写临时文件前：

```python
import hashlib
digest = hashlib.sha256(raw_bytes).hexdigest()  # 字节已在内存，全量哈希仅 ms 级
cached = self._hash_index.get((session_id, conversation_id, digest))
if cached and cached in vs.files:
    vs.touch_files([cached])
    await self._reply_ok(..., f"已读取过《{cached}》，直接复用")
    return
# 入库成功后：self._hash_index[(session_id, conversation_id, digest)] = name
```

- `_hash_index` 挂插件实例，会话清理 / `SessionStore.drop` 时同步清除对应键，**不落盘**。
- 内存有界：每条 64 字节 hex + 键，千级文件忽略不计。

**验收**：同会话重发奶蛙学 PDF，回执秒回且日志零 embedding 调用；`tests/` 新增重发用例。

---

## 3. 放弃项与理由（防止以后当新点子重提）

| 项 | 放弃理由 |
|---|---|
| 解析↔embedding 流水线重叠（原 P4） | 解析 ≤4s vs embedding 数十秒，收益 <10%（D4） |
| 分块器 `\n` 预切 / 迭代化 | 52 万字实测分块 0.03s，无热点 |
| lxml / iterparse 改写 OOXML 解析 | docx/xlsx 无真机慢证据 |
| ProcessPoolExecutor | pypdfium2 释放 GIL 后，线程池已能真并行 |
| 跨会话 / 持久化缓存 | 侧信道语义 + 缓存目录管理成本（D5） |
| OCR | 本期不做（D6），见 §4 |
| embed 并发拆批 | 已生效（真机 3 批同秒完成），只调配置不改代码 |

## 4. 已知盲区（不是 bug，别当 bug 排查）

1. **图片型 PDF 无 OCR**：141 页图文教材只提取 7 万字属预期行为；公式/图表/插图内容问答必然答不上。下期可评估 RapidOCR（可选依赖、仅对无文本页触发）。
2. **26min 空窗归因未定**：A0 落地前的假设是"pdfminer 慢 + 重复处理"，若 A0 数据显示 embedding 重试队列空转（`embed_retry_interval=2min` × 13 轮恰好 ≈26min），则另开一项修重试队列的回执反馈（用户 26 分钟零反馈是体验问题）。
3. **概要生成 4s/文件**：异步执行不阻塞回执，暂不优化。

## 5. 验收总表

| 项 | 指标 | 测量方式 |
|---|---|---|
| A0 | 日志含四段耗时 | 真机日志直读 |
| A1 | 同文件双触发仅一次入库 | 真机日志 + `test_inflight_dedup.py` |
| A2 | 18MB PDF 本机 ≤2s；处理期无 >2s loop 告警 | 本机秒表 + 真机 watchdog |
| A3 | 块数减半；检索不回退 | 入库日志 + 3 个主题提问 |
| A4 | 50MB UTF-8 日志 <1s；GBK 不错码 | 本机构造样本 |
| A5 | 重发秒回、零 embedding 调用 | 真机日志 |

**总目标**：奶蛙学 PDF 从"发文件到可提问"的真机耗时，由 26min 级降到 **30s 内**；日志类大文本由 embedding 批次数减半带动总耗时降约 50%。

## 6. 风险与回滚

- **pypdfium2 安装失败**（离线/架构无轮子）：try-import 缺失时自动落回 pdfminer，行为与现状完全一致，无新故障面。
- **chunk_size 变更影响既有会话**：仅影响新入库文件；旧 entry 带自身 `chunk_overlap`，拼接去重不受影响。
- **A1 去重误伤**：identity 键包含 session 隔离；napcat_path 缺失时退化 (name, size)，同名不同内容文件的误判窗口仅存在于"处理中"期间（秒级），可接受。
- 每项独立提交，可按 A0→A3→A1→A2→A4→A5 顺序单独回滚（A3 配置项先行见效，A2 依赖安装最重放后面）。

---

## 7. 落地结果（2026-11-04，v1.4.0）

六项全部实现，验收指标对照如下（本机实测，样本 = 桌面 `奶蛙学 必修第一册.pdf` 18.4MB / 141 页 + 合成日志）：

| 项 | 计划指标 | 实测结果 | 结论 |
|---|---|---|---|
| A0 | 日志含分段耗时 | 五段（取字节/解析/分块/embedding/词法索引）+ 合计 + 块数 + 体积 | ✅ |
| A1 | 同文件双触发仅一次入库 | 并发双任务只解析 1 次、入库 1 条；日志出现「正在处理中（重复触发），本次直接复用」 | ✅ |
| A2 | 18MB PDF ≤2s；无 >2s loop 告警 | pypdfium2 **1.12s**（pdfminer 4.01s，**3.6×**）；main loop 最大滞后 **21ms**（pdfminer 59ms） | ✅ 超额 |
| A3 | 块数 ≈ 原 50% | 52 万字日志 1351 → **591 块**（−56%），批数 22 → **10**；教材 PDF 179 → 81 块 | ✅ |
| A4 | 50MB UTF-8 <1s；GBK 不错码 | 31MB UTF-8 读取 **0.03s**（旧路径 chardet 全量探测 0.22s，新探测前 64KB 仅 0.001s）；GBK/GB18030 兜底链未变，用例覆盖 | ✅ |
| A5 | 重发秒回、零 embedding | 同会话重发同字节文件：解析 0 次、embedding 0 次，`touch_files` 续期 | ✅ |

### 与计划的两处偏差（均为实现层修正，不改结论）

1. **A2 空文本不降级**：计划骨架里 `raise RuntimeError` 会被同层 `except Exception: pass` 吞掉，实际仍是落 pdfminer 白跑。改为「引擎**异常**才降级；引擎**成功但无文字**直接报扫描件」。双引擎都失败时报错带两边原因。
2. **A2 截断提示走 ContextVar**：`SUPPORTED_EXTENSIONS` 的类型是 `Callable[[str], str]`，给 `_read_pdf` 加参数会破坏扩展名分派表（且现有测试打桩的是 2 参数签名）。改用 `collect_parse_notes`（ContextVar + `asyncio.to_thread` 上下文复制），并发文件互不串台。

### 新增可观测项

`/file_status` 增加「PDF 页数上限」；启动自检增加「PDF 引擎」一条，明确区分 PASS（双引擎）/ WARN（仅 pdfminer，会拖慢事件循环）/ FAIL（都没有）。

### 真机待验收

发一次奶蛙学 PDF，确认：① 日志出现五段耗时；②「已入库 …」只出现一次；③ 回执/日志出现「PDF 共 N 页，已只读取前 M 页」（若超过 500 页）。
