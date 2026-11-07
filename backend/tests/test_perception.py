"""感知层单测(W2-C 新增,≥6 条):纯函数,无 DB、无网络。"""

from __future__ import annotations

import pytest

from app.perception import (
    MAX_TEXT_LENGTH,
    PerceptionError,
    Percept,
    excerpt,
    normalize,
    normalize_attachments,
)


def test_normalize_strips_zero_width_and_outer_space():
    """零宽字符与首尾空白被清掉(复制粘贴常见脏字符)。"""
    percept = normalize("  \u200b退货\u200d政策\ufeff  ")
    assert percept.text == "退货政策"
    assert percept.truncated is False
    assert percept.original_length is None


def test_normalize_rejects_empty_after_cleaning():
    with pytest.raises(PerceptionError) as excinfo:
        normalize(" \u200b\ufeff ")
    assert excinfo.value.field == "text"


def test_normalize_rejects_non_string_text():
    with pytest.raises(PerceptionError):
        normalize(123)  # type: ignore[arg-type]


def test_normalize_truncates_long_text_and_marks_it():
    raw = "退" * (MAX_TEXT_LENGTH + 1)
    percept = normalize(raw)
    assert len(percept.text) == MAX_TEXT_LENGTH
    assert percept.truncated is True
    assert percept.original_length == MAX_TEXT_LENGTH + 1


def test_attachments_accept_http_urls_and_dedupe():
    percept = normalize(
        "发一下破损照片",
        attachments=["https://cdn.example.com/a.jpg", "https://cdn.example.com/a.jpg"],
        channel="app",
    )
    assert percept.attachments == ["https://cdn.example.com/a.jpg"]
    assert percept.channel == "app"
    assert percept.locale == "zh-CN"


def test_attachments_reject_base64_and_local_path():
    with pytest.raises(PerceptionError) as base64_err:
        normalize_attachments(["data:image/png;base64,AAAA"])
    assert base64_err.value.field == "attachments"
    with pytest.raises(PerceptionError):
        normalize_attachments(["C:/Users/me/a.png"])
    with pytest.raises(PerceptionError):
        normalize_attachments(["ftp://example.com/a.png"])


def test_percept_is_frozen_and_forbids_extra_fields():
    percept = normalize("你好")
    with pytest.raises(Exception):
        percept.text = "改不了"  # type: ignore[misc]
    with pytest.raises(Exception):
        Percept(text="你好", unknown="x")  # type: ignore[call-arg]


def test_excerpt_is_short_preview():
    percept = normalize("很长的消息" * 20)
    assert excerpt(percept, limit=10).endswith("…")
    assert len(excerpt(percept, limit=10)) == 11
