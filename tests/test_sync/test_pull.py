"""Tests for the sync pull module."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from docmost_cli.api.client import DocmostClient
from docmost_cli.config.settings import DocmostSettings
from docmost_cli.convert.prosemirror_to_md import convert_to_markdown
from docmost_cli.sync.frontmatter import read_sync_file, write_sync_file
from docmost_cli.sync.manifest import (
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    build_manifest,
    build_server_revision,
    compute_content_hash,
    sanitize_filename,
    save_manifest,
)
from docmost_cli.sync.pull import (
    _ACTIVE_PUBLISH_TOKENS,
    PullResult,
    _atomic_exchange_directories,
    _publish_journal_path,
    _publish_staged_pull,
    _recover_interrupted_publish,
    _remove_publish_journal,
    _rename_directory_noreplace,
    _snapshot_target,
    _staging_is_recovery_data,
    _sync_directory,
    _temporary_sibling,
    _write_publish_journal,
    flatten_tree,
    pull_space,
)

# ---------------------------------------------------------------------------
# Helper: create a client for integration tests
# ---------------------------------------------------------------------------

_TEST_URL = "https://docs.example.com"


def _make_client() -> DocmostClient:
    settings = DocmostSettings(url=_TEST_URL, api_key="dm_test1234567890")
    return DocmostClient(settings)


# ---------------------------------------------------------------------------
# flatten_tree — pure unit tests (no mocking needed)
# ---------------------------------------------------------------------------


class TestFlattenTreeFlat:
    """Three root pages produce a flat list with parent_id=None."""

    def test_flat_list(self) -> None:
        tree = [
            {"id": "p1", "title": "Page One", "icon": "", "children": []},
            {"id": "p2", "title": "Page Two", "icon": "X", "children": []},
            {"id": "p3", "title": "Page Three", "children": []},
        ]
        result = flatten_tree(tree)
        assert len(result) == 3
        for item in result:
            assert item["parent_id"] is None
        assert result[0]["id"] == "p1"
        assert result[1]["id"] == "p2"
        assert result[2]["id"] == "p3"
        assert result[0]["title"] == "Page One"
        # Missing icon defaults to ""
        assert result[2]["icon"] == ""


class TestFlattenTreeNested:
    """Nested children are flattened with correct parent_ids."""

    def test_nested(self) -> None:
        tree = [
            {
                "id": "root",
                "title": "Root",
                "icon": "",
                "children": [
                    {
                        "id": "child-1",
                        "title": "Child 1",
                        "icon": "",
                        "children": [
                            {
                                "id": "grandchild",
                                "title": "Grandchild",
                                "icon": "",
                                "children": [],
                            }
                        ],
                    },
                    {
                        "id": "child-2",
                        "title": "Child 2",
                        "icon": "",
                        "children": [],
                    },
                ],
            }
        ]
        result = flatten_tree(tree)
        assert len(result) == 4

        ids_and_parents = [(r["id"], r["parent_id"]) for r in result]
        assert ("root", None) in ids_and_parents
        assert ("child-1", "root") in ids_and_parents
        assert ("child-2", "root") in ids_and_parents
        assert ("grandchild", "child-1") in ids_and_parents

    def test_deep_tree_does_not_hit_recursion_limit(self) -> None:
        root: dict[str, object] = {
            "id": "page-0",
            "title": "Page 0",
            "children": [],
        }
        current = root
        for level in range(1, 1101):
            child: dict[str, object] = {
                "id": f"page-{level}",
                "title": f"Page {level}",
                "children": [],
            }
            current["children"] = [child]
            current = child

        result = flatten_tree([root])

        assert len(result) == 1101
        assert result[-1]["id"] == "page-1100"


class TestFlattenTreeEmpty:
    """Empty list produces empty result."""

    def test_empty(self) -> None:
        assert flatten_tree([]) == []


# ---------------------------------------------------------------------------
# pull_space — integration tests with httpx_mock
# ---------------------------------------------------------------------------

# Shared ProseMirror doc for mock responses
_PM_DOC = {
    "type": "doc",
    "content": [
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "Hello world"}],
        }
    ],
}


def _mock_resolve_space(httpx_mock, slug: str = "test", space_id: str = "space-1") -> None:
    """Add mock for resolve_space_id (which calls list_spaces -> POST /spaces)."""
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/spaces",
        json={"data": {"items": [{"id": space_id, "slug": slug, "name": slug.capitalize()}]}},
    )


def _mock_sidebar_pages(httpx_mock, pages: list[dict]) -> None:
    """Add mock for build_page_tree (POST /pages/sidebar-pages)."""
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/pages/sidebar-pages",
        json={"data": {"items": pages}},
    )


def _mock_page_content(
    httpx_mock,
    page_id: str,
    title: str,
    pm_content: dict | None = None,
    markdown_content: str | None = None,
    canonical_available: bool = True,
    updated_at: str = "2026-01-01T00:00:00.000Z",
    *,
    parent_page_id: str | None = None,
) -> None:
    """Add matching raw-content and canonical-Markdown page responses."""
    content = pm_content or _PM_DOC
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/pages/info",
        json={
            "id": page_id,
            "title": title,
            "icon": "",
            "parentPageId": parent_page_id,
            "spaceId": "space-1",
            "updatedAt": updated_at,
            "content": content,
        },
    )
    # Rich-content-safe pull asks the server to perform canonical conversion.
    if canonical_available:
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/info",
            json={
                "id": page_id,
                "title": title,
                "spaceId": "space-1",
                "updatedAt": updated_at,
                "content": (
                    markdown_content
                    if markdown_content is not None
                    else convert_to_markdown(content)
                ),
            },
        )
    else:
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/info",
            json={
                "id": page_id,
                "title": title,
                "spaceId": "space-1",
                "updatedAt": updated_at,
                "content": None,
            },
        )


def _snapshot_files(root: Path) -> dict[str, bytes]:
    """Return every regular file beneath root for rollback comparisons."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class TestPullEmptySpace:
    """Space with no pages creates dir + empty manifest."""

    def test_empty_space(self, httpx_mock, tmp_path: Path) -> None:
        target = tmp_path / "test"
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(httpx_mock, [])

        with _make_client() as client:
            result = pull_space(client, "test", target)

        assert isinstance(result, PullResult)
        assert result.pages_pulled == 0
        assert result.dir_path == target
        assert target.exists()

        # Manifest should exist and be empty
        manifest_path = target / MANIFEST_FILENAME
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["version"] == MANIFEST_VERSION
        assert manifest["space_slug"] == "test"
        assert manifest["space_id"] == "space-1"
        assert manifest["pages"] == {}


