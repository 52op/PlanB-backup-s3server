import datetime
import hashlib
import hmac
import urllib.parse
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    auth_type: str
    access_key: str
    reason: str


def auth_type_from_header(auth_header: str) -> str:
    if not auth_header:
        return "NONE"
    if auth_header.startswith("AWS4-HMAC-SHA256"):
        return "V4"
    if auth_header.startswith("AWS "):
        return "V2"
    return "UNKNOWN"


def safe_ak(ak: str) -> str:
    if not ak:
        return "-"
    if len(ak) <= 4:
        return "*" * len(ak)
    return ak[:2] + "*" * (len(ak) - 4) + ak[-2:]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def uri_encode(value: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(value, safe=safe)


def _uri_encode_preserving_pct(value: str, safe: str = "-_.~") -> str:
    """
    SigV4 canonicalization requires URI-encoding but must avoid double-encoding
    existing percent-escaped sequences from the incoming request.
    """
    return urllib.parse.quote(value, safe=safe + "%")


def canonical_uri(path: str) -> str:
    if not path:
        return "/"
    return _uri_encode_preserving_pct(path, safe="/-_.~")


def canonical_query(query: str) -> str:
    if not query:
        return ""
    items = []
    for part in query.split("&"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
        else:
            k, v = part, ""
        items.append(
            (
                _uri_encode_preserving_pct(k, safe="-_.~"),
                _uri_encode_preserving_pct(v, safe="-_.~"),
            )
        )
    items.sort(key=lambda x: (x[0], x[1]))
    return "&".join([f"{k}={v}" for k, v in items])


def parse_auth_v4(auth_header: str) -> Optional[Dict[str, str]]:
    """
    Parse:
    AWS4-HMAC-SHA256 Credential=<AK>/<date>/<region>/<service>/aws4_request, SignedHeaders=..., Signature=...
    """
    prefix = "AWS4-HMAC-SHA256 "
    if not auth_header.startswith(prefix):
        return None

    payload = auth_header[len(prefix) :]
    pairs = {}
    for token in payload.split(","):
        token = token.strip()
        if "=" not in token:
            continue
        k, v = token.split("=", 1)
        pairs[k.strip()] = v.strip()

    credential = pairs.get("Credential", "")
    signed_headers = pairs.get("SignedHeaders", "")
    signature = pairs.get("Signature", "").lower()

    if not credential or not signed_headers or not signature:
        return None

    cparts = credential.split("/")
    if len(cparts) != 5:
        return None

    access_key, date_part, region, service, term = cparts
    if term != "aws4_request":
        return None

    return {
        "access_key": access_key,
        "date": date_part,
        "region": region,
        "service": service,
        "signed_headers": signed_headers,
        "signature": signature,
        "credential_scope": f"{date_part}/{region}/{service}/aws4_request",
    }


def build_canonical_headers(headers, signed_headers: str) -> Tuple[str, str]:
    """
    headers: a case-insensitive mapping, e.g., BaseHTTPRequestHandler.headers
    """
    names = [h.strip().lower() for h in signed_headers.split(";") if h.strip()]
    names.sort()

    lines = []
    for name in names:
        value = headers.get(name, "")
        value = " ".join(value.strip().split())
        lines.append(f"{name}:{value}\n")

    return "".join(lines), ";".join(names)


def get_payload_hash(headers, allow_unsigned_payload: bool) -> str:
    v = headers.get("x-amz-content-sha256", "").strip()
    if v:
        if v == "UNSIGNED-PAYLOAD" and not allow_unsigned_payload:
            return ""
        return v
    return ""


def derive_signing_key(
    secret_key: str, date_part: str, region: str, service: str
) -> bytes:
    k_date = hmac.new(
        ("AWS4" + secret_key).encode("utf-8"),
        date_part.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    return k_signing


def parse_amz_date(amz_date: str) -> Optional[datetime.datetime]:
    """
    Expect format: YYYYMMDD'T'HHMMSS'Z'
    """
    if not amz_date:
        return None
    try:
        dt = datetime.datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ")
        return dt.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None


def check_time_skew(amz_date: str, max_skew_seconds: int) -> Tuple[bool, str]:
    dt = parse_amz_date(amz_date)
    if dt is None:
        return False, "invalid x-amz-date"
    now = datetime.datetime.now(datetime.timezone.utc)
    skew = abs((now - dt).total_seconds())
    if skew > max_skew_seconds:
        return False, f"x-amz-date skew too large ({int(skew)}s > {max_skew_seconds}s)"
    return True, "ok"


def verify_sigv4(
    handler,
    parsed_auth: Dict[str, str],
    secret_key: str,
    max_skew_seconds: int = 900,
    allow_unsigned_payload: bool = False,
) -> Tuple[bool, str]:
    method = handler.command.upper()
    parsed_url = urllib.parse.urlsplit(handler.path)

    can_uri = canonical_uri(parsed_url.path or "/")
    can_qs = canonical_query(parsed_url.query or "")

    can_headers, normalized_signed_headers = build_canonical_headers(
        handler.headers, parsed_auth["signed_headers"]
    )

    payload_hash = get_payload_hash(handler.headers, allow_unsigned_payload)
    if not payload_hash:
        return False, "missing/invalid x-amz-content-sha256"

    amz_date = handler.headers.get("x-amz-date", "").strip()
    if not amz_date:
        return False, "missing x-amz-date"

    time_ok, time_reason = check_time_skew(amz_date, max_skew_seconds)
    if not time_ok:
        return False, time_reason

    canonical_request = (
        f"{method}\n"
        f"{can_uri}\n"
        f"{can_qs}\n"
        f"{can_headers}\n"
        f"{normalized_signed_headers}\n"
        f"{payload_hash}"
    )
    canonical_request_hash = sha256_hex(canonical_request.encode("utf-8"))

    string_to_sign = (
        "AWS4-HMAC-SHA256\n"
        f"{amz_date}\n"
        f"{parsed_auth['credential_scope']}\n"
        f"{canonical_request_hash}"
    )

    signing_key = derive_signing_key(
        secret_key=secret_key,
        date_part=parsed_auth["date"],
        region=parsed_auth["region"],
        service=parsed_auth["service"],
    )
    expected = hmac.new(
        signing_key,
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, parsed_auth["signature"]):
        return False, "signature mismatch"

    return True, "ok"


def parse_presigned_params(query: str) -> Optional[Dict[str, str]]:
    """
    Parse pre-signed URL parameters from query string.
    Returns None if not a pre-signed request.
    """
    if "X-Amz-Signature" not in query and "x-amz-signature" not in query:
        return None
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    sig = (params.get("X-Amz-Signature") or params.get("x-amz-signature") or [None])[0]
    if not sig:
        return None
    algorithm = (params.get("X-Amz-Algorithm") or params.get("x-amz-algorithm") or [None])[0]
    credential = (params.get("X-Amz-Credential") or params.get("x-amz-credential") or [None])[0]
    amz_date = (params.get("X-Amz-Date") or params.get("x-amz-date") or [None])[0]
    expires = (params.get("X-Amz-Expires") or params.get("x-amz-expires") or [None])[0]
    signed_headers = (params.get("X-Amz-SignedHeaders") or params.get("x-amz-signedheaders") or [None])[0]
    if not all([algorithm, credential, amz_date, signed_headers, sig]):
        return None
    if algorithm != "AWS4-HMAC-SHA256":
        return None
    cparts = credential.split("/")
    if len(cparts) != 5:
        return None
    access_key, date_part, region, service, term = cparts
    if term != "aws4_request":
        return None
    return {
        "access_key": access_key,
        "date": date_part,
        "region": region,
        "service": service,
        "signed_headers": signed_headers,
        "signature": sig.lower(),
        "credential_scope": f"{date_part}/{region}/{service}/aws4_request",
        "amz_date": amz_date,
        "expires": expires,
    }


def build_canonical_query_from_params(raw_query: str, exclude_keys: set[str]) -> str:
    """
    Build a canonical query string from a raw query string,
    excluding specified keys, with proper URI encoding.
    """
    params = urllib.parse.parse_qs(raw_query, keep_blank_values=True)
    items = []
    for k, values in params.items():
        if k in exclude_keys:
            continue
        for v in values:
            items.append((_uri_encode_preserving_pct(k, safe="-_.~"),
                          _uri_encode_preserving_pct(v, safe="-_.~")))
    items.sort(key=lambda x: (x[0], x[1]))
    return "&".join([f"{k}={v}" for k, v in items])


def verify_presigned_url(
    handler,
    pre_signed: Dict[str, str],
    secret_key: str,
    max_skew_seconds: int = 900,
) -> Tuple[bool, str]:
    method = handler.command.upper()
    parsed_url = urllib.parse.urlsplit(handler.path)

    expires_str = pre_signed["expires"]
    try:
        expires = int(expires_str)
    except (ValueError, TypeError):
        return False, "invalid X-Amz-Expires"
    if expires < 0 or expires > 604800:
        return False, "X-Amz-Expires must be between 0 and 604800"

    time_ok, time_reason = check_time_skew(pre_signed["amz_date"], max_skew_seconds)
    if not time_ok:
        return False, time_reason

    can_qs = build_canonical_query_from_params(
        parsed_url.query or "", {"x-amz-signature", "X-Amz-Signature"}
    )
    can_uri = canonical_uri(parsed_url.path or "/")
    can_headers, normalized_signed_headers = build_canonical_headers(
        handler.headers, pre_signed["signed_headers"]
    )

    payload_hash = "UNSIGNED-PAYLOAD"
    canonical_request = (
        f"{method}\n"
        f"{can_uri}\n"
        f"{can_qs}\n"
        f"{can_headers}\n"
        f"{normalized_signed_headers}\n"
        f"{payload_hash}"
    )
    canonical_request_hash = sha256_hex(canonical_request.encode("utf-8"))

    string_to_sign = (
        "AWS4-HMAC-SHA256\n"
        f"{pre_signed['amz_date']}\n"
        f"{pre_signed['credential_scope']}\n"
        f"{canonical_request_hash}"
    )

    signing_key = derive_signing_key(
        secret_key=secret_key,
        date_part=pre_signed["date"],
        region=pre_signed["region"],
        service=pre_signed["service"],
    )
    expected = hmac.new(
        signing_key,
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, pre_signed["signature"]):
        return False, "signature mismatch (pre-signed)"

    return True, "ok(pre-signed)"


def check_auth(
    handler,
    access_key: str = "",
    secret_key: str = "",
    require_sigv4: bool = True,
    allow_v2: bool = False,
    max_skew_seconds: int = 900,
    allow_unsigned_payload: bool = False,
    app_config=None,
) -> AuthResult:
    if app_config is not None:
        access_key = app_config.auth.access_key
        secret_key = app_config.auth.secret_key
        require_sigv4 = app_config.security.require_sigv4
        allow_v2 = app_config.security.allow_v2
        max_skew_seconds = app_config.security.max_skew_seconds
        allow_unsigned_payload = app_config.security.allow_unsigned_payload

    auth = handler.headers.get("Authorization", "")
    req_auth_type = auth_type_from_header(auth)

    if not auth:
        # Check for pre-signed URL (V4 signature in query params)
        parsed_url = urllib.parse.urlsplit(handler.path)
        pre_signed = parse_presigned_params(parsed_url.query or "")
        if pre_signed:
            ak = pre_signed["access_key"]
            if ak != access_key:
                return AuthResult(False, "V4-PRESIGNED", ak, "access key mismatch")
            ok, reason = verify_presigned_url(
                handler=handler,
                pre_signed=pre_signed,
                secret_key=secret_key,
                max_skew_seconds=max_skew_seconds,
            )
            return AuthResult(ok, "V4-PRESIGNED", ak, reason)
        if require_sigv4:
            return AuthResult(False, req_auth_type, "", "missing Authorization")
        return AuthResult(False, "NONE", "", "no auth")

    # SigV4 full verification
    if auth.startswith("AWS4-HMAC-SHA256"):
        parsed = parse_auth_v4(auth)
        if not parsed:
            return AuthResult(False, req_auth_type, "", "malformed SigV4 Authorization")

        ak = parsed["access_key"]
        if ak != access_key:
            return AuthResult(False, req_auth_type, ak, "access key mismatch")

        ok, reason = verify_sigv4(
            handler=handler,
            parsed_auth=parsed,
            secret_key=secret_key,
            max_skew_seconds=max_skew_seconds,
            allow_unsigned_payload=allow_unsigned_payload,
        )
        return AuthResult(ok, req_auth_type, ak, reason)

    # Optional V2 compatibility mode (AK-only check)
    if auth.startswith("AWS "):
        if not allow_v2 and require_sigv4:
            return AuthResult(False, req_auth_type, "", "v2 disabled; require SigV4")
        try:
            ak = auth.split(" ", 1)[1].split(":", 1)[0]
        except Exception:
            return AuthResult(False, req_auth_type, "", "malformed V2 Authorization")
        if ak == access_key:
            return AuthResult(True, req_auth_type, ak, "ok(v2-ak-only)")
        return AuthResult(False, req_auth_type, ak, "access key mismatch")

    return AuthResult(False, req_auth_type, "", "unsupported Authorization type")
