from infra import trade_log


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
