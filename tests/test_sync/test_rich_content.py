"""Tests for sync rich-content loss prevention."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docmost_cli.api.client import DocmostClient
from docmost_cli.config.settings import DocmostSettings
from docmost_cli.sync.diff import ChangeType, PageChange, SyncDiff
from docmost_cli.sync.rich_content import (
    analyze_prosemirror,
    build_pulled_rich_content_state,
    fetch_canonical_markdown,
    find_current_rich_content_conflict,
    find_rich_content_conflicts,
    rewrite_attachment_urls,
)

_TEST_URL = "https://docs.example.com"
_FIXTURES = Path(__file__).parents[1] / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _make_client() -> DocmostClient:
    settings = DocmostSettings(url=_TEST_URL, api_key="dm_test1234567890")
    return DocmostClient(settings)


def test_supported_docmost_nodes_are_markdown_safe() -> None:
    content = _load_fixture("supported_rich_content.json")

    assert analyze_prosemirror(content) == ()


def test_null_content_is_a_safe_empty_page() -> None:
    assert analyze_prosemirror(None) == ()


def test_supported_round_trip_remains_pushable(tmp_path: Path) -> None:
    content = _load_fixture("supported_rich_content.json")
    state = build_pulled_rich_content_state(tmp_path, "safe-page", content)
    change = PageChange(
        page_id="safe-page",
        filename="safe.md",
        changes={ChangeType.CONTENT_CHANGED},
        local_meta={"title": "Safe page"},
        local_body="Updated Markdown\n",
        manifest_entry={"title": "Safe page", "rich_content": state},
    )

    assert state["unsafe_features"] == []
    assert find_rich_content_conflicts(SyncDiff(modified=[change])) == []


def test_unsupported_nodes_marks_and_attributes_are_reported() -> None:
    content = _load_fixture("unsupported_rich_content.json")

    features = set(analyze_prosemirror(content))

    assert {
        "node:mention",
        "node:hardBreak",
        "node:columns",
        "node:column",
        "node:details",
        "node:detailsSummary",
        "mark:underline",
        "mark:comment",
        "mark:textStyle",
        "attribute:paragraph.textAlign",
        "attribute:image.align",
        "attribute:image.width",
        "attribute:tableCell.colspan",
        "attribute:callout.icon",
    } <= features


def test_nested_task_content_is_protected_from_gfm_flattening() -> None:
    content = {
        "type": "doc",
        "content": [
            {
                "type": "taskList",
                "content": [
                    {
                        "type": "taskItem",
                        "attrs": {"checked": False},
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Parent"}],
                            },
                            {
                                "type": "bulletList",
                                "content": [
                                    {
                                        "type": "listItem",
                                        "content": [
                                            {
                                                "type": "paragraph",
                                                "content": [{"type": "text", "text": "Nested"}],
                                            }
                                        ],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ],
    }

    assert "structure:taskItem.content" in analyze_prosemirror(content)


def test_multi_paragraph_list_item_is_protected() -> None:
    content = {
        "type": "doc",
        "content": [
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": []},
                            {"type": "paragraph", "content": []},
                        ],
                    }
                ],
            }
        ],
    }

    assert "structure:listItem.content" in analyze_prosemirror(content)


def test_non_gfm_table_header_layout_is_protected() -> None:
    content = {
        "type": "doc",
        "content": [
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableCell",
                                "attrs": {"colspan": 1, "rowspan": 1},
                                "content": [{"type": "paragraph", "content": []}],
                            }
                        ],
                    }
                ],
            }
        ],
    }

    assert "structure:table.headers" in analyze_prosemirror(content)


def test_image_title_is_protected_from_attachment_html_loss() -> None:
    content = {
        "type": "doc",
        "content": [
            {
                "type": "image",
                "attrs": {
                    "src": "/api/files/image-id/diagram.png",
                    "attachmentId": "image-id",
                    "title": "Architecture tooltip",
                },
            }
        ],
    }

    assert "attribute:image.title" in analyze_prosemirror(content)


def test_external_image_title_remains_markdown_safe() -> None:
    content = {
        "type": "doc",
        "content": [
            {
                "type": "image",
                "attrs": {
                    "src": "https://example.com/diagram.png",
                    "alt": "Diagram",
                    "title": "Architecture tooltip",
                },
            }
        ],
    }

    assert analyze_prosemirror(content) == ()


def test_link_to_generated_node_id_is_protected() -> None:
    content = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"id": "section-id", "level": 2},
                "content": [{"type": "text", "text": "Section"}],
            },
            {
                "type": "paragraph",
                "attrs": {"id": "link-paragraph"},
                "content": [
                    {
                        "type": "text",
                        "text": "Jump",
                        "marks": [{"type": "link", "attrs": {"href": "#section-id"}}],
                    }
                ],
            },
        ],
    }

    assert "reference:generated-node-id" in analyze_prosemirror(content)


def test_generic_attachment_with_stable_id_is_markdown_safe() -> None:
    content = {
        "type": "doc",
        "content": [
            {
                "type": "attachment",
                "attrs": {
                    "url": "/api/files/file-id/handbook.pdf",
                    "name": "handbook.pdf",
                    "mime": "application/pdf",
                    "size": 42,
                    "attachmentId": "file-id",
                    "placeholder": None,
                },
            }
        ],
    }

    assert analyze_prosemirror(content) == ()


def test_generic_attachment_without_stable_reference_is_protected() -> None:
    content = {
        "type": "doc",
        "content": [
            {
                "type": "attachment",
                "attrs": {
                    "url": "https://example.com/handbook.pdf",
                    "name": "handbook.pdf",
                },
            }
        ],
    }

    assert "attribute:attachment.reference" in analyze_prosemirror(content)


def test_generic_attachment_with_mismatched_stable_id_is_protected() -> None:
    content = {
        "type": "doc",
        "content": [
            {
                "type": "attachment",
                "attrs": {
                    "url": "/api/files/url-id/handbook.pdf",
                    "name": "handbook.pdf",
                    "attachmentId": "different-id",
                },
            }
        ],
    }

    assert "attribute:attachment.reference" in analyze_prosemirror(content)


def test_pull_state_preserves_exact_raw_snapshot(tmp_path: Path) -> None:
    content = _load_fixture("unsupported_rich_content.json")

    state = build_pulled_rich_content_state(tmp_path, "page/id", content)

    snapshot_path = tmp_path / state["snapshot_path"]
    assert snapshot_path == tmp_path / ".docmost/raw-pages/page%2Fid.json"
    assert json.loads(snapshot_path.read_text(encoding="utf-8")) == content
    assert state["snapshot_hash"].startswith("sha256:")
    assert "node:mention" in state["unsafe_features"]


def test_pull_state_does_not_follow_predictable_temporary_symlink(tmp_path: Path) -> None:
    raw_pages = tmp_path / ".docmost" / "raw-pages"
    raw_pages.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive", encoding="utf-8")
    predictable_temporary_path = raw_pages / "page-1.json.tmp"
    try:
        predictable_temporary_path.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    state = build_pulled_rich_content_state(tmp_path, "page-1", {"type": "doc"})

    assert outside.read_text(encoding="utf-8") == "must survive"
    assert predictable_temporary_path.is_symlink()
    assert json.loads((tmp_path / state["snapshot_path"]).read_text(encoding="utf-8")) == {
        "type": "doc"
    }
    assert not list(raw_pages.glob(".page-1.json.*.tmp"))


def test_fetches_server_canonical_markdown(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/pages/info",
        json={
            "data": {
                "id": "page-1",
                "updatedAt": "revision-1",
                "content": "# Canonical\n",
            }
        },
    )

    with _make_client() as client:
        markdown = fetch_canonical_markdown(
            client,
            "page-1",
            expected_updated_at="revision-1",
        )

    assert markdown == "# Canonical\n"
    request = httpx_mock.get_requests()[0]
    assert json.loads(request.content) == {"pageId": "page-1", "format": "markdown"}


def test_canonical_markdown_retries_transient_failures(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr("docmost_cli.api.client.time.sleep", lambda _delay: None)
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/pages/info",
        status_code=503,
    )
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/pages/info",
        json={"data": {"id": "page-1", "content": "# Recovered\n"}},
    )

    with _make_client() as client:
        markdown = fetch_canonical_markdown(client, "page-1")

    assert markdown == "# Recovered\n"
    assert len(httpx_mock.get_requests()) == 2


def test_canonical_markdown_falls_back_when_format_is_unsupported(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/pages/info",
        status_code=404,
    )

    with _make_client() as client:
        markdown = fetch_canonical_markdown(client, "page-1")

    assert markdown is None


def test_rewrites_canonical_attachment_urls_to_local_paths() -> None:
    markdown = (
        "![Diagram](/api/files/image-id/diagram.png)\n"
        "[PDF](https://docs.example.com/api/files/pdf-id/file.pdf)\n"
        "Literal /api/files/image-id/diagram.png remains unchanged.\n"
        "```\n/api/files/pdf-id/file.pdf\n```\n"
    )

    rewritten = rewrite_attachment_urls(
        markdown,
        {
            "image-id": "files/image-id/diagram.png",
            "pdf-id": "files/pdf-id/file.pdf",
        },
        docmost_origin=_TEST_URL,
    )

    assert "![Diagram](files/image-id/diagram.png)" in rewritten
    assert "[PDF](files/pdf-id/file.pdf)" in rewritten
    assert "Literal /api/files/image-id/diagram.png remains unchanged." in rewritten
    assert "```\n/api/files/pdf-id/file.pdf\n```" in rewritten


def test_does_not_rewrite_attachment_links_inside_code() -> None:
    markdown = (
        "Outside: ![diagram](/api/files/image-id/diagram.png)\n"
        "Inline: `![diagram](/api/files/image-id/diagram.png)`\n"
        "~~~markdown\n"
        "[PDF](/api/files/pdf-id/file.pdf)\n"
        "~~~\n"
        "    [Indented](/api/files/pdf-id/file.pdf)\n"
    )

    rewritten = rewrite_attachment_urls(
        markdown,
        {
            "image-id": "files/image-id/diagram.png",
            "pdf-id": "files/pdf-id/file.pdf",
        },
    )

    assert "Outside: ![diagram](files/image-id/diagram.png)" in rewritten
    assert "Inline: `![diagram](/api/files/image-id/diagram.png)`" in rewritten
    assert "~~~markdown\n[PDF](/api/files/pdf-id/file.pdf)\n~~~" in rewritten
    assert "    [Indented](/api/files/pdf-id/file.pdf)" in rewritten


def test_does_not_rewrite_attachment_urls_from_another_origin() -> None:
    markdown = "[External](https://other.example/api/files/pdf-id/file.pdf)\n"

    rewritten = rewrite_attachment_urls(
        markdown,
        {"pdf-id": "files/pdf-id/file.pdf"},
        docmost_origin=_TEST_URL,
    )

    assert rewritten == markdown


def test_rewrites_markdown_escaped_attachment_url() -> None:
    markdown = r"[PDF](/api/files/pdf-id/file\(final\).pdf)"

    rewritten = rewrite_attachment_urls(
        markdown,
        {"pdf-id": "files/pdf-id/file%28final%29.pdf"},
    )

    assert rewritten == "[PDF](files/pdf-id/file%28final%29.pdf)"


def test_rewrites_attachment_with_escaped_and_nested_label() -> None:
    markdown = (
        r"![Before \] after](/api/files/image-id/diagram.png)"
        "\n"
        "[Outer [inner]](/api/files/pdf-id/file.pdf)\n"
    )

    rewritten = rewrite_attachment_urls(
        markdown,
        {
            "image-id": "files/image-id/diagram.png",
            "pdf-id": "files/pdf-id/file.pdf",
        },
    )

    assert r"![Before \] after](files/image-id/diagram.png)" in rewritten
    assert "[Outer [inner]](files/pdf-id/file.pdf)" in rewritten


def test_content_edit_is_blocked_when_pull_found_unsafe_features() -> None:
    change = PageChange(
        page_id="page-1",
        filename="rich.md",
        changes={ChangeType.CONTENT_CHANGED},
        local_meta={"title": "Rich page"},
        local_body="Flattened content\n",
        manifest_entry={
            "title": "Rich page",
            "rich_content": {
                "guard_version": 1,
                "source": "prosemirror",
                "snapshot_path": ".docmost/raw-pages/page-1.json",
                "unsafe_features": ["node:mention", "mark:comment"],
            },
        },
    )

    conflicts = find_rich_content_conflicts(SyncDiff(modified=[change]))

    assert len(conflicts) == 1
    assert conflicts[0].features == ("mark:comment", "node:mention")
    assert conflicts[0].snapshot_path == ".docmost/raw-pages/page-1.json"


def test_current_remote_rich_content_is_rechecked_before_replacement(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/pages/info",
        json={
            "id": "page-1",
            "title": "Rich page",
            "content": {
                "type": "doc",
                "content": [{"type": "mention", "attrs": {"id": "user-1"}}],
            },
        },
    )
    change = PageChange(
        page_id="page-1",
        filename="rich.md",
        changes={ChangeType.CONTENT_CHANGED},
        local_meta={"title": "Rich page"},
        local_body="Stale local edit\n",
        manifest_entry={
            "title": "Rich page",
            "rich_content": {
                "guard_version": 1,
                "source": "prosemirror",
                "snapshot_path": ".docmost/raw-pages/page-1.json",
                "unsafe_features": [],
            },
        },
    )

    with _make_client() as client:
        conflict = find_current_rich_content_conflict(client, change)

    assert conflict is not None
    assert conflict.features == ("node:mention",)


def test_metadata_only_edit_is_not_blocked() -> None:
    change = PageChange(
        page_id="page-1",
        filename="rich.md",
        changes={ChangeType.TITLE_CHANGED},
        local_meta={"title": "Renamed"},
        manifest_entry={
            "rich_content": {
                "guard_version": 1,
                "unsafe_features": ["node:mention"],
            }
        },
    )

    assert find_rich_content_conflicts(SyncDiff(modified=[change])) == []


def test_missing_unsafe_feature_list_fails_closed() -> None:
    change = PageChange(
        page_id="page-1",
        filename="broken.md",
        changes={ChangeType.CONTENT_CHANGED},
        local_meta={"title": "Broken guard"},
        manifest_entry={
            "rich_content": {
                "guard_version": 1,
                "source": "prosemirror",
            }
        },
    )

    conflicts = find_rich_content_conflicts(SyncDiff(modified=[change]))

    assert conflicts[0].features == ("guard:invalid-metadata",)


def test_legacy_manifest_entry_remains_compatible() -> None:
    change = PageChange(
        page_id="page-1",
        filename="legacy.md",
        changes={ChangeType.CONTENT_CHANGED},
        local_meta={"title": "Legacy"},
        manifest_entry={"title": "Legacy"},
    )

    assert find_rich_content_conflicts(SyncDiff(modified=[change])) == []
