"""Offline tests for the standalone XTQuant Tick capture command."""

from __future__ import annotations

import argparse
import json
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from data.tick_archive import verify_tick_archive
from scripts import capture_ticks


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        output_dir=str(tmp_path),
        trade_date="20260710",
        host="127.0.0.1",
        port=58610,
        # Stop immediately after subscribe; the fake SDK invokes its callback
        # synchronously so the test remains deterministic and fast.
        stop_time=datetime.now(capture_ticks.CHINA_TZ).time(),
        sector="沪深A股",
        include_star=False,
        queue_size=8,
        flush_every=1,
        verify=True,
    )


def _install_xtquant(monkeypatch, xtdata) -> None:
    package = types.ModuleType("xtquant")
    package.xtdata = xtdata
    monkeypatch.setitem(sys.modules, "xtquant", package)


def _tick(timestamp_ms: int, price: float = 10.0) -> dict:
    return {
        "time": timestamp_ms,
        "lastPrice": price,
        "askPrice": [price + 0.01],
        "askVol": [100],
        "bidPrice": [price],
        "bidVol": [120],
    }


class _FakeXtdata:
    def __init__(self, batches: list[dict]):
        self.batches = batches
        self.subscribed_codes: list[str] = []
        self.unsubscribed: list[int] = []

    def reconnect(self, host: str, port: int) -> None:
        self.connection = (host, port)

    def download_sector_data(self) -> None:
        return None

    def get_stock_list_in_sector(self, sector: str) -> list[str]:
        return ["000001.SZ", "688001.SH", "430001.BJ"]

    def subscribe_whole_quote(self, codes, callback) -> int:
        self.subscribed_codes = list(codes)
        for batch in self.batches:
            callback(batch)
        return 7

    def unsubscribe_quote(self, subscribe_id: int) -> None:
        self.unsubscribed.append(subscribe_id)


def test_capture_writes_and_verifies_a_replayable_segment(
    monkeypatch, tmp_path: Path
) -> None:
    xtdata = _FakeXtdata(
        [
            {"000001.SZ": _tick(1_720_000_000_001)},
            {"000001.SZ": _tick(1_720_000_000_002, 10.01)},
        ]
    )
    _install_xtquant(monkeypatch, xtdata)

    archive = capture_ticks.capture(_args(tmp_path))

    assert xtdata.subscribed_codes == ["000001.SZ"]
    assert xtdata.unsubscribed == [7]
    quality = verify_tick_archive(archive)
    assert quality["valid"] is True
    assert quality["record_count"] == 2


def test_capture_rejects_an_empty_segment(monkeypatch, tmp_path: Path) -> None:
    _install_xtquant(monkeypatch, _FakeXtdata([]))

    with pytest.raises(RuntimeError, match="Tick 归档为空"):
        capture_ticks.capture(_args(tmp_path))

    archive = next((tmp_path / "20260710").glob("*.jsonl.gz"))
    assert verify_tick_archive(archive)["valid"] is False


def test_capture_surfaces_background_writer_failure(
    monkeypatch, tmp_path: Path
) -> None:
    class FailingWriter(capture_ticks.TickArchiveWriter):
        def write_batch(self, datas, *, received_at_ns=None):
            raise OSError("disk full")

    _install_xtquant(
        monkeypatch,
        _FakeXtdata([{"000001.SZ": _tick(1_720_000_000_001)}]),
    )
    monkeypatch.setattr(capture_ticks, "TickArchiveWriter", FailingWriter)

    with pytest.raises(RuntimeError, match="Tick 归档写入失败") as error:
        capture_ticks.capture(_args(tmp_path))

    assert isinstance(error.value.__cause__, OSError)
    archive = next((tmp_path / "20260710").glob("*.jsonl.gz"))
    manifest = archive.with_name(archive.name[:-9] + "manifest.json")
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    assert metadata["record_count"] == 0
    assert metadata["dropped_batches"] == 1
    assert verify_tick_archive(archive)["valid"] is False
