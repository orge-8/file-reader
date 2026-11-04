"""文件解析模块（自包含，无 AstrBot 依赖）

把各类文件解析为纯文本，供分块向量化使用。
参考 astrbot_plugin_file_reader_pro 的解析器设计，但改用可选依赖 + 优雅降级：
解析器缺库时不抛硬错误，而是返回可读提示，让插件整体仍可加载。

支持类型（SUPPORTED_EXTENSIONS）：
  文档:  pdf / docx / doc / rtf / odt
  表格:  xlsx / xls / ods / csv
  演示:  pptx / ppt / odp
  代码:  py java cpp c h hpp cs js ts php rb go rs swift kt scala sh bash ps1 bat cmd
  标记:  md markdown html htm xml json yaml yml
  配置:  ini cfg conf properties env toml
  其他:  sql txt log lock gitignore url webloc
"""
from __future__ import annotations

import csv as _csv_mod
import io
import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterator, Optional

_LOGGER = logging.getLogger(__name__)

# ─── 可选依赖（try-import，缺失则降级） ──────────────────────────
try:
    import chardet  # type: ignore
except ImportError:  # pragma: no cover
    chardet = None

try:
    from pdfminer.high_level import extract_text  # type: ignore
except ImportError:  # pragma: no cover
    extract_text = None

# v1.4.0：PDF 首选引擎（C 扩展，解析期间**释放 GIL**，且比 pdfminer 快约 2.5×）。
# 用宽口径 except：轮子/DLL 损坏时抛的是 OSError 而不是 ImportError，
# 一律按"不可用"降级到 pdfminer，不能因为装了半个包就让 PDF 彻底读不了。
try:
    import pypdfium2 as _pdfium  # type: ignore
except Exception:  # noqa: BLE001  pragma: no cover
    _pdfium = None


# PDF 页数上限（v1.4.0）：由插件配置 reader.pdf_max_pages 注入，0 = 不限制。
# 放在模块级是因为解析函数签名要走 SUPPORTED_EXTENSIONS 的 `Callable[[str], str]`，
# 加参数会破坏扩展名分派表；插件侧用 set_pdf_max_pages() 同步。
_PDF_MAX_PAGES = 500

# 解析提示（v1.4.0）：解析器需要回话给调用方的内容（如"PDF 已截取前 N 页"）。
# 用 ContextVar 而不是返回值：`asyncio.to_thread` 会复制当前上下文，
# 所以事件循环里 with 住的列表在工作线程里同样可见，且并发文件互不串台。
_PARSE_NOTES: ContextVar[Optional[list]] = ContextVar("file_parser_notes", default=None)


def set_pdf_max_pages(pages: int) -> None:
    """设置 PDF 解析页数上限（0 或负数 = 不限制）。由插件在加载/热重载时同步。"""
    global _PDF_MAX_PAGES
    try:
        _PDF_MAX_PAGES = max(0, int(pages or 0))
    except (TypeError, ValueError):
        _PDF_MAX_PAGES = 500


def get_pdf_max_pages() -> int:
    """当前 PDF 页数上限（0 = 不限制）。"""
    return _PDF_MAX_PAGES


@contextmanager
def collect_parse_notes(notes: list) -> Iterator[None]:
    """收集本次解析的提示信息（线程安全）。

    用法：`with collect_parse_notes(buf): text = await asyncio.to_thread(read_any_file_to_text, p, n)`。
    """
    token = _PARSE_NOTES.set(notes)
    try:
        yield
    finally:
        _PARSE_NOTES.reset(token)


def _note(notes: Optional[list], text: str) -> None:
    """写一条解析提示：显式传入的列表优先，否则落到当前上下文的收集器。"""
    sink = notes if notes is not None else _PARSE_NOTES.get()
    if sink is not None:
        sink.append(text)


def pdf_engine_available() -> tuple[bool, bool]:
    """(pypdfium2 可用, pdfminer 可用)，供自检与状态命令使用。"""
    return (_pdfium is not None, extract_text is not None)

try:
    import docx2txt  # type: ignore
except ImportError:  # pragma: no cover
    docx2txt = None

try:
    import pandas as pd  # type: ignore
except ImportError:  # pragma: no cover
    pd = None

try:
    from pptx import Presentation  # type: ignore
except ImportError:  # pragma: no cover
    Presentation = None


# ─── 扩展名 → 处理函数映射 ────────────────────────────────────────
# Office 开放格式（docx / xlsx / pptx）本质是 zip 包 + XML，用标准库 zipfile /
# ElementTree 就能零依赖解析——插件跑在 MaiBot 的独立子进程里，装第三方库必须先
# 找准那个进程用的 python.exe，成本高还容易装错环境。策略：标准库优先（本地与
# 真机行为一致，便于排障）→ 失败或结果为空时退回第三方库兜底。

