"""Tests for Attachment API methods."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from docmost_cli.api.attachments import (
    download_attachment,
    get_attachment_info,
    search_attachments,
    upload_attachment,
)
from docmost_cli.api.client import DocmostClient

if TYPE_CHECKING:
    from pathlib import Path


ATTACHMENT_ID = "019c0000-1111-7222-8333-444444444444"


class TestAttachmentFiles:
    def test_upload_returns_stable_id_and_urls(
        self,
        httpx_mock,
        api_key_settings,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "Launch plan #1.png"
        source.write_bytes(b"png-bytes")
        httpx_mock.add_response(
            url="https://docs.example.com/api/files/upload",
            json={
                "id": ATTACHMENT_ID,
                "fileName": source.name,
                "mimeType": "image/png",
                "fileSize": 9,
                "pageId": "page-1",
            },
        )

        with DocmostClient(api_key_settings) as client:
            result = upload_attachment(client, page_id="page-1", file_path=source)

        assert result["id"] == ATTACHMENT_ID
        assert result["path"].endswith("/Launch%20plan%20%231.png")
        assert result["url"] == f"https://docs.example.com{result['path']}"
        body = httpx_mock.get_requests()[0].read()
        assert b'name="pageId"' in body
        assert b"page-1" in body
        assert source.name.encode() in body

    def test_replace_reuses_original_name_for_a_stable_url(
        self,
        httpx_mock,
        api_key_settings,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "renamed.png"
        source.write_bytes(b"new-image")
        original_name = "diagram.png"
        info = {
            "id": ATTACHMENT_ID,
            "fileName": original_name,
            "mimeType": "image/png",
            "fileSize": 3,
            "pageId": "page-1",
        }
        httpx_mock.add_response(url="https://docs.example.com/api/files/info", json=info)
        httpx_mock.add_response(url="https://docs.example.com/api/files/upload", json=info)

        with DocmostClient(api_key_settings) as client:
            result = upload_attachment(
                client,
                page_id="page-1",
                file_path=source,
                attachment_id=ATTACHMENT_ID,
            )

        assert result["id"] == ATTACHMENT_ID
        upload_body = httpx_mock.get_requests()[1].read()
        assert original_name.encode() in upload_body
        assert b'name="attachmentId"' in upload_body

    def test_info_and_download(self, httpx_mock, api_key_settings) -> None:
        info = {
            "id": ATTACHMENT_ID,
            "fileName": "report.pdf",
            "mimeType": "application/pdf",
            "fileSize": 7,
            "pageId": "page-1",
        }
        httpx_mock.add_response(url="https://docs.example.com/api/files/info", json=info)
        httpx_mock.add_response(
            url=f"https://docs.example.com/api/files/{ATTACHMENT_ID}/report.pdf",
            content=b"pdfdata",
        )

        with DocmostClient(api_key_settings) as client:
            normalized = get_attachment_info(client, ATTACHMENT_ID)
            downloaded_info, content = download_attachment(client, normalized)

        assert downloaded_info["url"].endswith(f"/{ATTACHMENT_ID}/report.pdf")
        assert content == b"pdfdata"


class TestSearchAttachments:
    def test_returns_results(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/search-attachments",
            json={
                "items": [
                    {
                        "id": "att-1",
                        "fileName": "diagram.png",
                        "highlight": "architecture diagram",
                    },
                    {"id": "att-2", "fileName": "report.pdf", "highlight": "diagram"},
                ]
            },
        )
        with DocmostClient(api_key_settings) as client:
            result = search_attachments(client, "diagram")
        items = result["items"]
        assert len(items) == 2
        assert items[0]["fileName"] == "diagram.png"

    def test_with_space_id_filter(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/search-attachments",
            json={
                "items": [
                    {"id": "att-3", "fileName": "logo.svg", "highlight": "company logo"},
                ]
            },
        )
        with DocmostClient(api_key_settings) as client:
            result = search_attachments(client, "logo", space_id="space-abc")
        request = httpx_mock.get_requests()[0]
        body = json.loads(request.content)
        assert body == {"query": "logo", "spaceId": "space-abc"}
        items = result["items"]
        assert len(items) == 1
        assert items[0]["id"] == "att-3"

    def test_unavailable_feature_has_actionable_error(
        self, httpx_mock, api_key_settings, capsys
    ) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/search-attachments",
            status_code=404,
        )

        with (
            DocmostClient(api_key_settings) as client,
            pytest.raises(SystemExit) as exc_info,
        ):
            search_attachments(client, "diagram")

        assert exc_info.value.code == 4
        assert "Enterprise attachment-indexing feature" in capsys.readouterr().err
