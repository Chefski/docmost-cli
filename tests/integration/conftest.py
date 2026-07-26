"""Safety-gated fixtures for real Docmost integration tests."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from docmost_cli.api.client import DocmostClient
from docmost_cli.config.settings import DocmostSettings

if TYPE_CHECKING:
    from collections.abc import Iterator


def enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


@pytest.fixture()
def integration_settings(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> DocmostSettings:
    if not request.config.getoption("--run-docmost-integration"):
        pytest.skip("pass --run-docmost-integration to contact a real Docmost instance")

    url = os.getenv("DOCMOST_INTEGRATION_URL")
    api_key = os.getenv("DOCMOST_INTEGRATION_API_KEY")
    email = os.getenv("DOCMOST_INTEGRATION_EMAIL")
    password = os.getenv("DOCMOST_INTEGRATION_PASSWORD")
    if not url:
        pytest.skip("DOCMOST_INTEGRATION_URL is not set")
    if not api_key and not (email and password):
        pytest.skip("integration API key or email/password credentials are not set")

    cache_dir = tmp_path_factory.mktemp("docmost-integration-cache")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_dir))
    return DocmostSettings(
        url=url,
        api_key=api_key,
        email=email,
        password=password,
    )


@pytest.fixture()
def integration_client(integration_settings: DocmostSettings) -> Iterator[DocmostClient]:
    with DocmostClient(integration_settings) as client:
        yield client


@pytest.fixture()
def read_space_id() -> str:
    space_id = os.getenv("DOCMOST_INTEGRATION_SPACE_ID")
    if not space_id:
        pytest.skip("DOCMOST_INTEGRATION_SPACE_ID is not set")
    return space_id


@pytest.fixture()
def mutation_space_id(request: pytest.FixtureRequest) -> str:
    if not request.config.getoption("--run-docmost-integration"):
        pytest.skip("real-instance integration tests are not enabled")
    if not enabled("DOCMOST_INTEGRATION_ALLOW_MUTATIONS"):
        pytest.skip("DOCMOST_INTEGRATION_ALLOW_MUTATIONS is not explicitly enabled")
    space_id = os.getenv("DOCMOST_INTEGRATION_MUTATION_SPACE_ID")
    if not space_id:
        pytest.skip("DOCMOST_INTEGRATION_MUTATION_SPACE_ID is not set")
    return space_id


@dataclass
class CreatedResources:
    page_ids: list[str] = field(default_factory=list)
    space_ids: list[str] = field(default_factory=list)


@pytest.fixture()
def created_resources(integration_client: DocmostClient) -> Iterator[CreatedResources]:
    """Delete only resources whose IDs were returned from this test run."""
    resources = CreatedResources()
    yield resources

    for page_id in reversed(resources.page_ids):
        response = integration_client.post_raw(
            "/pages/delete",
            json={"pageId": page_id, "permanentlyDelete": True},
            raise_on_error=False,
        )
        if not response.is_success:
            integration_client.post_raw(
                "/pages/delete",
                json={"pageId": page_id},
                raise_on_error=False,
            )

    for space_id in reversed(resources.space_ids):
        integration_client.post_raw(
            "/spaces/delete",
            json={"spaceId": space_id},
            raise_on_error=False,
        )
