# -*- coding: utf-8 -*-
"""格式解析测试（v1.3.0 批次 4）。

为什么要在 tmp 目录现场造夹具
----------------------------
仓库里不能塞二进制样本，而"声明支持却读不出来"恰恰是这次要修的问题
（odt 读成 zip 乱码、odp 走 pptx 解析器必然失败、rtf 读成控制字）。
用 zipfile 造最小合法文件，是唯一能稳定断言**真能读**的办法。
"""

import gzip
import json
import zipfile

import pytest

from file_parser import (
    RECOGNIZED_UNSUPPORTED,
    SUPPORTED_EXTENSIONS,
    detect_format,
    read_any_file_to_text,
)

_NS_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
_NS_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_NS_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_NS_DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"


def _zip(tmp_path, name, entries):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        for entry, data in entries.items():
            zf.writestr(entry, data)
    return str(p)


def _docx(tmp_path, name="a.docx"):
    doc = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>第一章 背景</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>这是正文内容。</w:t></w:r></w:p></w:body></w:document>"
    )
    return _zip(tmp_path, name, {"word/document.xml": doc})


def _xlsx(tmp_path, name="a.xlsx"):
    sheet = (
        '<?xml version="1.0"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row><c r="A1" t="inlineStr"><is><t>姓名</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>分数</t></is></c></row></sheetData></worksheet>'
    )
    return _zip(tmp_path, name, {"xl/worksheets/sheet1.xml": sheet})


def _pptx(tmp_path, name="a.pptx"):
    slide = (
        '<?xml version="1.0"?>'
        # p: 前缀必须声明——未声明前缀会让解析器走"去前缀"回退，命名空间就丢了
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<a:p><a:r><a:t>幻灯片正文</a:t></a:r></a:p></p:sld>"
    )
    return _zip(tmp_path, name, {"ppt/slides/slide1.xml": slide})


def _odt(tmp_path, name="a.odt", body=""):
    content = (
        '<?xml version="1.0"?>'
        f'<office:document-content xmlns:office="{_NS_OFFICE}" '
        f'xmlns:text="{_NS_TEXT}" xmlns:table="{_NS_TABLE}" xmlns:draw="{_NS_DRAW}">'
        f"<office:body>{body}</office:body></office:document-content>"
    )
    return _zip(
        tmp_path,
        name,
        {"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": content},
    )


