from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from feedback_lens.paths import PROJECT_ROOT


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive.")
    return value


@dataclass(frozen=True)
class WebSettings:
    upload_root: Path
    document_limit_bytes: int
    zip_limit_bytes: int
    zip_entry_limit: int
    zip_uncompressed_limit_bytes: int
    zip_compression_ratio_limit: int
    mail_backend: str
    public_base_url: str
    activation_ttl_hours: int
    password_reset_ttl_minutes: int
    identity_rate_limit_per_hour: int
    source_rate_limit_per_hour: int
    job_stale_seconds: int


def get_web_settings() -> WebSettings:
    upload_root = Path(
        os.environ.get(
            "FEEDBACK_LENS_UPLOAD_ROOT",
            str(PROJECT_ROOT / "uploads"),
        )
    ).expanduser()
    runtime_environment = os.environ.get(
        "FEEDBACK_LENS_ENV",
        "development",
    ).strip().lower()
    default_mail_backend = (
        "disabled"
        if runtime_environment in {"production", "prod"}
        else "console"
    )
    mail_backend = os.environ.get(
        "FEEDBACK_LENS_MAIL_BACKEND",
        default_mail_backend,
    ).strip().lower()
    if mail_backend not in {"memory", "console", "smtp", "disabled"}:
        raise RuntimeError(
            "FEEDBACK_LENS_MAIL_BACKEND must be memory, console, smtp, "
            "or disabled."
        )
    return WebSettings(
        upload_root=upload_root,
        document_limit_bytes=_positive_int(
            "FEEDBACK_LENS_DOCUMENT_LIMIT_BYTES",
            25 * 1024 * 1024,
        ),
        zip_limit_bytes=_positive_int(
            "FEEDBACK_LENS_ZIP_LIMIT_BYTES",
            1024 * 1024 * 1024,
        ),
        zip_entry_limit=_positive_int(
            "FEEDBACK_LENS_ZIP_ENTRY_LIMIT",
            5000,
        ),
        zip_uncompressed_limit_bytes=_positive_int(
            "FEEDBACK_LENS_ZIP_UNCOMPRESSED_LIMIT_BYTES",
            4 * 1024 * 1024 * 1024,
        ),
        zip_compression_ratio_limit=_positive_int(
            "FEEDBACK_LENS_ZIP_COMPRESSION_RATIO_LIMIT",
            100,
        ),
        mail_backend=mail_backend,
        public_base_url=os.environ.get(
            "FEEDBACK_LENS_PUBLIC_BASE_URL",
            "http://127.0.0.1:5001",
        ).rstrip("/"),
        activation_ttl_hours=_positive_int(
            "FEEDBACK_LENS_ACTIVATION_TTL_HOURS",
            72,
        ),
        password_reset_ttl_minutes=_positive_int(
            "FEEDBACK_LENS_PASSWORD_RESET_TTL_MINUTES",
            60,
        ),
        identity_rate_limit_per_hour=_positive_int(
            "FEEDBACK_LENS_IDENTITY_RATE_LIMIT",
            5,
        ),
        source_rate_limit_per_hour=_positive_int(
            "FEEDBACK_LENS_SOURCE_RATE_LIMIT",
            30,
        ),
        job_stale_seconds=_positive_int(
            "FEEDBACK_LENS_JOB_STALE_SECONDS",
            300,
        ),
    )


def get_secret_key() -> str:
    key = os.environ.get("FEEDBACK_LENS_SECRET_KEY")
    if key:
        return key
    return "dev_secret_key"
