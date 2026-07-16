"""Write Tick batches from the strategy's existing XTQuant subscription."""

from __future__ import annotations

from queue import Empty
from threading import Lock

from loguru import logger

from data.tick_archive import TickArchiveWriter


class ArchiveCallbackGate:
    """Serialize callback admission with the producer shutdown handshake."""

    def __init__(self):
        self._lock = Lock()
        self.accepting = True

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._lock.release()

    def close(self):
        with self:
            self.accepting = False


def run_live_tick_archive(archive_queue, dropped_batches, output_dir, trade_date,
                          stop_event=None, producer_done=None):
    """Drain all accepted callbacks and close an auditable segment.

    ``producer_done`` is set only after the XTQuant subscription has been
    cancelled and callback admission has been closed.  A global stop event is
    deliberately not sufficient to close the writer: an already-running SDK
    callback may still publish its final batch after that event is set.
    """
    writer = TickArchiveWriter(output_dir, trade_date)
    saved = 0
    write_failed = False
    try:
        while True:
            try:
                received_at_ns, datas = archive_queue.get(timeout=0.5)
                saved += writer.write_batch(datas, received_at_ns=received_at_ns)
            except Empty:
                if producer_done is not None:
                    if producer_done.is_set():
                        break
                elif stop_event is not None and stop_event.is_set():
                    break
    except BaseException:
        write_failed = True
        raise
    finally:
        with dropped_batches.get_lock():
            dropped = int(dropped_batches.value)
        invalid_batches = dropped + int(saved <= 0) + int(write_failed)
        path = writer.close(dropped_batches=invalid_batches)
        if saved <= 0:
            logger.error(f'[Tick归档] 归档为空，不可用于回测: {path}')
        elif dropped:
            logger.error(
                f'[Tick归档] 丢失 {dropped} 个回调批次，归档不可用于严谨回测: {path}')
        else:
            logger.info(f'[Tick归档] 保存 {saved} 条 Tick: {path}')
