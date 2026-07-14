import json
from types import SimpleNamespace

import pytest

from infra import trade_log
from analysis import review_daily
from engine.xt_callback import MyXtQuantTraderCallback, _broker_trade_datetime
from infra.common_enums import OrderType


def test_strategy_events_include_primary_or_shadow_source(monkeypatch):
    captured = []
    monkeypatch.setattr(trade_log, "append_trade_event", captured.append)

    primary = trade_log.record_strategy_event(
        {"信号来源": "primary"}, "buy_decision", "000001.SZ"
    )
    shadow = trade_log.record_strategy_event(
        {"信号来源": "shadow"}, "buy_decision", "000002.SZ"
    )
    legacy = trade_log.record_strategy_event(
        {}, "sell_decision", "000003.SZ"
    )

    assert primary["signal_source"] == "primary"
    assert shadow["signal_source"] == "shadow"
    assert "signal_source" not in legacy
    assert captured == [primary, shadow, legacy]


def test_daily_event_funnel_counts_confirmed_fills():
    summary = review_daily.build_event_summary([
        {'event_type': 'order_submitted'},
        {'event_type': 'trade_filled'},
        {'event_type': 'trade_filled'},
    ])

    assert summary['order_submitted'] == 1
    assert summary['trade_filled'] == 2


def test_tick_snapshot_preserves_explicit_daily_limit_price(monkeypatch):
    captured = []
    monkeypatch.setattr(trade_log, "append_trade_event", captured.append)

    event = trade_log.record_strategy_event(
        {"信号来源": "primary"},
        "buy_decision",
        "000001.SZ",
        snapshot={
            "time": 1, "lastPrice": 11.0, "limitUpPrice": 11.0,
            "bidPrice": [11.0], "askPrice": [0.0],
            "bidVol": [100], "askVol": [0],
        },
    )

    assert event["snapshot"]["limitUpPrice"] == 11.0


def test_order_submission_is_not_loaded_as_confirmed_trade(monkeypatch, tmp_path):
    monkeypatch.setattr(trade_log, 'TRADE_LOG_DIR', str(tmp_path))
    monkeypatch.setattr(review_daily, 'TRADE_LOG_DIR', str(tmp_path))

    trade_log.save_trade_log({
        'action': 'BUY', 'stock_code': '000001.SZ', 'order_id': 123,
        'price': 10.0, 'volume': 100,
    })

    files = list((tmp_path / trade_log.datetime.now().strftime('%Y%m%d')).glob(
        'trade_*.json'
    ))
    assert len(files) == 1
    assert review_daily.load_trade_logs(
        trade_log.datetime.now().strftime('%Y%m%d')
    ) == []


def test_daily_review_loads_only_explicit_confirmed_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(review_daily, 'TRADE_LOG_DIR', str(tmp_path))
    date_str = '20260714'
    log_dir = tmp_path / date_str
    log_dir.mkdir()
    (log_dir / 'fill_1.json').write_text(json.dumps({
        'record_type': 'fill', 'execution_status': 'FILLED',
        'action': 'BUY', 'stock_code': '000001.SZ',
    }), encoding='utf-8')
    (log_dir / 'trade_2.json').write_text(json.dumps({
        'action': 'SELL', 'stock_code': '000001.SZ',
    }), encoding='utf-8')

    logs = review_daily.load_trade_logs(date_str)

    assert len(logs) == 1
    assert logs[0]['record_type'] == 'fill'


def test_daily_review_rejects_unconfirmed_legacy_trade(tmp_path, monkeypatch):
    monkeypatch.setattr(review_daily, 'TRADE_LOG_DIR', str(tmp_path))
    log_dir = tmp_path / '20260714'
    log_dir.mkdir()
    (log_dir / 'trade_legacy.json').write_text(json.dumps({
        'record_type': 'trade', 'action': 'BUY',
        'stock_code': '000001.SZ', 'price': 10, 'volume': 100,
    }), encoding='utf-8')

    assert review_daily.load_trade_logs('20260714') == []


