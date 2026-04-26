from http.server import ThreadingHTTPServer

from .config import load_config
from .handler import create_s3_handler
from .logger import log


def run(config_path: str = "config.ini") -> None:
    """
    Application entry for running the S3-compatible HTTP server.
    """
    cfg = load_config(config_path)
    handler_cls = create_s3_handler(cfg)

    server = ThreadingHTTPServer((cfg.server.host, cfg.server.port), handler_cls)

    log(
        (
            "S3 server starting "
            f"host={cfg.server.host} port={cfg.server.port} "
            f"data_dir={cfg.server.data_dir} "
            f"require_sigv4={cfg.security.require_sigv4} "
            f"allow_v2={cfg.security.allow_v2} "
            f"max_skew_seconds={cfg.security.max_skew_seconds} "
            f"allow_unsigned_payload={cfg.security.allow_unsigned_payload}"
        ),
        cfg.server.log_file,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("S3 server stopping by keyboard interrupt", cfg.server.log_file)
    finally:
        server.server_close()
        log("S3 server stopped", cfg.server.log_file)


if __name__ == "__main__":
    run()
