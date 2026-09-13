"""Profiles and the 37-profile model-matrix copies the offline graders demand.

Why a copy: the canonical `benchmark/argabench_40/model_matrix.json` must not change (23 harness
tests pin it), yet every offline grading path insists on exactly 37 profiles:

- `reporting/argabench_matrix.py:1208` `classify_argabench_matrix` — raises unless 37;
- `reporting/argabench_semantic_report.py:398` `_profile_map` — raises unless
  `_EXPECTED_PROFILE_COUNT` (37), and `:1745` `matrix_scoring_ready` compares against it;
- `scripts/run_argabench_model_matrix.py:34` `load_profiles` — 37 unique ids.

So a devsim profile is graded through a copy in which one canonical slot (default `opus-5-high`)
is replaced. The copy is written next to the run (`<matrix_dir>/model-matrix.json`) and passed as
`model_matrix_path=` to `build_argabench_semantic_report`; the swap is recorded under
`devsim_provenance` for disclosure (the classifier requires exactly 37 profiles, so the swap must be stated).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from devsim.harness import model_matrix_path, read_json_object

DEVSIM_PROFILES_PATH = Path(__file__).resolve().with_name("profiles.json")
EXPECTED_PROFILE_COUNT = 37
DEFAULT_SWAP_SLOT = "opus-5-high"
# Slots consumed, in order, when more than one devsim profile shares a matrix copy.
DEFAULT_SWAP_SLOTS: tuple[str, ...] = (
    "opus-5-high",
    "opus-5-xhigh",
    "opus-5-max",
    "opus-5-medium",
    "opus-5-low",
    "opus-4-8-max",
    "opus-4-8-xhigh",
    "opus-4-8-high",
    "opus-4-8-medium",
    "opus-4-8-low",
)
PROFILE_IDENTITY_FIELDS = ("id", "label", "provider", "model_id", "requested_effort", "api_effort", "thinking")
PROFILE_PRICING_FIELDS = (
    "input_usd_per_million",
    "output_usd_per_million",
    "cache_read_usd_per_million",
    "pricing_source",
)


class ProfileError(ValueError):
    """A profile is unknown, ambiguous or incomplete."""


def _profiles_of(payload: Mapping[str, Any], *, label: str) -> list[dict[str, Any]]:
    raw = payload.get("profiles")
    if not isinstance(raw, list):
        raise ProfileError(f"{label}: profiles must be an array")
    profiles: list[dict[str, Any]] = []
    for item in cast(list[object], raw):
        if not isinstance(item, dict):
            raise ProfileError(f"{label}: every profile must be an object")
        profiles.append(cast(dict[str, Any], item))
    return profiles


def validate_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    missing = [name for name in (*PROFILE_IDENTITY_FIELDS, *PROFILE_PRICING_FIELDS) if name not in profile]
    if missing:
        raise ProfileError(f"profile {profile.get('id')!r} is missing {', '.join(missing)}")
    return dict(profile)


def canonical_matrix() -> dict[str, Any]:
    return read_json_object(model_matrix_path())


def canonical_profiles() -> list[dict[str, Any]]:
    profiles = _profiles_of(canonical_matrix(), label="canonical model matrix")
    if len(profiles) != EXPECTED_PROFILE_COUNT:
        raise ProfileError(f"canonical model matrix must contain {EXPECTED_PROFILE_COUNT} profiles")
    return profiles


def devsim_profiles(path: Path = DEVSIM_PROFILES_PATH) -> list[dict[str, Any]]:
    return [validate_profile(profile) for profile in _profiles_of(read_json_object(path), label=str(path))]


def list_profiles() -> dict[str, dict[str, Any]]:
    """All selectable profiles: canonical first, then devsim (ids must not collide)."""

    profiles: dict[str, dict[str, Any]] = {}
    for profile in canonical_profiles():
        profiles[str(profile["id"])] = dict(profile)
    for profile in devsim_profiles():
        profile_id = str(profile["id"])
        if profile_id in profiles:
            raise ProfileError(f"devsim profile {profile_id!r} collides with a canonical profile")
        profiles[profile_id] = profile
    return profiles


def load_profile(profile_id: str) -> dict[str, Any]:
    """Read a profile from the canonical matrix or from `devsim/profiles.json`."""

    profiles = list_profiles()
    try:
        return validate_profile(profiles[profile_id])
    except KeyError as error:
        raise ProfileError(f"unknown profile {profile_id!r}; expected one of: {', '.join(sorted(profiles))}") from error


def is_canonical(profile_id: str) -> bool:
    return any(profile.get("id") == profile_id for profile in canonical_profiles())


def matrix_with_profiles(
    swap_in: Sequence[Mapping[str, Any]],
    *,
    swap_out: Sequence[str] = DEFAULT_SWAP_SLOTS,
) -> dict[str, Any]:
    """A 37-profile matrix in which `swap_in[i]` replaces the canonical slot `swap_out[i]`.

    Canonical profiles passed in `swap_in` are left in place (nothing is swapped for them).
    """

    matrix = canonical_matrix()
    profiles = [dict(profile) for profile in canonical_profiles()]
    by_id = {str(profile["id"]): index for index, profile in enumerate(profiles)}
    swaps: dict[str, str] = {}
    slots = list(swap_out)
    for candidate in swap_in:
        profile = validate_profile(candidate)
        profile_id = str(profile["id"])
        if profile_id in by_id and profile_id not in swaps.values():
            continue  # canonical profile: already present
        if profile_id in swaps.values():
            continue  # already swapped in
        slot = next((slot for slot in slots if slot not in swaps and slot in by_id), None)
        if slot is None:
            raise ProfileError("no canonical slot left to swap out")
        profiles[by_id[slot]] = profile
        swaps[slot] = profile_id
    if len(profiles) != EXPECTED_PROFILE_COUNT or len({str(profile["id"]) for profile in profiles}) != len(profiles):
        raise ProfileError("matrix copy must keep 37 unique profiles")
    matrix["profiles"] = profiles
    matrix["profile_count"] = len(profiles)
    matrix["devsim_provenance"] = {
        "source": str(model_matrix_path()),
        "swapped": [{"slot": slot, "profile_id": profile_id} for slot, profile_id in swaps.items()],
        "note": (
            "Offline graders require exactly 37 profiles; canonical slots listed above were replaced "
            "by devsim profiles. Every other profile is byte-identical to the canonical matrix."
        ),
    }
    return matrix


def write_matrix(dest: Path, matrix: Mapping[str, Any]) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(matrix, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return dest


def write_matrix_copy(
    dest: Path,
    swap_out: str = DEFAULT_SWAP_SLOT,
    swap_in: Mapping[str, Any] | None = None,
) -> Path:
    """Write a 37-profile matrix with `swap_out` replaced by `swap_in` (or a plain copy if None)."""

    swap_in_list: list[Mapping[str, Any]] = [swap_in] if swap_in is not None else []
    return write_matrix(dest, matrix_with_profiles(swap_in_list, swap_out=(swap_out, *DEFAULT_SWAP_SLOTS)))


def matrix_profiles(path: Path) -> list[dict[str, Any]]:
    return _profiles_of(read_json_object(path), label=str(path))


__all__ = [
    "DEFAULT_SWAP_SLOT",
    "DEFAULT_SWAP_SLOTS",
    "DEVSIM_PROFILES_PATH",
    "EXPECTED_PROFILE_COUNT",
    "PROFILE_IDENTITY_FIELDS",
    "PROFILE_PRICING_FIELDS",
    "ProfileError",
    "canonical_matrix",
    "canonical_profiles",
    "devsim_profiles",
    "is_canonical",
    "list_profiles",
    "load_profile",
    "matrix_profiles",
    "matrix_with_profiles",
    "validate_profile",
    "write_matrix",
    "write_matrix_copy",
]
