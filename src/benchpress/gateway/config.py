"""Gateway settings: `benchpress.toml` + env, layered under CLI/API overrides.

Pure settings — this module never imports `sqlalchemy`. Secrets never sit in the config file directly:
every secret-shaped value is an `env:NAME` reference, resolved here and never logged. Precedence, lowest
to highest: dataclass defaults < `benchpress.toml` < environment variables < explicit `overrides`; each
layer only ever *sets* the fields it mentions.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal, cast

__all__ = [
    "ApprovalRuleConfig",
    "ConfigError",
    "Settings",
    "UpstreamConfig",
    "is_loopback",
    "load_settings",
    "resolve_secret",
]


class ConfigError(ValueError):
    """Raised when `benchpress.toml` + env + overrides don't add up to usable settings."""


@dataclass(frozen=True)
class UpstreamConfig:
    provider: str
    base_url: str
    token: str | None = field(default=None, repr=False)
    role: str | None = None


@dataclass(frozen=True)
class ApprovalRuleConfig:
    name: str
    provider: str = "*"
    methods: tuple[str, ...] = ("POST", "PUT", "PATCH")
    path: str = "*"
    classes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    # network and store
    host: str = "127.0.0.1"
    port: int = 8787
    store: str = "sqlite:///benchpress.db"
    policy_dir: Path | None = None
    # auth and limits
    auth: Literal["api_key", "none"] = "api_key"
    metrics_auth: Literal["api_key", "none"] = "api_key"
    requests_per_minute: int = 600
    max_body_bytes: int = 1_048_576
    # approvals
    approval_ttl_seconds: int = 3600
    approval_webhook_url: str | None = None
    approval_webhook_secret: str | None = field(default=None, repr=False)
    approval_rules: tuple[ApprovalRuleConfig, ...] = ()
    # everything else
    upstreams: tuple[UpstreamConfig, ...] = ()
    console: bool = True
    idempotency_lease_seconds: float = 300.0
    sessions_cache: int = 1024


def resolve_secret(ref: str, env: Mapping[str, str], *, what: str) -> str:
    """Resolve an `env:NAME` reference to its value. Never accepts a secret written out inline."""
    if not ref.startswith("env:"):
        raise ConfigError(f"{what} must be an 'env:NAME' reference")
    name = ref.removeprefix("env:")
    value = env.get(name)
    if value is None:
        raise ConfigError(f"{what} references unset environment variable {name!r}")
    return value