_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

# _parse_xml 兜底分支的替换正则（模块级预编译，v1.1.0）
_RE_XML_PREFIX_TAG = re.compile(r"<(/?)([A-Za-z0-9]+):")
_RE_XML_NS_DECL = re.compile(r'xmlns:[A-Za-z0-9]+="[^"]*"')
_RE_XML_PREFIX_ATTR = re.compile(r"\s([A-Za-z0-9]+):([A-Za-z0-9]+)=")


def _docx_from_zip(path: str) -> str:
    """标准库解析 docx：按段落 <w:p> 收集 <w:t> 文本，制表/换行转字符。"""
    import zipfile
    import xml.etree.ElementTree as ET

    w = f"{{{_NS_W}}}"
    out: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        parts = [n for n in names if n == "word/document.xml"]
        # 页眉页脚作为补充内容
        parts += sorted(n for n in names if n.startswith("word/header") or n.startswith("word/footer"))
        if not parts:
            raise RuntimeError("找不到 word/document.xml，可能不是有效的 docx")
        for name in parts:
            root = _parse_xml(zf.read(name))
            for para in root.iter(w + "p"):
                buf: list[str] = []
                for node in para.iter():
                    if node.tag == w + "t":
                        buf.append(node.text or "")
                    elif node.tag == w + "tab":
                        buf.append("\t")
                    elif node.tag in (w + "br", w + "cr"):
                        buf.append("\n")
                line = "".join(buf).strip()
                if line:
                    out.append(line)
    return "\n".join(out)


def _ooxml_slide_or_sheet_order(name: str) -> int:
    """'xl/worksheets/sheet3.xml' / 'ppt/slides/slide12.xml' → 序号。"""
    m = re.search(r"(\d+)\.xml$", name)
    return int(m.group(1)) if m else 0


def _xlsx_col_index(ref: str) -> int:
    """单元格引用 'AB12' → 列序号（0 基）；解析不出时返回大数保持原顺序。"""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1 if n else 10**6


def _xlsx_from_zip(path: str) -> str:
    """标准库解析 xlsx：sharedStrings + worksheets，按行制表符拼接。"""
    import zipfile
    import xml.etree.ElementTree as ET

    s = f"{{{_NS_S}}}"
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()

        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = _parse_xml(zf.read("xl/sharedStrings.xml"))
            for si in root.iter(s + "si"):
                shared.append("".join(t.text or "" for t in si.iter(s + "t")))

        sheet_titles: list[str] = []
        if "xl/workbook.xml" in names:
            wb = _parse_xml(zf.read("xl/workbook.xml"))
            sheet_titles = [str(sh.get("name") or "") for sh in wb.iter(s + "sheet")]

        sheets = sorted(
            (n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")),
            key=_ooxml_slide_or_sheet_order,
        )
        if not sheets:
            raise RuntimeError("找不到工作表，可能不是有效的 xlsx")

        blocks: list[str] = []
        for i, sheet in enumerate(sheets):
            root = _parse_xml(zf.read(sheet))
            rows: list[str] = []
            for row in root.iter(s + "row"):
                cells: list[tuple[int, str]] = []
                for c in row.iter(s + "c"):
                    ctype = c.get("t")
                    if ctype == "s":  # 共享字符串索引
                        v = c.find(s + "v")
                        try:
                            val = shared[int(v.text)] if v is not None and v.text else ""
                        except (ValueError, IndexError):
                            val = ""
                    elif ctype == "inlineStr":
                        is_el = c.find(s + "is")
                        val = "".join(x.text or "" for x in is_el.iter(s + "t")) if is_el is not None else ""
                    else:
                        v = c.find(s + "v")
                        val = v.text if v is not None and v.text is not None else ""
                    cells.append((_xlsx_col_index(str(c.get("r") or "")), val))
                if cells:
                    cells.sort(key=lambda x: x[0])
                    rows.append("\t".join(v for _, v in cells))
            if rows:
                title = sheet_titles[i] if i < len(sheet_titles) and sheet_titles[i] else f"Sheet{i + 1}"
                blocks.append(f"=== {title} ===\n" + "\n".join(rows))
    return "\n\n".join(blocks)


def _pptx_from_zip(path: str) -> str:
    """标准库解析 pptx：按幻灯片顺序收集 <a:p> 段落文本。"""
    import zipfile
    import xml.etree.ElementTree as ET

    a = f"{{{_NS_A}}}"
    with zipfile.ZipFile(path) as zf:
        slides = [n for n in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)]
        slides.sort(key=_ooxml_slide_or_sheet_order)
        if not slides:
            raise RuntimeError("找不到幻灯片，可能不是有效的 pptx")
        out: list[str] = []
        for i, name in enumerate(slides, 1):
            root = _parse_xml(zf.read(name))
            texts: list[str] = []
            for para in root.iter(a + "p"):
                line = "".join(t.text or "" for t in para.iter(a + "t")).strip()
                if line:
                    texts.append(line)
            if texts:
                out.append(f"=== 幻灯片 {i} ===\n" + "\n".join(texts))
    return "\n\n".join(out)


