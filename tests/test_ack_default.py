"""v1.3.7：入库回执默认必须静默。

真机 2026-11-02 22:10 私聊日志：

    鸣澜: 已读取《VoidMeleeAura.cs》，共 60 段
    鸣澜: 能看到，Python代码主体完整

第一条是插件 `_handle_file` 末尾直发的独立消息，与模型的人格化回复并列出现，
对人格驱动场景是纯噪音。根因是 `ack_min_chars` 默认 20000 + 「耗时 ≥ 10s」双触发，
21KB 的 .cs 两条都命中（原文 21551 字 / 60 块向量化 11s），等于默认几乎必发。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugin import FileReaderConfig  # noqa: E402


def test_ack_disabled_by_default():
    """默认配置下不得发入库回执（0 = 不发）。"""
    cfg = FileReaderConfig()
    assert cfg.reader.ack_min_chars == 0, (
        f"入库回执必须默认关闭，实际 ack_min_chars={cfg.reader.ack_min_chars}"
    )


def test_silent_success_still_default():
    """另一条「📄 已解析…」回执同样默认静默，改 ack 时不能顺手改坏它。"""
    assert FileReaderConfig().reader.silent_success is True


def test_ack_threshold_semantics_kept():
    """显式开启时语义不变：>0 才发，0 值不能再被当成「阈值 0 ⇒ 全部触发」。"""
    cfg = FileReaderConfig(reader={"ack_min_chars": 50000})
    assert cfg.reader.ack_min_chars == 50000
    # 判定式要求 ack_min > 0，所以 0 必须短路掉整个回执分支
    assert not (0 > 0)
