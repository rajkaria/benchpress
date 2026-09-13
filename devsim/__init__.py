"""devsim — local, deterministic twins of the SaaS providers ArgaBench provisions.

Two uses, one code path:

1. A fast, free development substrate for Benchpress: the same `provider_api` surface the
   real apps and Arga twins expose, served on loopback ports.
2. A grader-faithful stand-in for Arga twins so the *unmodified* ArgaBench runner, gateway,
   state capture and grader can run end to end without hosted access (Track D).

Nothing under `devsim/` is imported by `src/benchpress`.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