class TestPullCreatesFiles:
    """Two pages produce 2 .md files + manifest on disk."""

    def test_creates_files(self, httpx_mock, tmp_path: Path) -> None:
        target = tmp_path / "test"

        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {"id": "p1", "title": "Page One", "icon": "", "hasChildren": False, "children": []},
                {"id": "p2", "title": "Page Two", "icon": "", "hasChildren": False, "children": []},
            ],
        )

        # Mock content fetch for page 1
        _mock_page_content(httpx_mock, "p1", "Page One")
        # Mock content fetch for page 2
        _mock_page_content(httpx_mock, "p2", "Page Two")

        with _make_client() as client:
            result = pull_space(client, "test", target)

        assert result.pages_pulled == 2

        # Check .md files exist
        md_files = list(target.glob("*.md"))
        assert len(md_files) == 2

        # Check manifest exists and has 2 pages
        manifest_path = target / MANIFEST_FILENAME
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert len(manifest["pages"]) == 2
        assert "p1" in manifest["pages"]
        assert "p2" in manifest["pages"]
        assert not list(tmp_path.glob(".test.pull-*"))
        expected_revision = build_server_revision(
            {
                "id": "p1",
                "title": "Page One",
                "icon": "",
                "parentPageId": None,
                "spaceId": "space-1",
                "content": _PM_DOC,
                "updatedAt": "2026-01-01T00:00:00.000Z",
            }
        )
        assert manifest["pages"]["p1"]["server_revision"] == expected_revision

    def test_server_page_metadata_drives_staged_file_and_revision(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": "p1",
                    "title": "Stale Sidebar Title",
                    "icon": "old",
                    "hasChildren": False,
                    "children": [],
                }
            ],
        )
        _mock_page_content(
            httpx_mock,
            "p1",
            "Current Server Title",
            parent_page_id="server-parent",
        )

        with _make_client() as client:
            pull_space(client, "test", target)

        current_filename = sanitize_filename("Current Server Title", "p1")
        metadata, _markdown = read_sync_file(target / current_filename)
        assert metadata == {
            "id": "p1",
            "title": "Current Server Title",
            "parent_id": "server-parent",
            "icon": "",
        }
        assert not (target / sanitize_filename("Stale Sidebar Title", "p1")).exists()
        manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        expected_revision = build_server_revision(
            {
                "id": "p1",
                "title": "Current Server Title",
                "icon": "",
                "parentPageId": "server-parent",
                "spaceId": "space-1",
                "content": _PM_DOC,
                "updatedAt": "2026-01-01T00:00:00.000Z",
            }
        )
        assert manifest["pages"]["p1"]["server_revision"] == expected_revision

    def test_uses_server_markdown_and_records_raw_source_guard(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        rich_doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "mention",
                            "attrs": {"id": "user-1", "label": "Ada"},
                        }
                    ],
                }
            ],
        }
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": "p1",
                    "title": "Rich Page",
                    "icon": "",
                    "hasChildren": False,
                    "children": [],
                }
            ],
        )
        _mock_page_content(
            httpx_mock,
            "p1",
            "Rich Page",
            rich_doc,
            markdown_content="Server canonical output\n",
        )

        with _make_client() as client:
            pull_space(client, "test", target)

        page_text = next(target.glob("*.md")).read_text(encoding="utf-8")
        assert page_text.endswith("Server canonical output\n")
        manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        guard = manifest["pages"]["p1"]["rich_content"]
        assert guard["unsafe_features"] == ["node:mention"]
        snapshot = target / guard["snapshot_path"]
        assert json.loads(snapshot.read_text(encoding="utf-8")) == rich_doc

    def test_local_converter_fallback_is_protected(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": "p1",
                    "title": "Fallback Page",
                    "icon": "",
                    "hasChildren": False,
                    "children": [],
                }
            ],
        )
        _mock_page_content(
            httpx_mock,
            "p1",
            "Fallback Page",
            canonical_available=False,
        )

        with _make_client() as client:
            pull_space(client, "test", target)

        manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        assert manifest["pages"]["p1"]["rich_content"]["unsafe_features"] == [
            "conversion:local-fallback"
        ]

    def test_canonical_markdown_without_revision_uses_local_fallback(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": "p1",
                    "title": "Fallback Page",
                    "icon": "",
                    "hasChildren": False,
                    "children": [],
                }
            ],
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/info",
            json={
                "id": "p1",
                "title": "Fallback Page",
                "icon": "",
                "parentPageId": None,
                "spaceId": "space-1",
                "updatedAt": "revision-1",
                "content": _PM_DOC,
            },
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/info",
            json={
                "id": "p1",
                "title": "Fallback Page",
                "content": "Unverified canonical output\n",
            },
        )

        with _make_client() as client:
            pull_space(client, "test", target)

        page_text = next(target.glob("*.md")).read_text(encoding="utf-8")
        assert page_text.endswith("Hello world\n")
        manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        assert (
            "conversion:local-fallback"
            in manifest["pages"]["p1"]["rich_content"]["unsafe_features"]
        )

    def test_stable_unversioned_split_content_uses_local_conversion(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": "p1",
                    "title": "Legacy Page",
                    "icon": "",
                    "hasChildren": False,
                    "children": [],
                }
            ],
        )
        for _ in range(2):
            httpx_mock.add_response(
                url=f"{_TEST_URL}/api/pages/info",
                json={
                    "id": "p1",
                    "title": "Legacy Page",
                    "icon": "",
                    "parentPageId": None,
                    "spaceId": "space-1",
                    "updatedAt": "revision-1",
                },
            )
            httpx_mock.add_response(
                url=f"{_TEST_URL}/api/pages/content",
                json={"data": {"content": _PM_DOC}},
            )

        with _make_client() as client:
            pull_space(client, "test", target)

        manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        entry = manifest["pages"]["p1"]
        assert entry["server_revision"]["updated_at"] == "revision-1"
        assert "conversion:local-fallback" in entry["rich_content"]["unsafe_features"]
        assert "conversion:unverified-revision" not in entry["rich_content"]["unsafe_features"]

    def test_blank_revision_tokens_require_stable_local_conversion(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": "p1",
                    "title": "Legacy Page",
                    "icon": "",
                    "hasChildren": False,
                    "children": [],
                }
            ],
        )
        old_content = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Old content"}],
                }
            ],
        }
        latest_content = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Latest content"}],
                }
            ],
        }
        page_metadata = {
            "id": "p1",
            "title": "Legacy Page",
            "icon": "",
            "parentPageId": None,
            "spaceId": "space-1",
            "updatedAt": "",
        }
        for content in (old_content, latest_content, latest_content):
            httpx_mock.add_response(
                url=f"{_TEST_URL}/api/pages/info",
                json=page_metadata,
            )
            httpx_mock.add_response(
                url=f"{_TEST_URL}/api/pages/content",
                json={"data": {"content": content, "updatedAt": ""}},
            )

        with _make_client() as client:
            pull_space(client, "test", target)

        manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        entry = manifest["pages"]["p1"]
        expected_revision = build_server_revision(
            {
                **page_metadata,
                "content": latest_content,
            }
        )
        assert entry["server_revision"]["fingerprint"] == expected_revision["fingerprint"]
        assert "conversion:local-fallback" in entry["rich_content"]["unsafe_features"]
        assert "conversion:unverified-revision" in entry["rich_content"]["unsafe_features"]
        page_text = next(target.glob("*.md")).read_text(encoding="utf-8")
        assert page_text.endswith("Latest content\n")

    def test_retries_when_raw_and_markdown_revisions_differ(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": "p1",
                    "title": "Changing Page",
                    "icon": "",
                    "hasChildren": False,
                    "children": [],
                }
            ],
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/info",
            json={
                "id": "p1",
                "title": "Changing Page",
                "updatedAt": "revision-1",
                "content": _PM_DOC,
            },
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/info",
            json={
                "id": "p1",
                "title": "Changing Page",
                "updatedAt": "revision-2",
                "content": "stale pairing",
            },
        )
        second_doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Second revision"}],
                }
            ],
        }
        _mock_page_content(
            httpx_mock,
            "p1",
            "Changing Page",
            second_doc,
            markdown_content="Second revision\n",
            updated_at="revision-2",
        )

        with _make_client() as client:
            pull_space(client, "test", target)

        page_text = next(target.glob("*.md")).read_text(encoding="utf-8")
        assert page_text.endswith("Second revision\n")
        manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        snapshot = target / manifest["pages"]["p1"]["rich_content"]["snapshot_path"]
        assert json.loads(snapshot.read_text(encoding="utf-8")) == second_doc


