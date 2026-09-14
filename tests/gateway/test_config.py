from __future__ import annotations

from pathlib import Path

import pytest

from benchpress.gateway.config import ConfigError, Settings, is_loopback, load_settings

TOML = """
[server]
port = 9000
policy_dir = "policies"

[approvals]
ttl_seconds = 60
webhook_url = "https://hooks.example/bp"
webhook_secret = "env:HOOK_SECRET"

[[approvals.rules]]
name = "stripe-writes"
provider = "stripe"
methods = ["POST"]
path = "/v1/customers/*"

[[upstreams]]
provider = "hubspot"
base_url = "https://api.hubapi.com"
token = "env:HUBSPOT_TOKEN"
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "benchpress.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_are_local_and_safe() -> None:
    settings = load_settings(env={})
    assert settings == Settings()
    assert settings.host == "127.0.0.1" and settings.auth == "api_key"


def test_file_env_and_overrides_layer_in_order(tmp_path: Path) -> None:
    path = _write(tmp_path, TOML)
    env = {"HUBSPOT_TOKEN": "pat-123", "HOOK_SECRET": "s3cret", "BENCHPRESS_PORT": "9100"}
    settings = load_settings(path, env=env, overrides={"store": "sqlite:///other.db"})
    assert settings.port == 9100 and settings.store == "sqlite:///other.db"
    assert settings.policy_dir == tmp_path / "policies"
    assert settings.approval_ttl_seconds == 60 and settings.approval_webhook_secret == "s3cret"
    assert settings.approval_rules[0].name == "stripe-writes" and settings.approval_rules[0].methods == ("POST",)
    assert settings.upstreams[0].token == "pat-123"
    assert "pat-123" not in repr(settings) and "s3cret" not in repr(settings)


def test_config_path_from_env(tmp_path: Path) -> None:
    path = _write(tmp_path, "[server]\nport = 9200\n")
    assert load_settings(env={"BENCHPRESS_CONFIG": str(path)}).port == 9200


@pytest.mark.parametrize(
    ("text", "env", "needle"),
    [
        ("[server]\nhost = \"0.0.0.0\"\nauth = \"none\"\n", {}, "auth"),
        ("[[upstreams]]\nprovider = \"hubspot\"\nbase_url = \"https://h\"\ntoken = \"pat-inline\"\n", {}, "env:"),
        ("[[upstreams]]\nprovider = \"hubspot\"\nbase_url = \"https://h\"\ntoken = \"env:MISSING\"\n", {}, "MISSING"),
        ("[limits]\nrequests_per_minute = 0\n", {}, "requests_per_minute"),
        ("[server]\nporty = 1\n", {}, "porty"),
        ("[[upstreams]]\nprovider = \"hubspot\"\nbase_url = \"ftp://h\"\n", {}, "base_url"),
    ],
)
def test_invalid_config_names_the_problem(tmp_path: Path, text: str, env: dict[str, str], needle: str) -> None:
    with pytest.raises(ConfigError, match=needle):
        load_settings(_write(tmp_path, text), env=env)


def test_is_loopback() -> None:
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1")
    assert not is_loopback("0.0.0.0") and not is_loopback("10.0.0.5")
