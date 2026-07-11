"""Small XTQuant constant fallback for offline analysis and unit tests."""

try:
    from xtquant import xtconstant as xtconstant
except ImportError:  # pragma: no cover - exercised only without the QMT SDK
    class _XTConstantFallback:
        STOCK_BUY = 23
        STOCK_SELL = 24
        FIX_PRICE = 11
        MARKET_SH_CONVERT_5_CANCEL = 42
        MARKET_SH_CONVERT_5_LIMIT = 43
        MARKET_PEER_PRICE_FIRST = 44
        MARKET_MINE_PRICE_FIRST = 45
        MARKET_SZ_INSTBUSI_RESTCANCEL = 46
        MARKET_SZ_CONVERT_5_CANCEL = 47
        MARKET_SZ_FULL_OR_CANCEL = 48
        ORDER_UNREPORTED = 48
        ORDER_WAIT_REPORTING = 49
        ORDER_REPORTED = 50
        ORDER_REPORTED_CANCEL = 51
        ORDER_PARTSUCC_CANCEL = 52
        ORDER_PART_CANCEL = 53
        ORDER_CANCELED = 54
        ORDER_PART_SUCC = 55
        ORDER_SUCCEEDED = 56
        ORDER_JUNK = 57
        ORDER_UNKNOWN = 255
        SH_MARKET = 0
        SZ_MARKET = 1

    xtconstant = _XTConstantFallback()
