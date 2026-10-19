"""The helper container's entry point: `python -m uam.self_update --target ID --image REF`.

It replaces UAM's container with one on the new image (or rolls back) and writes the outcome as
JSON to `--result`, which the next UAM process reads at startup.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from uam.self_update.engine import SdkEngineApi
from uam.self_update.recreate import RecreateResult, recreate

log = logging.getLogger("uam.self_update")


def main(argv: list[str] | None = None) -> int:
    """Run the recreate; exit 0 when UAM runs the new image, 1 otherwise."""
    parser = argparse.ArgumentParser(prog="python -m uam.self_update")
    parser.add_argument("--target", required=True, help="id or name of UAM's container")
    parser.add_argument("--image", required=True, help="image ref the new container should run")
    parser.add_argument("--result", help="where to write the JSON result")
    parser.add_argument("--delay", type=float, default=3.0, help="seconds to wait first")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("UAM_LOG_LEVEL", "INFO"), stream=sys.stderr)
    # Give UAM a moment to answer the request that started the update before it is stopped.
    time.sleep(args.delay)
    try:
        result = recreate(SdkEngineApi(), args.target, args.image)
    except Exception as exc:  # noqa: BLE001 - still report something
        log.exception("self-update failed")
        result = RecreateResult(False, f"self-update failed: {exc}")
    log.info("self-update finished: %s", result.message)
    if args.result:
        path = Path(args.result)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result.to_dict()), encoding="utf-8")
        tmp.replace(path)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
