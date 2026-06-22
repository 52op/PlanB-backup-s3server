from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import posixpath
import shutil
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler
from typing import Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

from .auth import check_auth, safe_ak
from .config import AppConfig
from .logger import log
from .responses import (
    S3_NS,
    build_delete_result_xml,
    send_access_denied,
    send_invalid_bucket_name,
    send_invalid_uri,
    send_no_such_bucket,
    send_no_such_key,
    send_s3_error,
    send_xml_response,
)


def _md5_file_hex(path: str, chunk_size: int = 1024 * 1024) -> str:
    """
    Compute md5 for a file in streaming mode to avoid loading whole file into memory.
    """
    m = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            m.update(chunk)
    return m.hexdigest()


def _format_http_gmt(ts: float) -> str:
    return datetime.datetime.utcfromtimestamp(ts).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _format_iso8601_utc(ts: float) -> str:
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_etag_quotes(etag_value: str) -> str:
    v = (etag_value or "").strip()
    if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    return v


def _parse_if_none_match(header_value: str) -> list[str]:
    raw = (header_value or "").strip()
    if not raw:
        return []
    if raw == "*":
        return ["*"]
    parts = []
    for p in raw.split(","):
        token = _strip_etag_quotes(p.strip())
        if token:
            parts.append(token)
    return parts


def _if_none_match_hit(header_value: str, current_etag: str) -> bool:
    candidates = _parse_if_none_match(header_value)
    if not candidates:
        return False
    if "*" in candidates:
        return True
    return current_etag in candidates


def _parse_http_date_to_utc(header_value: str) -> Optional[datetime.datetime]:
    if not header_value:
        return None
    try:
        dt = parsedate_to_datetime(header_value)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt


def _if_modified_since_not_modified(header_value: str, mtime_ts: float) -> bool:
    dt = _parse_http_date_to_utc(header_value)
    if dt is None:
        return False
    obj_dt = datetime.datetime.fromtimestamp(
        mtime_ts, tz=datetime.timezone.utc
    ).replace(microsecond=0)
    return obj_dt <= dt


def _encode_continuation_token(last_key: str) -> str:
    return base64.urlsafe_b64encode(last_key.encode("utf-8")).decode("ascii")


def _decode_continuation_token(token: str) -> str:
    # Add missing padding for urlsafe base64
    pad = "=" * ((4 - len(token) % 4) % 4)
    raw = base64.urlsafe_b64decode((token + pad).encode("ascii"))
    return raw.decode("utf-8")


def _build_copy_object_result_xml(etag: str, last_modified_ts: float) -> bytes:
    last_modified = _format_iso8601_utc(last_modified_ts)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<CopyObjectResult>"
        f"<LastModified>{xml_escape(last_modified)}</LastModified>"
        f"<ETag>&quot;{xml_escape(etag)}&quot;</ETag>"
        "</CopyObjectResult>"
    ).encode("utf-8")


def _parse_copy_source_header(copy_source: str) -> Optional[Tuple[str, str]]:
    """
    Parse x-amz-copy-source header.
    Expected:
    - /src-bucket/src-key
    - src-bucket/src-key
    Query string (e.g. versionId) is ignored.
    """
    if not copy_source:
        return None
    raw = copy_source.strip()
    if not raw:
        return None
    raw = raw.lstrip("/")
    if "?" in raw:
        raw = raw.split("?", 1)[0]
    raw = urllib.parse.unquote(raw)
    if "/" not in raw:
        return None
    src_bucket, src_key = raw.split("/", 1)
    if not src_bucket or not src_key:
        return None
    return src_bucket, src_key


def _meta_sidecar_path(obj_path: str) -> str:
    return f"{obj_path}.meta.json"


def _extract_amz_meta_headers(headers) -> dict[str, str]:
    """
    Extract user metadata from request headers:
    x-amz-meta-<key>: <value>
    """
    meta: dict[str, str] = {}
    for name in headers.keys():
        if not name:
            continue
        lower = name.lower()
        if not lower.startswith("x-amz-meta-"):
            continue
        meta_key = lower[len("x-amz-meta-") :].strip()
        if not meta_key:
            continue
        meta[meta_key] = headers.get(name, "")
    return meta


def _load_sidecar_metadata(obj_path: str) -> dict[str, str]:
    meta_path = _meta_sidecar_path(obj_path)
    if not os.path.isfile(meta_path):
        return {}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            out: dict[str, str] = {}
            for k, v in data.items():
                out[str(k)] = "" if v is None else str(v)
            return out
    except Exception:
        return {}
    return {}


def _save_sidecar_metadata(obj_path: str, metadata: dict[str, str]) -> None:
    meta_path = _meta_sidecar_path(obj_path)
    if not metadata:
        # Keep clean when no metadata
        if os.path.exists(meta_path):
            try:
                os.remove(meta_path)
            except Exception:
                pass
        return
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)


def _copy_sidecar_metadata(src_obj_path: str, dst_obj_path: str) -> None:
    src_meta = _meta_sidecar_path(src_obj_path)
    dst_meta = _meta_sidecar_path(dst_obj_path)
    if os.path.isfile(src_meta):
        shutil.copy2(src_meta, dst_meta)
    elif os.path.exists(dst_meta):
        try:
            os.remove(dst_meta)
        except Exception:
            pass


def _parse_single_range_header(
    range_header: str, size: int
) -> Optional[Tuple[int, int]]:
    """
    Parse HTTP Range header for bytes, single range only.
    Supported forms:
    - bytes=START-END
    - bytes=START-
    - bytes=-SUFFIX_LEN
    Returns (start, end) inclusive, or None if invalid/unsupported.
    """
    if not range_header:
        return None
    value = range_header.strip()
    if not value.startswith("bytes="):
        return None

    spec = value[len("bytes=") :].strip()
    # We only support single-range; reject multipart ranges.
    if "," in spec:
        return None

    if "-" not in spec:
        return None

    start_str, end_str = spec.split("-", 1)
    start_str = start_str.strip()
    end_str = end_str.strip()

    if size < 0:
        return None

    # bytes=-SUFFIX
    if start_str == "":
        if end_str == "":
            return None
        try:
            suffix_len = int(end_str)
        except ValueError:
            return None
        if suffix_len <= 0:
            return None
        if size == 0:
            return None
        if suffix_len >= size:
            return 0, size - 1
        return size - suffix_len, size - 1

    # bytes=START- or bytes=START-END
    try:
        start = int(start_str)
    except ValueError:
        return None
    if start < 0:
        return None
    if start >= size:
        return None

    if end_str == "":
        return start, size - 1

    try:
        end = int(end_str)
    except ValueError:
        return None
    if end < start:
        return None
    if end >= size:
        end = size - 1
    return start, end


