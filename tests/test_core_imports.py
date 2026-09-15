"""The core package stays light: the gateway's dependencies load only when the gateway is imported."""

from __future__ import annotations

import subprocess
import sys

HEAVY = ("fastapi", "starlette", "sqlalchemy", "alembic", "prometheus_client", "opentelemetry", "uvicorn")


def test_import_benchpress_does_not_load_server_dependencies() -> None:
    code = "import sys, benchpress, benchpress.verified, benchpress.loop; print(','.join(sorted(sys.modules)))"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    loaded = set(proc.stdout.strip().split(","))
    offenders = sorted(name for name in loaded if name.split(".")[0] in HEAVY)
    assert offenders == []


def test_span_is_a_no_op_without_opentelemetry() -> None:
    code = (
        "import sys; sys.modules['opentelemetry'] = None\n"
        "from benchpress.telemetry import span\n"
        "with span('x', a=1) as h: h.set('b', 2)\n"
        "print('ok')"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "ok"