class TestPullAttachments:
    def test_downloads_assets_and_rewrites_page_to_local_path(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        attachment_id = "019c0000-1111-7222-8333-444444444444"
        pm_doc = {
            "type": "doc",
            "content": [
                {
                    "type": "image",
                    "attrs": {
                        "src": f"/api/files/{attachment_id}/diagram.png",
                        "alt": "Architecture",
                        "attachmentId": attachment_id,
                    },
                }
            ],
        }
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": "p1",
                    "title": "Page One",
                    "icon": "",
                    "hasChildren": False,
                    "children": [],
                }
            ],
        )
        _mock_page_content(httpx_mock, "p1", "Page One", pm_doc)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/files/info",
            json={
                "id": attachment_id,
                "fileName": "diagram.png",
                "mimeType": "image/png",
                "fileSize": 11,
                "pageId": "p1",
                "updatedAt": "2026-01-01T00:00:00.000Z",
            },
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/files/{attachment_id}/diagram.png",
            content=b"image-bytes",
        )

        with _make_client() as client:
            result = pull_space(client, "test", target)

        asset_path = target / "files" / attachment_id / "diagram.png"
        assert result.attachments_pulled == 1
        assert asset_path.read_bytes() == b"image-bytes"
        page_content = next(target.glob("*.md")).read_text()
        assert f"![Architecture](files/{attachment_id}/diagram.png)" in page_content
        manifest = json.loads((target / MANIFEST_FILENAME).read_text())
        assert manifest["pages"]["p1"]["attachment_ids"] == [attachment_id]
        assert manifest["assets"][attachment_id]["path"] == (f"files/{attachment_id}/diagram.png")
        assert manifest["assets"][attachment_id]["server_updated_at"] == "2026-01-01T00:00:00.000Z"


class TestPullWritesCorrectFrontmatter:
    """Verify frontmatter has id, title, parent_id, icon."""

    def test_frontmatter_fields(self, httpx_mock, tmp_path: Path) -> None:
        target = tmp_path / "test"

        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": "root-1",
                    "title": "Root Page",
                    "icon": "",
                    "hasChildren": True,
                    "children": [
                        {
                            "id": "child-1",
                            "title": "Child Page",
                            "icon": "",
                            "hasChildren": False,
                            "children": [],
                        }
                    ],
                },
            ],
        )

        # Mock content for root page
        _mock_page_content(httpx_mock, "root-1", "Root Page")
        # Mock content for child page
        _mock_page_content(
            httpx_mock,
            "child-1",
            "Child Page",
            parent_page_id="root-1",
        )

        with _make_client() as client:
            result = pull_space(client, "test", target)

        assert result.pages_pulled == 2

        # Find child file (its filename contains "child-1" prefix)
        child_files = [f for f in target.glob("*.md") if "child-1" in f.name]
        assert len(child_files) == 1

        content = child_files[0].read_text(encoding="utf-8")
        # Check frontmatter contains expected fields
        assert "---" in content
        assert "id: child-1" in content
        assert "title: Child Page" in content
        assert "parent_id: root-1" in content

        # Find root file
        root_files = [f for f in target.glob("*.md") if "root-1" in f.name]
        assert len(root_files) == 1
        root_content = root_files[0].read_text(encoding="utf-8")
        assert "id: root-1" in root_content
        # Root page has no parent, so parent_id is empty string
        assert "parent_id:" in root_content


