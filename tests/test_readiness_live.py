from __future__ import annotations

import struct

import pytest

from scripts.readiness_live import (
    DEFAULT_URL,
    PNG_SIGNATURE,
    latency_summary,
    png_dimensions,
    validate_disposable_target,
)


def test_target_must_be_disposable_loopback_port():
    assert validate_disposable_target(DEFAULT_URL) == DEFAULT_URL
    for target in (
        "https://127.0.0.1:18080",
        "http://example.com:18080",
        "http://127.0.0.1:8090",
        "http://127.0.0.1:18080/unexpected",
        "http://user:pass@127.0.0.1:18080",
    ):
        with pytest.raises(ValueError):
            validate_disposable_target(target)


def test_internal_target_requires_explicit_disposable_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("READINESS_DISPOSABLE", "1")
    assert validate_disposable_target("http://127.0.0.1:8080", allow_internal=True) == "http://127.0.0.1:8080"
    monkeypatch.delenv("READINESS_DISPOSABLE")
    with pytest.raises(ValueError):
        validate_disposable_target("http://127.0.0.1:8080", allow_internal=True)


def test_png_dimensions_reads_only_header():
    png = PNG_SIGNATURE + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 1440, 900) + b"\x08\x02\x00\x00\x00"
    assert png_dimensions(png) == (1440, 900)
    with pytest.raises(ValueError):
        png_dimensions(b"not png")


def test_latency_summary_is_bounded_and_uses_milliseconds():
    summary = latency_summary([0.001, 0.003, 0.002])
    assert summary == {
        "count": 3,
        "min_ms": 1.0,
        "mean_ms": 2.0,
        "p50_ms": 2.0,
        "p95_ms": 3.0,
        "max_ms": 3.0,
    }
    assert latency_summary([]) == {"count": 0}
