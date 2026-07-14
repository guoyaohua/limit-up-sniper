"""Lossless, streamable Tick archives used by shadow trading and backtests.

The archive deliberately keeps the original XTQuant payload instead of flattening
order-book arrays. Each callback batch receives an id so replay can reproduce
the same market-data boundaries seen by the live strategy.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


ARCHIVE_SCHEMA_VERSION = 1
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _json_value(value: Any) -> Any:
    """Convert numpy/pandas-like scalars and arrays to strict JSON values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_value(value.tolist())
    if hasattr(value, "item"):
        return _json_value(value.item())
    return str(value)


@dataclass(frozen=True)
class TickBatch:
    """One original quote callback reconstructed from an archive."""

    batch_id: int
    received_at_ns: int
    ticks: dict[str, dict[str, Any]]
    trade_date: str = ""
    session_id: str = ""

    @property
    def event_time_ms(self) -> int:
        return max(
            (int(tick.get("time", 0) or 0) for tick in self.ticks.values()),
            default=0,
        )

    @property
    def received_time_ms(self) -> int:
        """Wall-clock time when the callback reached the strategy process.

        Exchange Tick timestamps and local decision-log timestamps are different
        clock domains.  Event replay must align a logged decision with callback
        receipt time, otherwise a quote whose exchange clock is slightly ahead
        can make the decision appear before it was actually made.
        """
        return self.received_at_ns // 1_000_000 if self.received_at_ns > 0 else 0