class TestPullRefusesWithoutForce:
    """Existing manifest without --force should SystemExit."""

    def test_refuses(self, tmp_path: Path) -> None:
        target = tmp_path / "test"
        target.mkdir(parents=True)

        # Pre-create a manifest in the target directory
        manifest = {
            "version": MANIFEST_VERSION,
            "space_slug": "test",
            "space_id": "space-1",
            "synced_at": "2026-01-01T00:00:00+00:00",
            "pages": {"old-page": {"title": "Old", "filename": "Old--old-page.md"}},
        }
        (target / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")

        with _make_client() as client, pytest.raises(SystemExit):
            pull_space(client, "test", target, force=False)


class TestPullOverwritesWithForce:
    """Existing manifest with --force should succeed."""

    def test_overwrites(self, httpx_mock, tmp_path: Path) -> None:
        target = tmp_path / "test"
        target.mkdir(parents=True)

        # Pre-create a manifest in the target directory
        manifest = {
            "version": MANIFEST_VERSION,
            "space_slug": "test",
            "space_id": "space-1",
            "synced_at": "2026-01-01T00:00:00+00:00",
            "pages": {"old-page": {"title": "Old", "filename": "Old--old-page.md"}},
        }
        (target / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")

        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {"id": "p1", "title": "New Page", "icon": "", "hasChildren": False, "children": []},
            ],
        )
        _mock_page_content(httpx_mock, "p1", "New Page")

        with _make_client() as client:
            result = pull_space(client, "test", target, force=True)

        assert result.pages_pulled == 1

        # New manifest should have just the new page
        manifest_path = target / MANIFEST_FILENAME
        new_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert "p1" in new_manifest["pages"]
        assert "old-page" not in new_manifest["pages"]


