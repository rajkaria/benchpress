"""Alembic environment for the gateway store. A regular package (not just data) so `env.py` and
`versions/*.py` ship as ordinary package modules in the built wheel; `script.py.mako` is the one
non-Python file here and needs its own `package-data` entry in `pyproject.toml`.
"""

from __future__ import annotations
