import pandas as pd
import pytest

from scripts.prepare_market_data import ensure_limit_up_lists


def test_prepare_limit_up_lists_without_overwriting_existing_files(tmp_path):
    output = tmp_path / "output"
    target = output / "涨停列表"
    target.mkdir(parents=True)
    all_path = target / "涨停_20260710.txt"
    first_path = target / "首次涨停_20260710.txt"
    all_path.write_text("000001.SZ\n", encoding="utf-8")
    first_path.write_text("000001.SZ\n", encoding="utf-8")

    def should_not_fetch(**kwargs):
        raise AssertionError("existing files must not be fetched or overwritten")

    result = ensure_limit_up_lists(
        "20260710", output_root=output, fetcher=should_not_fetch
    )

    assert result["created"] is False
    assert all_path.read_text(encoding="utf-8") == "000001.SZ\n"
    assert first_path.read_text(encoding="utf-8") == "000001.SZ\n"


def test_prepare_limit_up_lists_filters_market_and_classifies_first_board(tmp_path):
    frame = pd.DataFrame(
        {
            "代码": ["000001", "600000", "300001", "688001", "920001"],
            "连板数": [1, 2, 1, 1, 1],
        }
    )

    result = ensure_limit_up_lists(
        "20260710", output_root=tmp_path, fetcher=lambda **kwargs: frame
    )

    directory = tmp_path / "涨停列表"
    assert result["created"] is True
    assert (directory / "涨停_20260710.txt").read_text(encoding="utf-8").splitlines() == [
        "000001.SZ",
        "600000.SH",
        "300001.SZ",
    ]
    assert (directory / "首次涨停_20260710.txt").read_text(encoding="utf-8").splitlines() == [
        "000001.SZ",
        "300001.SZ",
    ]


def test_prepare_limit_up_lists_fails_closed_on_empty_source(tmp_path):
    with pytest.raises(RuntimeError, match="未返回"):
        ensure_limit_up_lists(
            "20260710",
            output_root=tmp_path,
            fetcher=lambda **kwargs: pd.DataFrame(),
        )


def test_prepare_limit_up_lists_rejects_empty_existing_file(tmp_path):
    target = tmp_path / "涨停列表"
    target.mkdir()
    (target / "涨停_20260710.txt").write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="为空"):
        ensure_limit_up_lists(
            "20260710",
            output_root=tmp_path,
            fetcher=lambda **kwargs: pytest.fail("invalid cache must fail before fetch"),
        )


def test_prepare_limit_up_lists_rejects_inconsistent_existing_files(tmp_path):
    target = tmp_path / "涨停列表"
    target.mkdir()
    (target / "涨停_20260710.txt").write_text(
        "000001.SZ\n", encoding="utf-8"
    )
    (target / "首次涨停_20260710.txt").write_text(
        "600000.SH\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="不是涨停清单的子集"):
        ensure_limit_up_lists("20260710", output_root=tmp_path)


def test_prepare_limit_up_lists_requires_explicit_board_count(tmp_path):
    frame = pd.DataFrame({"代码": ["000001"], "涨停统计": ["10/6"]})

    with pytest.raises(RuntimeError, match="连板数"):
        ensure_limit_up_lists(
            "20260710", output_root=tmp_path, fetcher=lambda **kwargs: frame
        )


@pytest.mark.parametrize("board_count", ["", "nan", 0, 1.5])
def test_prepare_limit_up_lists_rejects_invalid_board_count(tmp_path, board_count):
    frame = pd.DataFrame({"代码": ["000001"], "连板数": [board_count]})

    with pytest.raises(RuntimeError, match="连板数"):
        ensure_limit_up_lists(
            "20260710", output_root=tmp_path, fetcher=lambda **kwargs: frame
        )


def test_prepare_limit_up_lists_rejects_corrupt_stock_code(tmp_path):
    frame = pd.DataFrame({"代码": ["not-a-code"], "连板数": [1]})

    with pytest.raises(RuntimeError, match="无法识别"):
        ensure_limit_up_lists(
            "20260710", output_root=tmp_path, fetcher=lambda **kwargs: frame
        )
