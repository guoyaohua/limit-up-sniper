"""Capture full-market XTQuant Tick callbacks into daily replay archives."""

from __future__ import annotations

import argparse
import queue
import signal
import sys
import threading
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import IP, PORT
from data.tick_archive import CHINA_TZ, TickArchiveWriter, verify_tick_archive


def _parse_time(value: str) -> dt_time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("time must use HH:MM") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="保存沪深 A 股完整 Tick 及五档盘口，供影子交易和事件回放使用。"
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT_DIR / "output" / "tick_archive"),
        help="每日归档根目录",
    )
    parser.add_argument("--trade-date", help="归档日期 YYYYMMDD，默认今天")
    parser.add_argument("--host", default=IP, help="XTQuant 数据服务地址")
    parser.add_argument("--port", type=int, default=PORT, help="XTQuant 数据服务端口")
    parser.add_argument("--stop-time", type=_parse_time, default=dt_time(15, 5))
    parser.add_argument("--sector", default="沪深A股", help="XTQuant 板块名称")
    parser.add_argument(
        "--include-star",
        action="store_true",
        help="包含科创板；默认与本策略股票池一致，排除科创板和北交所",
    )
    parser.add_argument("--queue-size", type=int, default=2048)
    parser.add_argument("--flush-every", type=int, default=1000)
    parser.add_argument("--verify", action="store_true", help="结束后流式校验归档")
    return parser


def _select_stocks(xtdata, sector: str, include_star: bool) -> list[str]:
    xtdata.download_sector_data()
    stocks = xtdata.get_stock_list_in_sector(sector)
    selected = []
    for code in stocks:
        if code.endswith(".BJ"):
            continue
        if not include_star and code.startswith("68"):
            continue
        selected.append(code)
    return sorted(set(selected))


def capture(args: argparse.Namespace) -> Path:
    if args.queue_size <= 0:
        raise ValueError("--queue-size 必须大于 0")
    if args.flush_every <= 0:
        raise ValueError("--flush-every 必须大于 0")
    try:
        from xtquant import xtdata
    except ImportError as exc:
        raise RuntimeError("未找到 XTQuant SDK；请在 QMT Python 环境运行此脚本") from exc

    trade_date = args.trade_date or datetime.now(CHINA_TZ).strftime("%Y%m%d")
    batch_queue: queue.Queue[tuple[int, dict[str, dict[str, Any]]]] = queue.Queue(
        maxsize=args.queue_size
    )
    stop_event = threading.Event()
    producer_done = threading.Event()
    accepting_callbacks = True
    callback_lock = threading.Lock()
    consumer_errors: queue.Queue[BaseException] = queue.Queue(maxsize=1)
    dropped_batches = 0
    saved_records = 0

    writer = TickArchiveWriter(
        args.output_dir, trade_date, flush_every=args.flush_every
    )

    def consume() -> None:
        nonlocal saved_records
        try:
            # ``stop_event`` only asks the subscription loop to stop.  The
            # writer must stay alive until unsubscribe has completed and no
            # callback can enqueue another batch, otherwise the final callback
            # can be silently lost at shutdown.
            while not producer_done.is_set() or not batch_queue.empty():
                try:
                    received_at_ns, datas = batch_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    saved_records += writer.write_batch(
                        datas, received_at_ns=received_at_ns
                    )
                finally:
                    batch_queue.task_done()
        except BaseException as exc:
            try:
                consumer_errors.put_nowait(exc)
            except queue.Full:
                pass
            stop_event.set()

    def on_data(datas) -> None:
        nonlocal dropped_batches
        if not datas:
            return
        # Serialize callback admission with shutdown.  unsubscribe_quote() may
        # return while an already-running callback is finishing; after this
        # lock is closed no producer can enqueue behind the consumer's exit.
        with callback_lock:
            if not accepting_callbacks:
                return
            # Never block XTQuant's callback thread.  On overflow fail the
            # capture immediately; the manifest records the loss so this
            # segment can never be accepted by a rigorous backtest.
            try:
                batch_queue.put_nowait((time.time_ns(), dict(datas)))
            except queue.Full:
                dropped_batches += 1
                stop_event.set()

    consumer = threading.Thread(
        target=consume, name="tick-archive-writer", daemon=True
    )
    consumer.start()
    subscribe_id = -1
    archive_path: Path | None = None
    consumer_error: BaseException | None = None
    try:
        xtdata.reconnect(args.host, args.port)
        stocks = _select_stocks(xtdata, args.sector, args.include_star)
        if not stocks:
            raise RuntimeError("XTQuant 未返回可订阅股票")
        print(f"订阅 {len(stocks)} 只股票，归档目录: {writer.output_dir}")
        subscribe_id = xtdata.subscribe_whole_quote(stocks, callback=on_data)
        if subscribe_id < 0:
            raise RuntimeError(f"全推行情订阅失败: {subscribe_id}")

        def request_stop(signum, frame) -> None:
            stop_event.set()

        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, request_stop)
        while not stop_event.is_set():
            if datetime.now(CHINA_TZ).time() >= args.stop_time:
                stop_event.set()
                break
            time.sleep(0.5)
    finally:
        if subscribe_id >= 0:
            try:
                xtdata.unsubscribe_quote(subscribe_id)
            except Exception:
                pass
        # Closing admission under the same lock used by callbacks guarantees
        # that every accepted batch is visible before ``producer_done``.
        with callback_lock:
            accepting_callbacks = False
        producer_done.set()
        stop_event.set()
        consumer.join(timeout=120)
        if consumer.is_alive():
            raise RuntimeError("归档写入线程未能在 120 秒内排空队列")
        if not consumer_errors.empty():
            consumer_error = consumer_errors.get_nowait()
        # A writer exception may happen after some valid records were flushed.
        # Mark that segment incomplete as well, so verification rejects it even
        # if an operator overlooks this process's non-zero exit status.
        archive_path = writer.close(
            dropped_batches=dropped_batches + int(consumer_error is not None)
        )

    if consumer_error is not None:
        raise RuntimeError(
            f"Tick 归档写入失败，文件不可用于回测: {archive_path}"
        ) from consumer_error
    if dropped_batches:
        raise RuntimeError(
            f"Tick 采集丢失 {dropped_batches} 个回调批次，文件不可用于回测: "
            f"{archive_path}"
        )
    if saved_records <= 0:
        raise RuntimeError(f"Tick 归档为空，文件不可用于回测: {archive_path}")

    print(
        f"保存完成: {saved_records} 条, 丢弃批次: {dropped_batches}, "
        f"文件: {archive_path}"
    )
    if args.verify:
        quality = verify_tick_archive(archive_path)
        print(quality)
        if not quality["valid"]:
            raise RuntimeError("Tick 归档完整性校验失败")
    return archive_path


def main() -> int:
    args = build_parser().parse_args()
    try:
        capture(args)
        return 0
    except Exception as exc:
        print(f"Tick 采集失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
