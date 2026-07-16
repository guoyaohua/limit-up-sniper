import threading
import sys
from datetime import time as dt_time
from multiprocessing import Value
from queue import Queue
from types import SimpleNamespace

from data.tick_archive import iter_tick_records, verify_tick_archive
from engine.live_archive import ArchiveCallbackGate, run_live_tick_archive


def test_archive_waits_for_producer_done_and_drains_tail_callback(tmp_path):
    archive_queue = Queue()
    dropped = Value('i', 0)
    stop_event = threading.Event()
    producer_done = threading.Event()
    stop_event.set()

    writer = threading.Thread(
        target=run_live_tick_archive,
        args=(archive_queue, dropped, tmp_path, '20260716'),
        kwargs={'stop_event': stop_event, 'producer_done': producer_done},
    )
    writer.start()

    # A global stop may precede an already-running XTQuant callback.  The
    # archive must remain open until callback admission is explicitly closed.
    archive_queue.put((123_000_000, {
        '000001.SZ': {'time': 123, 'lastPrice': 10.0},
    }))
    producer_done.set()
    writer.join(timeout=5)

    assert not writer.is_alive()
    archive = next((tmp_path / '20260716').glob('*.jsonl.gz'))
    assert verify_tick_archive(archive)['valid'] is True
    assert [record['stock_code'] for record in iter_tick_records(archive)] == [
        '000001.SZ'
    ]


def test_archive_callback_gate_closes_admission_atomically():
    gate = ArchiveCallbackGate()
    with gate:
        assert gate.accepting is True
    gate.close()
    with gate:
        assert gate.accepting is False


def test_quote_subscription_is_cancelled_on_normal_return(monkeypatch):
    import engine.tick_processor as tick_processor

    unsubscribed = []
    fake_xtdata = SimpleNamespace(
        subscribe_whole_quote=lambda stocks, callback: 7,
        unsubscribe_quote=lambda subscribe_id: unsubscribed.append(subscribe_id),
    )
    monkeypatch.setitem(sys.modules, 'xtquant', SimpleNamespace(xtdata=fake_xtdata))
    monkeypatch.setattr(tick_processor.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(tick_processor, 'STOP_TIME', dt_time(0, 0))

    tick_processor.create_whole_quote_task(
        ['000001.SZ'], {'000001.SZ': {'股票名称': '测试'}}, Queue()
    )

    assert unsubscribed == [7]
