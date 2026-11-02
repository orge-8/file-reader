# -*- coding: utf-8 -*-
"""NapCat 多文件段匹配（v1.3.0 批次 1：修静默串档）。

背景
----
防抖合并后，一次上传的 N 个文件共用**同一个 message_id**，消息体里却是 N 条
`[文件] xxx，大小: N` 提示。旧实现 `_pick_onebot_file_seg` 永远返回第一个 file 段，
于是 14 个文件全被读成第一个文件的内容，再按各自文件名分别入库——**静默串档**，
而且污染检索（库里存着 14 份「同一份内容 + 14 个不同文件名」）。

这类故障在真机上没有任何报错，只能靠「按名+大小选中正确段」的断言守住。
"""

from plugin import FileReaderPlugin


def _msg(*segs):
    return {"message": list(segs)}


def _file_seg(name, size=0, file_id="id"):
    """完整 OneBot 段（_pick_onebot_file_segs 的输入形态）。"""
    seg = {"type": "file", "data": {"file_id": file_id, "file_name": name}}
    if size:
        seg["data"]["file_size"] = size
    return seg


def _file_data(name, size=0, file_id="id"):
    """段内的 data（_pick_onebot_file_segs 的**输出**形态，也是 _match_file_seg 的输入）。"""
    data = {"file_id": file_id, "file_name": name}
    if size:
        data["file_size"] = size
    return data


# ── 段提取 ──────────────────────────────────────────────────────


def test_pick_returns_all_file_segs():
    msg = _msg(
        _file_seg("a.docx", 100, "id1"),
        {"type": "text", "data": {"text": "hello"}},
        _file_seg("b.docx", 200, "id2"),
    )
    segs = FileReaderPlugin._pick_onebot_file_segs(msg)
    assert len(segs) == 2
    assert [s["file_id"] for s in segs] == ["id1", "id2"]


def test_pick_handles_nested_data_message():
    """OneBot 有些实现把 message 数组放在 data.message 里。"""
    msg = {"data": {"message": [_file_seg("a.txt", 10, "id1")]}}
    segs = FileReaderPlugin._pick_onebot_file_segs(msg)
    assert len(segs) == 1


def test_pick_returns_empty_when_no_file_seg():
    assert FileReaderPlugin._pick_onebot_file_segs({"message": [{"type": "text"}]}) == []
    assert FileReaderPlugin._pick_onebot_file_segs({}) == []


# ── 按名 + 大小匹配 ─────────────────────────────────────────────


def test_match_second_prompt_gets_second_file():
    """核心场景：3 个段，第 2 条提示必须取到第 2 个 file_id（旧版恒取第 1 个）。"""
    segs = [
        _file_data("报告.docx", 1000, "id1"),
        _file_data("预算.xlsx", 2000, "id2"),
        _file_data("说明.pdf", 3000, "id3"),
    ]
    seg, mode = FileReaderPlugin._match_file_seg(segs, "预算.xlsx", 2000)
    assert seg is not None and seg["file_id"] == "id2"
    assert mode == "name"


def test_match_disambiguates_same_name_by_size():
    """同名不同大小（群里的「新建文档(1).docx」）按 size_hint 消歧。"""
    segs = [
        _file_data("新建文档.docx", 111, "id1"),
        _file_data("新建文档.docx", 222, "id2"),
    ]
    seg, mode = FileReaderPlugin._match_file_seg(segs, "新建文档.docx", 222)
    assert seg["file_id"] == "id2"
    assert mode == "name"


def test_match_falls_back_to_first_when_name_unknown():
    """名字对不上时回退第一个（保留旧行为），但 mode 必须标记为 fallback 以便观测。"""
    segs = [
        _file_data("a.docx", 100, "id1"),
        _file_data("b.docx", 200, "id2"),
    ]
    seg, mode = FileReaderPlugin._match_file_seg(segs, "完全对不上.pdf", 0)
    assert seg["file_id"] == "id1"
    assert mode == "fallback"


def test_match_ambiguous_when_multiple_hits():
    segs = [
        _file_data("同名.docx", 300, "id1"),
        _file_data("同名.docx", 300, "id2"),
    ]
    seg, mode = FileReaderPlugin._match_file_seg(segs, "同名.docx", 300)
    assert mode == "ambiguous"
    assert seg["file_id"] == "id1"


