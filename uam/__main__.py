"""`python -m uam`: serve the app with uvicorn on `0.0.0.0:${UAM_PORT:-8484}`."""

from __future__ import annotations

import os

import uvicorn

from uam.app import create_app
from uam.logging import setup_logging


def main() -> None:
    """Run the server."""
    setup_logging(os.environ.get("UAM_LOG_LEVEL", "INFO"))
    uvicorn.run(
        create_app(),
        host=os.environ.get("UAM_HOST", "0.0.0.0"),  # noqa: S104 - it's a server in a container
        port=int(os.environ.get("UAM_PORT", "8484")),
        log_config=None,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