def _parse_xml(data: bytes) -> "ET.Element":
    """容错解析 OOXML 内的 XML：r: 等前缀未声明时退化为本地名匹配。"""
    import xml.etree.ElementTree as ET

    try:
        return ET.fromstring(data)
    except ET.ParseError:
        # 部分 OOXML 写入器会引用 r: / mc: 等前缀却不声明；去掉所有前缀后重试
        text = data.decode("utf-8", errors="replace")
        text = _RE_XML_PREFIX_TAG.sub(r"<\1\2_", text)  # <r:id> → <r_id>，避免 unbound prefix
        text = _RE_XML_NS_DECL.sub("", text)
        text = _RE_XML_PREFIX_ATTR.sub(r" \1_\2=", text)  # 属性 r:id → r_id
        return ET.fromstring(text)


def _parse_office(stdlib_fn: Callable[[str], str], third_party: Optional[Callable[[str], str]], label: str, path: str) -> str:
    """Office 解析：标准库优先，第三方库兜底，都失败给出可操作提示。"""
    errors: list[str] = []
    try:
        text = stdlib_fn(path)
        if text and text.strip():
            return text
        errors.append("标准库解析结果为空")
    except Exception as e:  # noqa: BLE001
        errors.append(f"标准库解析失败({type(e).__name__}: {e})")
    if third_party is not None:
        try:
            text = third_party(path)
            if text and text.strip():
                return text
            errors.append("第三方库解析结果为空")
        except Exception as e:  # noqa: BLE001
            errors.append(f"第三方库解析失败({type(e).__name__}: {e})")
    raise RuntimeError(f"{label} 解析失败：{'；'.join(errors)}。文件可能是加密/损坏/纯图片扫描件")


def _pd_read_excel_all(path: str) -> str:
    """pandas 兜底：读全部 sheet。"""
    sheets = pd.read_excel(path, sheet_name=None)
    parts = []
    for name, df in sheets.items():
        parts.append(f"=== {name} ===\n{df.to_string(index=False)}")
    return "\n\n".join(parts)


# ─── v1.3.0 新增格式（全部零依赖） ──────────────────────────────
# ODF 家族（odt/ods/odp）此前是**假支持**：odt 走 _read_txt 读成 zip 乱码、
# ods 走 pandas、odp 走 pptx 解析器（结构完全不同，必然失败）。这里用 zipfile +
# ElementTree 直解，与其它 OOXML 解析器同一套路。
_NS_ODF_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_NS_ODF_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_NS_ODF_DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
_NS_ODF_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"

# 老二进制 Office（doc/xls/ppt）的 OLE 复合文档魔数
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _is_ole(path: str) -> bool:
    """是否 OLE 复合文档（旧版二进制 Office）。"""
    try:
        with open(path, "rb") as f:
            return f.read(8) == _OLE_MAGIC
    except OSError:
        return False


def _odf_collect(node, t: str, buf: list[str]) -> None:
    """递归收集文本：tab → \\t，line-break → \\n，s（空格）按 c 属性展开。

    递归而不是 `node.iter()`：ElementTree 把「标签之间的文字」放在 `.text` / `.tail`，
    只遍历子孙会漏掉节点自身的 text（`<text:p>正文</text:p>` 的文字就在 p.text 上）。
    """
    if node.text:
        buf.append(node.text)
    for child in node:
        tag = child.tag
        if tag == t + "tab":
            buf.append("\t")
        elif tag == t + "line-break":
            buf.append("\n")
        elif tag == t + "s":
            try:
                buf.append(" " * max(1, int(child.get(t + "c") or 1)))
            except (TypeError, ValueError):
                buf.append(" ")
        else:
            _odf_collect(child, t, buf)
        if child.tail:
            buf.append(child.tail)


def _odf_para_text(node, t: str) -> str:
    """ODF 段落 → 纯文本。"""
    buf: list[str] = []
    _odf_collect(node, t, buf)
    return "".join(buf).strip()


