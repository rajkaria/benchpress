"""The console static mount: serves the built React app at `/`, or explains how to build it.

`CONSOLE_DIST` is `packages/console`'s build output, copied into the installed package as
`benchpress/console_dist` (Task 12). A checkout without a build yet gets a plain HTML page telling the
reader how to produce one, instead of a 404 or a crash.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.staticfiles import StaticFiles

import benchpress

__all__ = ["CONSOLE_DIST", "mount_console"]

CONSOLE_DIST = Path(benchpress.__file__).parent / "console_dist"

_NOT_BUILT_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Benchpress console</title></head>
<body>
<h1>Benchpress console is not built</h1>
<p>Build it once, then restart the gateway:</p>
<pre>cd packages/console && npm ci && npm run build</pre>
</body>
</html>
"""


def mount_console(app: FastAPI, dist: Path = CONSOLE_DIST) -> None:
    """Serve `dist` (built `index.html` and assets) at `/` with client-side routing, or explain the build.

    Must be the LAST registration on `app`: a `StaticFiles(html=True)` mount at "/" answers every request
    that no earlier route claimed, but Starlette tries routes in registration order, so a mount added here
    would shadow any route added *after* it. Every API route — including Task 11's `/mcp` — must already
    be registered by the time this is called.
    """
    if (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="console")
        return

    @app.get("/", include_in_schema=False)
    async def _console_not_built() -> HTMLResponse:
        return HTMLResponse(_NOT_BUILT_HTML)
