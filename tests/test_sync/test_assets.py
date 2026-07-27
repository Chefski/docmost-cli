"""Tests for sync attachment discovery and stable-ID rewriting."""

from __future__ import annotations

from typing import TYPE_CHECKING

from docmost_cli.api.client import DocmostClient
from docmost_cli.config.settings import DocmostSettings
from docmost_cli.sync.assets import (
    asset_markdown_path,
    asset_relative_path,
    collect_attachment_ids,
    compute_file_hash,
    discover_local_assets,
    prepare_markdown_assets,
)

if TYPE_CHECKING:
    from pathlib import Path

_TEST_URL = "https://docs.example.com"
ATTACHMENT_ID = "019c0000-1111-7222-8333-444444444444"


def _make_client() -> DocmostClient:
    return DocmostClient(DocmostSettings(url=_TEST_URL, api_key="dm_test1234567890"))


def test_collect_attachment_ids_deduplicates_nested_nodes() -> None:
    document = {
        "type": "doc",
        "content": [
            {"type": "image", "attrs": {"attachmentId": ATTACHMENT_ID}},
            {
                "type": "table",
                "content": [
                    {"type": "attachment", "attrs": {"attachmentId": ATTACHMENT_ID}},
                    {"type": "pdf", "attrs": {"attachmentId": "second-id"}},
                    {
                        "type": "image",
                        "attrs": {"src": "/api/files/legacy-id/legacy.png"},
                    },
                ],
            },
        ],
    }

    assert collect_attachment_ids(document) == [ATTACHMENT_ID, "second-id", "legacy-id"]


def test_asset_relative_path_normalizes_windows_unsafe_filenames() -> None:
    assert (
        asset_relative_path(ATTACHMENT_ID, "reports/CON: quarterly?.txt. ")
        == f"files/{ATTACHMENT_ID}/CON- quarterly-.txt"
    )
    assert asset_relative_path(ATTACHMENT_ID, "CON.txt") == f"files/{ATTACHMENT_ID}/_CON.txt"


def test_discover_local_assets_decodes_url_paths(tmp_path: Path) -> None:
    asset = tmp_path / "files" / ATTACHMENT_ID / "Launch plan.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")
    markdown_path = asset_markdown_path(asset.relative_to(tmp_path).as_posix())

    references = discover_local_assets(f"![Plan]({markdown_path})", tmp_path)

    assert len(references) == 1
    assert references[0].absolute_path == asset
    assert references[0].is_image is True


def test_discover_local_assets_handles_escaped_and_nested_labels(tmp_path: Path) -> None:
    asset = tmp_path / "files" / ATTACHMENT_ID / "Launch plan.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")
    markdown_path = asset_markdown_path(asset.relative_to(tmp_path).as_posix())

    references = discover_local_assets(
        rf"![Before \] [nested]]({markdown_path})",
        tmp_path,
    )

    assert len(references) == 1
    assert references[0].absolute_path == asset
    assert references[0].label == r"Before \] [nested]"


def test_discover_local_assets_decodes_escaped_destination(tmp_path: Path) -> None:
    asset = tmp_path / "files" / "report(final).pdf"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"pdf")

    references = discover_local_assets(r"[Report](files/report\(final\).pdf)", tmp_path)

    assert references[0].absolute_path == asset


def test_discover_local_assets_preserves_escaped_query_characters(tmp_path: Path) -> None:
    asset = tmp_path / "files" / "report?#.pdf"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"pdf")

    references = discover_local_assets(r"[Report](files/report\?\#.pdf)", tmp_path)

    assert references[0].absolute_path == asset


def test_discover_local_assets_ignores_code_examples(tmp_path: Path) -> None:
    asset = tmp_path / "files" / "report.pdf"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"pdf")
    markdown = (
        "`[inline](files/report.pdf)`\n"
        "```\n[fenced](files/report.pdf)\n```\n"
        "    [indented](files/report.pdf)\n"
    )

    assert discover_local_assets(markdown, tmp_path) == []


def test_prepare_new_image_uploads_and_embeds_stable_id(
    httpx_mock,
    tmp_path: Path,
) -> None:
    image = tmp_path / "diagram.png"
    image.write_bytes(b"image-bytes")
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/files/upload",
        json={
            "id": ATTACHMENT_ID,
            "fileName": "diagram.png",
            "mimeType": "image/png",
            "fileSize": 11,
            "pageId": "page-1",
        },
    )

    with _make_client() as client:
        content, entries, attachment_ids = prepare_markdown_assets(
            client,
            page_id="page-1",
            markdown="Before\n\n![Diagram](diagram.png)\n",
            dir_path=tmp_path,
            manifest={"assets": {}},
        )

    assert attachment_ids == [ATTACHMENT_ID]
    assert f'data-attachment-id="{ATTACHMENT_ID}"' in content
    assert f"/api/files/{ATTACHMENT_ID}/diagram.png" in content
    assert entries[ATTACHMENT_ID]["path"] == "diagram.png"
    assert entries[ATTACHMENT_ID]["content_hash"] == compute_file_hash(image)


