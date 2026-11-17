from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class DomainError(ValueError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class Unauthenticated(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class AccessDenied(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class NotFound(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class Conflict(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def _error(status: int, exc):
    return JSONResponse(status_code=status, content={"error": exc.code, "message": exc.message})


def install(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return _error(422, exc)

    @app.exception_handler(Unauthenticated)
    async def unauthenticated(request: Request, exc: Unauthenticated):
        return _error(401, exc)

    @app.exception_handler(AccessDenied)
    async def access_denied(request: Request, exc: AccessDenied):
        return _error(403, exc)

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound):
        return _error(404, exc)

    @app.exception_handler(Conflict)
    async def conflict(request: Request, exc: Conflict):
        return _error(409, exc)
