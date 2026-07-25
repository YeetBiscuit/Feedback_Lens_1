from __future__ import annotations


class ApiError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def to_dict(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}
