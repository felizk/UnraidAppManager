import json
import logging

import pytest

from uam.logging import JsonFormatter, redact
from uam.settings import Settings


@pytest.mark.parametrize(
    ("raw", "leak"),
    [
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789 used", "ghp_abc"),
        ("github_pat_11ABCDEFG0123456789_abcdefghijklmnop", "github_pat_11"),
        ("Authorization: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("url?password=hunter2&x=1", "hunter2"),
        ('{"secret": "s3cr3t"}', "s3cr3t"),
    ],
)
def test_redact_hides_secrets(raw, leak):
    assert leak not in redact(raw)


def test_redact_keeps_normal_text():
    assert redact("pulled ghcr.io/felizk/notes:latest") == "pulled ghcr.io/felizk/notes:latest"


def test_json_formatter_redacts():
    record = logging.LogRecord("uam", logging.INFO, __file__, 1, "using %s", ("ghp_secretsecret123",), None)
    out = json.loads(JsonFormatter().format(record))
    assert out["level"] == "info" and "ghp_secret" not in out["msg"]


def test_settings_round_trip_ignores_unknown_keys():
    s = Settings(port_range_start=40000, reserved_ports=[40001])
    data = s.to_dict() | {"removed_in_future": 1}
    assert Settings.from_dict(data) == s
