import os
import posixpath
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional, Tuple


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    path: str
    size: int
    mtime: float


class StoragePathError(ValueError):
    """Raised when bucket/key cannot be safely resolved under the data directory."""


def _ensure_abs(path: str) -> str:
    return os.path.abspath(path)


def _is_within(child_abs: str, parent_abs: str) -> bool:
    try:
        common = os.path.commonpath([child_abs, parent_abs])
    except ValueError:
        # Different drives on Windows, definitely not within
        return False
    return common == parent_abs


def normalize_bucket(bucket: str) -> str:
    """
    Basic bucket validation for local filesystem mapping safety.
    This is not a full AWS bucket-name validator; it focuses on path safety.
    """
    if bucket is None:
        raise StoragePathError("bucket is required")
    b = bucket.strip()
    if not b:
        raise StoragePathError("bucket is empty")
    if "/" in b or "\\" in b:
        raise StoragePathError("bucket cannot contain path separators")
    if b in {".", ".."}:
        raise StoragePathError("bucket is invalid")
    if "\x00" in b:
        raise StoragePathError("bucket contains null byte")
    return b


def normalize_key(key: Optional[str]) -> str:
    """
    Normalize object key to a safe relative path form:
    - convert backslashes to slashes
    - strip leading slash
    - collapse dot segments
    """
    if key is None:
        return ""
    if "\x00" in key:
        raise StoragePathError("key contains null byte")
    k = key.replace("\\", "/").lstrip("/")
    norm = posixpath.normpath(k)

    # posixpath.normpath("") -> "."
    if norm == ".":
        return ""

    # traversal check after normalization
    if norm == ".." or norm.startswith("../"):
        raise StoragePathError("key traversal is not allowed")
    return norm


def split_bucket_key(raw_path: str) -> Tuple[str, str]:
    """
    Convert URL path style '/bucket/key' (or 'bucket/key') into (bucket, key).
    """
    if raw_path is None:
        raise StoragePathError("path is required")
    clean = raw_path.strip().lstrip("/")
    if not clean:
        return "", ""
    if "/" not in clean:
        return clean, ""
    bucket, key = clean.split("/", 1)
    return bucket, key


def resolve_bucket_dir(data_dir: str, bucket: str) -> str:
    """
    Resolve and validate the absolute bucket directory path.
    """
    root = _ensure_abs(data_dir)
    b = normalize_bucket(bucket)
    bucket_dir = _ensure_abs(os.path.join(root, b))
    if not _is_within(bucket_dir, root):
        raise StoragePathError("resolved bucket directory escapes data dir")
    return bucket_dir


def resolve_object_path(data_dir: str, bucket: str, key: str) -> Tuple[str, str]:
    """
    Resolve object path safely under data_dir/bucket.
    Returns (bucket_dir_abs, object_path_abs)
    """
    bucket_dir = resolve_bucket_dir(data_dir, bucket)
    norm_key = normalize_key(key)

    if not norm_key:
        return bucket_dir, bucket_dir

    rel_os = norm_key.replace("/", os.sep)
    obj_path = _ensure_abs(os.path.join(bucket_dir, rel_os))
    if not _is_within(obj_path, bucket_dir):
        raise StoragePathError("resolved object path escapes bucket directory")
    return bucket_dir, obj_path


def ensure_bucket(data_dir: str, bucket: str) -> str:
    """
    Ensure bucket directory exists and return its absolute path.
    """
    bucket_dir = resolve_bucket_dir(data_dir, bucket)
    os.makedirs(bucket_dir, exist_ok=True)
    return bucket_dir


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def iter_objects(
    data_dir: str,
    bucket: str,
    prefix: str = "",
) -> Iterator[ObjectInfo]:
    """
    Iterate objects in a bucket with optional key prefix filtering.
    """
    bucket_dir = resolve_bucket_dir(data_dir, bucket)
    if not os.path.isdir(bucket_dir):
        return

    pfx = normalize_key(prefix) if prefix else ""
    for root, _, files in os.walk(bucket_dir):
        for filename in files:
            full = os.path.join(root, filename)
            rel = os.path.relpath(full, bucket_dir).replace("\\", "/")
            if pfx and not rel.startswith(pfx):
                continue
            st = os.stat(full)
            yield ObjectInfo(
                key=rel,
                path=full,
                size=st.st_size,
                mtime=st.st_mtime,
            )


def read_object_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def write_object_bytes(path: str, data: bytes) -> None:
    ensure_parent_dir(path)
    with open(path, "wb") as f:
        f.write(data)