class TestPullAtomicPublication:
    """Pull stages a complete snapshot and rolls back every failed attempt."""

    def test_partial_failure_preserves_previous_sync(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        old_filename = sanitize_filename("Previous Page", "old-page")
        old_body = "Previous body.\n"
        write_sync_file(
            target / old_filename,
            {
                "id": "old-page",
                "title": "Previous Page",
                "parent_id": "",
                "icon": "",
            },
            old_body,
        )
        save_manifest(
            target,
            build_manifest(
                "test",
                "space-1",
                [
                    {
                        "id": "old-page",
                        "title": "Previous Page",
                        "filename": old_filename,
                        "parent_id": None,
                        "icon": "",
                        "content_hash": compute_content_hash(old_body),
                    }
                ],
            ),
        )
        (target / "personal-notes.txt").write_text("keep me", encoding="utf-8")
        before = _snapshot_files(target)

        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": "p1",
                    "title": "Downloaded First",
                    "icon": "",
                    "hasChildren": False,
                    "children": [],
                },
                {
                    "id": "p2",
                    "title": "Fails Second",
                    "icon": "",
                    "hasChildren": False,
                    "children": [],
                },
            ],
        )
        attachment_id = "partial-download-attachment"
        _mock_page_content(
            httpx_mock,
            "p1",
            "Downloaded First",
            {
                "type": "doc",
                "content": [
                    {
                        "type": "image",
                        "attrs": {
                            "src": f"/api/files/{attachment_id}/partial.png",
                            "attachmentId": attachment_id,
                        },
                    }
                ],
            },
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/files/info",
            json={
                "id": attachment_id,
                "fileName": "partial.png",
                "mimeType": "image/png",
                "fileSize": 7,
                "pageId": "p1",
            },
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/files/{attachment_id}/partial.png",
            content=b"partial",
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/info",
            status_code=404,
            json={"message": "page unavailable"},
        )

        with _make_client() as client, pytest.raises(SystemExit):
            pull_space(client, "test", target, force=True)

        assert _snapshot_files(target) == before
        assert not list(tmp_path.glob(".test.pull-staging-*"))
        assert not list(tmp_path.glob(".test.pull-backup-*"))

    def test_publication_rename_failure_restores_previous_directory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        staging = tmp_path / ".test.pull-staging-manual"
        staging.mkdir()
        (staging / "new.txt").write_text("new", encoding="utf-8")
        real_noreplace = _rename_directory_noreplace

        def fail_staging_publish(source: Path, destination: Path) -> None:
            if source == staging:
                raise OSError("simulated publish failure")
            real_noreplace(source, destination)

        monkeypatch.setattr(
            "docmost_cli.sync.pull._rename_directory_noreplace",
            fail_staging_publish,
        )
        monkeypatch.setattr(
            "docmost_cli.sync.pull._atomic_exchange_directories",
            lambda _left, _right: False,
        )

        with pytest.raises(OSError, match="simulated publish failure"):
            _publish_staged_pull(staging, target)

        assert (target / "old.txt").read_text(encoding="utf-8") == "old"
        assert not (target / "new.txt").exists()
        assert not list(tmp_path.glob(".test.pull-backup-*"))
        assert not _publish_journal_path(target).exists()

    def test_recovery_restores_backup_after_interrupted_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        staging = _temporary_sibling(target, "pull-staging")
        (staging / "new.txt").write_text("new", encoding="utf-8")
        backup = _temporary_sibling(target, "pull-backup")
        backup.rmdir()
        journal = _write_publish_journal(target, staging, backup)
        os.replace(target, backup)
        _ACTIVE_PUBLISH_TOKENS.discard(journal["owner_token"])

        _recover_interrupted_publish(target)

        assert (target / "old.txt").read_text(encoding="utf-8") == "old"
        assert not staging.exists()
        assert not backup.exists()
        assert not _publish_journal_path(target).exists()

    def test_atomic_exchange_keeps_both_complete_snapshots(self, tmp_path: Path) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        staging = tmp_path / ".test.pull-staging-exchange"
        staging.mkdir()
        (staging / "new.txt").write_text("new", encoding="utf-8")

        if not _atomic_exchange_directories(staging, target):
            pytest.skip("atomic directory exchange is not available on this filesystem")

        assert (target / "new.txt").read_text(encoding="utf-8") == "new"
        assert (staging / "old.txt").read_text(encoding="utf-8") == "old"

    def test_intervening_local_change_aborts_publication(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        old_filename = sanitize_filename("Old Page", "same-page")
        write_sync_file(
            target / old_filename,
            {"id": "same-page", "title": "Old Page", "parent_id": "", "icon": ""},
            "old\n",
        )
        save_manifest(
            target,
            build_manifest(
                "test",
                "space-1",
                [
                    {
                        "id": "same-page",
                        "title": "Old Page",
                        "filename": old_filename,
                        "parent_id": None,
                        "icon": "",
                        "content_hash": compute_content_hash("old\n"),
                    }
                ],
            ),
        )
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": "same-page",
                    "title": "New Page",
                    "icon": "",
                    "hasChildren": False,
                    "children": [],
                }
            ],
        )

        def change_target_during_download(_request: httpx.Request) -> httpx.Response:
            (target / "intervening.txt").write_text("do not lose", encoding="utf-8")
            return httpx.Response(
                200,
                json={
                    "id": "same-page",
                    "title": "New Page",
                    "spaceId": "space-1",
                    "updatedAt": "2026-07-26T18:00:00.000Z",
                    "content": _PM_DOC,
                },
            )

        httpx_mock.add_callback(
            change_target_during_download,
            url=f"{_TEST_URL}/api/pages/info",
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/info",
            json={
                "id": "same-page",
                "title": "New Page",
                "spaceId": "space-1",
                "updatedAt": "2026-07-26T18:00:00.000Z",
                "content": "Hello world\n",
            },
        )
        with _make_client() as client, pytest.raises(RuntimeError, match="changed while"):
            pull_space(client, "test", target, force=True)

        assert (target / "intervening.txt").read_text(encoding="utf-8") == "do not lose"
        assert (target / old_filename).exists()
        assert not (target / sanitize_filename("New Page", "same-page")).exists()
        assert not list(tmp_path.glob(".test.pull-*"))

    def test_incomplete_child_tree_preserves_existing_target(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        existing = target / "existing.txt"
        existing.write_text("keep", encoding="utf-8")
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": "root",
                    "title": "Root",
                    "icon": "",
                    "hasChildren": True,
                    "children": [],
                }
            ],
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/sidebar-pages",
            status_code=404,
        )

        with _make_client() as client, pytest.raises(RuntimeError, match="complete child tree"):
            pull_space(client, "test", target, force=True)

        assert existing.read_text(encoding="utf-8") == "keep"
        assert not list(tmp_path.glob(".test.pull-*"))

    def test_change_during_atomic_exchange_is_rolled_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        expected_snapshot = _snapshot_target(target)
        staging = _temporary_sibling(target, "pull-staging")
        (staging / "new.txt").write_text("new", encoding="utf-8")
        real_exchange = _atomic_exchange_directories
        if not real_exchange(staging, target):
            pytest.skip("atomic directory exchange is not available on this filesystem")
        assert real_exchange(staging, target)
        exchange_count = 0

        def edit_then_exchange(left: Path, right: Path) -> bool:
            nonlocal exchange_count
            exchange_count += 1
            if exchange_count == 1:
                (target / "late-edit.txt").write_text("keep", encoding="utf-8")
            return real_exchange(left, right)

        monkeypatch.setattr(
            "docmost_cli.sync.pull._atomic_exchange_directories",
            edit_then_exchange,
        )
        if not real_exchange(staging, target):
            pytest.skip("atomic directory exchange is not available on this filesystem")
        assert real_exchange(staging, target)

        with pytest.raises(RuntimeError, match="changed while"):
            _publish_staged_pull(staging, target, expected_snapshot=expected_snapshot)

        assert (target / "old.txt").read_text(encoding="utf-8") == "old"
        assert (target / "late-edit.txt").read_text(encoding="utf-8") == "keep"
        assert (staging / "new.txt").read_text(encoding="utf-8") == "new"
        assert not _publish_journal_path(target).exists()

    def test_failed_conflict_rollback_is_never_auto_published(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        expected_snapshot = _snapshot_target(target)
        staging = _temporary_sibling(target, "pull-staging")
        (staging / "new.txt").write_text("new", encoding="utf-8")
        real_exchange = _atomic_exchange_directories
        exchange_count = 0

        def fail_rollback(left: Path, right: Path) -> bool:
            nonlocal exchange_count
            exchange_count += 1
            if exchange_count == 1:
                (target / "late-edit.txt").write_text("keep", encoding="utf-8")
                return real_exchange(left, right)
            return False

        monkeypatch.setattr(
            "docmost_cli.sync.pull._atomic_exchange_directories",
            fail_rollback,
        )

        with pytest.raises(RuntimeError, match="changed while"):
            _publish_staged_pull(staging, target, expected_snapshot=expected_snapshot)

        assert (target / "new.txt").read_text(encoding="utf-8") == "new"
        assert (staging / "old.txt").read_text(encoding="utf-8") == "old"
        assert (staging / "late-edit.txt").read_text(encoding="utf-8") == "keep"
        journal_path = _publish_journal_path(target)
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        assert payload["phase"] == "conflict"
        _ACTIVE_PUBLISH_TOKENS.discard(payload["owner_token"])

        with pytest.raises(RuntimeError, match="preserved publication conflict"):
            _recover_interrupted_publish(target)

        assert (target / "new.txt").exists()
        assert (staging / "late-edit.txt").exists()

    def test_conflict_rollback_preserves_intervening_target_replacement(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        expected_snapshot = _snapshot_target(target)
        staging = _temporary_sibling(target, "pull-staging")
        (staging / "new.txt").write_text("new", encoding="utf-8")
        generated_snapshot = tmp_path / "generated-snapshot"
        real_exchange = _atomic_exchange_directories
        if not real_exchange(staging, target):
            pytest.skip("atomic directory exchange is not available on this filesystem")
        assert real_exchange(staging, target)
        exchange_count = 0

        def replace_before_rollback(left: Path, right: Path) -> bool:
            nonlocal exchange_count
            exchange_count += 1
            if exchange_count == 1:
                (target / "late-edit.txt").write_text("keep", encoding="utf-8")
                return real_exchange(left, right)
            os.replace(target, generated_snapshot)
            target.mkdir()
            (target / "replacement.txt").write_text("replacement", encoding="utf-8")
            return real_exchange(left, right)

        monkeypatch.setattr(
            "docmost_cli.sync.pull._atomic_exchange_directories",
            replace_before_rollback,
        )

        with pytest.raises(RuntimeError, match="changed while"):
            _publish_staged_pull(staging, target, expected_snapshot=expected_snapshot)

        assert (target / "old.txt").read_text(encoding="utf-8") == "old"
        assert (target / "late-edit.txt").read_text(encoding="utf-8") == "keep"
        assert (staging / "replacement.txt").read_text(encoding="utf-8") == "replacement"
        assert (generated_snapshot / "new.txt").read_text(encoding="utf-8") == "new"
        assert _staging_is_recovery_data(target, staging)
        assert _publish_journal_path(target).exists()

    def test_active_publication_is_not_recovered(self, tmp_path: Path) -> None:
        target = tmp_path / "test"
        target.mkdir()
        staging = _temporary_sibling(target, "pull-staging")
        backup = _temporary_sibling(target, "pull-backup")
        journal = _write_publish_journal(target, staging, backup)

        with pytest.raises(RuntimeError, match="another pull publication is active"):
            _recover_interrupted_publish(target)

        assert target.exists()
        assert staging.exists()
        assert backup.exists()
        _ACTIVE_PUBLISH_TOKENS.discard(journal["owner_token"])
        _recover_interrupted_publish(target)

    def test_journal_unlink_failure_is_not_reported_as_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        staging = _temporary_sibling(target, "pull-staging")
        backup = _temporary_sibling(target, "pull-backup")
        journal = _write_publish_journal(target, staging, backup)
        journal_path = _publish_journal_path(target)
        real_unlink = Path.unlink

        def fail_journal_unlink(path: Path, *, missing_ok: bool = False) -> None:
            if path == journal_path:
                raise PermissionError("simulated journal unlink failure")
            real_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_journal_unlink)
        with pytest.raises(PermissionError, match="simulated journal unlink failure"):
            _remove_publish_journal(target)

        assert journal_path.exists()
        _ACTIVE_PUBLISH_TOKENS.discard(journal["owner_token"])

    def test_failed_exchange_rollback_preserves_old_staging_for_recovery(
        self,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        staging = _temporary_sibling(target, "pull-staging")
        (staging / "new.txt").write_text("new", encoding="utf-8")
        backup = _temporary_sibling(target, "pull-backup")
        journal = _write_publish_journal(target, staging, backup)
        if not _atomic_exchange_directories(staging, target):
            _ACTIVE_PUBLISH_TOKENS.discard(journal["owner_token"])
            pytest.skip("atomic directory exchange is not available on this filesystem")
        _ACTIVE_PUBLISH_TOKENS.discard(journal["owner_token"])

        assert _staging_is_recovery_data(target, staging)
        assert (staging / "old.txt").read_text(encoding="utf-8") == "old"

        _recover_interrupted_publish(target)
        assert (target / "new.txt").read_text(encoding="utf-8") == "new"
        assert not staging.exists()

    def test_post_exchange_fsync_failure_preserves_both_snapshots(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        staging = _temporary_sibling(target, "pull-staging")
        (staging / "new.txt").write_text("new", encoding="utf-8")
        if not _atomic_exchange_directories(staging, target):
            pytest.skip("atomic directory exchange is not available on this filesystem")
        assert _atomic_exchange_directories(staging, target)
        expected_snapshot = _snapshot_target(target)
        real_sync_directory = _sync_directory
        sync_count = 0

        def fail_after_exchange(path: Path) -> None:
            nonlocal sync_count
            sync_count += 1
            if sync_count == 2:
                (target / "post-exchange-edit.txt").write_text("keep", encoding="utf-8")
                raise OSError("simulated exchange fsync failure")
            real_sync_directory(path)

        monkeypatch.setattr(
            "docmost_cli.sync.pull._sync_directory",
            fail_after_exchange,
        )

        with pytest.raises(OSError, match="simulated exchange fsync failure"):
            _publish_staged_pull(staging, target, expected_snapshot=expected_snapshot)

        assert (target / "new.txt").read_text(encoding="utf-8") == "new"
        assert (target / "post-exchange-edit.txt").read_text(encoding="utf-8") == "keep"
        assert (staging / "old.txt").read_text(encoding="utf-8") == "old"
        assert _staging_is_recovery_data(target, staging)
        assert _publish_journal_path(target).exists()

    def test_recovery_preserves_backup_when_target_was_recreated(
        self,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        staging = _temporary_sibling(target, "pull-staging")
        (staging / "new.txt").write_text("new", encoding="utf-8")
        backup = _temporary_sibling(target, "pull-backup")
        backup.rmdir()
        journal = _write_publish_journal(target, staging, backup)
        os.replace(target, backup)
        target.mkdir()
        (target / "replacement.txt").write_text("replacement", encoding="utf-8")
        _ACTIVE_PUBLISH_TOKENS.discard(journal["owner_token"])

        with pytest.raises(RuntimeError, match="unexpected replacement"):
            _recover_interrupted_publish(target)

        assert (target / "replacement.txt").read_text(encoding="utf-8") == "replacement"
        assert (backup / "old.txt").read_text(encoding="utf-8") == "old"
        assert (staging / "new.txt").read_text(encoding="utf-8") == "new"
        assert _publish_journal_path(target).exists()

    def test_recovery_does_not_replace_target_appearing_during_restore(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        staging = _temporary_sibling(target, "pull-staging")
        (staging / "new.txt").write_text("new", encoding="utf-8")
        backup = _temporary_sibling(target, "pull-backup")
        backup.rmdir()
        journal = _write_publish_journal(target, staging, backup)
        os.replace(target, backup)
        _ACTIVE_PUBLISH_TOKENS.discard(journal["owner_token"])
        real_noreplace = _rename_directory_noreplace

        def recreate_before_restore(source: Path, destination: Path) -> None:
            if source == backup and destination == target:
                target.mkdir()
                (target / "replacement.txt").write_text("replacement", encoding="utf-8")
            real_noreplace(source, destination)

        monkeypatch.setattr(
            "docmost_cli.sync.pull._rename_directory_noreplace",
            recreate_before_restore,
        )

        with pytest.raises(RuntimeError, match="unexpected replacement"):
            _recover_interrupted_publish(target)

        assert (target / "replacement.txt").read_text(encoding="utf-8") == "replacement"
        assert (backup / "old.txt").read_text(encoding="utf-8") == "old"
        assert (staging / "new.txt").read_text(encoding="utf-8") == "new"
        assert _publish_journal_path(target).exists()

    def test_fsync_failure_after_publish_preserves_backup_and_journal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        staging = _temporary_sibling(target, "pull-staging")
        (staging / "new.txt").write_text("new", encoding="utf-8")
        real_sync_directory = _sync_directory
        sync_count = 0

        def fail_after_second_rename(path: Path) -> None:
            nonlocal sync_count
            sync_count += 1
            if sync_count == 4:
                raise OSError("simulated directory fsync failure")
            real_sync_directory(path)

        monkeypatch.setattr(
            "docmost_cli.sync.pull._atomic_exchange_directories",
            lambda _left, _right: False,
        )
        monkeypatch.setattr(
            "docmost_cli.sync.pull._sync_directory",
            fail_after_second_rename,
        )

        with pytest.raises(OSError, match="simulated directory fsync failure"):
            _publish_staged_pull(staging, target)

        backups = list(tmp_path.glob(".test.pull-backup-*"))
        assert (target / "new.txt").read_text(encoding="utf-8") == "new"
        assert len(backups) == 1
        assert (backups[0] / "old.txt").read_text(encoding="utf-8") == "old"
        journal_path = _publish_journal_path(target)
        assert journal_path.exists()

        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        _ACTIVE_PUBLISH_TOKENS.discard(payload["owner_token"])

        def fail_recovery_sync(_path: Path) -> None:
            raise OSError("recovery durability unavailable")

        monkeypatch.setattr(
            "docmost_cli.sync.pull._sync_directory",
            fail_recovery_sync,
        )
        with pytest.raises(OSError, match="recovery durability unavailable"):
            _recover_interrupted_publish(target)

        assert (backups[0] / "old.txt").read_text(encoding="utf-8") == "old"
        assert journal_path.exists()

    def test_fsync_failure_after_moving_target_rolls_back_immediately(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        staging = _temporary_sibling(target, "pull-staging")
        (staging / "new.txt").write_text("new", encoding="utf-8")
        real_sync_directory = _sync_directory
        sync_count = 0

        def fail_first_rename_sync(path: Path) -> None:
            nonlocal sync_count
            sync_count += 1
            if sync_count == 2:
                raise OSError("simulated first rename fsync failure")
            real_sync_directory(path)

        monkeypatch.setattr(
            "docmost_cli.sync.pull._atomic_exchange_directories",
            lambda _left, _right: False,
        )
        monkeypatch.setattr(
            "docmost_cli.sync.pull._sync_directory",
            fail_first_rename_sync,
        )

        with pytest.raises(OSError, match="simulated first rename fsync failure"):
            _publish_staged_pull(staging, target)

        assert (target / "old.txt").read_text(encoding="utf-8") == "old"
        assert (staging / "new.txt").read_text(encoding="utf-8") == "new"
        assert not list(tmp_path.glob(".test.pull-backup-*"))
        assert not _publish_journal_path(target).exists()

    def test_fsync_failure_does_not_overwrite_recreated_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        staging = _temporary_sibling(target, "pull-staging")
        (staging / "new.txt").write_text("new", encoding="utf-8")
        real_sync_directory = _sync_directory
        sync_count = 0

        def fail_after_recreation(path: Path) -> None:
            nonlocal sync_count
            sync_count += 1
            if sync_count == 2:
                target.mkdir()
                (target / "replacement.txt").write_text("replacement", encoding="utf-8")
                raise OSError("simulated first rename fsync failure")
            real_sync_directory(path)

        monkeypatch.setattr(
            "docmost_cli.sync.pull._atomic_exchange_directories",
            lambda _left, _right: False,
        )
        monkeypatch.setattr(
            "docmost_cli.sync.pull._sync_directory",
            fail_after_recreation,
        )

        with pytest.raises(OSError, match="simulated first rename fsync failure"):
            _publish_staged_pull(staging, target)

        backups = list(tmp_path.glob(".test.pull-backup-*"))
        assert (target / "replacement.txt").read_text(encoding="utf-8") == "replacement"
        assert len(backups) == 1
        assert (backups[0] / "old.txt").read_text(encoding="utf-8") == "old"
        assert (staging / "new.txt").read_text(encoding="utf-8") == "new"
        assert _publish_journal_path(target).exists()

    def test_fallback_publish_does_not_replace_recreated_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        staging = _temporary_sibling(target, "pull-staging")
        (staging / "new.txt").write_text("new", encoding="utf-8")
        real_replace = os.replace

        def recreate_after_target_move(
            source: str | os.PathLike[str],
            destination: str | os.PathLike[str],
        ) -> None:
            real_replace(source, destination)
            if Path(source) == target:
                target.mkdir()
                (target / "replacement.txt").write_text("replacement", encoding="utf-8")

        monkeypatch.setattr(
            "docmost_cli.sync.pull._atomic_exchange_directories",
            lambda _left, _right: False,
        )
        monkeypatch.setattr("docmost_cli.sync.pull.os.replace", recreate_after_target_move)

        with pytest.raises(FileExistsError):
            _publish_staged_pull(staging, target)

        backups = list(tmp_path.glob(".test.pull-backup-*"))
        assert (target / "replacement.txt").read_text(encoding="utf-8") == "replacement"
        assert len(backups) == 1
        assert (backups[0] / "old.txt").read_text(encoding="utf-8") == "old"
        assert (staging / "new.txt").read_text(encoding="utf-8") == "new"
        assert _publish_journal_path(target).exists()

    def test_initial_target_appearance_is_not_replaced(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        staging = _temporary_sibling(target, "pull-staging")
        (staging / "new.txt").write_text("new", encoding="utf-8")

        def target_appeared(_source: Path, destination: Path) -> None:
            destination.mkdir()
            (destination / "intervening.txt").write_text("keep", encoding="utf-8")
            raise FileExistsError(destination)

        monkeypatch.setattr(
            "docmost_cli.sync.pull._rename_directory_noreplace",
            target_appeared,
        )

        with pytest.raises(RuntimeError, match="appeared while"):
            _publish_staged_pull(staging, target, expected_snapshot=None)

        assert (target / "intervening.txt").read_text(encoding="utf-8") == "keep"
        assert (staging / "new.txt").read_text(encoding="utf-8") == "new"


class TestPullManagedCleanup:
    """Forced pulls replace managed state while retaining unrelated files."""

    def test_removes_stale_and_renamed_managed_files_only(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        current_id = "same-page-id"
        deleted_id = "deleted-page-id"
        old_filename = sanitize_filename("Old Title", current_id)
        deleted_filename = sanitize_filename("Deleted Page", deleted_id)
        for page_id, title, filename in (
            (current_id, "Old Title", old_filename),
            (deleted_id, "Deleted Page", deleted_filename),
        ):
            write_sync_file(
                target / filename,
                {"id": page_id, "title": title, "parent_id": "", "icon": ""},
                "old\n",
            )

        old_asset = target / "files" / "old-asset" / "old.png"
        stale_asset = target / "files" / "stale-asset" / "stale.png"
        unrelated_asset = target / "files" / "manual" / "readme.txt"
        for path, content in (
            (old_asset, b"old"),
            (stale_asset, b"stale"),
            (unrelated_asset, b"unrelated"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (target / "personal-notes.md").write_text("unrelated notes", encoding="utf-8")
        raw_pages = target / ".docmost" / "raw-pages"
        raw_pages.mkdir(parents=True)
        current_snapshot = raw_pages / f"{current_id}.json"
        deleted_snapshot = raw_pages / f"{deleted_id}.json"
        current_snapshot.write_text('{"old":"current"}\n', encoding="utf-8")
        deleted_snapshot.write_text('{"old":"deleted"}\n', encoding="utf-8")
        unrelated_internal = target / ".docmost" / "local-note.txt"
        unrelated_internal.write_text("keep", encoding="utf-8")

        save_manifest(
            target,
            build_manifest(
                "test",
                "space-1",
                [
                    {
                        "id": current_id,
                        "title": "Old Title",
                        "filename": old_filename,
                        "parent_id": None,
                        "icon": "",
                        "content_hash": compute_content_hash("old\n"),
                        "rich_content": {"snapshot_path": f".docmost/raw-pages/{current_id}.json"},
                    },
                    {
                        "id": deleted_id,
                        "title": "Deleted Page",
                        "filename": deleted_filename,
                        "parent_id": None,
                        "icon": "",
                        "content_hash": compute_content_hash("old\n"),
                        "rich_content": {"snapshot_path": f".docmost/raw-pages/{deleted_id}.json"},
                    },
                ],
                {
                    "old-asset": {"path": "files/old-asset/old.png"},
                    "stale-asset": {"path": "files/stale-asset/stale.png"},
                },
            ),
        )

        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": current_id,
                    "title": "New Title",
                    "icon": "",
                    "hasChildren": False,
                    "children": [],
                }
            ],
        )
        _mock_page_content(httpx_mock, current_id, "New Title")

        with _make_client() as client:
            pull_space(client, "test", target, force=True)

        new_filename = sanitize_filename("New Title", current_id)
        assert (target / new_filename).exists()
        assert not (target / old_filename).exists()
        assert not (target / deleted_filename).exists()
        assert not old_asset.exists()
        assert not stale_asset.exists()
        assert unrelated_asset.read_bytes() == b"unrelated"
        assert (target / "personal-notes.md").read_text(encoding="utf-8") == "unrelated notes"
        assert json.loads(current_snapshot.read_text(encoding="utf-8")) == _PM_DOC
        assert not deleted_snapshot.exists()
        assert unrelated_internal.read_text(encoding="utf-8") == "keep"
        manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        assert set(manifest["pages"]) == {current_id}
        assert manifest["assets"] == {}

    def test_empty_space_removes_managed_state_and_keeps_unrelated_files(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        filename = sanitize_filename("Old Page", "old-page")
        write_sync_file(
            target / filename,
            {"id": "old-page", "title": "Old Page", "parent_id": "", "icon": ""},
            "old\n",
        )
        save_manifest(
            target,
            build_manifest(
                "test",
                "space-1",
                [
                    {
                        "id": "old-page",
                        "title": "Old Page",
                        "filename": filename,
                        "parent_id": None,
                        "icon": "",
                        "content_hash": compute_content_hash("old\n"),
                    }
                ],
            ),
        )
        unrelated = target / "README.txt"
        unrelated.write_text("local-only", encoding="utf-8")

        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(httpx_mock, [])

        with _make_client() as client:
            result = pull_space(client, "test", target, force=True)

        assert result.pages_pulled == 0
        assert not (target / filename).exists()
        assert unrelated.read_text(encoding="utf-8") == "local-only"
        manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        assert manifest["pages"] == {}
        assert manifest["assets"] == {}


class TestPullPathSafety:
    """Staging paths stay outside the target on every supported platform."""

    def test_normalizes_parent_segments_before_creating_staging(
        self,
        httpx_mock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "local.txt").write_text("keep", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(httpx_mock, [])

        with _make_client() as client:
            pull_space(client, "test", Path("workspace") / "child" / "..")

        assert (workspace / MANIFEST_FILENAME).exists()
        assert (workspace / "local.txt").read_text(encoding="utf-8") == "keep"
        assert not list(workspace.glob(".*.pull-staging-*"))
        assert not list(tmp_path.glob(".workspace.pull-*"))

    def test_rejects_filesystem_root(self, tmp_path: Path) -> None:
        root = Path(tmp_path.anchor)

        with _make_client() as client, pytest.raises(SystemExit):
            pull_space(client, "test", root)

    def test_rejects_current_directory_inside_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        current = target / "nested"
        current.mkdir(parents=True)
        monkeypatch.chdir(current)

        with _make_client() as client, pytest.raises(SystemExit):
            pull_space(client, "test", target)

    def test_rejects_target_below_symlinked_parent(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        linked_parent = tmp_path / "linked"
        try:
            linked_parent.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable")
        target = linked_parent / "sync"

        with _make_client() as client, pytest.raises(SystemExit):
            pull_space(client, "test", target)

        assert not target.exists()
        assert not list(outside.iterdir())
        assert not list(tmp_path.glob(".sync.pull-*"))

    def test_rejects_managed_cleanup_through_symlinked_parent(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        external_file = outside / "external.txt"
        external_file.write_text("must survive", encoding="utf-8")
        linked_parent = target / "files" / "asset-id"
        linked_parent.parent.mkdir()
        try:
            linked_parent.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable")
        save_manifest(
            target,
            build_manifest(
                "test",
                "space-1",
                [],
                {"asset-id": {"path": "files/asset-id/external.txt"}},
            ),
        )
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(httpx_mock, [])

        with _make_client() as client, pytest.raises(FileExistsError, match="symlink"):
            pull_space(client, "test", target, force=True)

        assert external_file.read_text(encoding="utf-8") == "must survive"
        assert linked_parent.is_symlink()
        assert not list(tmp_path.glob(".test.pull-*"))

    def test_rejects_raw_snapshot_write_through_symlinked_parent(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "test"
        target.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        linked_parent = target / ".docmost"
        try:
            linked_parent.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable")
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(
            httpx_mock,
            [
                {
                    "id": "page-1",
                    "title": "Page",
                    "icon": "",
                    "hasChildren": False,
                    "children": [],
                }
            ],
        )
        _mock_page_content(httpx_mock, "page-1", "Page")

        with _make_client() as client, pytest.raises(FileExistsError, match="symlink"):
            pull_space(client, "test", target, force=True)

        assert linked_parent.is_symlink()
        assert not list(outside.iterdir())
        assert not list(tmp_path.glob(".test.pull-*"))

    def test_new_target_uses_normal_mkdir_permissions(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        comparison = tmp_path / "normal-directory"
        comparison.mkdir()
        expected_mode = stat.S_IMODE(comparison.stat().st_mode)
        target = tmp_path / "test"
        _mock_resolve_space(httpx_mock)
        _mock_sidebar_pages(httpx_mock, [])

        with _make_client() as client:
            pull_space(client, "test", target)

        assert stat.S_IMODE(target.stat().st_mode) == expected_mode
