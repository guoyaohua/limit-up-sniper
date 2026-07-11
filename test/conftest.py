"""Shared pytest policy: external integration tests are opt-in."""

import os
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def pytest_collection_modifyitems(config, items):
    if os.getenv('RUN_INTEGRATION_TESTS', '').lower() in {'1', 'true', 'yes'}:
        return

    skip = pytest.mark.skip(
        reason='set RUN_INTEGRATION_TESTS=true to run external integration tests'
    )
    integration_names = {
        'test_sector_monitor.py',
        'test_sector_monitor_real_data.py',
        'test_stock_monitor.py',
        'test_ths_login.py',
    }
    for item in items:
        if Path(str(item.fspath)).name in integration_names:
            item.add_marker(pytest.mark.integration)
            item.add_marker(skip)
