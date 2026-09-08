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
import os
import re
from pathlib import Path
from typing import Callable, Optional

# ─── 可选依赖（try-import，缺失则降级） ──────────────────────────
try:
    import chardet  # type: ignore
except ImportError:  # pragma: no cover
    chardet = None

try:
    from pdfminer.high_level import extract_text  # type: ignore
except ImportError:  # pragma: no cover
    extract_text = None

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

try:
    from docx import Document  # type: ignore
except ImportError:  # pragma: no cover
    Document = None


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


def _pdf_from_stdlib(path: str) -> str:
    """标准库 PDF 兜底：只支持未压缩文本流的简单 PDF，尽力而为。"""
    raw = _read_txt(path)
    # 提取文本流里括号字面量（简单启发式，对压缩 PDF 无效）
    chunks = re.findall(r"\((?:[^()\\]|\\.)*\)", raw)
    texts = []
    for c in chunks:
        s = c[1:-1]
        s = re.sub(r"\\([()\\])", r"\1", s)
        # 只保留含可读字符的片段
        cleaned = "".join(ch if 32 <= ord(ch) < 127 or "\u4e00" <= ch <= "\u9fff" else " " for ch in s).strip()
        if len(cleaned) >= 4 and any(ch.isalnum() for ch in cleaned):
            texts.append(cleaned)
    return "\n".join(texts)


def _pd_read_excel_all(path: str) -> str:
    """pandas 兜底：读全部 sheet。"""
    sheets = pd.read_excel(path, sheet_name=None)
    parts = []
    for name, df in sheets.items():
        parts.append(f"=== {name} ===\n{df.to_string(index=False)}")
    return "\n\n".join(parts)


# ─── 扩展名 → 处理函数（原映射表，函数体改为标准库优先） ────────────
def _read_pdf(path: str) -> str:
    if extract_text is None:
        raise RuntimeError("缺少依赖 pdfminer.six，请安装：pip install pdfminer.six")
    return extract_text(path)


def _read_docx(path: str) -> str:
    """docx = zip + XML，标准库直解；docx2txt 仅作兜底（不再必装）。"""
    return _parse_office(_docx_from_zip, docx2txt.process if docx2txt else None, "docx", path)


def _read_doc(path: str) -> str:
    # .doc 老格式：尝试用 python-docx 转换（实际 python-docx 不支持 .doc，做降级提示）
    if Document is None:
        raise RuntimeError("缺少依赖 python-docx，请安装：pip install python-docx")
    try:
        doc = Document(path)
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception as e:  # .doc 二进制老格式，python-docx 无法解析
        raise RuntimeError(f".doc 老格式暂不支持直接解析（{e}），建议转为 .docx 后重传") from e


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
    with open(path, "rb") as f:
        raw = f.read()
    encoding = "utf-8"
    if chardet is not None:
        det = chardet.detect(raw)
        if det and det.get("encoding") and det.get("confidence", 0) > 0.5:
            encoding = det["encoding"]
    for enc in (encoding, "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


SUPPORTED_EXTENSIONS: dict[str, Callable[[str], str]] = {
    # 文档
    "pdf": _read_pdf,
    "docx": _read_docx,
    "doc": _read_doc,
    "rtf": _read_txt,
    "odt": _read_txt,
    # 表格
    "xlsx": _read_excel,
    "xls": _read_excel,
    "ods": _read_excel,
    "csv": _read_csv,
    # 演示
    "pptx": _read_pptx,
    "ppt": _read_pptx,
    "odp": _read_pptx,
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
    "html": _read_txt,
    "htm": _read_txt,
    "xml": _read_txt,
    "json": _read_txt,
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

    ext = get_extension(file_name or path.name)
    handler = SUPPORTED_EXTENSIONS.get(ext)
    if handler is None:
        raise ValueError(f"不支持的文件类型: .{ext or '(无扩展名)'}")

    return handler(str(path))


def describe_supported_types() -> str:
    """供状态命令 / 提示使用。"""
    return "pdf/docx/doc/rtf/odt, xlsx/xls/ods/csv, pptx/ppt/odp, " \
           "代码(py/java/cpp/js/ts/go/rs/sh/bat...), md/html/xml/json/yaml, " \
           "ini/env/toml, txt/log/sql 等"
