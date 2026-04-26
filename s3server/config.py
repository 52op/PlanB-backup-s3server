import configparser
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    data_dir: str
    log_file: str


@dataclass(frozen=True)
class AuthConfig:
    access_key: str
    secret_key: str


@dataclass(frozen=True)
class SecurityConfig:
    require_sigv4: bool
    allow_v2: bool
    max_skew_seconds: int
    allow_unsigned_payload: bool


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    auth: AuthConfig
    security: SecurityConfig


def _as_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return default


def _read_int(
    parser: configparser.ConfigParser, section: str, key: str, default: int
) -> int:
    try:
        return parser.getint(section, key)
    except Exception:
        return default


def load_config(config_path: str = "config.ini") -> AppConfig:
    parser = configparser.ConfigParser()

    # Defaults
    parser["SERVER"] = {
        "host": "0.0.0.0",
        "port": "4431",
        "data_dir": "./s3data",
        "log_file": "s3server.log",
    }
    parser["AUTH"] = {
        "access_key": "s3admin",
        "secret_key": "12345678",
    }
    parser["SECURITY"] = {
        "require_sigv4": "true",
        "allow_v2": "false",
        "max_skew_seconds": "900",
        "allow_unsigned_payload": "false",
    }

    parser.read(config_path, encoding="utf-8")

    host = parser.get("SERVER", "host", fallback="0.0.0.0").strip() or "0.0.0.0"
    port = _read_int(parser, "SERVER", "port", 4431)
    if port <= 0 or port > 65535:
        port = 4431

    data_dir_raw = (
        parser.get("SERVER", "data_dir", fallback="./s3data").strip() or "./s3data"
    )
    data_dir = os.path.abspath(data_dir_raw)

    log_file_raw = (
        parser.get("SERVER", "log_file", fallback="s3server.log").strip()
        or "s3server.log"
    )
    log_file = os.path.abspath(log_file_raw)

    access_key = parser.get("AUTH", "access_key", fallback="s3admin").strip()
    secret_key = parser.get("AUTH", "secret_key", fallback="12345678").strip()

    if not access_key:
        raise ValueError("AUTH.access_key cannot be empty")
    if not secret_key:
        raise ValueError("AUTH.secret_key cannot be empty")

    require_sigv4 = _as_bool(
        parser.get("SECURITY", "require_sigv4", fallback="true"), True
    )
    allow_v2 = _as_bool(parser.get("SECURITY", "allow_v2", fallback="false"), False)
    max_skew_seconds = _read_int(parser, "SECURITY", "max_skew_seconds", 900)
    if max_skew_seconds < 0:
        max_skew_seconds = 900
    allow_unsigned_payload = _as_bool(
        parser.get("SECURITY", "allow_unsigned_payload", fallback="false"), False
    )

    os.makedirs(data_dir, exist_ok=True)

    return AppConfig(
        server=ServerConfig(
            host=host,
            port=port,
            data_dir=data_dir,
            log_file=log_file,
        ),
        auth=AuthConfig(
            access_key=access_key,
            secret_key=secret_key,
        ),
        security=SecurityConfig(
            require_sigv4=require_sigv4,
            allow_v2=allow_v2,
            max_skew_seconds=max_skew_seconds,
            allow_unsigned_payload=allow_unsigned_payload,
        ),
    )