def _odf_from_zip(path: str, kind: str) -> str:
    """标准库解析 ODF：content.xml 一个文件就装下了正文、表格与幻灯片。"""
    import zipfile

    t = "{%s}" % _NS_ODF_TEXT
    tb = "{%s}" % _NS_ODF_TABLE
    dr = "{%s}" % _NS_ODF_DRAW
    of = "{%s}" % _NS_ODF_OFFICE

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if "content.xml" not in names:
            raise RuntimeError("找不到 content.xml，可能不是有效的 ODF 文件")
        root = _parse_xml(zf.read("content.xml"))

    if kind == "ods":
        blocks: list[str] = []
        for table in root.iter(tb + "table"):
            title = table.get(tb + "name") or ""
            rows: list[str] = []
            for row in table.iter(tb + "table-row"):
                cells: list[str] = []
                for cell in row.iter(tb + "table-cell"):
                    txt = " ".join(_odf_para_text(p, t) for p in cell.iter(t + "p")).strip()
                    if not txt:
                        val = cell.get(of + "value")
                        txt = "" if val is None else str(val)
                    cells.append(txt)
                if any(cells):
                    rows.append("\t".join(cells))
            if rows:
                blocks.append(f"=== {title or 'Sheet'} ===\n" + "\n".join(rows))
        if not blocks:
            raise RuntimeError("ODS 里没有可读取的表格内容（可能是空的或纯图片）")
        return "\n\n".join(blocks)

    if kind == "odp":
        out: list[str] = []
        for i, page in enumerate(root.iter(dr + "page"), 1):
            texts = [
                line
                for line in (_odf_para_text(p, t) for p in page.iter(t + "p"))
                if line
            ]
            if texts:
                out.append(f"=== 幻灯片 {i} ===\n" + "\n".join(texts))
        if not out:
            raise RuntimeError("ODP 里没有可读取的文本（可能是纯图片幻灯片）")
        return "\n\n".join(out)

    # odt：段落/标题按文档顺序；表格单独成块（表格内的段落不再重复输出）
    inside_table = set()
    for cell in root.iter(tb + "table-cell"):
        for p in cell.iter(t + "p"):
            inside_table.add(id(p))
    lines: list[str] = []
    for node in root.iter():
        if node.tag in (t + "h", t + "p") and id(node) not in inside_table:
            line = _odf_para_text(node, t)
            if line:
                lines.append(line)
    for table in root.iter(tb + "table"):
        rows = []
        for row in table.iter(tb + "table-row"):
            cells = []
            for cell in row.iter(tb + "table-cell"):
                txt = " ".join(_odf_para_text(p, t) for p in cell.iter(t + "p")).strip()
                if not txt:
                    val = cell.get(of + "value")
                    txt = "" if val is None else str(val)
                cells.append(txt)
            if any(cells):
                rows.append("\t".join(cells))
        if rows:
            lines.append("\n".join(rows))
    if not lines:
        raise RuntimeError("ODT 里没有可读取的文本（可能是加密、损坏或纯图片）")
    return "\n".join(lines)


class _TextHTMLParser(HTMLParser):
    """HTML → 文本：跳过 script/style/noscript/svg，块级标签补换行。"""

    _SKIP = {"script", "style", "noscript", "svg", "head"}
    _BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):  # noqa: D102
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):  # noqa: D102
        if tag in self._SKIP:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):  # noqa: D102
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


def _html_to_text(raw: str) -> str:
    """HTML 去标签。script/style 内容不得进入上下文（占 token 且毫无信息）。"""
    parser = _TextHTMLParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001  畸形 HTML 退回粗暴去标签
        return re.sub(r"<[^>]+>", " ", raw)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


def _read_html(path: str) -> str:
    """html/htm：此前按原文入库，脚本与样式会吃掉大量 token。"""
    text = _html_to_text(_read_txt(path))
    if not text.strip():
        raise RuntimeError("HTML 里没有可读取的正文（可能是纯脚本页面）")
    return text