class TickArchiveWriter:
    """Write one immutable gzip JSONL capture segment and its manifest.

    A trading day may contain several segments (for example after a reconnect or
    process restart).  ``session_id`` is part of every record so local batch ids
    can never collide when the segments are replayed together.
    """

    def __init__(
        self,
        output_root: str | os.PathLike[str],
        trade_date: str | None = None,
        *,
        source: str = "xtquant.whole_quote",
        flush_every: int = 1000,
        filename: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.trade_date = trade_date or datetime.now(CHINA_TZ).strftime("%Y%m%d")
        if len(self.trade_date) != 8 or not self.trade_date.isdigit():
            raise ValueError("trade_date must use YYYYMMDD")
        self.output_dir = Path(output_root).expanduser().resolve() / self.trade_date
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or uuid.uuid4().hex
        if not self.session_id or any(char.isspace() for char in self.session_id):
            raise ValueError("session_id must be a non-empty token")
        if filename is None:
            started = datetime.now(CHINA_TZ).strftime("%H%M%S")
            filename = f"ticks-{started}-{self.session_id[:8]}.jsonl.gz"
        if not filename.endswith((".jsonl", ".jsonl.gz")):
            raise ValueError("filename must end with .jsonl or .jsonl.gz")
        self.path = self.output_dir / filename
        if self.path.exists():
            raise FileExistsError(
                f"refusing to append to immutable Tick segment: {self.path}"
            )
        manifest_name = (
            self.path.name[:-9] + "manifest.json"
            if self.path.name.endswith(".jsonl.gz")
            else self.path.name[:-6] + "manifest.json"
        )
        self.manifest_path = self.output_dir / manifest_name
        self.source = source
        self.flush_every = max(1, int(flush_every))
        self._stream = (
            gzip.open(self.path, mode="wt", encoding="utf-8")
            if self.path.suffix == ".gz"
            else self.path.open(mode="w", encoding="utf-8")
        )
        self._batch_id = 0
        self._record_count = 0
        self._batch_count = 0
        self._since_flush = 0
        self._first_event_ms: int | None = None
        self._last_event_ms: int | None = None
        self._stock_counts: Counter[str] = Counter()
        self._closed = False
        self._started_at = datetime.now(CHINA_TZ).isoformat()

    def write_batch(
        self,
        datas: Mapping[str, Mapping[str, Any]],
        *,
        received_at_ns: int | None = None,
    ) -> int:
        """Write one quote callback and return the number of stock ticks saved."""
        if self._closed:
            raise RuntimeError("tick archive is already closed")
        if not datas:
            return 0

        received_at_ns = int(received_at_ns or time.time_ns())
        # XTQuant callbacks must contain mappings.  Reject a malformed symbol
        # atomically rather than silently dropping market data from a segment
        # that could otherwise pass integrity verification.
        valid_ticks: list[tuple[str, dict[str, Any], int]] = []
        for stock_code, raw_tick in datas.items():
            if not isinstance(raw_tick, Mapping):
                raise TypeError(f"invalid Tick payload for {stock_code}")
            tick = _json_value(raw_tick)
            # Parse every event time before assigning an id or touching the
            # stream.  If one payload is malformed, callers may catch the
            # exception and safely continue without retaining a partial batch.
            event_ms = int(tick.get("time", 0) or 0)
            valid_ticks.append((str(stock_code), tick, event_ms))
        if not valid_ticks:
            return 0

        self._batch_id += 1
        written = 0
        for sequence, (stock_code, tick, event_ms) in enumerate(valid_ticks):
            record = {
                "schema_version": ARCHIVE_SCHEMA_VERSION,
                "kind": "tick",
                "trade_date": self.trade_date,
                "source": self.source,
                "session_id": self.session_id,
                "batch_id": self._batch_id,
                "sequence": sequence,
                "received_at_ns": received_at_ns,
                "event_time_ms": event_ms,
                "stock_code": stock_code,
                "data": tick,
            }
            self._stream.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            )
            self._stream.write("\n")
            written += 1
            self._record_count += 1
            self._since_flush += 1
            self._stock_counts[stock_code] += 1
            if event_ms:
                self._first_event_ms = (
                    event_ms
                    if self._first_event_ms is None
                    else min(self._first_event_ms, event_ms)
                )
                self._last_event_ms = (
                    event_ms
                    if self._last_event_ms is None
                    else max(self._last_event_ms, event_ms)
                )

        if written:
            self._batch_count += 1
        if self._since_flush >= self.flush_every:
            self.flush()
        return written

    def flush(self) -> None:
        if not self._closed:
            self._stream.flush()
            self._since_flush = 0

    def close(self, *, dropped_batches: int = 0) -> Path:
        if self._closed:
            return self.path
        self._stream.close()
        self._closed = True
        digest = hashlib.sha256()
        with self.path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "trade_date": self.trade_date,
            "source": self.source,
            "session_id": self.session_id,
            "archive_file": self.path.name,
            "compression": "gzip",
            "started_at": self._started_at,
            "closed_at": datetime.now(CHINA_TZ).isoformat(),
            "batch_count": self._batch_count,
            "record_count": self._record_count,
            "dropped_batches": int(dropped_batches),
            "first_event_time_ms": self._first_event_ms,
            "last_event_time_ms": self._last_event_ms,
            "stock_count": len(self._stock_counts),
            "stock_records": dict(sorted(self._stock_counts.items())),
            "size_bytes": self.path.stat().st_size,
            "sha256": digest.hexdigest(),
        }
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.manifest_path)
        return self.path

    def __enter__(self) -> "TickArchiveWriter":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def discover_tick_files(
    paths: str | os.PathLike[str] | Sequence[str | os.PathLike[str]],
) -> list[Path]:
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    result: list[Path] = []
    seen: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if path.is_dir():
            candidates = sorted(
                set(path.rglob("*.jsonl.gz")) | set(path.rglob("*.jsonl"))
            )
        elif path.is_file():
            candidates = [path]
        else:
            raise FileNotFoundError(path)
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
    return result


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, mode="rt", encoding="utf-8")
    return path.open(mode="r", encoding="utf-8")