def test_broker_fill_is_idempotent_and_loaded_for_review(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_log, 'TRADE_LOG_DIR', str(tmp_path))
    monkeypatch.setattr(review_daily, 'TRADE_LOG_DIR', str(tmp_path))
    record = {
        'record_type': 'ignored-by-writer',
        'action': 'BUY',
        'stock_code': '000001.SZ',
        'trade_id': 'broker/123',
        'price': 10.0,
        'volume': 100,
    }

    assert trade_log.save_trade_fill(record) is True
    assert trade_log.save_trade_fill(record) is False

    date_str = trade_log.datetime.now().strftime('%Y%m%d')
    logs = review_daily.load_trade_logs(date_str)
    assert len(logs) == 1
    assert logs[0]['trade_id'] == 'broker/123'
    assert logs[0]['execution_status'] == 'FILLED'


def test_xt_trade_callback_persists_confirmed_fill(monkeypatch):
    captured = []

    class QuietLogger:
        def warning(self, _message):
            pass

    monkeypatch.setattr('engine.xt_callback.send_email', lambda *_args: None)
    callback = MyXtQuantTraderCallback(QuietLogger(), fill_sink=captured.append)
    callback.on_stock_trade(SimpleNamespace(
        stock_code='000001.SZ',
        order_type=OrderType.买入.value,
        traded_id='fill-1',
        traded_time=93101,
        traded_price=10.01,
        traded_volume=100,
        traded_amount=1001.0,
        order_id=123,
        order_sysid='sys-1',
        strategy_name='test',
        order_remark='排板',
        offset_flag=0,
        account_id='account-1',
        commission=0.35,
    ))

    assert len(captured) == 1
    assert captured[0]['action'] == 'BUY'
    assert captured[0]['trade_id'] == 'fill-1'
    assert captured[0]['price'] == 10.01
    assert captured[0]['fees'] == 0.35
    assert captured[0]['account_id'] == 'account-1'
    assert captured[0]['timestamp'][11:19] == '09:31:01'


def test_broker_epoch_trade_time_preserves_original_trade_date():
    timestamp = int(
        review_daily.datetime(2026, 7, 13, 14, 30).timestamp()
    )

    traded_at = _broker_trade_datetime(timestamp)

    assert traded_at.strftime('%Y%m%d %H:%M:%S') == '20260713 14:30:00'


def test_xt_trade_callback_excludes_other_account_strategies(monkeypatch):
    captured = []
    warnings = []

    class QuietLogger:
        def warning(self, message):
            warnings.append(message)

    monkeypatch.setattr('engine.xt_callback.send_email', lambda *_args: None)
    callback = MyXtQuantTraderCallback(
        QuietLogger(), stategy_name='limit-up-sniper',
        fill_sink=captured.append,
    )
    callback.on_stock_trade(SimpleNamespace(
        stock_code='000001.SZ',
        order_type=OrderType.买入.value,
        traded_id='manual-fill',
        traded_time=93101,
        traded_price=10.01,
        traded_volume=100,
        traded_amount=1001.0,
        order_id=123,
        order_sysid='sys-1',
        strategy_name='manual-or-other-strategy',
        order_remark='手工买入',
        offset_flag=0,
        account_id='account-1',
    ))

    assert captured == []
    assert len(warnings) == 1
    assert '策略名称不匹配' in warnings[0]