def _read_epub(path: str) -> str:
    """epub：container.xml → OPF spine 顺序 → XHTML 去标签。"""
    import zipfile

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        opf_path = None
        if "META-INF/container.xml" in names:
            root = _parse_xml(zf.read("META-INF/container.xml"))
            for node in root.iter():
                if str(node.tag).endswith("rootfile"):
                    opf_path = node.get("full-path")
                    break
        if not opf_path:
            opf_path = next((n for n in names if n.endswith(".opf")), None)
        if not opf_path:
            raise RuntimeError("找不到 OPF 描述文件，可能不是有效的 epub")

        opf_root = _parse_xml(zf.read(opf_path))
        base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""

        manifest: dict[str, tuple[str, str]] = {}
        for node in opf_root.iter():
            if str(node.tag).endswith("item"):
                manifest[node.get("id") or ""] = (node.get("href") or "", node.get("media-type") or "")
        spine: list[tuple[str, str]] = []
        for node in opf_root.iter():
            if str(node.tag).endswith("itemref"):
                item = manifest.get(node.get("idref") or "")
                if item:
                    spine.append(item)
        if not spine:
            spine = [v for v in manifest.values() if "html" in v[1] or v[0].endswith((".xhtml", ".html"))]

        out: list[str] = []
        total = 0
        for href, _mt in spine:
            if len(out) >= 200 or total > 2_000_000:  # 上限：文档 200 个 / 正文 200 万字
                break
            target = (base + href if base else href).split("#")[0]
            if target not in names:
                tail = href.split("/")[-1]
                target = next((n for n in names if n.endswith(tail)), "")
                if not target:
                    continue
            try:
                text = _html_to_text(zf.read(target).decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                continue
            if text.strip():
                out.append(text)
                total += len(text)
        if not out:
            raise RuntimeError("epub 里没有可读取的正文（可能是加密或异常结构）")
        return "\n\n".join(out)


def _read_ipynb(path: str) -> str:
    """ipynb：此前按 JSON 原文入库，outputs 里的 base64 图片会把分块撑爆。"""
    import json

    try:
        data = json.loads(_read_txt(path))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ipynb 不是合法 JSON：{e}") from e
    cells = data.get("cells") or []
    out: list[str] = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        src = cell.get("source") or []
        text = "".join(src) if isinstance(src, list) else str(src)
        if not text.strip():
            continue  # 跳过 outputs：base64 图片/HTML 表格对检索是纯噪音
        tag = "[code]" if cell.get("cell_type") == "code" else "[md]"
        out.append(f"{tag} {text.strip()}")
    if not out:
        raise RuntimeError("ipynb 里没有单元格内容")
    return "\n\n".join(out)


def _read_eml(path: str) -> str:
    """eml：优先 text/plain，回退 html 去标签。"""
    import email
    from email import policy

    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        raise RuntimeError(f"读取邮件失败：{e}") from e
    if len(raw) > 20 * 1024 * 1024:
        raise RuntimeError("邮件超过 20MB，已跳过")
    msg = email.message_from_bytes(raw, policy=policy.default)
    subject = str(msg.get("Subject") or "")
    body_plain = ""
    body_html = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() != "text":
                continue
            try:
                content = part.get_content()
            except Exception:  # noqa: BLE001
                continue
            if part.get_content_subtype() == "plain" and not body_plain:
                body_plain = str(content)
            elif part.get_content_subtype() == "html" and not body_html:
                body_html = str(content)
    else:
        try:
            content = str(msg.get_content())
        except Exception:  # noqa: BLE001
            content = ""
        if msg.get_content_subtype() == "html":
            body_html = content
        else:
            body_plain = content
    body = body_plain.strip() or _html_to_text(body_html).strip()
    if not body:
        raise RuntimeError("邮件里没有可读取的正文")
    return (f"主题：{subject}\n\n{body}" if subject else body)


def _read_gz(path: str) -> str:
    """gz：解压到临时文件，按内层扩展名递归解析（防 zip/gz bomb：解压后仍受 max_file_size 约束）。"""
    import gzip
    import shutil
    import tempfile

    inner = Path(path).name
    if inner.lower().endswith(".gz"):
        inner = inner[:-3]
    fd, tmp_path = tempfile.mkstemp(suffix=Path(inner).suffix or "")
    try:
        with os.fdopen(fd, "wb") as out_f:
            with gzip.open(path, "rb") as src:
                shutil.copyfileobj(src, out_f, length=1024 * 256)
        return read_any_file_to_text(tmp_path, inner)
    except OSError as e:
        raise RuntimeError(f"gzip 解压失败：{e}") from e
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _read_legacy_office(path: str, kind: str) -> str:
    """老二进制 Office（doc/ppt）：是 OLE 就给准确提示，不是就按内容试 OOXML。

    为什么不静默失败：旧版把 .doc 丢给 python-docx，报的是「缺少依赖 python-docx」
    或一堆 XML 解析异常，用户（和模型）完全看不懂。真正的原因是**格式不支持**，
    必须直说，并给出可执行动作（另存为 .docx）。
    """
    if _is_ole(path):
        raise RuntimeError(
            f"这是旧版二进制 Office 格式（.{kind}），本插件无法直接解析，请另存为 .{kind}x 后重发"
        )
    sniffed, _basis = detect_format(path)
    if sniffed == "docx":
        return _read_docx(path)
    if sniffed == "pptx":
        return _read_pptx(path)
    if sniffed == "xlsx":
        return _read_excel(path)
    raise RuntimeError(
        f"无法解析 .{kind} 文件：内容既不是可解析的 Office 开放格式包，也读不出文本，"
        f"建议另存为 .{kind}x 或 .txt 后重发"
    )


def _read_xls(path: str) -> str:
    """xls：OLE 二进制需 pandas + xlrd；不是 OLE 说明只是扩展名错，按内容解析。"""
    if _is_ole(path):
        if pd is None:
            raise RuntimeError("旧版二进制 .xls 需要 pandas + xlrd，请另存为 .xlsx / .csv 后重发")
        try:
            return _pd_read_excel_all(path)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"旧版二进制 .xls 解析失败（{e}），建议另存为 .xlsx / .csv 后重发") from e
    sniffed, _basis = detect_format(path)
    if sniffed == "xlsx":
        return _read_excel(path)
    raise RuntimeError("无法解析 .xls：不是有效的 Excel 包，建议另存为 .xlsx / .csv 后重发")