def test_single_seg_behaviour_unchanged():
    segs = [_file_data("only.docx", 10, "id1")]
    seg, mode = FileReaderPlugin._match_file_seg(segs, "only.docx", 10)
    assert seg["file_id"] == "id1" and mode == "name"


def test_no_segs_returns_none():
    seg, mode = FileReaderPlugin._match_file_seg([], "x.docx", 1)
    assert seg is None and mode == "none"


def test_match_ignores_directory_prefix():
    """段里给的是带路径的名字时，只比较文件名部分。"""
    segs = [_file_data("/data/cache/报告.docx", 100, "id1")]
    seg, mode = FileReaderPlugin._match_file_seg(segs, "报告.docx", 100)
    assert mode == "name" and seg["file_id"] == "id1"


def test_match_uses_alternative_name_fields():
    """不同适配器字段命名不一：file_name / name / file / title 都要能认。"""
    for key in ("name", "file", "title"):
        segs = [{key: "x.txt", "file_id": "id1"}]
        seg, mode = FileReaderPlugin._match_file_seg(segs, "x.txt", 0)
        assert mode == "name", f"字段 {key} 未被识别"


def test_match_by_unique_size_when_name_is_placeholder():
    """v1.3.1：SnowLuma 把无 name 的 file 段兜底成「附件」，name 永远匹配不上；
    size_hint 唯一命中一个段时应直接采用并标 size，而不是 fallback（噪音 + 串档风险）。

    真机 2026-11-02 21:26 私聊：`[文件] 附件，大小: 15397，file_ref: …`，
    段内真实文件名在 file 字段（飞天显卡呜呜转神教_白话版.docx）。
    """
    segs = [
        _file_data("飞天显卡呜呜转神教_白话版.docx", 15397, "id1"),
        _file_data("另一个文件.pdf", 888, "id2"),
    ]
    seg, mode = FileReaderPlugin._match_file_seg(segs, "附件", 15397)
    assert seg is not None and seg["file_id"] == "id1"
    assert mode == "size", mode


def test_match_fallback_still_when_size_not_unique():
    """size 也消歧不了时保持旧 fallback 语义（不改变既有行为）。"""
    segs = [
        _file_data("a.docx", 100, "id1"),
        _file_data("b.docx", 100, "id2"),
    ]
    seg, mode = FileReaderPlugin._match_file_seg(segs, "附件", 100)
    assert seg["file_id"] == "id1"
    assert mode == "fallback", mode


def test_match_zero_size_hint_keeps_fallback():
    """size_hint=0（无从消歧）时不得因新规则改变结果。"""
    segs = [_file_data("a.docx", 100, "id1"), _file_data("b.docx", 200, "id2")]
    seg, mode = FileReaderPlugin._match_file_seg(segs, "附件", 0)
    assert seg["file_id"] == "id1"
    assert mode == "fallback", mode


def test_seg_to_file_returns_file_id_for_structured_segment():
    """v1.3.2：SnowLuma v1.1.10+ 的结构化段只有 file_id/name/size/busid。

    旧版返回 None ⇒ 整段丢弃 + `文件段无法提取内容` warning ⇒ 只能退到文本 hints +
    get_msg 回溯。新版把 file_id 交出去，`_handle_file` 用 get_file 一步取路径。
    """
    fdata = FileReaderPlugin._seg_to_file(
        None,  # 该方法不使用 self
        {"file_id": "abc123", "name": "附件", "size": 15397, "busid": 102},
    )
    assert fdata is not None, "结构化段不应再被丢弃"
    assert fdata["file_id"] == "abc123"
    assert fdata["name"] == "附件"
    assert fdata["busid"] == 102
    assert fdata["size_hint"] == 15397


def test_seg_to_file_prefers_actual_content_over_file_id():
    """有真实内容（base64/path/url）时仍以内容为准，不能被 file_id 分支抢走。"""
    import base64 as _b64

    payload = _b64.b64encode("正文".encode("utf-8")).decode()
    fdata = FileReaderPlugin._seg_to_file(None, {"file_id": "abc", "base64": payload, "name": "x.txt"})
    assert fdata is not None and fdata.get("bytes") == "正文".encode("utf-8"), fdata


def test_seg_to_file_without_any_locator_returns_none():
    """既无内容也无 file_id 时维持原语义（返回 None）。"""
    assert FileReaderPlugin._seg_to_file(None, {"name": "空", "size": 1}) is None
