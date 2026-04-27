"""uvicorn entry — starts the FastAPI app, HTTPS if cert exists, else HTTP."""
from __future__ import annotations
import logging
import sys
import webbrowser
from pathlib import Path

import uvicorn

from . import paths
from .cert_gen import ensure_cert
from .aws import public_ip


HOST = "0.0.0.0"
HTTPS_PORT = 8443
HTTP_PORT = 8080  # only used if cert generation fails


def _setup_logging() -> None:
    paths.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(paths.LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> None:
    paths.chdir_data()
    _setup_logging()
    log = logging.getLogger("fauji.main")
    log.info("FaujiBot supervisor starting. Install root: %s", paths.INSTALL_ROOT)

    open_browser = "--no-browser" not in sys.argv

    cert = key = None
    try:
        cert, key = ensure_cert()
        port = HTTPS_PORT
        scheme = "https"
    except Exception as e:
        log.warning("HTTPS cert generation failed (%s). Falling back to HTTP.", e)
        port = HTTP_PORT
        scheme = "http"

    local = f"{scheme}://localhost:{port}"
    log.info("Local URL:  %s", local)
    ip = public_ip()
    if ip:
        log.info("Phone URL:  %s://%s:%s   (open AWS Security Group inbound TCP %s)",
                 scheme, ip, port, port)

    if open_browser:
        try:
            webbrowser.open(local, new=2)
        except Exception:
            pass

    uvicorn.run(
        "supervisor.api:app",
        host=HOST,
        port=port,
        ssl_certfile=cert,
        ssl_keyfile=key,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