# ------------------------- Multipart upload helpers ------------------------- #

MULTIPART_DIR = ".uploads"


def _upload_parts_dir(data_dir: str, bucket: str, upload_id: str) -> str:
    return os.path.join(data_dir, bucket, MULTIPART_DIR, upload_id)


def _upload_meta_path(data_dir: str, bucket: str, upload_id: str) -> str:
    return os.path.join(data_dir, bucket, MULTIPART_DIR, f"{upload_id}.meta")


def _build_create_multipart_result_xml(bucket: str, key: str, upload_id: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<CreateMultipartUploadResult>"
        f"<Bucket>{xml_escape(bucket)}</Bucket>"
        f"<Key>{xml_escape(key)}</Key>"
        f"<UploadId>{xml_escape(upload_id)}</UploadId>"
        "</CreateMultipartUploadResult>"
    )
    return xml.encode("utf-8")


def _build_list_parts_result_xml(
    bucket: str, key: str, upload_id: str, parts: list[tuple[int, str, int]],
    max_parts: int, part_number_marker: int,
) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<ListPartsResult>",
        f"<Bucket>{xml_escape(bucket)}</Bucket>",
        f"<Key>{xml_escape(key)}</Key>",
        f"<UploadId>{xml_escape(upload_id)}</UploadId>",
        f"<MaxParts>{max_parts}</MaxParts>",
        f"<PartNumberMarker>{part_number_marker}</PartNumberMarker>",
        "<IsTruncated>false</IsTruncated>",
    ]
    for pnum, etag, size in parts:
        lines.append("<Part>")
        lines.append(f"<PartNumber>{pnum}</PartNumber>")
        lines.append(f"<ETag>{xml_escape(etag)}</ETag>")
        lines.append(f"<Size>{size}</Size>")
        lines.append("</Part>")
    lines.append("</ListPartsResult>")
    return "".join(lines).encode("utf-8")


def _build_list_multipart_uploads_result_xml(
    bucket: str, uploads: list[tuple[str, str, str]],
) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<ListMultipartUploadsResult>",
        f"<Bucket>{xml_escape(bucket)}</Bucket>",
        "<IsTruncated>false</IsTruncated>",
    ]
    for key, upload_id, initiated in uploads:
        lines.append("<Upload>")
        lines.append(f"<Key>{xml_escape(key)}</Key>")
        lines.append(f"<UploadId>{xml_escape(upload_id)}</UploadId>")
        lines.append(f"<Initiated>{xml_escape(initiated)}</Initiated>")
        lines.append("</Upload>")
    lines.append("</ListMultipartUploadsResult>")
    return "".join(lines).encode("utf-8")


def _build_complete_multipart_result_xml(
    bucket: str, key: str, etag: str,
) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<CompleteMultipartUploadResult>"
        f"<Bucket>{xml_escape(bucket)}</Bucket>"
        f"<Key>{xml_escape(key)}</Key>"
        f"<ETag>{xml_escape(etag)}</ETag>"
        "</CompleteMultipartUploadResult>"
    )
    return xml.encode("utf-8")