def iter_tick_records(
    paths: str | os.PathLike[str] | Sequence[str | os.PathLike[str]],
    *,
    stock_codes: Iterable[str] | None = None,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield validated records in archive/file order."""
    selected = set(stock_codes or ())
    for path in discover_tick_files(paths):
        with _open_text(path) as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
                if record.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
                    raise ValueError(
                        f"unsupported tick schema at {path}:{line_number}: "
                        f"{record.get('schema_version')!r}"
                    )
                if record.get("kind") != "tick":
                    raise ValueError(f"invalid record kind at {path}:{line_number}")
                required = (
                    "trade_date",
                    "session_id",
                    "batch_id",
                    "sequence",
                    "received_at_ns",
                    "event_time_ms",
                    "stock_code",
                    "data",
                )
                missing = [field for field in required if field not in record]
                if missing:
                    raise ValueError(
                        f"missing fields at {path}:{line_number}: {', '.join(missing)}"
                    )
                if not isinstance(record["data"], dict):
                    raise ValueError(f"invalid Tick payload at {path}:{line_number}")
                code = str(record["stock_code"])
                if not code:
                    raise ValueError(f"empty stock code at {path}:{line_number}")
                try:
                    event_ms = int(record["event_time_ms"] or 0)
                    record["batch_id"] = int(record["batch_id"])
                    record["sequence"] = int(record["sequence"])
                    record["received_at_ns"] = int(record["received_at_ns"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"invalid numeric field at {path}:{line_number}"
                    ) from exc
                if selected and code not in selected:
                    continue
                if start_time_ms is not None and event_ms < start_time_ms:
                    continue
                if end_time_ms is not None and event_ms > end_time_ms:
                    continue
                yield record


def iter_tick_batches(
    paths: str | os.PathLike[str] | Sequence[str | os.PathLike[str]],
    **filters: Any,
) -> Iterator[TickBatch]:
    """Rebuild original callback batches without loading a trading day in memory."""
    current_key: tuple[str, str, int] | None = None
    current_ticks: dict[str, dict[str, Any]] = {}
    received_at_ns = 0
    for record in iter_tick_records(paths, **filters):
        key = (
            str(record["trade_date"]),
            str(record["session_id"]),
            int(record["batch_id"]),
        )
        if current_key is not None and key != current_key:
            yield TickBatch(
                current_key[2],
                received_at_ns,
                current_ticks,
                trade_date=current_key[0],
                session_id=current_key[1],
            )
            current_ticks = {}
        current_key = key
        received_at_ns = int(record.get("received_at_ns", 0) or 0)
        current_ticks[str(record["stock_code"])] = dict(record["data"])
    if current_key is not None:
        yield TickBatch(
            current_key[2],
            received_at_ns,
            current_ticks,
            trade_date=current_key[0],
            session_id=current_key[1],
        )


def _manifest_for_archive(path: Path) -> Path:
    name = (
        path.name[:-9] + "manifest.json"
        if path.name.endswith(".jsonl.gz")
        else path.name[:-6] + "manifest.json"
    )
    return path.with_name(name)


def verify_tick_archive(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Stream one segment and report integrity and ordering quality checks."""
    archive_path = Path(path).expanduser().resolve()
    count = 0
    batches: set[tuple[str, str, int]] = set()
    trade_dates: set[str] = set()
    session_ids: set[str] = set()
    stocks: set[str] = set()
    out_of_order = 0
    out_of_order_received = 0
    last_by_stock: dict[str, int] = {}
    first_received_at_ns: int | None = None
    last_received_at_ns: int | None = None
    previous_received_at_ns = 0
    duplicate_records = 0
    invalid_batch_structure_records = 0
    payload_time_mismatch_records = 0
    record_keys: set[tuple[str, str, int, int]] = set()
    closed_batch_keys: set[tuple[str, str, int]] = set()
    current_batch_key: tuple[str, str, int] | None = None
    current_batch_received_at_ns = 0
    expected_sequence = 0
    current_batch_stocks: set[str] = set()
    last_batch_id_by_session: dict[tuple[str, str], int] = {}
    for record in iter_tick_records(archive_path):
        count += 1
        trade_dates.add(str(record["trade_date"]))
        session_ids.add(str(record["session_id"]))
        code = str(record["stock_code"])
        event_ms = int(record.get("event_time_ms", 0) or 0)
        received_at_ns = int(record.get("received_at_ns", 0) or 0)
        batch_key = (
            str(record["trade_date"]),
            str(record["session_id"]),
            int(record["batch_id"]),
        )
        sequence = int(record["sequence"])
        batches.add(batch_key)
        if batch_key != current_batch_key:
            if current_batch_key is not None:
                closed_batch_keys.add(current_batch_key)
            session_key = batch_key[:2]
            expected_batch_id = last_batch_id_by_session.get(session_key, 0) + 1
            if batch_key in closed_batch_keys or batch_key[2] != expected_batch_id:
                invalid_batch_structure_records += 1
            last_batch_id_by_session[session_key] = batch_key[2]
            current_batch_key = batch_key
            current_batch_received_at_ns = received_at_ns
            expected_sequence = 0
            current_batch_stocks = set()
        if (
            sequence != expected_sequence
            or received_at_ns != current_batch_received_at_ns
            or code in current_batch_stocks
        ):
            invalid_batch_structure_records += 1
        expected_sequence = sequence + 1
        current_batch_stocks.add(code)
        try:
            payload_event_ms = int(record["data"].get("time", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            payload_event_ms = -1
        if payload_event_ms != event_ms:
            payload_time_mismatch_records += 1
        record_key = (
            str(record["trade_date"]),
            str(record["session_id"]),
            int(record["batch_id"]),
            int(record["sequence"]),
        )
        if record_key in record_keys:
            duplicate_records += 1
        record_keys.add(record_key)
        stocks.add(code)
        if event_ms and event_ms < last_by_stock.get(code, 0):
            out_of_order += 1
        if event_ms:
            last_by_stock[code] = event_ms
        if received_at_ns <= 0 or (
            previous_received_at_ns and received_at_ns < previous_received_at_ns
        ):
            out_of_order_received += 1
        if received_at_ns > 0:
            first_received_at_ns = (
                received_at_ns
                if first_received_at_ns is None
                else min(first_received_at_ns, received_at_ns)
            )
            last_received_at_ns = (
                received_at_ns
                if last_received_at_ns is None
                else max(last_received_at_ns, received_at_ns)
            )
            previous_received_at_ns = received_at_ns
    manifest_path = _manifest_for_archive(archive_path)
    manifest_present = manifest_path.is_file()
    checksum_matches: bool | None = None
    dropped_batches: int | None = None
    manifest_count_matches: bool | None = None
    manifest_trade_date_matches: bool | None = None
    manifest_session_matches: bool | None = None
    if manifest_present:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        dropped_batches = int(manifest.get("dropped_batches", 0) or 0)
        digest = hashlib.sha256()
        with archive_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        checksum_matches = digest.hexdigest() == manifest.get("sha256")
        manifest_count_matches = (
            count == int(manifest.get("record_count", -1))
            and len(batches) == int(manifest.get("batch_count", -1))
        )
        manifest_trade_date_matches = (
            len(trade_dates) == 1
            and manifest.get("trade_date") == next(iter(trade_dates))
        )
        manifest_session_matches = (
            len(session_ids) == 1
            and manifest.get("session_id") == next(iter(session_ids))
        )

    return {
        "valid": (
            count > 0
            and out_of_order == 0
            and out_of_order_received == 0
            and duplicate_records == 0
            and invalid_batch_structure_records == 0
            and payload_time_mismatch_records == 0
            and manifest_present
            and checksum_matches is True
            and manifest_count_matches is True
            and manifest_trade_date_matches is True
            and manifest_session_matches is True
            and dropped_batches == 0
        ),
        "record_count": count,
        "batch_count": len(batches),
        "stock_count": len(stocks),
        "out_of_order_records": out_of_order,
        "out_of_order_received_records": out_of_order_received,
        "trade_date": next(iter(trade_dates)) if len(trade_dates) == 1 else None,
        "trade_date_count": len(trade_dates),
        "session_id": next(iter(session_ids)) if len(session_ids) == 1 else None,
        "session_id_count": len(session_ids),
        "first_received_at_ns": first_received_at_ns,
        "last_received_at_ns": last_received_at_ns,
        "duplicate_records": duplicate_records,
        "invalid_batch_structure_records": invalid_batch_structure_records,
        "payload_time_mismatch_records": payload_time_mismatch_records,
        "manifest_present": manifest_present,
        "manifest_count_matches": manifest_count_matches,
        "manifest_trade_date_matches": manifest_trade_date_matches,
        "manifest_session_matches": manifest_session_matches,
        "checksum_matches": checksum_matches,
        "dropped_batches": dropped_batches,
    }