def test_daily_review_fifo_matches_partial_fills_and_deducts_fees(monkeypatch):
    monkeypatch.setattr(review_daily, 'COMMISSION_RATE', 0.0)
    monkeypatch.setattr(review_daily, 'MIN_COMMISSION', 0.0)
    monkeypatch.setattr(review_daily, 'STAMP_DUTY_RATE', 0.0)
    monkeypatch.setattr(review_daily, 'TRANSFER_FEE_RATE', 0.0)
    fills = [
        {'record_type': 'fill', 'action': 'BUY', 'stock_code': 'A',
         'trade_id': 'b1', 'timestamp': '2026-07-14 09:30:00',
         'price': 10.0, 'volume': 100, 'fees': 1.0},
        {'record_type': 'fill', 'action': 'BUY', 'stock_code': 'A',
         'trade_id': 'b2', 'timestamp': '2026-07-14 09:31:00',
         'price': 11.0, 'volume': 100, 'fees': 1.0},
        {'record_type': 'fill', 'action': 'SELL', 'stock_code': 'A',
         'trade_id': 's1', 'timestamp': '2026-07-14 10:00:00',
         'price': 12.0, 'volume': 150, 'fees': 1.5},
    ]

    pairs = review_daily.match_buy_sell_pairs(fills)

    assert [(p['buy_price'], p['volume']) for p in pairs[:2]] == [
        (10.0, 100), (11.0, 50)
    ]
    assert pairs[0]['pnl_amount'] == pytest.approx(198.0)
    assert pairs[1]['pnl_amount'] == pytest.approx(49.0)
    assert pairs[2]['status'] == '未平仓'
    assert pairs[2]['volume'] == 50


def test_daily_review_matches_t1_exit_against_prior_day_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(review_daily, 'TRADE_LOG_DIR', str(tmp_path))
    for date_str, filename, fill in [
        ('20260713', 'fill_buy.json', {
            'record_type': 'fill', 'execution_status': 'FILLED',
            'action': 'BUY', 'stock_code': '000001.SZ', 'trade_id': 'b1',
            'timestamp': '2026-07-13 14:30:00', 'price': 10.0,
            'volume': 100, 'fees': 1.0,
        }),
        ('20260714', 'fill_sell.json', {
            'record_type': 'fill', 'execution_status': 'FILLED',
            'action': 'SELL', 'stock_code': '000001.SZ', 'trade_id': 's1',
            'timestamp': '2026-07-14 09:35:00', 'price': 11.0,
            'volume': 100, 'fees': 1.0,
        }),
        ('20260715', 'fill_future.json', {
            'record_type': 'fill', 'execution_status': 'FILLED',
            'action': 'BUY', 'stock_code': '000002.SZ', 'trade_id': 'future',
            'timestamp': '2026-07-15 09:30:00', 'price': 20.0,
            'volume': 100, 'fees': 1.0,
        }),
    ]:
        log_dir = tmp_path / date_str
        log_dir.mkdir()
        (log_dir / filename).write_text(json.dumps(fill), encoding='utf-8')

    ledger = review_daily.load_trade_ledger('20260714')
    pairs = review_daily.select_daily_pairs(
        review_daily.match_buy_sell_pairs(ledger), '20260714'
    )

    assert {fill['trade_id'] for fill in ledger} == {'b1', 's1'}
    assert len(pairs) == 1
    assert pairs[0]['buy_date'] == '20260713'
    assert pairs[0]['sell_date'] == '20260714'
    assert pairs[0]['pnl_amount'] == pytest.approx(98.0)


def test_daily_review_excludes_old_realized_pairs_but_keeps_open_inventory():
    fills = [
        {'action': 'BUY', 'stock_code': 'OLD', 'trade_id': 'old-b',
         'timestamp': '2026-07-10 09:30:00', 'price': 10, 'volume': 100,
         'fees': 0},
        {'action': 'SELL', 'stock_code': 'OLD', 'trade_id': 'old-s',
         'timestamp': '2026-07-11 09:30:00', 'price': 11, 'volume': 100,
         'fees': 0},
        {'action': 'BUY', 'stock_code': 'OPEN', 'trade_id': 'open-b',
         'timestamp': '2026-07-12 09:30:00', 'price': 20, 'volume': 100,
         'fees': 0},
    ]

    pairs = review_daily.select_daily_pairs(
        review_daily.match_buy_sell_pairs(fills), '20260714'
    )

    assert len(pairs) == 1
    assert pairs[0]['stock_code'] == 'OPEN'
    assert pairs[0]['status'] == '未平仓'


