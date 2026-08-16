from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from werkzeug.datastructures import FileStorage

from feedback_lens.web.config import get_web_settings


class UploadValidationError(ValueError):
    pass


@dataclass(frozen=True)
class StoredUpload:
    original_file_name: str
    storage_path: Path
    content_hash: str
    size_bytes: int
    extension: str


MAGIC_PREFIXES = {
    ".pdf": (b"%PDF-",),
    ".zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".xlsx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
}


def validate_relative_archive_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise UploadValidationError("The ZIP contains an unsafe file path.")
    return path


def store_upload(
    file_storage: FileStorage,
    category: str,
    allowed_extensions: set[str],
    size_limit: int | None = None,
) -> StoredUpload:
    if file_storage is None or not file_storage.filename:
        raise UploadValidationError("Choose a file to upload.")
    original_name = Path(file_storage.filename).name
    extension = Path(original_name).suffix.lower()
    if extension not in allowed_extensions:
        allowed = ", ".join(sorted(allowed_extensions))
        raise UploadValidationError(
            f"Unsupported file type. Accepted types: {allowed}."
        )

    settings = get_web_settings()
    limit = size_limit or settings.document_limit_bytes
    target_dir = (
        settings.upload_root
        / category
        / uuid.uuid4().hex
    )
    target_dir.mkdir(parents=True, exist_ok=False)
    target_path = target_dir / f"source{extension}"

    digest = hashlib.sha256()
    size = 0
    prefix = b""
    try:
        with target_path.open("xb") as handle:
            while True:
                chunk = file_storage.stream.read(1024 * 1024)
                if not chunk:
                    break
                if not prefix:
                    prefix = chunk[:8]
                size += len(chunk)
                if size > limit:
                    raise UploadValidationError(
                        f"The uploaded file exceeds the {limit} byte limit."
                    )
                digest.update(chunk)
                handle.write(chunk)
    except Exception:
        target_path.unlink(missing_ok=True)
        try:
            target_dir.rmdir()
        except OSError:
            pass
        raise

    if size == 0:
        target_path.unlink(missing_ok=True)
        target_dir.rmdir()
        raise UploadValidationError("The uploaded file is empty.")
    expected_prefixes = MAGIC_PREFIXES.get(extension)
    if expected_prefixes and not any(
        prefix.startswith(expected) for expected in expected_prefixes
    ):
        target_path.unlink(missing_ok=True)
        target_dir.rmdir()
        raise UploadValidationError(
            f"The file content does not match its {extension} extension."
        )
    return StoredUpload(
        original_file_name=original_name,
        storage_path=target_path,
        content_hash=digest.hexdigest(),
        size_bytes=size,
        extension=extension,
    )


def remove_stored_upload(path: str | Path) -> None:
    target = Path(path).resolve()
    upload_root = get_web_settings().upload_root.resolve()
    if target == upload_root or upload_root not in target.parents:
        raise UploadValidationError("Refusing to remove a file outside uploads.")
    target.unlink(missing_ok=True)
    parent = target.parent
    try:
        parent.rmdir()
    except OSError:
        pass


def safe_archive_target(root: Path, relative_path: PurePosixPath) -> Path:
    root = root.resolve()
    target = root.joinpath(*relative_path.parts).resolve()
    if target == root or root not in target.parents:
        raise UploadValidationError("The ZIP contains an unsafe file path.")
    return target
