"""Tests for the sync push module."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from docmost_cli.api.client import DocmostClient
from docmost_cli.api.pages import try_update_page_content
from docmost_cli.config.settings import DocmostSettings
from docmost_cli.sync.assets import compute_file_hash
from docmost_cli.sync.diff import PageChange
from docmost_cli.sync.frontmatter import read_sync_file, write_sync_file
from docmost_cli.sync.manifest import (
    build_page_entry,
    compute_content_hash,
    load_manifest,
    save_manifest,
)
from docmost_cli.sync.push import (
    PushResult,
    _community_replace,
    _topological_sort,
    push_space,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_URL = "https://docs.example.com"

FAKE_PAGE_ID = "019a2a69-bbbb-cccc-dddd-eeeeeeeeeeee"
FAKE_PAGE_ID_2 = "029b3b79-aaaa-bbbb-cccc-ffffffffffff"
FAKE_PAGE_ID_3 = "039c4c89-1111-2222-3333-444444444444"
FAKE_SPACE_ID = "space-uuid-123"


def _make_client() -> DocmostClient:
    settings = DocmostSettings(url=_TEST_URL, api_key="dm_test1234567890")
    return DocmostClient(settings)


def _make_manifest(pages: dict | None = None) -> dict:
    """Build a minimal manifest dict for testing."""
    return {
        "version": 1,
        "space_slug": "eng",
        "space_id": FAKE_SPACE_ID,
        "synced_at": "2026-01-01T00:00:00+00:00",
        "pages": pages or {},
    }


def _write_page(
    dir_path: Path,
    filename: str,
    *,
    page_id: str = "",
    title: str = "Test Page",
    parent_id: str = "",
    icon: str = "",
    body: str = "Some content.\n",
) -> None:
    """Write a sync file with given metadata and body."""
    meta: dict[str, str] = {}
    if page_id is not None:
        meta["id"] = page_id
    meta["title"] = title
    if parent_id:
        meta["parent_id"] = parent_id
    if icon:
        meta["icon"] = icon
    write_sync_file(dir_path / filename, meta, body)


def _setup_synced_dir(
    tmp_path: Path,
    pages: dict | None = None,
) -> Path:
    """Create a directory with manifest. Returns the directory path."""
    target = tmp_path / "eng"
    target.mkdir(parents=True, exist_ok=True)
    manifest = _make_manifest(pages)
    save_manifest(target, manifest)
    return target


def _mock_resolve_space(httpx_mock, slug: str = "eng") -> None:
    """Add mock for resolve_space_id (calls list_spaces -> POST /spaces)."""
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/spaces",
        json={"data": {"items": [{"id": FAKE_SPACE_ID, "slug": slug, "name": slug.capitalize()}]}},
    )


# ---------------------------------------------------------------------------
# PushResult dataclass
# ---------------------------------------------------------------------------


class TestPushResult:
    """Tests for PushResult dataclass defaults."""

    def test_defaults(self) -> None:
        result = PushResult()
        assert result.created == 0
        assert result.updated == 0
        assert result.moved == 0
        assert result.deleted == 0
        assert result.unchanged == 0
        assert result.id_remaps == {}


# ---------------------------------------------------------------------------
# _topological_sort — pure unit tests
# ---------------------------------------------------------------------------


class TestTopologicalSort:
    """Tests for _topological_sort ordering new pages."""

    def test_no_parents(self) -> None:
        """Pages with no parent are returned in original order."""
        changes = [
            PageChange(page_id="", filename="a.md", local_meta={"title": "A"}),
            PageChange(page_id="", filename="b.md", local_meta={"title": "B"}),
        ]
        result = _topological_sort(changes, set())
        assert len(result) == 2
        assert result[0].filename == "a.md"
        assert result[1].filename == "b.md"

    def test_parent_exists_on_server(self) -> None:
        """Pages whose parent is already on server come first."""
        changes = [
            PageChange(
                page_id="",
                filename="child.md",
                local_meta={"title": "Child", "parent_id": "existing-parent"},
            ),
        ]
        result = _topological_sort(changes, {"existing-parent"})
        assert len(result) == 1
        assert result[0].filename == "child.md"

    def test_parent_is_also_new(self) -> None:
        """Child should come after parent when both are new.

        Note: Since new pages don't have IDs yet, topological sort
        resolves parents that are in existing_ids. A new parent won't
        be in existing_ids, so child goes after parent is added to resolved.
        Actually, new pages without IDs can't reference each other by ID,
        so if parent_id references something not in existing_ids and not
        resolvable, the page gets added at the end.
        """
        parent = PageChange(page_id="", filename="parent.md", local_meta={"title": "Parent"})
        child = PageChange(
            page_id="",
            filename="child.md",
            local_meta={"title": "Child", "parent_id": "nonexistent"},
        )
        result = _topological_sort([child, parent], set())
        # Parent has no parent_id, so it resolves first.
        # Child has parent_id="nonexistent" which is never resolved,
        # so it gets appended at the end of the no-progress fallback.
        assert result[0].filename == "parent.md"
        assert result[1].filename == "child.md"

    def test_empty_list(self) -> None:
        """Empty input returns empty output."""
        result = _topological_sort([], set())
        assert result == []

    def test_circular_reference(self) -> None:
        """Circular references are handled gracefully (appended at end)."""
        a = PageChange(
            page_id="",
            filename="a.md",
            local_meta={"title": "A", "parent_id": "id-b"},
        )
        b = PageChange(
            page_id="",
            filename="b.md",
            local_meta={"title": "B", "parent_id": "id-a"},
        )
        result = _topological_sort([a, b], set())
        # Both have unresolvable parents, so both get appended
        assert len(result) == 2


# ---------------------------------------------------------------------------
# try_update_page_content — unit test with httpx_mock
# ---------------------------------------------------------------------------


class TestTryContentUpdate:
    """Tests for try_update_page_content probing."""

    def test_success(self, httpx_mock) -> None:
        """Returns True when the core page update endpoint succeeds."""
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/update",
            json={"data": {"id": FAKE_PAGE_ID}},
        )
        with _make_client() as client:
            result = try_update_page_content(client, page_id=FAKE_PAGE_ID, content="new content")
        assert result is True

    def test_failure_404(self, httpx_mock) -> None:
        """Returns False when endpoint returns 404."""
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/update",
            status_code=404,
        )
        with _make_client() as client:
            result = try_update_page_content(client, page_id=FAKE_PAGE_ID, content="content")
        assert result is False


# ---------------------------------------------------------------------------
# _community_replace — integration test
# ---------------------------------------------------------------------------


class TestCommunityUpdate:
    """Tests for _community_replace create-then-delete."""

    def test_create_then_delete(self, httpx_mock) -> None:
        """Creates new page, then deletes old one. Returns new ID."""
        new_page_id = "new-page-id-1234"

        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/create",
            json={"data": {"id": new_page_id}},
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/delete",
            json={"data": {}},
        )

        with _make_client() as client:
            result_id = _community_replace(
                client,
                space_id=FAKE_SPACE_ID,
                old_page_id=FAKE_PAGE_ID,
                title="Updated Page",
                content="New body",
                parent_id="parent-123",
                icon="rocket",
            )

        assert result_id == new_page_id

        requests = httpx_mock.get_requests()
        urls = [str(r.url) for r in requests]
        assert f"{_TEST_URL}/api/pages/create" in urls[0]
        assert f"{_TEST_URL}/api/pages/delete" in urls[1]
        assert json.loads(requests[0].content)["parentPageId"] == "parent-123"
        assert json.loads(requests[0].content)["icon"] == "rocket"

    def test_no_parent_no_icon(self, httpx_mock) -> None:
        """Skips move and icon update when not needed."""
        new_page_id = "new-page-id-5678"

        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/create",
            json={"id": new_page_id},
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/delete",
            json={"data": {}},
        )

        with _make_client() as client:
            result_id = _community_replace(
                client,
                space_id=FAKE_SPACE_ID,
                old_page_id=FAKE_PAGE_ID,
                title="Simple Page",
                content="Body",
                parent_id=None,
                icon="",
            )

        assert result_id == new_page_id
        requests = httpx_mock.get_requests()
        assert len(requests) == 2  # Only create + delete


# ---------------------------------------------------------------------------
# push_space — no changes
# ---------------------------------------------------------------------------


class TestPushNoChanges:
    """push_space with files matching manifest returns no-op."""

    def test_no_changes(self, httpx_mock, tmp_path: Path) -> None:
        body = "Hello world.\n"
        content_hash = compute_content_hash(body)

        target = _setup_synced_dir(
            tmp_path,
            pages={
                FAKE_PAGE_ID: build_page_entry(
                    title="My Page",
                    filename="my-page.md",
                    parent_id=None,
                    icon="",
                    content_hash=content_hash,
                )
            },
        )
        _write_page(
            target,
            "my-page.md",
            page_id=FAKE_PAGE_ID,
            title="My Page",
            body=body,
        )

        _mock_resolve_space(httpx_mock)

        with _make_client() as client:
            result = push_space(client, "eng", target)

        assert isinstance(result, PushResult)
        assert result.created == 0
        assert result.updated == 0
        assert result.moved == 0
        assert result.deleted == 0
        assert result.unchanged == 1


# ---------------------------------------------------------------------------
# push_space — new page (create)
# ---------------------------------------------------------------------------


class TestPushNewPage:
    """push_space creates new pages through /pages/create."""

    def test_create_new_page(self, httpx_mock, tmp_path: Path) -> None:
        new_page_id = "created-page-id-1"

        target = _setup_synced_dir(tmp_path, pages={})
        _write_page(
            target,
            "new-page.md",
            page_id="",
            title="New Page",
            body="Fresh content.\n",
        )

        _mock_resolve_space(httpx_mock)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/create",
            json={"id": new_page_id},
        )

        with _make_client() as client:
            result = push_space(client, "eng", target)

        assert result.created == 1

        # Verify the file now has the ID in frontmatter
        meta, body = read_sync_file(target / "new-page.md")
        assert meta["id"] == new_page_id

        # Verify manifest was updated
        manifest = load_manifest(target)
        assert new_page_id in manifest["pages"]

    def test_create_new_page_with_parent_and_icon(self, httpx_mock, tmp_path: Path) -> None:
        """New page sends parent and icon in the create request."""
        new_page_id = "created-page-id-2"
        parent_id = "existing-parent-id"

        target = _setup_synced_dir(
            tmp_path,
            pages={
                parent_id: build_page_entry(
                    title="Parent",
                    filename="parent.md",
                    parent_id=None,
                    icon="",
                    content_hash=compute_content_hash("Parent body.\n"),
                )
            },
        )
        _write_page(
            target,
            "parent.md",
            page_id=parent_id,
            title="Parent",
            body="Parent body.\n",
        )
        _write_page(
            target,
            "child-new.md",
            page_id="",
            title="Child Page",
            parent_id=parent_id,
            icon="star",
            body="Child content.\n",
        )

        _mock_resolve_space(httpx_mock)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/create",
            json={"id": new_page_id},
        )

        with _make_client() as client:
            result = push_space(client, "eng", target)

        assert result.created == 1
        assert result.unchanged == 1  # parent is unchanged
        create_request = next(
            request for request in httpx_mock.get_requests() if "/pages/create" in str(request.url)
        )
        create_body = json.loads(create_request.content)
        assert create_body["parentPageId"] == parent_id
        assert create_body["icon"] == "star"


# ---------------------------------------------------------------------------
# push_space — content update
# ---------------------------------------------------------------------------


class TestPushContentUpdate:
    """push_space updates content in place through the core page endpoint."""

    def test_enterprise_content_update(self, httpx_mock, tmp_path: Path) -> None:
        old_body = "Old content.\n"
        new_body = "Updated content.\n"

        target = _setup_synced_dir(
            tmp_path,
            pages={
                FAKE_PAGE_ID: build_page_entry(
                    title="My Page",
                    filename="my-page.md",
                    parent_id=None,
                    icon="",
                    content_hash=compute_content_hash(old_body),
                )
            },
        )
        _write_page(
            target,
            "my-page.md",
            page_id=FAKE_PAGE_ID,
            title="My Page",
            body=new_body,
        )

        _mock_resolve_space(httpx_mock)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/update",
            json={"data": {"id": FAKE_PAGE_ID}},
        )

        with _make_client() as client:
            result = push_space(client, "eng", target)

        assert result.updated == 1
        assert result.id_remaps == {}

        # Manifest should be updated with new hash
        manifest = load_manifest(target)
        assert manifest["pages"][FAKE_PAGE_ID]["content_hash"] == compute_content_hash(new_body)


# ---------------------------------------------------------------------------
# push_space — content update preserves IDs on Community too
# ---------------------------------------------------------------------------


class TestPushContentUpdateCommunity:
    """The shared core endpoint avoids destructive Community page replacement."""

    def test_community_fallback(self, httpx_mock, tmp_path: Path) -> None:
        old_body = "Old content.\n"
        new_body = "Updated content.\n"

        target = _setup_synced_dir(
            tmp_path,
            pages={
                FAKE_PAGE_ID: build_page_entry(
                    title="My Page",
                    filename="my-page.md",
                    parent_id=None,
                    icon="",
                    content_hash=compute_content_hash(old_body),
                )
            },
        )
        _write_page(
            target,
            "my-page.md",
            page_id=FAKE_PAGE_ID,
            title="My Page",
            body=new_body,
        )

        _mock_resolve_space(httpx_mock)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/update",
            json={"data": {"id": FAKE_PAGE_ID}},
        )

        with _make_client() as client:
            result = push_space(client, "eng", target)

        assert result.updated == 1
        assert result.id_remaps == {}

        # Frontmatter and manifest retain the stable page ID.
        meta, _ = read_sync_file(target / "my-page.md")
        assert meta["id"] == FAKE_PAGE_ID

        manifest = load_manifest(target)
        assert FAKE_PAGE_ID in manifest["pages"]


class TestPushAttachmentUpdate:
    def test_changed_asset_reuses_attachment_id_and_updates_page_reference(
        self,
        httpx_mock,
        tmp_path: Path,
    ) -> None:
        attachment_id = "019c0000-1111-7222-8333-444444444444"
        relative_path = f"files/{attachment_id}/diagram.png"
        body = f"![Architecture]({relative_path})\n"
        target = _setup_synced_dir(
            tmp_path,
            pages={
                FAKE_PAGE_ID: build_page_entry(
                    title="My Page",
                    filename="my-page.md",
                    parent_id=None,
                    icon="",
                    content_hash=compute_content_hash(body),
                    attachment_ids=[attachment_id],
                )
            },
        )
        _write_page(
            target,
            "my-page.md",
            page_id=FAKE_PAGE_ID,
            title="My Page",
            body=body,
        )
        asset = target / relative_path
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"new-image-bytes")
        manifest = load_manifest(target)
        manifest["assets"] = {
            attachment_id: {
                "file_name": "diagram.png",
                "path": relative_path,
                "mime_type": "image/png",
                "size": 3,
                "page_id": FAKE_PAGE_ID,
                "content_hash": "sha256:old",
                "server_path": f"/api/files/{attachment_id}/diagram.png",
            }
        }
        save_manifest(target, manifest)

        _mock_resolve_space(httpx_mock)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/files/upload",
            json={
                "id": attachment_id,
                "fileName": "diagram.png",
                "mimeType": "image/png",
                "fileSize": asset.stat().st_size,
                "pageId": FAKE_PAGE_ID,
            },
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/update",
            json={"id": FAKE_PAGE_ID},
        )

        with _make_client() as client:
            result = push_space(client, "eng", target)

        assert result.updated == 1
        upload_body = httpx_mock.get_requests()[1].read()
        assert attachment_id.encode() in upload_body
        update_payload = json.loads(httpx_mock.get_requests()[2].content)
        assert f'data-attachment-id="{attachment_id}"' in update_payload["content"]
        updated_manifest = load_manifest(target)
        assert updated_manifest["assets"][attachment_id]["content_hash"] == compute_file_hash(asset)


# ---------------------------------------------------------------------------
# push_space — title/icon change only
# ---------------------------------------------------------------------------


class TestPushMetaChange:
    """push_space updates title/icon via update_page_meta."""

    def test_title_change(self, httpx_mock, tmp_path: Path) -> None:
        body = "Same content.\n"
        content_hash = compute_content_hash(body)

        target = _setup_synced_dir(
            tmp_path,
            pages={
                FAKE_PAGE_ID: build_page_entry(
                    title="Old Title",
                    filename="page.md",
                    parent_id=None,
                    icon="",
                    content_hash=content_hash,
                )
            },
        )
        _write_page(
            target,
            "page.md",
            page_id=FAKE_PAGE_ID,
            title="New Title",
            body=body,
        )

        _mock_resolve_space(httpx_mock)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/update",
            json={"data": {"id": FAKE_PAGE_ID}},
        )

        with _make_client() as client:
            result = push_space(client, "eng", target)

        assert result.updated == 1

        # Verify update_page_meta was called
        requests = httpx_mock.get_requests()
        update_requests = [r for r in requests if str(r.url) == f"{_TEST_URL}/api/pages/update"]
        assert len(update_requests) == 1
        body_json = json.loads(update_requests[0].content)
        assert body_json["title"] == "New Title"

    def test_icon_change(self, httpx_mock, tmp_path: Path) -> None:
        body = "Same content.\n"
        content_hash = compute_content_hash(body)

        target = _setup_synced_dir(
            tmp_path,
            pages={
                FAKE_PAGE_ID: build_page_entry(
                    title="My Page",
                    filename="page.md",
                    parent_id=None,
                    icon="old-icon",
                    content_hash=content_hash,
                )
            },
        )
        _write_page(
            target,
            "page.md",
            page_id=FAKE_PAGE_ID,
            title="My Page",
            icon="new-icon",
            body=body,
        )

        _mock_resolve_space(httpx_mock)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/update",
            json={"data": {"id": FAKE_PAGE_ID}},
        )

        with _make_client() as client:
            result = push_space(client, "eng", target)

        assert result.updated == 1


# ---------------------------------------------------------------------------
# push_space — move only
# ---------------------------------------------------------------------------


class TestPushMoveOnly:
    """push_space moves pages with changed parent_id."""

    def test_move_page(self, httpx_mock, tmp_path: Path) -> None:
        body = "Content.\n"
        content_hash = compute_content_hash(body)

        target = _setup_synced_dir(
            tmp_path,
            pages={
                FAKE_PAGE_ID: build_page_entry(
                    title="Moved Page",
                    filename="moved.md",
                    parent_id="old-parent",
                    icon="",
                    content_hash=content_hash,
                )
            },
        )
        _write_page(
            target,
            "moved.md",
            page_id=FAKE_PAGE_ID,
            title="Moved Page",
            parent_id="new-parent",
            body=body,
        )

        _mock_resolve_space(httpx_mock)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/move",
            json={"data": {"id": FAKE_PAGE_ID}},
        )

        with _make_client() as client:
            result = push_space(client, "eng", target)

        assert result.moved == 1
        assert result.updated == 0

        # Verify manifest updated
        manifest = load_manifest(target)
        assert manifest["pages"][FAKE_PAGE_ID]["parent_id"] == "new-parent"


# ---------------------------------------------------------------------------
# push_space — combined update and move
# ---------------------------------------------------------------------------


class TestPushUpdateAndMove:
    """push_space applies both operations when a modified page also moves."""

    def test_content_update_then_move(self, httpx_mock, tmp_path: Path, capsys) -> None:
        old_body = "Old content.\n"
        new_body = "New content.\n"
        target = _setup_synced_dir(
            tmp_path,
            pages={
                FAKE_PAGE_ID: build_page_entry(
                    title="My Page",
                    filename="page.md",
                    parent_id="old-parent",
                    icon="",
                    content_hash=compute_content_hash(old_body),
                )
            },
        )
        _write_page(
            target,
            "page.md",
            page_id=FAKE_PAGE_ID,
            title="My Page",
            parent_id="new-parent",
            body=new_body,
        )

        _mock_resolve_space(httpx_mock)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/update",
            json={"data": {"id": FAKE_PAGE_ID}},
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/move",
            json={"data": {"id": FAKE_PAGE_ID}},
        )

        with _make_client() as client:
            result = push_space(client, "eng", target)

        assert result.updated == 1
        assert result.moved == 1
        requests = httpx_mock.get_requests()
        assert [request.url.path for request in requests] == [
            "/api/spaces",
            "/api/pages/update",
            "/api/pages/move",
        ]
        assert json.loads(requests[1].content)["content"] == new_body
        assert json.loads(requests[2].content)["parentPageId"] == "new-parent"

        manifest = load_manifest(target)
        assert manifest["pages"][FAKE_PAGE_ID]["content_hash"] == compute_content_hash(new_body)
        assert manifest["pages"][FAKE_PAGE_ID]["parent_id"] == "new-parent"
        assert "1 updated, 1 moved" in capsys.readouterr().err

    def test_metadata_update_then_move(self, httpx_mock, tmp_path: Path) -> None:
        body = "Same content.\n"
        target = _setup_synced_dir(
            tmp_path,
            pages={
                FAKE_PAGE_ID: build_page_entry(
                    title="Old Title",
                    filename="page.md",
                    parent_id="old-parent",
                    icon="old-icon",
                    content_hash=compute_content_hash(body),
                )
            },
        )
        _write_page(
            target,
            "page.md",
            page_id=FAKE_PAGE_ID,
            title="New Title",
            parent_id="new-parent",
            icon="new-icon",
            body=body,
        )

        _mock_resolve_space(httpx_mock)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/update",
            json={"data": {"id": FAKE_PAGE_ID}},
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/move",
            json={"data": {"id": FAKE_PAGE_ID}},
        )

        with _make_client() as client:
            result = push_space(client, "eng", target)

        assert result.updated == 1
        assert result.moved == 1
        requests = httpx_mock.get_requests()
        update_payload = json.loads(requests[1].content)
        assert update_payload["title"] == "New Title"
        assert update_payload["icon"] == "new-icon"
        assert json.loads(requests[2].content)["parentPageId"] == "new-parent"

        manifest = load_manifest(target)
        assert manifest["pages"][FAKE_PAGE_ID]["title"] == "New Title"
        assert manifest["pages"][FAKE_PAGE_ID]["icon"] == "new-icon"
        assert manifest["pages"][FAKE_PAGE_ID]["parent_id"] == "new-parent"

    def test_failed_move_does_not_advance_manifest_parent(self, httpx_mock, tmp_path: Path) -> None:
        old_body = "Old content.\n"
        new_body = "New content.\n"
        target = _setup_synced_dir(
            tmp_path,
            pages={
                FAKE_PAGE_ID: build_page_entry(
                    title="My Page",
                    filename="page.md",
                    parent_id="old-parent",
                    icon="",
                    content_hash=compute_content_hash(old_body),
                )
            },
        )
        _write_page(
            target,
            "page.md",
            page_id=FAKE_PAGE_ID,
            title="My Page",
            parent_id="new-parent",
            body=new_body,
        )

        _mock_resolve_space(httpx_mock)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/update",
            json={"data": {"id": FAKE_PAGE_ID}},
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/move",
            status_code=400,
        )

        with _make_client() as client, pytest.raises(SystemExit):
            push_space(client, "eng", target)

        requests = httpx_mock.get_requests()
        assert [request.url.path for request in requests] == [
            "/api/spaces",
            "/api/pages/update",
            "/api/pages/move",
        ]
        manifest = load_manifest(target)
        assert manifest["pages"][FAKE_PAGE_ID]["content_hash"] == compute_content_hash(old_body)
        assert manifest["pages"][FAKE_PAGE_ID]["parent_id"] == "old-parent"


# ---------------------------------------------------------------------------
# push_space — dry run
# ---------------------------------------------------------------------------


class TestPushDryRun:
    """push_space with dry_run=True makes no API calls beyond resolve."""

    def test_dry_run_no_mutations(self, httpx_mock, tmp_path: Path) -> None:
        old_body = "Old.\n"
        new_body = "New.\n"

        target = _setup_synced_dir(
            tmp_path,
            pages={
                FAKE_PAGE_ID: build_page_entry(
                    title="My Page",
                    filename="page.md",
                    parent_id=None,
                    icon="",
                    content_hash=compute_content_hash(old_body),
                )
            },
        )
        _write_page(
            target,
            "page.md",
            page_id=FAKE_PAGE_ID,
            title="My Page",
            body=new_body,
        )

        _mock_resolve_space(httpx_mock)

        with _make_client() as client:
            result = push_space(client, "eng", target, dry_run=True)

        # No mutation APIs should have been called
        requests = httpx_mock.get_requests()
        mutation_urls = [
            str(r.url)
            for r in requests
            if any(
                ep in str(r.url)
                for ep in [
                    "/pages/create",
                    "/pages/delete",
                    "/pages/move",
                    "/pages/update",
                ]
            )
        ]
        assert mutation_urls == []

        # Result should reflect no actual changes made
        assert result.created == 0
        assert result.updated == 0


# ---------------------------------------------------------------------------
# push_space — deletions
# ---------------------------------------------------------------------------


class TestPushDeletions:
    """push_space deletion behavior."""

    def test_delete_flag_removes_pages(self, httpx_mock, tmp_path: Path) -> None:
        """With --delete, pages in manifest but not locally are deleted."""
        target = _setup_synced_dir(
            tmp_path,
            pages={
                FAKE_PAGE_ID: build_page_entry(
                    title="Deleted Page",
                    filename="deleted.md",
                    parent_id=None,
                    icon="",
                    content_hash="sha256:abc",
                )
            },
        )
        # No .md files -> the manifest page is "deleted"

        _mock_resolve_space(httpx_mock)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/delete",
            json={"data": {}},
        )

        with _make_client() as client:
            result = push_space(client, "eng", target, delete=True)

        assert result.deleted == 1

        manifest = load_manifest(target)
        assert FAKE_PAGE_ID not in manifest["pages"]

    def test_no_delete_flag_skips_deletion(self, httpx_mock, tmp_path: Path) -> None:
        """Without --delete, deleted pages are reported but not removed."""
        target = _setup_synced_dir(
            tmp_path,
            pages={
                FAKE_PAGE_ID: build_page_entry(
                    title="Deleted Page",
                    filename="deleted.md",
                    parent_id=None,
                    icon="",
                    content_hash="sha256:abc",
                )
            },
        )

        _mock_resolve_space(httpx_mock)

        with _make_client() as client:
            result = push_space(client, "eng", target, delete=False)

        assert result.deleted == 0

        # Page should still be in manifest
        manifest = load_manifest(target)
        assert FAKE_PAGE_ID in manifest["pages"]


# ---------------------------------------------------------------------------
# push_space — no manifest
# ---------------------------------------------------------------------------


class TestPushNoManifest:
    """push_space fails when no manifest exists."""

    def test_no_manifest(self, httpx_mock, tmp_path: Path) -> None:
        target = tmp_path / "eng"
        target.mkdir(parents=True)
        # No manifest file

        _mock_resolve_space(httpx_mock)

        with _make_client() as client, pytest.raises(SystemExit):
            push_space(client, "eng", target)