def test_trade_ledger_deduplicates_replayed_broker_trade(tmp_path, monkeypatch):
    monkeypatch.setattr(review_daily, 'TRADE_LOG_DIR', str(tmp_path))
    fill = {
        'record_type': 'fill', 'execution_status': 'FILLED',
        'action': 'BUY', 'stock_code': '000001.SZ', 'trade_id': 'same-id',
        'strategy_name': 'main', 'timestamp': '2026-07-14 09:30:00',
        'price': 10.0, 'volume': 100, 'fees': 1.0,
    }
    log_dir = tmp_path / '20260714'
    log_dir.mkdir()
    for index in (1, 2):
        (log_dir / f'fill_{index}.json').write_text(
            json.dumps(fill), encoding='utf-8'
        )

    ledger = review_daily.load_trade_ledger('20260714')

    assert len(ledger) == 1
    assert ledger[0]['trade_id'] == 'same-id'


def test_trade_ledger_excludes_future_dated_fill_in_old_directory(
        tmp_path, monkeypatch):
    monkeypatch.setattr(review_daily, 'TRADE_LOG_DIR', str(tmp_path))
    log_dir = tmp_path / '20260714'
    log_dir.mkdir()
    (log_dir / 'fill_future.json').write_text(json.dumps({
        'record_type': 'fill', 'execution_status': 'FILLED',
        'action': 'BUY', 'stock_code': '000001.SZ', 'trade_id': 'future',
        'trade_date': '20260715', 'price': 10.0, 'volume': 100,
    }), encoding='utf-8')

    assert review_daily.load_trade_ledger('20260714') == []


def test_daily_review_does_not_cross_match_accounts_or_strategies():
    fills = [
        {'action': 'BUY', 'stock_code': 'A', 'account_id': 'acct-1',
         'strategy_name': 's1', 'timestamp': '2026-07-13 09:30:00',
         'price': 10, 'volume': 100, 'fees': 0},
        {'action': 'SELL', 'stock_code': 'A', 'account_id': 'acct-2',
         'strategy_name': 's1', 'timestamp': '2026-07-14 09:30:00',
         'price': 11, 'volume': 100, 'fees': 0},
        {'action': 'SELL', 'stock_code': 'A', 'account_id': 'acct-1',
         'strategy_name': 's2', 'timestamp': '2026-07-14 09:31:00',
         'price': 12, 'volume': 100, 'fees': 0},
    ]

    pairs = review_daily.match_buy_sell_pairs(fills)

    assert all(pair['pnl_amount'] is None for pair in pairs)
    assert sum(pair['status'] == '未平仓' for pair in pairs) == 1
    assert sum('缺失买入' in pair['status'] for pair in pairs) == 2


def test_daily_review_uses_fee_estimate_for_invalid_commission(monkeypatch):
    monkeypatch.setattr(review_daily, 'COMMISSION_RATE', 0.0)
    monkeypatch.setattr(review_daily, 'MIN_COMMISSION', 5.0)
    monkeypatch.setattr(review_daily, 'STAMP_DUTY_RATE', 0.0)
    monkeypatch.setattr(review_daily, 'TRANSFER_FEE_RATE', 0.0)

    assert review_daily._fill_fees({
        'action': 'BUY', 'price': 10, 'volume': 100, 'fees': None,
    }) == 5.0
    assert review_daily._fill_fees({
        'action': 'BUY', 'price': 10, 'volume': 100, 'fees': 0,
    }) == 5.0
    assert review_daily._fill_fees({
        'action': 'BUY', 'price': 10, 'volume': 100, 'fees': 'invalid',
    }) == 5.0


def test_daily_review_fee_estimate_rejects_nonfinite_configuration(monkeypatch):
    monkeypatch.setattr(review_daily, 'COMMISSION_RATE', float('nan'))
    monkeypatch.setattr(review_daily, 'MIN_COMMISSION', float('nan'))
    monkeypatch.setattr(review_daily, 'STAMP_DUTY_RATE', float('nan'))
    monkeypatch.setattr(review_daily, 'TRANSFER_FEE_RATE', float('nan'))

    assert review_daily._fill_fees({
        'action': 'BUY', 'price': 10, 'volume': 100, 'fees': None,
    }) == pytest.approx(5.01)
