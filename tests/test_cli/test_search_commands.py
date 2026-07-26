"""Tests for search CLI command."""

import json

from typer.testing import CliRunner

from docmost_cli.cli.main import app

runner = CliRunner()


class TestSearchCommand:
    def test_search_json(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/search",
            json={
                "items": [
                    {"id": "p1", "title": "Found Page", "highlight": "match context"},
                ]
            },
        )
        result = runner.invoke(
            app, ["--config", str(tmp_config), "search", "query", "test", "--json"]
        )
        assert result.exit_code == 0
        assert "Found Page" in result.output

    def test_search_with_space_filter(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "s1", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/search",
            json={"items": []},
        )
        result = runner.invoke(
            app,
            ["--config", str(tmp_config), "search", "query", "test", "--space", "eng", "--json"],
        )
        assert result.exit_code == 0

    def test_search_sends_offset_not_cursor_or_type(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/search",
            json={"items": []},
        )

        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config),
                "search",
                "query",
                "test",
                "--limit",
                "5",
                "--offset",
                "10",
                "--json",
            ],
        )

        assert result.exit_code == 0
        body = json.loads(httpx_mock.get_requests()[0].content)
        assert body == {"query": "test", "limit": 5, "offset": 10}

    def test_type_filter_is_not_advertised(self) -> None:
        result = runner.invoke(app, ["search", "query", "--help"])

        assert result.exit_code == 0
        assert "--type" not in result.output
