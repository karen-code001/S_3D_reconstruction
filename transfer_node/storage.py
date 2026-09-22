import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile


SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


class UploadTooLarge(ValueError):
    pass


@dataclass(frozen=True)
class StoredUpload:
    path: Path
    size: int
    sha256: str


async def store_upload(file: UploadFile, task_dir: Path, max_bytes: int) -> StoredUpload:
    task_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "video.bin").suffix
    suffix = suffix.lower() if SAFE_SUFFIX.fullmatch(suffix) else ".bin"
    temporary = task_dir / "source.part"
    destination = task_dir / f"source{suffix}"
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLarge(f"upload exceeds {max_bytes} bytes")
                digest.update(chunk)
                output.write(chunk)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return StoredUpload(path=destination, size=size, sha256=digest.hexdigest())


def task_source(task_dir: Path) -> Path | None:
    return next((path for path in task_dir.glob("source.*") if path.name != "source.part"), None)

