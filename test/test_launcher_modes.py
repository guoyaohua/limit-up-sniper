from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_safe_cmd_entrypoints_pin_their_modes_and_default_lanes():
    simulation = (ROOT / 'scripts' / 'run-simulation.cmd').read_text(
        encoding='utf-8'
    )
    live = (ROOT / 'scripts' / 'run-live.cmd').read_text(encoding='utf-8')

    assert '-Mode simulation -Coverage -ArchiveTicks' in simulation
    assert '-Mode live -Mirror -Coverage -ArchiveTicks' in live


def test_experiment_entrypoint_remains_paper_only():
    experiment = (ROOT / 'scripts' / 'run-experiment.cmd').read_text(
        encoding='utf-8'
    )
    assert '-Mode simulation' in experiment
    assert '-Mode live' not in experiment
    assert '-ExperimentId expanded-pool-v1' in experiment
    assert 'expanded-pool-v1.json' in experiment


def test_launcher_resets_env_lanes_before_applying_explicit_switches():
    launcher = (ROOT / 'scripts' / 'run.ps1').read_text(encoding='utf-8')
    reset = launcher.index('$env:LIMIT_UP_ENABLE_COVERAGE = "false"')
    enable = launcher.index('$env:LIMIT_UP_ENABLE_COVERAGE = "true"')
    assert reset < enable
    assert '-Mirror 只能与 -Mode live' in launcher
    assert 'Challenger 必须同时提供' in launcher
    assert 'LIMIT_UP_PAPER_DB 必须是目录' in launcher
    assert '$env:LIMIT_UP_DEBUG_MODE = "false"' in launcher
