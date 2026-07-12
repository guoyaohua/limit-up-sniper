import json
from pathlib import Path

from data.tick_archive import (
    TickArchiveWriter,
    iter_tick_batches,
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
