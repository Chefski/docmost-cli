"""Regression coverage for the explicit real-instance integration opt-in."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration import test_live_instance

ROOT = Path(__file__).resolve().parents[2]


def test_explicit_integration_opt_in_rejects_missing_core_configuration() -> None:
    env = os.environ.copy()
    for name in (
        "DOCMOST_INTEGRATION_URL",
        "DOCMOST_INTEGRATION_API_KEY",
        "DOCMOST_INTEGRATION_EMAIL",
        "DOCMOST_INTEGRATION_PASSWORD",
    ):
        env.pop(name, None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/integration",
            "--run-docmost-integration",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    output = result.stdout + result.stderr
    assert "DOCMOST_INTEGRATION_URL" in output
    assert "DOCMOST_INTEGRATION_API_KEY" in output


def test_attachment_search_uses_registered_endpoint_drift_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selectors: list[tuple[str, str]] = []
    monkeypatch.setenv("DOCMOST_INTEGRATION_EDITION", "enterprise")
    monkeypatch.setenv("DOCMOST_INTEGRATION_ATTACHMENT_SEARCH", "1")
    monkeypatch.setattr(
        test_live_instance,
        "_skip_known_drift",
        lambda *values: selectors.extend(values),
    )
    monkeypatch.setattr(
        test_live_instance,
        "search_attachments",
        lambda client, query: {"items": []},
    )

    test_live_instance.test_enterprise_attachment_search(object())

    assert selectors == [("endpoint", "/search-attachments")]