def _epub(tmp_path, name="a.epub"):
    container = (
        '<?xml version="1.0"?>'
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
        '<manifest><item id="c1" href="chap1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    )
    chap = (
        "<html><body><h1>第一章</h1><p>epub 正文内容</p>"
        "<script>var noise = 1;</script><style>.x{color:red}</style></body></html>"
    )
    return _zip(
        tmp_path,
        name,
        {
            "mimetype": "application/epub+zip",
            "META-INF/container.xml": container,
            "OEBPS/content.opf": opf,
            "OEBPS/chap1.xhtml": chap,
        },
    )


# ── OOXML（回归：这些本来就该能读） ──────────────────────────────


def test_docx(tmp_path):
    text = read_any_file_to_text(_docx(tmp_path), "a.docx")
    assert "第一章 背景" in text and "这是正文内容。" in text


def test_xlsx(tmp_path):
    text = read_any_file_to_text(_xlsx(tmp_path), "a.xlsx")
    assert "姓名" in text and "分数" in text


def test_pptx(tmp_path):
    text = read_any_file_to_text(_pptx(tmp_path), "a.pptx")
    assert "幻灯片正文" in text


# ── ODF（v1.3.0 真解析：旧版是假支持） ──────────────────────────


def test_odt(tmp_path):
    body = "<text:h>标题一</text:h><text:p>第一段正文</text:p><text:p>第二段正文</text:p>"
    text = read_any_file_to_text(_odt(tmp_path, body=body), "a.odt")
    assert "标题一" in text
    assert "第一段正文" in text and "第二段正文" in text
    assert "PK" not in text  # 不该有 zip 二进制噪声


def test_ods(tmp_path):
    body = (
        '<table:table table:name="成绩"><table:table-row>'
        '<table:table-cell><text:p>姓名</text:p></table:table-cell>'
        '<table:table-cell office:value="95"><text:p>95</text:p></table:table-cell>'
        "</table:table-row>"
        '<table:table-row><table:table-cell><text:p>李四</text:p></table:table-cell>'
        '<table:table-cell office:value="88"/></table:table-row>'
        "</table:table>"
    )
    content = (
        '<?xml version="1.0"?>'
        f'<office:document-content xmlns:office="{_NS_OFFICE}" '
        f'xmlns:text="{_NS_TEXT}" xmlns:table="{_NS_TABLE}">'
        f"<office:body>{body}</office:body></office:document-content>"
    )
    p = _zip(
        tmp_path,
        "a.ods",
        {"mimetype": "application/vnd.oasis.opendocument.spreadsheet", "content.xml": content},
    )
    text = read_any_file_to_text(p, "a.ods")
    assert "成绩" in text and "李四" in text and "95" in text


def test_odp(tmp_path):
    body = '<draw:page><text:p>第一页内容</text:p></draw:page><draw:page><text:p>第二页内容</text:p></draw:page>'
    content = (
        '<?xml version="1.0"?>'
        f'<office:document-content xmlns:office="{_NS_OFFICE}" '
        f'xmlns:text="{_NS_TEXT}" xmlns:draw="{_NS_DRAW}">'
        f"<office:body>{body}</office:body></office:document-content>"
    )
    p = _zip(
        tmp_path,
        "a.odp",
        {"mimetype": "application/vnd.oasis.opendocument.presentation", "content.xml": content},
    )
    text = read_any_file_to_text(p, "a.odp")
    assert "幻灯片 1" in text and "第一页内容" in text and "第二页内容" in text


# ── 新增格式 ────────────────────────────────────────────────────


def test_epub(tmp_path):
    text = read_any_file_to_text(_epub(tmp_path), "a.epub")
    assert "epub 正文内容" in text
    assert "noise" not in text and "color:red" not in text  # script/style 被跳过


def test_html_strips_script_and_style(tmp_path):
    p = tmp_path / "a.html"
    p.write_text(
        "<html><head><style>.x{color:red}</style></head><body>"
        "<h1>标题</h1><p>可见正文</p><script>var secret=1;</script></body></html>",
        encoding="utf-8",
    )
    text = read_any_file_to_text(str(p), "a.html")
    assert "可见正文" in text and "标题" in text
    assert "secret" not in text and "color:red" not in text


def test_ipynb_skips_outputs(tmp_path):
    p = tmp_path / "a.ipynb"
    p.write_text(
        json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "source": ["# 说明"]},
                    {
                        "cell_type": "code",
                        "source": ["print('hello')"],
                        "outputs": [{"data": {"image/png": "iVBORw0KGgoAAAANSUhEUg=="}}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    text = read_any_file_to_text(str(p), "a.ipynb")
    assert "print('hello')" in text and "[code]" in text and "[md]" in text
    assert "iVBORw0KGgo" not in text  # base64 输出不得入库


def test_eml(tmp_path):
    p = tmp_path / "a.eml"
    p.write_text(
        "Subject: 项目进度\nContent-Type: text/plain; charset=utf-8\n\n正文：进度正常。\n",
        encoding="utf-8",
    )
    text = read_any_file_to_text(str(p), "a.eml")
    assert "项目进度" in text and "进度正常" in text


def test_gz(tmp_path):
    inner = tmp_path / "inner.txt"
    inner.write_text("压缩包里的正文内容", encoding="utf-8")
    gz_path = tmp_path / "a.txt.gz"
    with gzip.open(gz_path, "wb") as f:
        f.write(inner.read_bytes())
    text = read_any_file_to_text(str(gz_path), "a.txt.gz")
    assert "压缩包里的正文内容" in text


def test_srt_is_plain_text(tmp_path):
    p = tmp_path / "a.srt"
    p.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕内容\n", encoding="utf-8")
    assert "字幕内容" in read_any_file_to_text(str(p), "a.srt")


# ── 老二进制 Office / RTF：给准确原因 ───────────────────────────


def test_ole_doc_gives_actionable_hint(tmp_path):
    p = tmp_path / "a.doc"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(RuntimeError) as exc:
        read_any_file_to_text(str(p), "a.doc")
    assert "另存为" in str(exc.value)
    assert ".doc" in str(exc.value) or "旧版" in str(exc.value)


def test_ole_content_with_wrong_extension(tmp_path):
    """扩展名写着 .txt，内容其实是 OLE：也要给出转存提示而不是乱码。"""
    p = tmp_path / "a.txt"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(ValueError) as exc:
        read_any_file_to_text(str(p), "a.txt")
    assert "旧版二进制" in str(exc.value)


def test_rtf_is_recognized_but_unsupported(tmp_path):
    p = tmp_path / "a.rtf"
    p.write_text(r"{\rtf1\ansi\ansicpg936 内容}", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        read_any_file_to_text(str(p), "a.rtf")
    assert "RTF" in str(exc.value) and "另存为" in str(exc.value)
    assert "rtf" in RECOGNIZED_UNSUPPORTED


def test_rtf_not_in_supported_extensions():
    """rtf 必须从支持列表移除——留着等于承诺能读，实际读出来是控制字乱码。"""
    assert "rtf" not in SUPPORTED_EXTENSIONS


def test_docx_with_wrong_extension_is_sniffed(tmp_path):
    """docx 被改名成 .bin：扩展名不可信，靠内容嗅探仍然读得出来。"""
    p = _docx(tmp_path, name="a.bin")
    text = read_any_file_to_text(p, "a.bin")
    assert "第一章 背景" in text


# ── 内容嗅探 ────────────────────────────────────────────────────


def test_detect_format_by_magic(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.7\n..." )
    assert detect_format(str(p))[0] == "pdf"


def test_detect_format_ole(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    assert detect_format(str(p)) == ("ole", "magic")


def test_detect_format_epub_by_mimetype(tmp_path):
    assert detect_format(_epub(tmp_path)) == ("epub", "mimetype")


def test_detect_format_odf_by_mimetype(tmp_path):
    assert detect_format(_odt(tmp_path)) == ("odt", "mimetype")


def test_detect_format_docx_by_zip(tmp_path):
    assert detect_format(_docx(tmp_path))[0] == "docx"


def test_detect_format_unknown(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("就是一段普通文本", encoding="utf-8")
    assert detect_format(str(p)) == ("", "")


def test_unknown_extension_and_unknown_content_raises(tmp_path):
    """既没有扩展名、内容也嗅探不出来 → 明确说「无法识别」。"""
    p = tmp_path / "afile"
    p.write_bytes(b"\x01\x02\x03\x04\x05\x06\x07\x08")
    with pytest.raises(ValueError) as exc:
        read_any_file_to_text(str(p), "afile")
    assert "无法识别" in str(exc.value)


def test_known_but_unsupported_extension_says_unsupported(tmp_path):
    """有扩展名但不被支持 → 说「不支持的文件类型」，别把两件事混成一句。"""
    p = tmp_path / "a.bin"
    p.write_bytes(b"\x01\x02\x03\x04\x05\x06\x07\x08")
    with pytest.raises(ValueError) as exc:
        read_any_file_to_text(str(p), "a.bin")
    assert "不支持的文件类型" in str(exc.value)


def test_supported_list_does_not_claim_false_formats():
    """声明支持的类型必须真的有解析器——这是"格式可信"的底线。"""
    for ext in ("pdf", "docx", "odt", "ods", "odp", "epub", "html", "ipynb", "eml", "gz", "csv"):
        assert ext in SUPPORTED_EXTENSIONS, f"{ext} 声称支持却没有解析器"