def detect_format(path: str) -> tuple[str, str]:
    """内容嗅探：返回 `(格式名, 依据)`，无法识别返回 `("", "")`。

    安全性：只读文件头 8 字节与 ZIP 的**中央目录**，最多额外读 `mimetype`
    （一个几十字节的小文件），绝不解压正文。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return "", ""
    if head.startswith(b"%PDF-"):
        return "pdf", "magic"
    if head.startswith(b"{\\rtf"):
        return "rtf", "magic"
    if head == _OLE_MAGIC:
        return "ole", "magic"
    if head.startswith(b"PK\x03\x04"):
        import zipfile

        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                if "mimetype" in names:
                    mime = zf.read("mimetype").decode("ascii", errors="ignore").strip()
                    mapping = {
                        "application/epub+zip": "epub",
                        "application/vnd.oasis.opendocument.text": "odt",
                        "application/vnd.oasis.opendocument.spreadsheet": "ods",
                        "application/vnd.oasis.opendocument.presentation": "odp",
                    }
                    if mime in mapping:
                        return mapping[mime], "mimetype"
                if "word/document.xml" in names:
                    return "docx", "zip"
                if any(n.startswith("ppt/slides/") for n in names):
                    return "pptx", "zip"
                if any(n.startswith("xl/worksheets/") for n in names):
                    return "xlsx", "zip"
                if "content.xml" in names:
                    return "odt", "zip"
        except (zipfile.BadZipFile, OSError):
            return "", ""
    return "", ""


# 识别但没有（或不值得）解析器的格式：给出**准确原因**而不是乱码
RECOGNIZED_UNSUPPORTED: dict[str, str] = {
    "rtf": "RTF 暂不支持（读出来是控制字乱码），请另存为 .docx 或 .txt 后重发",
}


# ─── 扩展名 → 处理函数（原映射表，函数体改为标准库优先） ────────────
def _pdfium_extract(path: str, max_pages: int, notes: Optional[list] = None) -> str:
    """pypdfium2 提取全文（v1.4.0）。超过 max_pages 时截断并登记提示。"""
    pdf = _pdfium.PdfDocument(path)
    try:
        total = len(pdf)
        limit = total if max_pages <= 0 else min(total, max_pages)
        parts: list[str] = []
        for i in range(limit):
            page = pdf[i]
            try:
                parts.append(page.get_textpage().get_text_range())
            finally:
                page.close()
        if limit < total:
            _note(notes, f"PDF 共 {total} 页，已只读取前 {limit} 页（reader.pdf_max_pages={max_pages}）")
        return "\n".join(parts)
    finally:
        pdf.close()


def _read_pdf(path: str, notes: Optional[list] = None) -> str:
    """PDF 解析：pypdfium2 优先 → pdfminer 兜底 → 双失败报两个引擎的原因。

    v1.4.0：旧版只有 pdfminer（纯 Python、慢、抢 GIL，真机 18MB 教材解析 26min
    期间 main loop 被拖出多次秒级卡顿）。pypdfium2 是 C 扩展且释放 GIL，
    同时把「纯扫描件无文字」这种情形提前识别，不再让 pdfminer 白跑几分钟。
    """
    max_pages = _PDF_MAX_PAGES
    errors: list[str] = []

    if _pdfium is not None:
        try:
            text = _pdfium_extract(path, max_pages, notes)
        except Exception as e:  # noqa: BLE001  引擎异常一律降级，不让单点故障终止解析
            errors.append(f"pypdfium2 {type(e).__name__}: {e}")
            _LOGGER.warning("pypdfium2 解析失败，回退 pdfminer: %s (%s)", e, path)
        else:
            if text.strip():
                return text
            # 空文本 = 纯扫描件/图片型 PDF：直接给准确原因，pdfminer 也提不出来
            raise RuntimeError(
                "PDF 未提取到任何文字，可能是扫描件或图片型 PDF（本插件不含 OCR，"
                "如需识别图片文字请先用工具转成文本再发）"
            )

    if extract_text is None:
        if errors:
            raise RuntimeError("PDF 解析失败（" + "；".join(errors) + "），且未安装 pdfminer.six 兜底")
        raise RuntimeError("缺少 PDF 解析依赖：请安装 pypdfium2（推荐）或 pdfminer.six")

    try:
        text = extract_text(path, maxpages=max_pages if max_pages > 0 else 0)
    except Exception as e:  # noqa: BLE001  双引擎都失败时把两边原因都带上
        errors.append(f"pdfminer {type(e).__name__}: {e}")
        raise RuntimeError("PDF 解析失败（" + "；".join(errors) + "）") from e
    if not text.strip():
        raise RuntimeError(
            "PDF 未提取到任何文字，可能是扫描件或图片型 PDF（本插件不含 OCR，"
            "如需识别图片文字请先用工具转成文本再发）"
        )
    return text


def _read_docx(path: str) -> str:
    """docx = zip + XML，标准库直解；docx2txt 仅作兜底（不再必装）。"""
    return _parse_office(_docx_from_zip, docx2txt.process if docx2txt else None, "docx", path)


def _read_excel(path: str) -> str:
    """xlsx 走标准库；xls/ods 仍需 pandas（建议另存为 xlsx/csv）。"""
    if str(path).lower().endswith(".xlsx"):
        return _parse_office(_xlsx_from_zip, _pd_read_excel_all if pd is not None else None, "xlsx", path)
    if pd is None:
        raise RuntimeError("xls/ods 需要 pandas，请安装：pip install pandas openpyxl（或另存为 .xlsx / .csv）")
    return _pd_read_excel_all(path)


def _read_csv(path: str) -> str:
    """csv 用标准库 csv 模块解析（无需 pandas）。"""
    text = _read_txt(path)
    rows = ["\t".join(row) for row in _csv_mod.reader(io.StringIO(text))]
    return "\n".join(rows)


def _read_pptx(path: str) -> str:
    """pptx = zip + XML，标准库直解；python-pptx 仅作兜底。"""
    return _parse_office(_pptx_from_zip, Presentation if Presentation is not None else None, "pptx", path)


def _read_txt(path: str) -> str:
    """纯文本读取（v1.4.0 提速）：UTF-8 快路径 + chardet 只探前 64KB。

    旧版对**全量字节**跑 `chardet.detect`——50MB 的日志光探测就要几秒，
    而这批文件占实际使用量的绝大多数。现在绝大多数文件（UTF-8）
    连一次探测都不做；只有解码失败才探测，且只探头部 64KB。
    """
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8")  # ① 快路径：命中即返回，零探测
    except UnicodeDecodeError:
        pass
    encoding = "utf-8"
    if chardet is not None:
        det = chardet.detect(raw[:65536])  # ② 只探前 64KB，成本恒定
        if det and det.get("encoding") and det.get("confidence", 0) > 0.5:
            encoding = det["encoding"]
    for enc in (encoding, "utf-8", "gbk", "gb18030", "latin-1"):  # ③ 兜底链不变
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


SUPPORTED_EXTENSIONS: dict[str, Callable[[str], str]] = {
    # 文档
    "pdf": _read_pdf,
    "docx": _read_docx,
    # v1.3.0：doc/ppt 不再"假装支持"——先判 OLE，是就给准确提示，不是就按内容试 OOXML
    "doc": lambda p: _read_legacy_office(p, "doc"),
    # v1.3.0：rtf 移出支持列表（旧版走 _read_txt，读出来全是控制字乱码）
    "odt": lambda p: _odf_from_zip(p, "odt"),
    "epub": _read_epub,
    "eml": _read_eml,
    # 表格
    "xlsx": _read_excel,
    "xls": _read_xls,
    "ods": lambda p: _odf_from_zip(p, "ods"),
    "csv": _read_csv,
    # 演示
    "pptx": _read_pptx,
    "ppt": lambda p: _read_legacy_office(p, "ppt"),
    "odp": lambda p: _odf_from_zip(p, "odp"),
    # 代码
    "py": _read_txt,
    "java": _read_txt,
    "cpp": _read_txt,
    "c": _read_txt,
    "h": _read_txt,
    "hpp": _read_txt,
    "cs": _read_txt,
    "js": _read_txt,
    "ts": _read_txt,
    "php": _read_txt,
    "rb": _read_txt,
    "go": _read_txt,
    "rs": _read_txt,
    "swift": _read_txt,
    "kt": _read_txt,
    "scala": _read_txt,
    "sh": _read_txt,
    "bash": _read_txt,
    "ps1": _read_txt,
    "bat": _read_txt,
    "cmd": _read_txt,
    # 标记
    "md": _read_txt,
    "markdown": _read_txt,
    # v1.3.0：html/htm 去标签（旧版原文入库，脚本样式吃掉大量 token）
    "html": _read_html,
    "htm": _read_html,
    "xml": _read_txt,
    "json": _read_txt,
    # v1.3.0：ipynb 走结构化解析，跳过 outputs（base64 图片会把分块撑爆）
    "ipynb": _read_ipynb,
    "yaml": _read_txt,
    "yml": _read_txt,
    # 配置
    "ini": _read_txt,
    "cfg": _read_txt,
    "conf": _read_txt,
    "properties": _read_txt,
    "env": _read_txt,
    "toml": _read_txt,
    # 其他
    "sql": _read_txt,
    "txt": _read_txt,
    "log": _read_txt,
    "lock": _read_txt,
    "gitignore": _read_txt,
    "url": _read_txt,
    "webloc": _read_txt,
    # v1.3.0 新增：字幕 / 排版 / 参考文献 / 压缩包
    "srt": _read_txt,
    "vtt": _read_txt,
    "ass": _read_txt,
    "tex": _read_txt,
    "rst": _read_txt,
    "bib": _read_txt,
    "gz": _read_gz,
}


def get_extension(file_name: str) -> str:
    """取小写扩展名（无扩展名返回空串）。"""
    stem = Path(file_name).stem if file_name else ""
    if "." in stem:  # 如 .gitignore：Path 会把整个当 stem
        pass
    suffix = Path(file_name).suffix.lstrip(".").lower()
    if not suffix:
        # 无扩展名但以 . 开头的特殊文件（.gitignore / .env）
        if file_name and file_name.startswith(".") and "." not in file_name[1:]:
            return file_name[1:].lower()
    return suffix


def is_supported(file_name: str) -> bool:
    return get_extension(file_name) in SUPPORTED_EXTENSIONS


def read_any_file_to_text(file_path: str, file_name: str = "") -> str:
    """统一入口：按扩展名分发到对应解析器。

    Raises:
        ValueError: 不支持的类型 / 文件不存在
        RuntimeError: 解析失败（含缺依赖提示）
    """
    path = Path(file_path)
    if not path.exists():
        raise ValueError(f"文件不存在: {file_path}")

    name = file_name or path.name
    ext = get_extension(name)
    sniffed, basis = detect_format(str(path))

    # ① 能识别但明确不支持的：给**准确原因**，不要让模型对着乱码猜
    if ext in RECOGNIZED_UNSUPPORTED:
        raise ValueError(f"{RECOGNIZED_UNSUPPORTED[ext]}（{name}）")
    if sniffed == "rtf":
        raise ValueError(f"{RECOGNIZED_UNSUPPORTED['rtf']}（{name}）")
    if sniffed == "ole" and ext not in ("doc", "xls", "ppt"):
        raise ValueError(
            f"这是旧版二进制 Office 格式（doc/xls/ppt），本插件无法直接解析，"
            f"请另存为 docx/xlsx/pptx 后重发（{name}）"
        )

    # ② 扩展名不可信（转发/改名/无扩展名）时以内容嗅探为准
    if not ext:
        if sniffed and sniffed in SUPPORTED_EXTENSIONS:
            ext = sniffed
        else:
            raise ValueError(f"无法识别文件格式（无扩展名且内容嗅探未命中）：{name}")
    elif sniffed and sniffed in SUPPORTED_EXTENSIONS and sniffed != ext:
        _LOGGER.info(
            "扩展名 .%s 与内容嗅探 %s(%s) 冲突，以内容为准: %s", ext, sniffed, basis, name
        )
        ext = sniffed

    handler = SUPPORTED_EXTENSIONS.get(ext)
    if handler is None:
        raise ValueError(f"不支持的文件类型: .{ext or '(无扩展名)'}")

    return handler(str(path))


def describe_supported_types() -> str:
    """供状态命令 / 提示使用。

    v1.3.0 重写：旧版把 doc/rtf/ppt/odt/ods/odp 都列进"支持"，实际多半读不出来，
    /file_status 与 README 两边还不一致。现在只写**真能读**的。
    """
    return (
        "pdf/docx/odt/epub/eml, xlsx/ods/csv, pptx/odp, html/ipynb/gz, "
        "代码与文本(py/java/cpp/js/ts/go/rs/sh/md/json/yaml/txt/srt/xml...), "
        "ini/env/toml/log/sql"
    )
