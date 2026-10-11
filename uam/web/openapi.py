"""Print the API's OpenAPI schema as JSON (`make openapi` writes it to docs/openapi.json)."""

from __future__ import annotations

import json

from uam.web.api import create_api_app


def main() -> None:
    """Write the schema to stdout."""
    print(json.dumps(create_api_app().openapi(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