class S3Handler(BaseHTTPRequestHandler):
    """
    Modular S3-like request handler.

    Supported operations:
    - GET /bucket/key          -> get object (supports single-range request)
    - GET /bucket?prefix=...   -> list objects (ListObjectsV2-like pagination)
    - PUT /bucket              -> create bucket
    - PUT /bucket/key          -> put object
    - DELETE /bucket/key       -> delete object
    - DELETE /bucket           -> delete empty bucket
    - HEAD /bucket/key         -> object metadata check
    """

    server_version = "AmazonS3"

    # set by create_s3_handler()
    config: Optional[AppConfig] = None

    def log_message(self, format: str, *args) -> None:
        # Disable default BaseHTTPRequestHandler logging.
        return

    # ------------------------- Common helpers ------------------------- #

    @property
    def cfg(self) -> AppConfig:
        if self.__class__.config is None:
            raise RuntimeError("S3Handler.config is not set")
        return self.__class__.config

    def _log(self, msg: str) -> None:
        log(msg, self.cfg.server.log_file)

    def _client_ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For", "").strip()
        if xff:
            return xff.split(",")[0].strip()
        if self.client_address and len(self.client_address) > 0:
            return str(self.client_address[0])
        return "-"

    def _log_request_start(self) -> None:
        ua = self.headers.get("User-Agent", "-")
        self._log(
            f"REQ start ip={self._client_ip()} method={self.command} path={self.path} ua={ua}"
        )

    def _auth_or_reject(self) -> bool:
        result = check_auth(
            handler=self,
            access_key=self.cfg.auth.access_key,
            secret_key=self.cfg.auth.secret_key,
            require_sigv4=self.cfg.security.require_sigv4,
            allow_v2=self.cfg.security.allow_v2,
            max_skew_seconds=self.cfg.security.max_skew_seconds,
            allow_unsigned_payload=self.cfg.security.allow_unsigned_payload,
        )
        if result.ok:
            self._log(
                f"AUTH ok ip={self._client_ip()} method={self.command} auth={result.auth_type} "
                f"ak={safe_ak(result.access_key)} reason={result.reason}"
            )
            return True

        self._log(
            f"AUTH fail ip={self._client_ip()} method={self.command} auth={result.auth_type} "
            f"ak={safe_ak(result.access_key)} reason={result.reason} path={self.path}"
        )
        send_access_denied(self)
        return False

    def _parse_bucket_key(self) -> Tuple[str, str]:
        parsed = urllib.parse.urlsplit(self.path)
        decoded = urllib.parse.unquote(parsed.path or "")
        clean = decoded.lstrip("/")
        if clean == "":
            return "", ""
        if "/" not in clean:
            return clean, ""
        bucket, key = clean.split("/", 1)
        return bucket, key

    def _bucket_dir(self, bucket: str) -> str:
        return os.path.abspath(os.path.join(self.cfg.server.data_dir, bucket))

    def _safe_join_bucket_key(self, bucket: str, key: str = "") -> Tuple[str, str]:
        """
        Return (bucket_dir, target_path) and enforce traversal safety.
        """
        bucket = (bucket or "").strip()
        if not bucket:
            raise ValueError("invalid bucket")
        if "/" in bucket or "\\" in bucket or bucket in {".", ".."}:
            raise ValueError("invalid bucket")

        data_dir = os.path.abspath(self.cfg.server.data_dir)
        bucket_dir = self._bucket_dir(bucket)

        if not bucket_dir.startswith(data_dir + os.sep):
            raise ValueError("invalid bucket path")

        key = "" if key is None else key
        key_posix = key.replace("\\", "/").lstrip("/")
        key_norm = posixpath.normpath(key_posix)

        if key_norm in {"", "."}:
            target = bucket_dir
        else:
            if key_norm == ".." or key_norm.startswith("../"):
                raise ValueError("invalid key traversal")
            rel_os = key_norm.replace("/", os.sep)
            target = os.path.abspath(os.path.join(bucket_dir, rel_os))

        if not (target == bucket_dir or target.startswith(bucket_dir + os.sep)):
            raise ValueError("invalid target path")

        return bucket_dir, target

    def _read_request_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        transfer_encoding = (self.headers.get("Transfer-Encoding", "") or "").lower()

        if length > 0:
            data = bytearray()
            while len(data) < length:
                chunk = self.rfile.read(length - len(data))
                if not chunk:
                    break
                data.extend(chunk)
            return bytes(data)

        if "chunked" in transfer_encoding:
            return self._read_chunked_body()

        return b""

    def _read_chunked_body(self) -> bytes:
        """
        解码 AWS S3 签名 V4 分块传输编码。
        格式: <hex-size>;chunk-signature=<sig>\r\n<data>\r\n...
        """
        result = bytearray()
        while True:
            size_line = self._read_line()
            if not size_line:
                break
            # 格式: "95b;chunk-signature=xxx" 或 "0;chunk-signature=xxx"
            size_str = size_line.split(";")[0].strip()
            try:
                chunk_size = int(size_str, 16)
            except ValueError:
                break
            if chunk_size == 0:
                # 读取最终的空 chunk-signature 行
                self._read_line()
                break
            chunk_data = self._read_exact(chunk_size)
            result.extend(chunk_data)
            self._read_line()  # 消费 \r\n
            self._read_line()  # 消费下一个 chunk-signature 行 (如果有的话, 但可能已经在下一轮读取)
        return bytes(result)

    def _read_line(self) -> str:
        """从 rfile 读取一行 (到 \\n), 返回去除 \\r\\n 的字符串。"""
        line = b""
        while True:
            b = self.rfile.read(1)
            if not b:
                break
            if b == b"\n":
                break
            line += b
        return line.decode("utf-8", errors="replace").rstrip("\r")

    def _read_exact(self, n: int) -> bytes:
        """精确读取 n 个字节。"""
        data = bytearray()
        while len(data) < n:
            chunk = self.rfile.read(n - len(data))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    # ------------------------- Response builders ------------------------- #

    def _send_list_bucket_result_v2(
        self,
        bucket: str,
        prefix: str,
        max_keys: int,
        key_count: int,
        is_truncated: bool,
        next_continuation_token: str,
        continuation_token: str,
        start_after: str,
        contents: list[dict],
    ) -> None:
        content_xml = []
        for item in contents:
            content_xml.append(
                "<Contents>"
                f"<Key>{xml_escape(item['key'])}</Key>"
                f"<LastModified>{item['last_modified']}</LastModified>"
                f'<ETag>"{item["etag"]}"</ETag>'
                f"<Size>{item['size']}</Size>"
                "<StorageClass>STANDARD</StorageClass>"
                "</Contents>"
            )

        next_token_xml = (
            f"<NextContinuationToken>{xml_escape(next_continuation_token)}</NextContinuationToken>"
            if next_continuation_token
            else ""
        )
        continuation_xml = (
            f"<ContinuationToken>{xml_escape(continuation_token)}</ContinuationToken>"
            if continuation_token
            else ""
        )
        start_after_xml = (
            f"<StartAfter>{xml_escape(start_after)}</StartAfter>" if start_after else ""
        )

        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<ListBucketResult xmlns="{S3_NS}">'
            f"<Name>{xml_escape(bucket)}</Name>"
            f"<Prefix>{xml_escape(prefix)}</Prefix>"
            f"<KeyCount>{key_count}</KeyCount>"
            f"<MaxKeys>{max_keys}</MaxKeys>"
            "<Delimiter></Delimiter>"
            "<EncodingType>url</EncodingType>"
            f"<IsTruncated>{'true' if is_truncated else 'false'}</IsTruncated>"
            f"{continuation_xml}"
            f"{next_token_xml}"
            f"{start_after_xml}"
            f"{''.join(content_xml)}"
            "</ListBucketResult>"
        ).encode("utf-8")

        send_xml_response(self, 200, body)
        self._log(
            "S3 LIST ok "
            f"bucket={bucket} prefix={prefix} key_count={key_count} "
            f"max_keys={max_keys} is_truncated={is_truncated}"
        )

    # ------------------------- HTTP methods ------------------------- #

    def do_HEAD(self) -> None:
        self._log_request_start()
        if not self._auth_or_reject():
            return

        bucket, key = self._parse_bucket_key()
        if not bucket:
            send_s3_error(self, 404, "NotFound", "Not Found")
            self._log(f"S3 HEAD miss path={self.path}")
            return

        # HeadBucket: HEAD /{bucket} with no key
        if not key:
            try:
                bucket_dir, _ = self._safe_join_bucket_key(bucket, "")
            except ValueError:
                send_invalid_bucket_name(self)
                self._log(f"S3 HEAD bucket invalid bucket={bucket}")
                return
            if not os.path.isdir(bucket_dir):
                send_no_such_bucket(self, bucket)
                self._log(f"S3 HEAD bucket miss bucket={bucket}")
                return
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self._log(f"S3 HEAD bucket ok bucket={bucket}")
            return

        try:
            _, fp = self._safe_join_bucket_key(bucket, key)
        except ValueError as e:
            send_invalid_uri(self, str(e))
            self._log(f"S3 HEAD invalid path bucket={bucket} key={key} err={e}")
            return

        if not os.path.isfile(fp):
            send_no_such_key(self, bucket, key)
            self._log(f"S3 HEAD miss bucket={bucket} key={key}")
            return

        size = os.path.getsize(fp)
        mtime_ts = os.path.getmtime(fp)
        mtime = _format_http_gmt(mtime_ts)
        etag = _md5_file_hex(fp)
        stored_meta = _load_sidecar_metadata(fp)

        if_match = self.headers.get("If-Match", "").strip()
        if_none_match = self.headers.get("If-None-Match", "").strip()
        if_modified_since = self.headers.get("If-Modified-Since", "").strip()

        if if_match:
            if if_match != "*" and not _if_none_match_hit(if_match, etag):
                send_s3_error(
                    self,
                    412,
                    "PreconditionFailed",
                    "At least one of the pre-conditions you specified did not hold.",
                )
                self._log(
                    f"S3 HEAD precondition failed If-Match bucket={bucket} key={key} if_match={if_match} etag={etag}"
                )
                return

        if if_none_match and _if_none_match_hit(if_none_match, etag):
            self.send_response(304)
            self.send_header("ETag", f'"{etag}"')
            self.send_header("Last-Modified", mtime)
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self._log(
                f"S3 HEAD not modified by If-None-Match bucket={bucket} key={key} etag={etag}"
            )
            return

        if if_modified_since and _if_modified_since_not_modified(
            if_modified_since, mtime_ts
        ):
            self.send_response(304)
            self.send_header("ETag", f'"{etag}"')
            self.send_header("Last-Modified", mtime)
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self._log(
                f"S3 HEAD not modified by If-Modified-Since bucket={bucket} key={key} if_modified_since={if_modified_since}"
            )
            return

        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.send_header("Last-Modified", mtime)
        self.send_header("ETag", f'"{etag}"')
        self.send_header("Accept-Ranges", "bytes")
        for meta_k, meta_v in stored_meta.items():
            self.send_header(f"x-amz-meta-{meta_k}", meta_v)
        self.end_headers()
        self._log(f"S3 HEAD hit bucket={bucket} key={key} size={size}")

    # ------------------------- Multipart upload handlers ------------------------- #

    def _uploads_dir(self) -> str:
        return os.path.join(self.cfg.server.data_dir, MULTIPART_DIR)

    def _upload_parts_dir(self, bucket: str, upload_id: str) -> str:
        return os.path.join(self._uploads_dir(), bucket, upload_id)

    def _upload_meta_path(self, bucket: str, upload_id: str) -> str:
        return os.path.join(self._uploads_dir(), bucket, f"{upload_id}.meta")

    def _handle_create_multipart_upload(self, bucket: str, key: str) -> None:
        upload_id = uuid.uuid4().hex
        meta = {
            "key": key,
            "upload_id": upload_id,
            "initiated": _format_iso8601_utc(time.time()),
        }
        meta_dir = os.path.join(self._uploads_dir(), bucket)
        os.makedirs(meta_dir, exist_ok=True)
        with open(self._upload_meta_path(bucket, upload_id), "w") as f:
            json.dump(meta, f)
        body = _build_create_multipart_result_xml(bucket, key, upload_id)
        send_xml_response(self, 200, body)
        self._log(f"S3 MPU create ok bucket={bucket} key={key} uploadId={upload_id}")

    def _handle_upload_part(self, bucket: str, key: str, upload_id: str, part_number: int) -> None:
        meta_path = self._upload_meta_path(bucket, upload_id)
        if not os.path.isfile(meta_path):
            send_s3_error(self, 404, "NoSuchUpload", "The specified upload does not exist.")
            self._log(f"S3 MPU upload-part no-such-upload bucket={bucket} key={key} uploadId={upload_id}")
            return
        part_dir = self._upload_parts_dir(bucket, upload_id)
        os.makedirs(part_dir, exist_ok=True)
        part_path = os.path.join(part_dir, str(part_number))
        data = self._read_request_body()
        with open(part_path, "wb") as f:
            f.write(data)
        etag = hashlib.md5(data).hexdigest()
        self.send_response(200)
        self.send_header("ETag", f'"{etag}"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        self._log(f"S3 MPU upload-part ok bucket={bucket} key={key} part={part_number} bytes={len(data)} uploadId={upload_id}")

    def _handle_complete_multipart_upload(self, bucket: str, key: str, upload_id: str) -> None:
        meta_path = self._upload_meta_path(bucket, upload_id)
        if not os.path.isfile(meta_path):
            send_s3_error(self, 404, "NoSuchUpload", "The specified upload does not exist.")
            return
        body = self._read_request_body()
        if not body.strip():
            send_s3_error(self, 400, "MalformedXML", "The XML you provided was not well-formed.")
            return
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            send_s3_error(self, 400, "MalformedXML", "The XML you provided was not well-formed.")
            return
        tag = root.tag.split("}")[-1]
        ns = {"s3": S3_NS} if "}" in root.tag else {}
        def _find_text(parent, child_tag):
            if ns:
                elem = parent.find(f"s3:{child_tag}", ns)
            else:
                elem = parent.find(child_tag)
            return elem.text if elem is not None and elem.text else ""
        if tag != "CompleteMultipartUpload":
            send_s3_error(self, 400, "MalformedXML", "Expected <CompleteMultipartUpload>.")
            return
        part_elems = root.findall("s3:Part", ns) if ns else root.findall("Part")
        if not part_elems:
            send_s3_error(self, 400, "MalformedXML", "At least one <Part> is required.")
            return
        parts = []
        for pe in part_elems:
            pn_str = _find_text(pe, "PartNumber")
            etag_str = _find_text(pe, "ETag")
            try:
                pn = int(pn_str)
            except (ValueError, TypeError):
                continue
            parts.append((pn, etag_str))
        parts.sort(key=lambda x: x[0])
        part_dir = self._upload_parts_dir(bucket, upload_id)
        tmp_path = None
        try:
            _, final_path = self._safe_join_bucket_key(bucket, key)
        except ValueError:
            send_invalid_uri(self, "Invalid key.")
            return
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        tmp_path = final_path + ".mpu_tmp"
        try:
            with open(tmp_path, "wb") as out:
                for pn, _ in parts:
                    pp = os.path.join(part_dir, str(pn))
                    if not os.path.isfile(pp):
                        send_s3_error(self, 400, "InvalidPart", f"Part {pn} was not uploaded.")
                        self._cleanup_upload(meta_path, part_dir, tmp_path)
                        return
                    with open(pp, "rb") as f:
                        while True:
                            chunk = f.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
            os.replace(tmp_path, final_path)
            tmp_path = None
        except Exception:
            if tmp_path and os.path.isfile(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            raise
        etag = _md5_file_hex(final_path)
        self._cleanup_upload(meta_path, part_dir, None)
        body = _build_complete_multipart_result_xml(bucket, key, f'"{etag}"')
        self._log(f"S3 MPU complete ok bucket={bucket} key={key} parts={len(parts)} uploadId={upload_id}")
        send_xml_response(self, 200, body)

    def _cleanup_upload(self, meta_path: str, part_dir: str, tmp_path: str | None) -> None:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        if os.path.isdir(part_dir):
            try:
                shutil.rmtree(part_dir)
            except Exception:
                pass
        if os.path.isfile(meta_path):
            try:
                os.remove(meta_path)
            except Exception:
                pass

    def _handle_abort_multipart_upload(self, bucket: str, key: str, upload_id: str) -> None:
        meta_path = self._upload_meta_path(bucket, upload_id)
        if not os.path.isfile(meta_path):
            send_s3_error(self, 404, "NoSuchUpload", "The specified upload does not exist.")
            return
        part_dir = self._upload_parts_dir(bucket, upload_id)
        self._cleanup_upload(meta_path, part_dir, None)
        self.send_response(204)
        self.end_headers()
        self._log(f"S3 MPU abort ok bucket={bucket} key={key} uploadId={upload_id}")

    def _handle_list_parts(self, bucket: str, key: str, upload_id: str) -> None:
        meta_path = self._upload_meta_path(bucket, upload_id)
        if not os.path.isfile(meta_path):
            send_s3_error(self, 404, "NoSuchUpload", "The specified upload does not exist.")
            return
        part_dir = self._upload_parts_dir(bucket, upload_id)
        parts = []
        if os.path.isdir(part_dir):
            for fname in os.listdir(part_dir):
                pp = os.path.join(part_dir, fname)
                if os.path.isfile(pp):
                    try:
                        pn = int(fname)
                    except ValueError:
                        continue
                    etag = _md5_file_hex(pp)
                    size = os.path.getsize(pp)
                    parts.append((pn, f'"{etag}"', size))
        parts.sort(key=lambda x: x[0])
        body = _build_list_parts_result_xml(
            bucket, key, upload_id, parts, 1000, 0,
        )
        self._log(f"S3 MPU list-parts ok bucket={bucket} key={key} parts={len(parts)} uploadId={upload_id}")
        send_xml_response(self, 200, body)

    def _handle_list_multipart_uploads(self, bucket: str) -> None:
        meta_dir = os.path.join(self._uploads_dir(), bucket)
        uploads = []
        if os.path.isdir(meta_dir):
            for fname in os.listdir(meta_dir):
                if fname.endswith(".meta"):
                    mp = os.path.join(meta_dir, fname)
                    try:
                        with open(mp) as f:
                            meta = json.load(f)
                        uploads.append((
                            meta.get("key", ""),
                            meta.get("upload_id", ""),
                            meta.get("initiated", ""),
                        ))
                    except Exception:
                        pass
        body = _build_list_multipart_uploads_result_xml(bucket, uploads)
        self._log(f"S3 MPU list-uploads ok bucket={bucket} uploads={len(uploads)}")
        send_xml_response(self, 200, body)

    def do_GET(self) -> None:
        self._log_request_start()
        if not self._auth_or_reject():
            return

        parsed = urllib.parse.urlsplit(self.path)
        bucket, key = self._parse_bucket_key()

        # ListParts: GET /{bucket}/{key}?uploadId=xxx
        if bucket and key:
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if "uploadId" in query:
                self._handle_list_parts(bucket, key, query["uploadId"][0])
                return

        # ListMultipartUploads: GET /{bucket}?uploads
        if bucket and not key:
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if "uploads" in query:
                self._handle_list_multipart_uploads(bucket)
                return

        # GET object
        if bucket and key:
            try:
                _, fp = self._safe_join_bucket_key(bucket, key)
            except ValueError as e:
                send_invalid_uri(self, str(e))
                self._log(
                    f"S3 GET object invalid path bucket={bucket} key={key} err={e}"
                )
                return

            if not os.path.isfile(fp):
                send_no_such_key(self, bucket, key)
                self._log(f"S3 GET object miss bucket={bucket} key={key}")
                return

            size = os.path.getsize(fp)
            mtime_ts = os.path.getmtime(fp)
            etag = _md5_file_hex(fp)
            mtime = _format_http_gmt(mtime_ts)
            stored_meta = _load_sidecar_metadata(fp)

            if_match = self.headers.get("If-Match", "").strip()
            if_none_match = self.headers.get("If-None-Match", "").strip()
            if_modified_since = self.headers.get("If-Modified-Since", "").strip()

            if if_match:
                if if_match != "*" and not _if_none_match_hit(if_match, etag):
                    send_s3_error(
                        self,
                        412,
                        "PreconditionFailed",
                        "At least one of the pre-conditions you specified did not hold.",
                    )
                    self._log(
                        f"S3 GET object precondition failed If-Match bucket={bucket} key={key} if_match={if_match} etag={etag}"
                    )
                    return

            if if_none_match and _if_none_match_hit(if_none_match, etag):
                self.send_response(304)
                self.send_header("ETag", f'"{etag}"')
                self.send_header("Last-Modified", mtime)
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self._log(
                    f"S3 GET object not modified by If-None-Match bucket={bucket} key={key} etag={etag}"
                )
                return

            if if_modified_since and _if_modified_since_not_modified(
                if_modified_since, mtime_ts
            ):
                self.send_response(304)
                self.send_header("ETag", f'"{etag}"')
                self.send_header("Last-Modified", mtime)
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self._log(
                    f"S3 GET object not modified by If-Modified-Since bucket={bucket} key={key} if_modified_since={if_modified_since}"
                )
                return

            range_header = self.headers.get("Range", "").strip()

            if range_header:
                parsed_range = _parse_single_range_header(range_header, size)
                if parsed_range is None:
                    # RFC 7233: invalid/unsatisfiable -> 416 + Content-Range: bytes */size
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("ETag", f'"{etag}"')
                    self.send_header("Last-Modified", mtime)
                    self.end_headers()
                    self._log(
                        f"S3 GET object range invalid bucket={bucket} key={key} range={range_header} size={size}"
                    )
                    return

                start, end = parsed_range
                length = end - start + 1

                self.send_response(206)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", f'"{etag}"')
                self.send_header("Last-Modified", mtime)
                for meta_k, meta_v in stored_meta.items():
                    self.send_header(f"x-amz-meta-{meta_k}", meta_v)
                self.end_headers()

                with open(fp, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)

                self._log(
                    f"S3 GET object range ok bucket={bucket} key={key} range={start}-{end}/{size}"
                )
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("ETag", f'"{etag}"')
            self.send_header("Last-Modified", mtime)
            self.send_header("Accept-Ranges", "bytes")
            for meta_k, meta_v in stored_meta.items():
                self.send_header(f"x-amz-meta-{meta_k}", meta_v)
            self.end_headers()

            with open(fp, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

            self._log(f"S3 GET object ok bucket={bucket} key={key} size={size}")
            return

        # LIST bucket (ListObjectsV2-like)
        if not bucket:
            send_no_such_bucket(self, "")
            self._log("S3 LIST miss empty bucket")
            return

        try:
            bucket_dir, _ = self._safe_join_bucket_key(bucket, "")
        except ValueError as e:
            send_invalid_bucket_name(self, str(e))
            self._log(f"S3 LIST invalid bucket bucket={bucket} err={e}")
            return

        if not os.path.isdir(bucket_dir):
            send_no_such_bucket(self, bucket)
            self._log(f"S3 LIST bucket miss bucket={bucket}")
            return

        query = urllib.parse.parse_qs(parsed.query or "")
        prefix = urllib.parse.unquote(query.get("prefix", [""])[0])

        list_type = query.get("list-type", [""])[0]
        # Keep behavior for non-v2 callers, but serve v2-compatible response anyway.
        is_v2 = list_type == "2" or list_type == ""

        max_keys_raw = query.get("max-keys", ["1000"])[0]
        try:
            max_keys = int(max_keys_raw)
        except ValueError:
            max_keys = 1000
        if max_keys < 0:
            max_keys = 0
        if max_keys > 1000:
            max_keys = 1000

        continuation_token = query.get("continuation-token", [""])[0]
        start_after = urllib.parse.unquote(query.get("start-after", [""])[0])

        if continuation_token and start_after:
            # S3 allows both in some contexts but continuation-token has priority;
            # keep deterministic behavior by ignoring start-after when token exists.
            start_after = ""

        resume_after_key = ""
        if continuation_token:
            try:
                resume_after_key = _decode_continuation_token(continuation_token)
            except Exception:
                send_invalid_uri(self, "Invalid continuation-token")
                self._log(
                    f"S3 LIST invalid continuation token bucket={bucket} token={continuation_token}"
                )
                return
        elif start_after:
            resume_after_key = start_after

        all_items: list[dict] = []
        for root, _, files in os.walk(bucket_dir):
            for fname in files:
                # Hide internal sidecar metadata files from S3 list results
                if fname.endswith(".meta.json"):
                    continue
                full = os.path.join(root, fname)
                rel_key = os.path.relpath(full, bucket_dir).replace("\\", "/")
                if prefix and not rel_key.startswith(prefix):
                    continue
                st = os.stat(full)
                all_items.append(
                    {
                        "key": rel_key,
                        "size": st.st_size,
                        "last_modified": _format_iso8601_utc(st.st_mtime),
                        "etag": _md5_file_hex(full),
                    }
                )

        # S3 lexicographic order by key
        all_items.sort(key=lambda x: x["key"])

        filtered: list[dict] = []
        if resume_after_key:
            for item in all_items:
                if item["key"] > resume_after_key:
                    filtered.append(item)
        else:
            filtered = all_items

        page = filtered[:max_keys] if max_keys >= 0 else filtered
        key_count = len(page)
        is_truncated = len(filtered) > key_count

        next_token = ""
        if is_truncated and key_count > 0:
            next_token = _encode_continuation_token(page[-1]["key"])

        if not is_v2:
            # Fallback for old list callers: still respond success with v2 shape.
            self._log(
                f"S3 LIST non-v2 query fallback bucket={bucket} query={parsed.query}"
            )

        self._send_list_bucket_result_v2(
            bucket=bucket,
            prefix=prefix,
            max_keys=max_keys,
            key_count=key_count,
            is_truncated=is_truncated,
            next_continuation_token=next_token,
            continuation_token=continuation_token,
            start_after=start_after,
            contents=page,
        )

    def do_PUT(self) -> None:
        self._log_request_start()
        if not self._auth_or_reject():
            return

        bucket, key = self._parse_bucket_key()
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        if not bucket:
            send_invalid_bucket_name(self, "Bucket name is required.")
            self._log("S3 PUT invalid empty bucket")
            return

        # UploadPart: PUT /{bucket}/{key}?uploadId=xxx&partNumber=N
        if key and "uploadId" in query and "partNumber" in query:
            try:
                pn = int(query["partNumber"][0])
            except (ValueError, TypeError):
                send_s3_error(self, 400, "InvalidArgument", "partNumber must be an integer.")
                return
            self._handle_upload_part(bucket, key, query["uploadId"][0], pn)
            return

        # Create bucket
        if not key:
            try:
                bucket_dir, _ = self._safe_join_bucket_key(bucket, "")
            except ValueError as e:
                send_invalid_bucket_name(self, str(e))
                self._log(f"S3 PUT bucket invalid bucket={bucket} err={e}")
                return

            os.makedirs(bucket_dir, exist_ok=True)
            self.send_response(200)
            self.end_headers()
            self._log(f"S3 PUT bucket create ok bucket={bucket}")
            return

        # Put object / CopyObject
        try:
            bucket_dir, dest = self._safe_join_bucket_key(bucket, key)
        except ValueError as e:
            send_invalid_uri(self, str(e))
            self._log(f"S3 PUT object invalid path bucket={bucket} key={key} err={e}")
            return

        os.makedirs(bucket_dir, exist_ok=True)
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        copy_source = self.headers.get("x-amz-copy-source", "").strip()
        metadata_directive = (
            self.headers.get("x-amz-metadata-directive", "COPY").strip().upper()
        )
        request_meta = _extract_amz_meta_headers(self.headers)

        copy_metadata_written = False
        if copy_source:
            parsed_src = _parse_copy_source_header(copy_source)
            if parsed_src is None:
                send_invalid_uri(self, "Invalid x-amz-copy-source")
                self._log(
                    f"S3 COPY object invalid source bucket={bucket} key={key} copy_source={copy_source}"
                )
                return

            src_bucket, src_key = parsed_src
            try:
                _, src_path = self._safe_join_bucket_key(src_bucket, src_key)
            except ValueError as e:
                send_invalid_uri(self, str(e))
                self._log(
                    f"S3 COPY object invalid source path src_bucket={src_bucket} src_key={src_key} err={e}"
                )
                return

            if not os.path.isfile(src_path):
                send_no_such_key(self, src_bucket, src_key)
                self._log(
                    f"S3 COPY object source miss src_bucket={src_bucket} src_key={src_key}"
                )
                return

            # Copy bytes and preserve timestamps for a closer S3-like metadata behavior
            shutil.copy2(src_path, dest)

            if metadata_directive == "REPLACE":
                _save_sidecar_metadata(dest, request_meta)
            else:
                _copy_sidecar_metadata(src_path, dest)
            copy_metadata_written = True

            etag = _md5_file_hex(dest)
            mtime_ts = os.path.getmtime(dest)
            body = _build_copy_object_result_xml(etag, mtime_ts)
            send_xml_response(
                self,
                200,
                body,
                extra_headers={
                    "ETag": f'"{etag}"',
                },
            )

            self._log(
                "S3 COPY object ok "
                f"src_bucket={src_bucket} src_key={src_key} "
                f"dst_bucket={bucket} dst_key={key} directive={metadata_directive} "
                f"meta_count={len(request_meta)} dest={dest}"
            )
            return

        body = self._read_request_body()
        with open(dest, "wb") as f:
            f.write(body)

        if not copy_metadata_written:
            _save_sidecar_metadata(dest, request_meta)

        etag = hashlib.md5(body).hexdigest()
        self.send_response(200)
        self.send_header("ETag", f'"{etag}"')
        self.end_headers()

        self._log(
            "S3 PUT object ok "
            f"bucket={bucket} key={key} bytes={len(body)} "
            f"meta_count={len(request_meta)} dest={dest}"
        )

    def do_DELETE(self) -> None:
        self._log_request_start()
        if not self._auth_or_reject():
            return

        bucket, key = self._parse_bucket_key()
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        if not bucket:
            send_invalid_bucket_name(self, "Bucket name is required.")
            self._log("S3 DELETE invalid empty bucket")
            return

        # AbortMultipartUpload: DELETE /{bucket}/{key}?uploadId=xxx
        if key and "uploadId" in query:
            self._handle_abort_multipart_upload(bucket, key, query["uploadId"][0])
            return

        # DELETE object
        if key:
            try:
                _, fp = self._safe_join_bucket_key(bucket, key)
            except ValueError as e:
                send_invalid_uri(self, str(e))
                self._log(
                    f"S3 DELETE object invalid path bucket={bucket} key={key} err={e}"
                )
                return

            if not os.path.isfile(fp):
                # S3 delete object is idempotent; return 204 even if key doesn't exist.
                self.send_response(204)
                self.end_headers()
                self._log(
                    f"S3 DELETE object noop bucket={bucket} key={key} (not found)"
                )
                return

            os.remove(fp)
            try:
                meta_path = _meta_sidecar_path(fp)
                if os.path.isfile(meta_path):
                    os.remove(meta_path)
            except Exception:
                pass
            self.send_response(204)
            self.end_headers()
            self._log(f"S3 DELETE object ok bucket={bucket} key={key}")
            return

        # DELETE bucket (must be empty)
        try:
            bucket_dir, _ = self._safe_join_bucket_key(bucket, "")
        except ValueError as e:
            send_invalid_bucket_name(self, str(e))
            self._log(f"S3 DELETE bucket invalid bucket={bucket} err={e}")
            return

        if not os.path.isdir(bucket_dir):
            send_no_such_bucket(self, bucket)
            self._log(f"S3 DELETE bucket miss bucket={bucket}")
            return

        # Ignore empty directories and internal metadata sidecar files when checking emptiness.
        has_real_objects = False
        for root, dirs, files in os.walk(bucket_dir):
            # Keep traversal deterministic and safe for in-place pruning
            dirs[:] = [d for d in dirs if d not in {".", ".."}]

            # Any non-sidecar file means bucket is not empty
            for fname in files:
                if not fname.endswith(".meta.json"):
                    has_real_objects = True
                    break
            if has_real_objects:
                break

        if has_real_objects:
            send_s3_error(
                self,
                409,
                "BucketNotEmpty",
                "The bucket you tried to delete is not empty.",
                resource=f"/{bucket}",
            )
            self._log(f"S3 DELETE bucket failed not empty bucket={bucket}")
            return

        # Remove leftover sidecar files and now-empty directories bottom-up.
        for root, dirs, files in os.walk(bucket_dir, topdown=False):
            for fname in files:
                if fname.endswith(".meta.json"):
                    try:
                        os.remove(os.path.join(root, fname))
                    except Exception:
                        pass
            for dname in dirs:
                dpath = os.path.join(root, dname)
                try:
                    if not os.listdir(dpath):
                        os.rmdir(dpath)
                except Exception:
                    pass

        try:
            os.rmdir(bucket_dir)
        except Exception:
            # If something still remains (unexpected), preserve S3-compatible error
            send_s3_error(
                self,
                409,
                "BucketNotEmpty",
                "The bucket you tried to delete is not empty.",
                resource=f"/{bucket}",
            )
            self._log(f"S3 DELETE bucket failed not empty bucket={bucket}")
            return

        self.send_response(204)
        self.end_headers()
        self._log(f"S3 DELETE bucket ok bucket={bucket}")

    def do_POST(self) -> None:
        self._log_request_start()
        if not self._auth_or_reject():
            return

        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        bucket, key = self._parse_bucket_key()

        # CompleteMultipartUpload: POST /{bucket}/{key}?uploadId=xxx
        if key and "uploadId" in params:
            self._handle_complete_multipart_upload(bucket, key, params["uploadId"][0])
            return

        # CreateMultipartUpload: POST /{bucket}/{key}?uploads
        if key and "uploads" in params:
            self._handle_create_multipart_upload(bucket, key)
            return

        # RemoveObjects (batch delete): POST /{bucket}?delete
        if "delete" in params:
            self._handle_batch_delete(parsed.path)
            return

        send_s3_error(self, 501, "NotImplemented", "POST operation is not implemented.")
        self._log(f"S3 POST not implemented path={self.path}")

    def _handle_batch_delete(self, raw_path: str) -> None:
        bucket, key = self._parse_bucket_key()
        if not bucket:
            send_invalid_bucket_name(self, "Bucket name is required.")
            self._log("S3 POST batch-delete invalid empty bucket")
            return

        try:
            body = self._read_request_body()
        except Exception:
            send_s3_error(self, 400, "MalformedXML", "The XML you provided was not well-formed.")
            return

        if not body.strip():
            send_s3_error(self, 400, "MalformedXML", "The XML you provided was not well-formed.")
            return

        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            send_s3_error(self, 400, "MalformedXML", "The XML you provided was not well-formed.")
            return

        tag = root.tag.split("}")[-1]
        ns = {"s3": S3_NS} if "}" in root.tag else {}

        if tag != "Delete":
            send_s3_error(self, 400, "MalformedXML", f"Expected <Delete> root element, got <{root.tag}>.")
            return

        def _find_text(parent, child_tag):
            """Find child text with optional namespace."""
            if ns:
                elem = parent.find(f"s3:{child_tag}", ns)
            else:
                elem = parent.find(child_tag)
            return elem.text if elem is not None and elem.text else ""

        quiet = _find_text(root, "Quiet")
        quiet_mode = quiet.strip().lower() == "true"

        objects = root.findall("s3:Object", ns) if ns else root.findall("Object")
        if not objects:
            send_s3_error(self, 400, "MalformedXML", "At least one <Object> is required.")
            return

        if len(objects) > 1000:
            send_s3_error(
                self, 400, "MalformedXML",
                "The batch delete request may not contain more than 1000 objects."
            )
            return

        deleted_keys: list[str] = []
        errors: list[tuple[str, str]] = []

        for obj in objects:
            key_val = _find_text(obj, "Key")
            if not key_val:
                errors.append(("", "InvalidArgument"))
                continue
            obj_key = key_val.strip()
            if not obj_key:
                errors.append(("", "InvalidArgument"))
                continue

            try:
                _, fp = self._safe_join_bucket_key(bucket, obj_key)
            except ValueError:
                errors.append((obj_key, "InvalidArgument"))
                continue

            if not os.path.isfile(fp):
                # S3 delete is idempotent — report as deleted even if missing
                deleted_keys.append(obj_key)
                continue

            try:
                os.remove(fp)
                meta_path = _meta_sidecar_path(fp)
                if os.path.isfile(meta_path):
                    os.remove(meta_path)
            except Exception:
                errors.append((obj_key, "InternalError"))
                continue

            deleted_keys.append(obj_key)

        body = build_delete_result_xml(deleted_keys, errors)

        if quiet_mode:
            quiet_body = ['<?xml version="1.0" encoding="UTF-8"?><DeleteResult>']
            for _key in deleted_keys:
                quiet_body.append("<Deleted></Deleted>")
            for key, code in errors:
                quiet_body.append(
                    f"<Error><Key>{xml_escape(key)}</Key><Code>{xml_escape(code)}</Code>"
                    f"<Message>{xml_escape(code)}</Message></Error>"
                )
            quiet_body.append("</DeleteResult>")
            body = "".join(quiet_body).encode("utf-8")

        send_xml_response(self, 200, body)
        self._log(
            f"S3 POST batch-delete ok bucket={bucket} "
            f"deleted={len(deleted_keys)} errors={len(errors)}"
        )


def create_s3_handler(cfg: AppConfig) -> type[S3Handler]:
    """
    Factory that binds runtime AppConfig into handler class.
    """

    class ConfiguredS3Handler(S3Handler):
        config = cfg

    return ConfiguredS3Handler
