"""Prepare first-run market files required before the strategy starts."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _normalise_code(value: object) -> str:
    text = str(value or "").strip().upper()
    if text.endswith(".0"):
        text = text[:-2]
    text = text.split(".")[0]
    if not text.isdigit() or len(text) > 6:
        return ""
    code = text.zfill(6)
    if code.startswith(("6", "900")):
        return f"{code}.SH"
    if code.startswith(("0", "3", "200")):
        return f"{code}.SZ"
    if code.startswith(("4", "8", "920")):
        return f"{code}.BJ"
    return ""


def _normalise_trade_date(value: object) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    digits = "".join(character for character in str(value) if character.isdigit())
    if len(digits) != 8:
        raise ValueError(f"无法识别交易日期: {value!r}")
    datetime.strptime(digits, "%Y%m%d")
    return digits


def resolve_previous_trade_date(today: str | None = None) -> str:
    """Return the latest A-share trading day strictly before today."""
    import akshare as ak

    target = datetime.strptime(
        today or datetime.now().strftime("%Y%m%d"), "%Y%m%d"
    ).date()
    calendar = ak.tool_trade_date_hist_sina()
    if "trade_date" not in calendar.columns:
        raise RuntimeError("交易日历缺少 trade_date 列")
    candidates: list[str] = []
    for value in calendar["trade_date"].tolist():
        trade_date = _normalise_trade_date(value)
        if datetime.strptime(trade_date, "%Y%m%d").date() < target:
            candidates.append(trade_date)
    if not candidates:
        raise RuntimeError(f"找不到 {target:%Y%m%d} 之前的交易日")
    return max(candidates)


def _find_column(columns: Iterable[object], names: tuple[str, ...]) -> str:
    lookup = {str(column).strip(): str(column) for column in columns}
    for name in names:
        if name in lookup:
            return lookup[name]
    raise RuntimeError(f"涨停池数据缺少字段，候选列: {', '.join(names)}")


def _write_codes(path: Path, codes: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = list(dict.fromkeys(code for code in codes if code))
    if not values:
        raise RuntimeError(f"拒绝写入空涨停清单: {path}")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(values) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_existing_codes(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"无法读取已有涨停清单: {path}") from exc
    if not lines or any(not line.strip() for line in lines):
        raise RuntimeError(f"已有涨停清单为空或包含空行: {path}")

    codes: list[str] = []
    for line in lines:
        raw_code = line.strip().upper()
        code = _normalise_code(raw_code)
        if code != raw_code or code.endswith(".BJ") or code.startswith("68"):
            raise RuntimeError(f"已有涨停清单包含无效代码 {line!r}: {path}")
        codes.append(code)
    if len(set(codes)) != len(codes):
        raise RuntimeError(f"已有涨停清单包含重复代码: {path}")
    return codes


def _parse_board_count(value: object, *, code: str) -> int:
    try:
        numeric = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{code} 的连板数无法识别: {value!r}") from exc
    if not math.isfinite(numeric) or not numeric.is_integer() or numeric < 1:
        raise RuntimeError(f"{code} 的连板数无效: {value!r}")
    return int(numeric)


def ensure_limit_up_lists(
    trade_date: str,
    *,
    output_root: str | Path = ROOT_DIR / "output",
    fetcher: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Create prior-day limit-up/first-board files without overwriting them."""
    trade_date = _normalise_trade_date(trade_date)
    directory = Path(output_root).expanduser().resolve() / "涨停列表"
    all_path = directory / f"涨停_{trade_date}.txt"
    first_path = directory / f"首次涨停_{trade_date}.txt"
    existing_all = _read_existing_codes(all_path) if all_path.is_file() else None
    existing_first = (
        _read_existing_codes(first_path) if first_path.is_file() else None
    )
    if existing_all is not None and existing_first is not None:
        if not set(existing_first).issubset(existing_all):
            raise RuntimeError(f"已有首板清单不是涨停清单的子集: {first_path}")
        return {
            "trade_date": trade_date,
            "created": False,
            "limit_up_count": len(existing_all),
            "first_board_count": len(existing_first),
            "all_path": str(all_path),
            "first_path": str(first_path),
        }

    if fetcher is None:
        import akshare as ak

        fetcher = ak.stock_zt_pool_em
    frame = fetcher(date=trade_date)
    if frame is None or getattr(frame, "empty", True):
        raise RuntimeError(f"公开行情源未返回 {trade_date} 涨停池")
    code_column = _find_column(frame.columns, ("代码", "股票代码"))
    board_column = _find_column(frame.columns, ("连板数",))

    all_codes: list[str] = []
    first_codes: list[str] = []
    for _, row in frame.iterrows():
        code = _normalise_code(row.get(code_column))
        if code.endswith(".BJ") or code.startswith("68"):
            continue
        if not code:
            raise RuntimeError(f"涨停池包含无法识别的股票代码: {row.get(code_column)!r}")
        all_codes.append(code)
        board_count = _parse_board_count(row.get(board_column), code=code)
        if board_count == 1:
            first_codes.append(code)

    all_codes = list(dict.fromkeys(all_codes))
    first_codes = list(dict.fromkeys(first_codes))
    if not first_codes:
        raise RuntimeError(f"{trade_date} 涨停池未解析出首板，拒绝继续")
    effective_all = existing_all if existing_all is not None else all_codes
    effective_first = existing_first if existing_first is not None else first_codes
    if not set(effective_first).issubset(effective_all):
        raise RuntimeError(f"{trade_date} 首板清单不是涨停清单的子集，拒绝写入")
    if existing_all is None:
        _write_codes(all_path, all_codes)
    if existing_first is None:
        _write_codes(first_path, first_codes)
    return {
        "trade_date": trade_date,
        "created": True,
        "limit_up_count": len(effective_all),
        "first_board_count": len(effective_first),
        "all_path": str(all_path),
        "first_path": str(first_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="准备策略首次启动所需的上一交易日涨停与首板清单。"
    )
    parser.add_argument("--today", help="基准日期 YYYYMMDD，默认今天")
    parser.add_argument("--trade-date", help="直接指定要准备的交易日 YYYYMMDD")
    parser.add_argument(
        "--output-dir", default=str(ROOT_DIR / "output"), help="输出根目录"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    trade_date = args.trade_date or resolve_previous_trade_date(args.today)
    result = ensure_limit_up_lists(trade_date, output_root=args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
