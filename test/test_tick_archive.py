import gzip
import hashlib
import json
from pathlib import Path

import pytest

from data.tick_archive import (
    TickArchiveWriter,
    iter_tick_batches,
    iter_tick_records,
    verify_tick_archive,
)


def _tick(timestamp_ms: int, price: float) -> dict:
    return {
        "time": timestamp_ms,
        "lastPrice": price,
        "askPrice": [price + 0.01],
        "askVol": [10],
        "bidPrice": [price - 0.01],
        "bidVol": [12],
    }


def test_writer_creates_immutable_segments_and_replays_sessions(tmp_path: Path):
    with TickArchiveWriter(tmp_path, "20260710", session_id="first") as writer:
        writer.write_batch({"000001.SZ": _tick(1_720_000_000_001, 10.0)})
        first_path = writer.path

    with TickArchiveWriter(tmp_path, "20260710", session_id="second") as writer:
        writer.write_batch({"000002.SZ": _tick(1_720_000_000_002, 20.0)})
        second_path = writer.path

    batches = list(iter_tick_batches(tmp_path / "20260710"))

    assert [batch.batch_id for batch in batches] == [1, 1]
    assert [batch.session_id for batch in batches] == ["first", "second"]
    assert list(batches[0].ticks) == ["000001.SZ"]
    assert list(batches[1].ticks) == ["000002.SZ"]
    assert verify_tick_archive(first_path)["valid"] is True
    assert verify_tick_archive(second_path)["valid"] is True


def test_batch_exposes_callback_receipt_clock(tmp_path: Path):
    writer = TickArchiveWriter(tmp_path, "20260710", session_id="receipt")
    writer.write_batch(
        {"000001.SZ": _tick(1_720_000_000_001, 10.0)},
        received_at_ns=1_720_000_000_123_000_000,
    )
    archive = writer.close()

    batch = next(iter_tick_batches(archive))

    assert batch.event_time_ms == 1_720_000_000_001
    assert batch.received_time_ms == 1_720_000_000_123


def test_writer_refuses_to_append_an_existing_segment(tmp_path: Path):
    filename = "fixed.jsonl.gz"
    with TickArchiveWriter(
        tmp_path, "20260710", filename=filename, session_id="first"
    ) as writer:
        writer.write_batch({"000001.SZ": _tick(1_720_000_000_001, 10.0)})

    try:
        TickArchiveWriter(
            tmp_path, "20260710", filename=filename, session_id="second"
        )
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing immutable segment should not be reopened")


def test_writer_rejects_invalid_payloads_without_consuming_a_batch_id(
    tmp_path: Path,
):
    writer = TickArchiveWriter(tmp_path, "20260710", session_id="skip-invalid")
    with pytest.raises(TypeError, match="invalid Tick payload"):
        writer.write_batch({"INVALID.SZ": None})
    with pytest.raises(TypeError, match="invalid Tick payload"):
        writer.write_batch({
            "INVALID.SZ": None,
            "000001.SZ": _tick(1_720_000_000_001, 10.0),
        })
    assert writer.write_batch({
        "000001.SZ": _tick(1_720_000_000_001, 10.0),
    }) == 1
    archive = writer.close()

    records = list(iter_tick_records(archive))

    assert [(record["batch_id"], record["sequence"]) for record in records] == [
        (1, 0)
    ]
    assert verify_tick_archive(archive)["valid"] is True


def test_writer_rejects_a_bad_event_time_before_writing_any_of_the_batch(
    tmp_path: Path,
):
    writer = TickArchiveWriter(tmp_path, "20260710", session_id="atomic-batch")
    with pytest.raises(ValueError):
        writer.write_batch({
            "000001.SZ": _tick(1_720_000_000_001, 10.0),
            "INVALID.SZ": {"time": "not-a-timestamp"},
        })
    writer.write_batch({"000002.SZ": _tick(1_720_000_000_002, 20.0)})
    archive = writer.close()

    records = list(iter_tick_records(archive))

    assert [(record["stock_code"], record["batch_id"]) for record in records] == [
        ("000002.SZ", 1)
    ]
    assert verify_tick_archive(archive)["valid"] is True


def test_verifier_rejects_dropped_batches_and_checksum_mismatch(tmp_path: Path):
    writer = TickArchiveWriter(tmp_path, "20260710", session_id="dropped")
    writer.write_batch({"000001.SZ": _tick(1_720_000_000_001, 10.0)})
    archive = writer.close(dropped_batches=2)

    result = verify_tick_archive(archive)
    assert result["valid"] is False
    assert result["dropped_batches"] == 2

    manifest_path = archive.with_name(archive.name[:-9] + "manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dropped_batches"] = 0
    manifest["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = verify_tick_archive(archive)
    assert result["valid"] is False
    assert result["checksum_matches"] is False


def test_verifier_rejects_corrupt_callback_batch_even_with_fresh_checksum(
    tmp_path: Path,
):
    writer = TickArchiveWriter(tmp_path, "20260710", session_id="batch-shape")
    writer.write_batch({
        "000001.SZ": _tick(1_720_000_000_001, 10.0),
        "000002.SZ": _tick(1_720_000_000_001, 20.0),
    })
    archive = writer.close()

    with gzip.open(archive, mode="rt", encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    records[1]["sequence"] = 2  # Sequence 1 disappeared inside one callback.
    with gzip.open(archive, mode="wt", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    manifest_path = archive.with_name(archive.name[:-9] + "manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest["size_bytes"] = archive.stat().st_size
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = verify_tick_archive(archive)

    assert result["checksum_matches"] is True
    assert result["manifest_count_matches"] is True
    assert result["invalid_batch_structure_records"] == 1
    assert result["valid"] is False