def test_prepare_unchanged_asset_reuses_id_without_upload(tmp_path: Path) -> None:
    relative_path = f"files/{ATTACHMENT_ID}/diagram.png"
    image = tmp_path / relative_path
    image.parent.mkdir(parents=True)
    image.write_bytes(b"same-image")
    manifest = {
        "assets": {
            ATTACHMENT_ID: {
                "file_name": "diagram.png",
                "path": relative_path,
                "mime_type": "image/png",
                "size": image.stat().st_size,
                "page_id": "page-1",
                "content_hash": compute_file_hash(image),
                "server_path": f"/api/files/{ATTACHMENT_ID}/diagram.png",
            }
        }
    }

    with _make_client() as client:
        content, entries, attachment_ids = prepare_markdown_assets(
            client,
            page_id="page-1",
            markdown=f"![Diagram]({relative_path})",
            dir_path=tmp_path,
            manifest=manifest,
        )

    assert attachment_ids == [ATTACHMENT_ID]
    assert f'data-attachment-id="{ATTACHMENT_ID}"' in content
    assert entries[ATTACHMENT_ID]["content_hash"] == compute_file_hash(image)


def test_prepare_changed_asset_replaces_bytes_in_place(httpx_mock, tmp_path: Path) -> None:
    relative_path = f"files/{ATTACHMENT_ID}/diagram.png"
    image = tmp_path / relative_path
    image.parent.mkdir(parents=True)
    image.write_bytes(b"changed-image")
    manifest = {
        "assets": {
            ATTACHMENT_ID: {
                "file_name": "diagram.png",
                "path": relative_path,
                "mime_type": "image/png",
                "size": 3,
                "page_id": "page-1",
                "content_hash": "sha256:old",
                "server_path": f"/api/files/{ATTACHMENT_ID}/diagram.png",
            }
        }
    }
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/files/upload",
        json={
            "id": ATTACHMENT_ID,
            "fileName": "diagram.png",
            "mimeType": "image/png",
            "fileSize": image.stat().st_size,
            "pageId": "page-1",
        },
    )

    with _make_client() as client:
        _, entries, attachment_ids = prepare_markdown_assets(
            client,
            page_id="page-1",
            markdown=f"![Diagram]({relative_path})",
            dir_path=tmp_path,
            manifest=manifest,
        )

    assert attachment_ids == [ATTACHMENT_ID]
    assert entries[ATTACHMENT_ID]["content_hash"] == compute_file_hash(image)
    request_body = httpx_mock.get_requests()[0].read()
    assert b'name="attachmentId"' in request_body
    assert ATTACHMENT_ID.encode() in request_body


def test_prepare_asset_for_another_page_creates_a_page_owned_copy(
    httpx_mock,
    tmp_path: Path,
) -> None:
    relative_path = f"files/{ATTACHMENT_ID}/diagram.png"
    image = tmp_path / relative_path
    image.parent.mkdir(parents=True)
    image.write_bytes(b"same-image")
    manifest = {
        "assets": {
            ATTACHMENT_ID: {
                "file_name": "diagram.png",
                "path": relative_path,
                "mime_type": "image/png",
                "size": image.stat().st_size,
                "page_id": "page-1",
                "content_hash": compute_file_hash(image),
                "server_path": f"/api/files/{ATTACHMENT_ID}/diagram.png",
            }
        }
    }
    copied_id = "019c0000-5555-7666-8777-888888888888"
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/files/upload",
        json={
            "id": copied_id,
            "fileName": "diagram.png",
            "mimeType": "image/png",
            "fileSize": image.stat().st_size,
            "pageId": "page-2",
        },
    )

    with _make_client() as client:
        content, entries, attachment_ids = prepare_markdown_assets(
            client,
            page_id="page-2",
            markdown=f"![Diagram]({relative_path})",
            dir_path=tmp_path,
            manifest=manifest,
        )

    assert attachment_ids == [copied_id]
    assert f'data-attachment-id="{copied_id}"' in content
    assert entries[copied_id]["page_id"] == "page-2"
    request_body = httpx_mock.get_requests()[0].read()
    assert b'name="attachmentId"' not in request_body


def test_prepare_prefers_the_asset_owned_by_the_target_page(tmp_path: Path) -> None:
    relative_path = f"files/{ATTACHMENT_ID}/diagram.png"
    image = tmp_path / relative_path
    image.parent.mkdir(parents=True)
    image.write_bytes(b"shared-local-bytes")
    page_two_id = "019c0000-5555-7666-8777-888888888888"
    shared_entry = {
        "file_name": "diagram.png",
        "path": relative_path,
        "mime_type": "image/png",
        "size": image.stat().st_size,
        "content_hash": compute_file_hash(image),
    }
    manifest = {
        "assets": {
            ATTACHMENT_ID: {
                **shared_entry,
                "page_id": "page-1",
                "server_path": f"/api/files/{ATTACHMENT_ID}/diagram.png",
            },
            page_two_id: {
                **shared_entry,
                "page_id": "page-2",
                "server_path": f"/api/files/{page_two_id}/diagram.png",
            },
        }
    }

    with _make_client() as client:
        content, _, attachment_ids = prepare_markdown_assets(
            client,
            page_id="page-1",
            markdown=f"![Diagram]({relative_path})",
            dir_path=tmp_path,
            manifest=manifest,
        )

    assert attachment_ids == [ATTACHMENT_ID]
    assert f'data-attachment-id="{ATTACHMENT_ID}"' in content
