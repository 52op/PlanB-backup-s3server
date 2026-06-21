from __future__ import annotations

from http import HTTPStatus
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def _escape(value: object) -> str:
    return xml_escape(
        "" if value is None else str(value), {'"': "&quot;", "'": "&apos;"}
    )


def build_s3_error_xml(
    code: str,
    message: str,
    resource: str = "",
    request_id: str = "0000000000000000",
) -> bytes:
    """
    Build a standard S3-compatible XML error body.

    Example:
    <Error>
      <Code>NoSuchKey</Code>
      <Message>The specified key does not exist.</Message>
      <Resource>/bucket/key</Resource>
      <RequestId>...</RequestId>
    </Error>
    """
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Error>"
        f"<Code>{_escape(code)}</Code>"
        f"<Message>{_escape(message)}</Message>"
        f"<Resource>{_escape(resource)}</Resource>"
        f"<RequestId>{_escape(request_id)}</RequestId>"
        "</Error>"
    )
    return xml.encode("utf-8")


def send_xml_response(
    handler,
    status_code: int,
    body: bytes,
    content_type: str = "application/xml",
    extra_headers: Optional[dict] = None,
) -> None:
    """
    Send a generic XML response through BaseHTTPRequestHandler-compatible object.
    """
    handler.send_response(status_code)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if extra_headers:
        for k, v in extra_headers.items():
            handler.send_header(str(k), str(v))
    handler.end_headers()
    handler.wfile.write(body)


def send_s3_error(
    handler,
    http_status: int,
    code: str,
    message: str,
    resource: Optional[str] = None,
    request_id: Optional[str] = None,
    extra_headers: Optional[dict] = None,
) -> None:
    """
    Send a S3-style XML error response.

    Parameters:
    - handler: BaseHTTPRequestHandler instance
    - http_status: HTTP status code (e.g., 404)
    - code: S3 error code string (e.g., NoSuchKey)
    - message: human readable message
    """
    resource_val = handler.path if resource is None else resource
    request_id_val = "0000000000000000" if request_id is None else request_id
    body = build_s3_error_xml(
        code=code,
        message=message,
        resource=resource_val,
        request_id=request_id_val,
    )
    send_xml_response(
        handler=handler,
        status_code=http_status,
        body=body,
        content_type="application/xml",
        extra_headers=extra_headers,
    )


def send_access_denied(
    handler, resource: Optional[str] = None, request_id: Optional[str] = None
) -> None:
    send_s3_error(
        handler=handler,
        http_status=HTTPStatus.FORBIDDEN,
        code="AccessDenied",
        message="Access Denied",
        resource=resource,
        request_id=request_id,
    )


def send_no_such_bucket(handler, bucket: str, request_id: Optional[str] = None) -> None:
    send_s3_error(
        handler=handler,
        http_status=HTTPStatus.NOT_FOUND,
        code="NoSuchBucket",
        message="The specified bucket does not exist.",
        resource=f"/{bucket}",
        request_id=request_id,
    )


def send_no_such_key(
    handler, bucket: str, key: str, request_id: Optional[str] = None
) -> None:
    send_s3_error(
        handler=handler,
        http_status=HTTPStatus.NOT_FOUND,
        code="NoSuchKey",
        message="The specified key does not exist.",
        resource=f"/{bucket}/{key}",
        request_id=request_id,
    )


def send_invalid_uri(
    handler, message: str = "Could not parse URI.", request_id: Optional[str] = None
) -> None:
    send_s3_error(
        handler=handler,
        http_status=HTTPStatus.BAD_REQUEST,
        code="InvalidURI",
        message=message,
        request_id=request_id,
    )


def send_invalid_bucket_name(
    handler,
    message: str = "The specified bucket is not valid.",
    request_id: Optional[str] = None,
) -> None:
    send_s3_error(
        handler=handler,
        http_status=HTTPStatus.BAD_REQUEST,
        code="InvalidBucketName",
        message=message,
        request_id=request_id,
    )


def build_delete_result_xml(deleted: list[str], errors: list[tuple[str, str]]) -> bytes:
    parts = ['<?xml version="1.0" encoding="UTF-8"?><DeleteResult>']
    for key in deleted:
        parts.append(f"<Deleted><Key>{_escape(key)}</Key></Deleted>")
    for key, code in errors:
        parts.append(
            f"<Error><Key>{_escape(key)}</Key><Code>{_escape(code)}</Code>"
            f"<Message>{_escape(code)}</Message></Error>"
        )
    parts.append("</DeleteResult>")
    return "".join(parts).encode("utf-8")