def is_loopback(host: str) -> bool:
    """True for `localhost`, `127.0.0.0/8`, and `::1` — the hosts `auth = \"none\"` is allowed on."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_SERVER_KEYS = {"host", "port", "store", "policy_dir", "auth", "metrics_auth", "console"}
_LIMITS_KEYS = {"requests_per_minute", "max_body_bytes", "idempotency_lease_seconds"}
_APPROVALS_KEYS = {"ttl_seconds", "webhook_url", "webhook_secret", "rules"}
_RULE_KEYS = {"name", "provider", "methods", "path", "classes"}
_UPSTREAM_KEYS = {"provider", "base_url", "token", "role"}
_TOP_LEVEL_KEYS = {"server", "limits", "approvals", "upstreams"}

_ENV_FIELDS = {
    "BENCHPRESS_HOST": "host",
    "BENCHPRESS_PORT": "port",
    "BENCHPRESS_STORE": "store",
    "BENCHPRESS_POLICY_DIR": "policy_dir",
    "BENCHPRESS_AUTH": "auth",
}

_SETTINGS_FIELD_NAMES = {f.name for f in fields(Settings)}


def _check_keys(table: Mapping[str, object], allowed: set[str], where: str) -> None:
    for key in table:
        if key not in allowed:
            raise ConfigError(f"unknown key {where}.{key}")


def _as_table(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be a table")
    return cast(Mapping[str, object], value)


def _as_list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise ConfigError(f"{where} must be an array")
    return cast(list[object], value)


def _as_str_tuple(value: object, where: str, default: tuple[str, ...]) -> tuple[str, ...]:
    return default if value is None else tuple(str(item) for item in _as_list(value, where))


def _parse_rule(raw: object) -> ApprovalRuleConfig:
    table = _as_table(raw, "approvals.rules entries")
    _check_keys(table, _RULE_KEYS, "approvals.rules")
    name = table.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError("approvals.rules entries need a non-empty name")
    return ApprovalRuleConfig(
        name=name,
        provider=str(table.get("provider", "*")),
        methods=_as_str_tuple(table.get("methods"), "approvals.rules.methods", ("POST", "PUT", "PATCH")),
        path=str(table.get("path", "*")),
        classes=_as_str_tuple(table.get("classes"), "approvals.rules.classes", ()),
    )


def _parse_upstream(raw: object, env: Mapping[str, str]) -> UpstreamConfig:
    table = _as_table(raw, "upstreams entries")
    _check_keys(table, _UPSTREAM_KEYS, "upstreams")
    provider = table.get("provider")
    if not isinstance(provider, str) or not provider:
        raise ConfigError("upstreams entries need a non-empty provider")
    base_url = table.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        raise ConfigError(f"upstreams[{provider}] needs a base_url")
    token_ref = table.get("token")
    token = None if token_ref is None else resolve_secret(str(token_ref), env, what=f"upstreams[{provider}].token")
    role = table.get("role")
    return UpstreamConfig(provider=provider, base_url=base_url, token=token, role=None if role is None else str(role))


def _parse_file(data: Mapping[str, object], config_dir: Path, env: Mapping[str, str]) -> dict[str, object]:
    for key in data:
        if key not in _TOP_LEVEL_KEYS:
            raise ConfigError(f"unknown config section [{key}]")

    out: dict[str, object] = {}

    server = _as_table(data.get("server", {}), "[server]")
    _check_keys(server, _SERVER_KEYS, "server")
    for key in ("host", "port", "store", "auth", "metrics_auth", "console"):
        if key in server:
            out[key] = server[key]
    if "policy_dir" in server:
        raw_dir = Path(str(server["policy_dir"]))
        out["policy_dir"] = raw_dir if raw_dir.is_absolute() else config_dir / raw_dir

    limits = _as_table(data.get("limits", {}), "[limits]")
    _check_keys(limits, _LIMITS_KEYS, "limits")
    for key in _LIMITS_KEYS:
        if key in limits:
            out[key] = limits[key]

    approvals = _as_table(data.get("approvals", {}), "[approvals]")
    _check_keys(approvals, _APPROVALS_KEYS, "approvals")
    if "ttl_seconds" in approvals:
        out["approval_ttl_seconds"] = approvals["ttl_seconds"]
    if "webhook_url" in approvals:
        out["approval_webhook_url"] = approvals["webhook_url"]
    if "webhook_secret" in approvals:
        out["approval_webhook_secret"] = resolve_secret(
            str(approvals["webhook_secret"]), env, what="approvals.webhook_secret"
        )
    if "rules" in approvals:
        rules = _as_list(approvals["rules"], "approvals.rules")
        out["approval_rules"] = tuple(_parse_rule(r) for r in rules)

    if "upstreams" in data:
        upstreams = _as_list(data["upstreams"], "upstreams")
        out["upstreams"] = tuple(_parse_upstream(u, env) for u in upstreams)

    return out


def _apply_env(merged: dict[str, object], env: Mapping[str, str]) -> None:
    for env_key, field_name in _ENV_FIELDS.items():
        if env_key not in env:
            continue
        raw = env[env_key]
        if field_name == "port":
            try:
                merged[field_name] = int(raw)
            except ValueError as exc:
                raise ConfigError(f"{env_key} must be an integer, got {raw!r}") from exc
        elif field_name == "policy_dir":
            merged[field_name] = Path(raw)
        else:
            merged[field_name] = raw


def _apply_overrides(merged: dict[str, object], overrides: Mapping[str, object]) -> None:
    for key, value in overrides.items():
        if key not in _SETTINGS_FIELD_NAMES:
            raise ConfigError(f"unknown settings override {key!r}")
        if key == "policy_dir" and value is not None:
            merged[key] = Path(cast(str, value))
        else:
            merged[key] = value


def _validate(settings: Settings) -> None:
    if (settings.auth == "none" or settings.metrics_auth == "none") and not is_loopback(settings.host):
        raise ConfigError(f"auth/metrics_auth may be 'none' only when host is loopback; host is {settings.host!r}")
    for limit_name, value in (
        ("requests_per_minute", settings.requests_per_minute),
        ("max_body_bytes", settings.max_body_bytes),
        ("idempotency_lease_seconds", settings.idempotency_lease_seconds),
        ("approval_ttl_seconds", settings.approval_ttl_seconds),
    ):
        if value <= 0:
            raise ConfigError(f"{limit_name} must be positive, got {value!r}")
    seen: set[str] = set()
    for rule in settings.approval_rules:
        if rule.name in seen:
            raise ConfigError(f"duplicate approval rule name {rule.name!r}")
        seen.add(rule.name)
    for upstream in settings.upstreams:
        if not upstream.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"upstream {upstream.provider!r} base_url must be http(s), got {upstream.base_url!r}")


def load_settings(
    path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> Settings:
    resolved_env: Mapping[str, str] = env if env is not None else os.environ
    config_path = path
    if config_path is None and "BENCHPRESS_CONFIG" in resolved_env:
        config_path = Path(resolved_env["BENCHPRESS_CONFIG"])

    merged: dict[str, object] = {}
    if config_path is not None:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        merged.update(_parse_file(data, config_path.parent, resolved_env))

    _apply_env(merged, resolved_env)
    if overrides:
        _apply_overrides(merged, overrides)

    settings = replace(Settings(), **cast(dict[str, Any], merged))
    _validate(settings)
    return settings
