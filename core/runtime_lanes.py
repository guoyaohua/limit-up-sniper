"""Runtime lane validation and immutable challenger profiles."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ALLOWED_PROFILE_KEYS = {
    "description",
    "include_exploration_candidates",
}


@dataclass(frozen=True)
class ChallengerProfile:
    experiment_id: str
    path: Path
    digest: str
    settings: Mapping[str, Any]

    @property
    def include_exploration_candidates(self) -> bool:
        return bool(self.settings.get("include_exploration_candidates", False))


def validate_experiment_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise ValueError(
            "ExperimentId 只能包含字母、数字、点、下划线和连字符，长度 1-64"
        )
    return normalized


def load_challenger_profile(
    profile_path: str | Path, experiment_id: str
) -> ChallengerProfile:
    experiment_id = validate_experiment_id(experiment_id)
    path = Path(profile_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Challenger 配置不存在: {path}")
    raw = path.read_bytes()
    try:
        settings = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Challenger 配置必须是 UTF-8 JSON 对象: {path}") from exc
    if not isinstance(settings, dict):
        raise ValueError("Challenger 配置顶层必须是 JSON 对象")
    unknown = sorted(set(settings) - _ALLOWED_PROFILE_KEYS)
    if unknown:
        raise ValueError(
            "Challenger 配置包含当前版本不支持的字段: " + ", ".join(unknown)
        )
    include = settings.get("include_exploration_candidates", False)
    if not isinstance(include, bool):
        raise ValueError("include_exploration_candidates 必须是 true/false")
    return ChallengerProfile(
        experiment_id=experiment_id,
        path=path,
        digest=hashlib.sha256(raw).hexdigest(),
        settings=settings,
    )


def freeze_challenger_profile(
    profile: ChallengerProfile, output_root: str | Path,
    execution_assumptions: Mapping[str, Any] | None = None,
) -> Path:
    """Persist an immutable experiment snapshot and reject configuration drift."""
    directory = Path(output_root).expanduser().resolve() / "experiments" / profile.experiment_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "frozen-config.json"
    snapshot = {
        "experiment_id": profile.experiment_id,
        "sha256": profile.digest,
        "source_path": str(profile.path),
        "settings": dict(profile.settings),
        "execution_assumptions": dict(execution_assumptions or {}),
    }
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        if (existing.get("sha256") != profile.digest or
                existing.get("execution_assumptions", {}) !=
                snapshot["execution_assumptions"]):
            raise RuntimeError(
                f"实验 {profile.experiment_id} 的配置或撮合假设已冻结且发生变化；"
                "请使用新的 ExperimentId"
            )
        return target
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(target)
    return target
