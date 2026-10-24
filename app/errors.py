"""API 오류 형식 (api.md 1.5): {"error": {"code", "message", "details"}}."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str = "",
        details: dict | None = None,
        headers: dict | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message or code
        self.details = details or {}
        self.headers = headers


def not_found(what: str = "") -> ApiError:
    return ApiError(404, "not_found", f"{what} 없음" if what else "없음")


def validation(field: str, problem: str) -> ApiError:
    return ApiError(
        422,
        "validation_error",
        "입력 검증 실패",
        {"fields": [{"field": field, "problem": problem}]},
    )


def _body(code: str, message: str, details: dict) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


_STATUS_CODES = {
    401: "unauthorized",
    403: "forbidden_role",
    404: "not_found",
    405: "not_found",
    413: "photo_too_large",
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        return JSONResponse(
            status_code=e.status,
            content=_body(e.code, e.message, e.details),
            headers=e.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        fields = [
            {
                "field": ".".join(str(p) for p in err["loc"] if p not in ("body", "query", "path")),
                "problem": err["msg"],
            }
            for err in e.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_body("validation_error", "입력 검증 실패", {"fields": fields}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException):
        code = _STATUS_CODES.get(e.status_code, "internal_error")
        return JSONResponse(status_code=e.status_code, content=_body(code, str(e.detail), {}))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, e: Exception):
        return JSONResponse(status_code=500, content=_body("internal_error", "서버 오류", {}))
