"""Self-explaining error envelope: every rejection carries a machine-readable
`code` and a human-readable `detail`, per the contract's self-explaining
principle.
"""

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, detail: str, extra: dict | None = None) -> None:
        self.status_code = status_code
        self.code = code
        self.detail = detail
        # Structured detail beyond the human-readable `detail` string -- e.g. a
        # per-item breakdown of which deposit events failed and how to recover.
        # Merged alongside `code`/`detail` in the response, never overriding them
        # -- `extra` is spread first below so `code`/`detail` are applied last
        # and always win on key collision.
        self.extra = extra or {}
        super().__init__(detail)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {**exc.extra, "code": exc.code, "detail": exc.detail}},
    )
