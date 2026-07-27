"""Tests for attachment CLI commands."""

import json

from typer.testing import CliRunner

from docmost_cli.cli.main import app

runner = CliRunner()


class TestAttachmentUpload:
    def test_upload_inserts_image_and_returns_url_json(
        self,
        tmp_config,
        tmp_path,
        httpx_mock,
    ) -> None:
        image = tmp_path / "diagram.png"
        image.write_bytes(b"image")
        attachment_id = "019c0000-1111-7222-8333-444444444444"
        httpx_mock.add_response(
            url="https://docs.example.com/api/files/upload",
            json={
                "id": attachment_id,
                "fileName": "diagram.png",
                "mimeType": "image/png",
                "fileSize": 5,
                "pageId": "page-1",
            },
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/update",
            json={"id": "page-1"},
        )

        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config),
                "attachment",
                "upload",
                "page-1",
                "--file",
                str(image),
                "--json",
            ],
        )

        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["id"] == attachment_id
        assert output["url"].endswith(f"/{attachment_id}/diagram.png")
        update = json.loads(httpx_mock.get_requests()[1].content)
        assert update["operation"] == "append"
        assert 'data-attachment-id="019c0000-1111-7222-8333-444444444444"' in update["content"]


class TestAttachmentSearch:
    def test_search_json(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/search-attachments",
            json={
                "items": [
                    {"id": "att-1", "fileName": "diagram.png", "highlight": "diagram"},
                    {"id": "att-2", "fileName": "screenshot.jpg", "highlight": "diagram"},
                ]
            },
        )
        result = runner.invoke(
            app,
            ["--config", str(tmp_config), "attachment", "search", "diagram", "--json"],
        )
        assert result.exit_code == 0
        assert "att-1" in result.output
        assert "diagram.png" in result.output
        assert "att-2" in result.output

    def test_search_with_space(self, tmp_config, httpx_mock) -> None:
        # First call resolves space slug to ID via listing all spaces
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "space-uuid", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/search-attachments",
            json={
                "items": [
                    {"id": "att-3", "fileName": "logo.svg", "highlight": "company logo"},
                ]
            },
        )
        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config),
                "attachment",
                "search",
                "logo",
                "--space",
                "eng",
                "--json",
            ],
        )
        assert result.exit_code == 0
        assert "att-3" in result.output
        assert "logo.svg" in result.output
