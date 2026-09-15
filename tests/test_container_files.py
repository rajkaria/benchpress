"""Static checks on the container and CI definitions (Docker itself runs in CI, not locally)."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_runs_serve_as_non_root_with_a_data_volume() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM node:22" in text and "npm ci" in text and "npm run build" in text
    assert ".[server,postgres]" in text
    assert "USER 10001" in text and 'VOLUME ["/data"]' in text and "EXPOSE 8787" in text
    assert 'ENTRYPOINT ["benchpress", "serve"]' in text
    assert "BENCHPRESS_STORE=sqlite:////data/benchpress.db" in text and "BENCHPRESS_HOST=0.0.0.0" in text
    assert "HEALTHCHECK" in text


def test_dockerignore_keeps_secrets_and_bulk_out() -> None:
    lines = set((ROOT / ".dockerignore").read_text(encoding="utf-8").split())
    for entry in (".env", ".git", ".venv", "node_modules", ".internal-docs", "reports", "site", ".claude"):
        assert entry in lines or f"**/{entry}" in lines, entry


def test_compose_has_a_postgres_profile() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["postgres"]["profiles"] == ["postgres"]
    assert "8787:8787" in services["benchpress"]["ports"]


def test_ci_runs_postgres_console_and_image_jobs() -> None:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = ci["jobs"]
    assert jobs["gateway-postgres"]["services"]["postgres"]["image"].startswith("postgres:16")
    assert any("BENCHPRESS_TEST_POSTGRES_URL" in str(step) for step in jobs["gateway-postgres"]["steps"])
    assert any("git diff --exit-code src/benchpress/console_dist" in str(step) for step in jobs["console"]["steps"])
    assert any("/healthz" in str(step) for step in jobs["image-smoke"]["steps"])


def test_image_workflow_publishes_to_ghcr_on_tags_and_main() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/image.yml").read_text(encoding="utf-8"))
    on = wf.get("on") or wf.get(True)  # PyYAML reads the bare key `on` as True
    assert on["push"]["tags"] == ["v*"] and on["push"]["branches"] == ["main"]
    assert wf["permissions"]["packages"] == "write"
    text = (ROOT / ".github/workflows/image.yml").read_text(encoding="utf-8")
    assert "ghcr.io/rajkaria/benchpress" in text and "linux/amd64,linux/arm64" in text
