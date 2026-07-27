"""Tests for Space API methods."""

import json

import pytest

from docmost_cli.api.client import DocmostClient
from docmost_cli.api.spaces import (
    create_space,
    get_space_info,
    list_spaces,
    resolve_space_id,
    update_space,
)


class TestListSpaces:
    def test_returns_spaces(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "s1", "name": "Eng", "slug": "eng"}]}},
        )
        with DocmostClient(api_key_settings) as client:
            result = list_spaces(client)
        assert result["data"]["items"][0]["slug"] == "eng"

    def test_with_limit(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [], "cursor": None}},
        )
        with DocmostClient(api_key_settings) as client:
            list_spaces(client, limit=10)
        request = httpx_mock.get_requests()[0]
        body = request.read()
        assert b'"limit":10' in body or b'"limit": 10' in body


class TestGetSpaceInfo:
    def test_by_slug(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "space-123", "name": "Engineering", "slug": "eng"}]}},
        )
        with DocmostClient(api_key_settings) as client:
            result = get_space_info(client, slug="eng")
        assert result["id"] == "space-123"

    def test_by_id(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces/info",
            json={"id": "space-123", "name": "Engineering", "slug": "eng"},
        )
        with DocmostClient(api_key_settings) as client:
            result = get_space_info(client, space_id="space-123")
        assert result["slug"] == "eng"


class TestResolveSpaceId:
    def test_returns_id(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "space-uuid", "slug": "eng", "name": "Eng"}]}},
        )
        with DocmostClient(api_key_settings) as client:
            space_id = resolve_space_id(client, "eng")
        assert space_id == "space-uuid"

    def test_nested_response(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "space-nested", "slug": "eng", "name": "Eng"}]}},
        )
        with DocmostClient(api_key_settings) as client:
            space_id = resolve_space_id(client, "eng")
        assert space_id == "space-nested"

    def test_not_found(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": []}},
        )
        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit) as exc:
            resolve_space_id(client, "nonexistent")
        assert exc.value.code == 4


class TestCreateSpace:
    def test_creates_space(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces/create",
            json={"id": "new-space", "name": "Test", "slug": "test"},
        )
        with DocmostClient(api_key_settings) as client:
            result = create_space(client, name="Test", slug="test")
        assert result["id"] == "new-space"

    def test_generates_required_slug_when_omitted(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces/create",
            json={"id": "new-space", "name": "Release Notes"},
        )
        with DocmostClient(api_key_settings) as client:
            create_space(client, name="Release Notes")

        request = httpx_mock.get_requests()[0]
        body = json.loads(request.content)
        assert body["name"] == "Release Notes"
        assert body["slug"].startswith("release-notes-")
        assert len(body["slug"]) <= 100

    def test_preserves_safe_canonical_name_as_generated_slug(
        self,
        httpx_mock,
        api_key_settings,
    ) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces/create",
            json={"id": "new-space"},
        )

        with DocmostClient(api_key_settings) as client:
            create_space(client, name="release-notes")

        request = httpx_mock.get_requests()[0]
        assert json.loads(request.content)["slug"] == "release-notes"

    def test_lossy_normalization_retains_name_uniqueness(
        self,
        httpx_mock,
        api_key_settings,
    ) -> None:
        for resource_id in ("space-team-a", "space-team-hyphen", "space-amp", "space-space"):
            httpx_mock.add_response(
                url="https://docs.example.com/api/spaces/create",
                json={"id": resource_id},
            )

        with DocmostClient(api_key_settings) as client:
            for name in ("Team A", "Team-A", "A&B", "A B"):
                create_space(client, name=name)

        slugs = [json.loads(request.content)["slug"] for request in httpx_mock.get_requests()]
        assert len(set(slugs)) == 4
        assert slugs[0].startswith("team-a-")
        assert slugs[1] == "team-a"
        assert slugs[2].startswith("a-b-")
        assert slugs[3].startswith("a-b-")

    def test_generates_distinct_slugs_for_non_ascii_names(
        self,
        httpx_mock,
        api_key_settings,
    ) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces/create",
            json={"id": "space-ja"},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces/create",
            json={"id": "space-ar"},
        )

        with DocmostClient(api_key_settings) as client:
            create_space(client, name="日本語")
            create_space(client, name="العربية")

        slugs = [json.loads(request.content)["slug"] for request in httpx_mock.get_requests()]
        assert slugs[0].startswith("space-")
        assert slugs[1].startswith("space-")
        assert slugs[0] != slugs[1]

    def test_generated_slugs_remain_valid_and_distinct_after_truncation(
        self,
        httpx_mock,
        api_key_settings,
    ) -> None:
        for resource_id in ("space-a", "space-a-name", "space-long"):
            httpx_mock.add_response(
                url="https://docs.example.com/api/spaces/create",
                json={"id": resource_id},
            )

        with DocmostClient(api_key_settings) as client:
            create_space(client, name="a")
            create_space(client, name="a-space")
            create_space(client, name="a" + "-" * 99 + "ignored")

        slugs = [json.loads(request.content)["slug"] for request in httpx_mock.get_requests()]
        assert len(set(slugs)) == 3
        assert all(2 <= len(slug) <= 100 for slug in slugs)
        assert slugs[0].startswith("a-")
        assert slugs[1] == "a-space"
        assert slugs[2].startswith("a-")

    def test_truncated_slugs_retain_name_uniqueness(
        self,
        httpx_mock,
        api_key_settings,
    ) -> None:
        for resource_id in ("space-long-a", "space-long-b"):
            httpx_mock.add_response(
                url="https://docs.example.com/api/spaces/create",
                json={"id": resource_id},
            )

        with DocmostClient(api_key_settings) as client:
            create_space(client, name="a" * 100)
            create_space(client, name="a" * 100 + "b")

        slugs = [json.loads(request.content)["slug"] for request in httpx_mock.get_requests()]
        assert len(set(slugs)) == 2
        assert all(2 <= len(slug) <= 100 for slug in slugs)


class TestUpdateSpace:
    def test_updates_space(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces/update",
            json={"id": "space-123", "name": "Updated"},
        )
        with DocmostClient(api_key_settings) as client:
            result = update_space(client, space_id="space-123", name="Updated")
        assert result["name"] == "Updated"
