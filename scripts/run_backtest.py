"""Run a user strategy plug-in against one or more Tick archive days."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from engine.backtest import BacktestConfig, BacktestEngine, write_backtest_result
from engine.paper_broker import BrokerConfig


def _load_strategy(path: str, factory_name: str, settings: dict):
    module_path = Path(path).expanduser().resolve()
    if not module_path.is_file():
        raise FileNotFoundError(module_path)
    spec = importlib.util.spec_from_file_location("limit_up_backtest_strategy", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载策略模块: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise AttributeError(f"策略模块必须提供可调用的 {factory_name}(settings)")
    return factory(settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用归档 Tick 进行事件驱动回测")
    parser.add_argument("--ticks", nargs="+", required=True, help="归档文件或目录")
    parser.add_argument("--strategy", required=True, help="策略插件 .py 路径")
    parser.add_argument("--factory", default="create_strategy")
    parser.add_argument("--settings", default="{}", help="传给工厂函数的 JSON 对象")
    parser.add_argument(
        "--output",
        default=str(ROOT_DIR / "output" / "backtests" / "result.json"),
    )
    parser.add_argument("--initial-cash", type=float, default=1_000_000)
    parser.add_argument("--commission-rate", type=float, default=0.0003)
    parser.add_argument("--minimum-commission", type=float, default=5.0)
    parser.add_argument("--stamp-duty-rate", type=float, default=0.0005)
    parser.add_argument("--transfer-fee-rate", type=float, default=0.00001)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--participation-rate", type=float, default=0.10)
    parser.add_argument("--allow-t0", action="store_true")
    parser.add_argument("--equity-every", type=int, default=100)
    parser.add_argument("--close-at-end", action="store_true")
    parser.add_argument(
        "--same-tick-execution",
        action="store_true",
        help="研究用途：允许信号在生成它的同一 Tick 成交（默认下一笔 Tick，避免前视）",
    )
    parser.add_argument(
        "--skip-archive-validation",
        action="store_true",
        help="允许回放缺失 manifest/校验失败的数据；严谨回测不应使用",
    )
    parser.add_argument("--stocks", nargs="*", help="仅回放指定股票")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        settings = json.loads(args.settings)
        if not isinstance(settings, dict):
            raise ValueError("--settings 必须是 JSON 对象")
        strategy = _load_strategy(args.strategy, args.factory, settings)
        broker_config = BrokerConfig(
            initial_cash=args.initial_cash,
            commission_rate=args.commission_rate,
            minimum_commission=args.minimum_commission,
            stamp_duty_rate=args.stamp_duty_rate,
            transfer_fee_rate=args.transfer_fee_rate,
            slippage_bps=args.slippage_bps,
            participation_rate=args.participation_rate,
            allow_t0=args.allow_t0,
        )
        engine = BacktestEngine(
            strategy,
            broker_config=broker_config,
            config=BacktestConfig(
                sample_equity_every_batches=args.equity_every,
                close_positions_at_end=args.close_at_end,
                execute_on_next_tick=not args.same_tick_execution,
                validate_archives=not args.skip_archive_validation,
            ),
        )
        result = engine.run(args.ticks, stock_codes=args.stocks)
        output = write_backtest_result(result, args.output)
        print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
        print(f"完整结果: {output}")
        return 0
    except Exception as exc:
        print(f"回测失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
