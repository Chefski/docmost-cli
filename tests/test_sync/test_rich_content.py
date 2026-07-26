"""Tests for sync rich-content loss prevention."""

from __future__ import annotations

import json
from pathlib import Path

from docmost_cli.api.client import DocmostClient
from docmost_cli.config.settings import DocmostSettings
from docmost_cli.sync.diff import ChangeType, PageChange, SyncDiff
from docmost_cli.sync.rich_content import (
    analyze_prosemirror,
    build_pulled_rich_content_state,
    fetch_canonical_markdown,
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


def test_pull_state_preserves_exact_raw_snapshot(tmp_path: Path) -> None:
    content = _load_fixture("unsupported_rich_content.json")

    state = build_pulled_rich_content_state(tmp_path, "page/id", content)

    snapshot_path = tmp_path / state["snapshot_path"]
    assert snapshot_path == tmp_path / ".docmost/raw-pages/page%2Fid.json"
    assert json.loads(snapshot_path.read_text(encoding="utf-8")) == content
    assert state["snapshot_hash"].startswith("sha256:")
    assert "node:mention" in state["unsafe_features"]


def test_fetches_server_canonical_markdown(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/pages/info",
        json={"data": {"id": "page-1", "content": "# Canonical\n"}},
    )

    with _make_client() as client:
        markdown = fetch_canonical_markdown(client, "page-1")

    assert markdown == "# Canonical\n"
    request = httpx_mock.get_requests()[0]
    assert json.loads(request.content) == {"pageId": "page-1", "format": "markdown"}


def test_rewrites_canonical_attachment_urls_to_local_paths() -> None:
    markdown = (
        "![Diagram](/api/files/image-id/diagram.png)\n"
        "[PDF](https://docs.example.com/api/files/pdf-id/file.pdf)\n"
    )

    rewritten = rewrite_attachment_urls(
        markdown,
        {
            "image-id": "files/image-id/diagram.png",
            "pdf-id": "files/pdf-id/file.pdf",
        },
    )

    assert "![Diagram](files/image-id/diagram.png)" in rewritten
    assert "[PDF](files/pdf-id/file.pdf)" in rewritten


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


def test_legacy_manifest_entry_remains_compatible() -> None:
    change = PageChange(
        page_id="page-1",
        filename="legacy.md",
        changes={ChangeType.CONTENT_CHANGED},
        local_meta={"title": "Legacy"},
        manifest_entry={"title": "Legacy"},
    )

    assert find_rich_content_conflicts(SyncDiff(modified=[change])) == []
