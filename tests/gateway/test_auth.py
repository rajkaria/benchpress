from __future__ import annotations

from pathlib import Path

import pytest

from benchpress.gateway.auth import RateLimiter, bearer_token, ensure_bootstrap
from benchpress.gateway.config import ConfigError
from benchpress.gateway.store import Store


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_bearer_token_parsing() -> None:
    assert bearer_token("Bearer bp_abc") == "bp_abc"
    assert bearer_token("bearer bp_abc") == "bp_abc"
    assert bearer_token("Basic xyz") is None and bearer_token(None) is None and bearer_token("Bearer ") is None


def test_rate_limiter_refills_per_key() -> None:
    clock = Clock()
    limiter = RateLimiter(per_minute=2, clock=clock)
    assert limiter.allow("k1") and limiter.allow("k1") and not limiter.allow("k1")
    assert limiter.allow("k2")
    clock.now += 30.0
    assert limiter.allow("k1") and not limiter.allow("k1")


def test_bootstrap_creates_one_default_workspace(store: Store) -> None:
    generated = ensure_bootstrap(store, {})
    assert generated is not None and store.key_for(generated) is not None
    assert ensure_bootstrap(store, {}) is None
    assert [w.name for w in store.workspaces()] == ["default"]


def test_bootstrap_key_from_env(store: Store) -> None:
    key = "bp_" + "k" * 40
    assert ensure_bootstrap(store, {"BENCHPRESS_BOOTSTRAP_KEY": key}) is None
    assert store.key_for(key) is not None


def test_bootstrap_key_must_look_like_a_key(store: Store) -> None:
    with pytest.raises(ConfigError, match="BENCHPRESS_BOOTSTRAP_KEY"):
        ensure_bootstrap(store, {"BENCHPRESS_BOOTSTRAP_KEY": "short"})


def test_bootstrap_empty_string_treated_as_unset(store: Store) -> None:
    generated = ensure_bootstrap(store, {"BENCHPRESS_BOOTSTRAP_KEY": ""})
    assert generated is not None and store.key_for(generated) is not None


def test_workspace_create_prints_a_key_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from benchpress import cli

    url = f"sqlite:///{tmp_path / 'ws.db'}"
    assert cli.main(["workspace", "create", "acme", "--store", url]) == 0
    out = capsys.readouterr().out
    key = next(token for token in out.split() if token.startswith("bp_"))
    assert Store.open(url).key_for(key) is not None
    assert cli.main(["workspace", "create", "acme", "--store", url]) == 1
    assert cli.main(["workspace", "key", "acme", "--name", "second", "--store", url]) == 0
