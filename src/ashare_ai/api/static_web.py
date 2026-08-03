"""Static SPA serving used by the native Windows entry point."""

from __future__ import annotations

from pathlib import Path

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class NativeSPAStaticFiles(StaticFiles):
    """Serve hashed assets and fall back to the SPA entry for client routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path == "" or path.startswith("api/"):
                raise
            if self.directory is None:
                raise
            index = Path(self.directory) / "index.html"
            if not index.is_file():
                raise
            return FileResponse(index)
