"""Shared test fixtures."""

from pathlib import Path

import pytest

from docmost_cli.config.settings import DocmostSettings


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the explicit opt-in for real Docmost integration tests."""
    parser.addoption(
        "--run-docmost-integration",
        action="store_true",
        default=False,
        help="run tests against the explicitly configured Docmost instance",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip every integration-marked test unless the suite is explicitly enabled."""
    if config.getoption("--run-docmost-integration"):
        return
    skip = pytest.mark.skip(
        reason="pass --run-docmost-integration to run real Docmost integration tests"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolate_session_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep tests from reading or writing a developer's real session cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture()
def tmp_config(tmp_path: Path) -> Path:
    """Create a temp config file with default profile."""
    config = tmp_path / "config.toml"
    config.write_text(
        '[default]\nurl = "https://docs.example.com"\napi_key = "dm_test1234567890"\n'
    )
    return config


@pytest.fixture()
def tmp_config_session(tmp_path: Path) -> Path:
    """Create a temp config file with session auth."""
    config = tmp_path / "config.toml"
    config.write_text(
        "[default]\n"
        'url = "https://docs.example.com"\n'
        'email = "user@example.com"\n'
        'password = "secret123"\n'
    )
    return config


@pytest.fixture()
def api_key_settings() -> DocmostSettings:
    """Settings with API key auth."""
    return DocmostSettings(
        url="https://docs.example.com",
        api_key="dm_test1234567890",
    )


@pytest.fixture()
def session_settings() -> DocmostSettings:
    """Settings with session auth."""
    return DocmostSettings(
        url="https://docs.example.com",
        email="user@example.com",
        password="secret123",
    )
