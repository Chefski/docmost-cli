"""Tests for Page API methods."""

import json

import pytest

from docmost_cli.api.client import DocmostClient
from docmost_cli.api.pages import (
    PageImportPlacementError,
    copy_page,
    create_page_via_import,
    delete_page,
    duplicate_page,
    export_page,
    export_page_archive,
    get_page_children,
    get_page_content,
    get_page_history,
    get_page_info,
    get_sidebar_pages,
    import_page,
    import_page_archive,
    list_recent_pages,
    move_page,
    update_page_content,
    update_page_meta,
)


class TestGetPageInfo:
    def test_returns_info(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={"id": "page-1", "title": "Test Page", "spaceId": "s1"},
        )
        with DocmostClient(api_key_settings) as client:
            result = get_page_info(client, "page-1")
        assert result["title"] == "Test Page"

    def test_not_found(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            status_code=404,
        )
        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit) as exc:
            get_page_info(client, "nonexistent")
        assert exc.value.code == 4


class TestCreatePageViaImport:
    def test_sends_multipart(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/import",
            json={"id": "new-page"},
        )
        with DocmostClient(api_key_settings) as client:
            result = create_page_via_import(
                client,
                space_id="space-1",
                title="New Page",
                content="Hello world",
            )
        assert result["id"] == "new-page"

        # Verify the request was sent
        request = httpx_mock.get_requests()[0]
        assert request.method == "POST"

    def test_empty_content(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/import",
            json={"id": "empty-page"},
        )
        with DocmostClient(api_key_settings) as client:
            result = create_page_via_import(
                client,
                space_id="space-1",
                title="Empty Page",
                content="",
            )
        assert result["id"] == "empty-page"

    def test_with_parent(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/import",
            json={"id": "child-page"},
        )
        with DocmostClient(api_key_settings) as client:
            result = create_page_via_import(
                client,
                space_id="space-1",
                title="Child",
                content="Content",
                parent_page_id="parent-1",
            )
        assert result["id"] == "child-page"


class TestUpdatePageMeta:
    def test_update_title(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/update",
            json={"id": "page-1", "title": "New Title"},
        )
        with DocmostClient(api_key_settings) as client:
            result = update_page_meta(client, page_id="page-1", title="New Title")
        assert result["title"] == "New Title"


class TestUpdatePageContent:
    def test_sends_content(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/update",
            json={"success": True},
        )
        with DocmostClient(api_key_settings) as client:
            result = update_page_content(
                client, page_id="page-1", content="# Updated\n\nNew content"
            )
        assert result["success"] is True
        request = httpx_mock.get_requests()[0]
        payload = json.loads(request.content)
        assert payload["operation"] == "replace"
        assert payload["format"] == "markdown"

    def test_update_404(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/update",
            status_code=404,
        )
        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit) as exc:
            update_page_content(client, page_id="page-1", content="test")
        assert exc.value.code == 4


class TestDeletePage:
    def test_deletes(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/delete",
            json={"id": "page-1"},
        )
        with DocmostClient(api_key_settings) as client:
            result = delete_page(client, "page-1")
        assert result["id"] == "page-1"


class TestMovePage:
    def test_move_to_parent(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/move",
            json={"id": "page-1"},
        )
        with DocmostClient(api_key_settings) as client:
            result = move_page(client, page_id="page-1", parent_page_id="parent-1")
        assert result["id"] == "page-1"

    def test_move_to_space(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/move",
            json={"id": "page-1"},
        )
        with DocmostClient(api_key_settings) as client:
            result = move_page(client, page_id="page-1", space_id="space-2")
        assert result["id"] == "page-1"


class TestGetPageContent:
    def test_enterprise_content_endpoint(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/content",
            json={"content": {"type": "doc", "content": []}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={"id": "page-1", "title": "Test", "spaceId": "s1"},
        )
        with DocmostClient(api_key_settings) as client:
            result = get_page_content(client, "page-1")
        assert result["id"] == "page-1"
        assert "content" in result

    def test_fallback_to_info(self, httpx_mock, api_key_settings) -> None:
        # Content endpoint returns 404 (Community edition)
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/content",
            status_code=404,
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={"id": "page-1", "title": "Test", "content": {"type": "doc", "content": []}},
        )
        with DocmostClient(api_key_settings) as client:
            result = get_page_content(client, "page-1")
        assert result["id"] == "page-1"


class TestListRecentPages:
    def test_list_pages(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/recent",
            json={
                "data": {
                    "items": [
                        {"id": "p1", "title": "Page 1", "updatedAt": "2026-03-20"},
                    ]
                }
            },
        )
        with DocmostClient(api_key_settings) as client:
            result = list_recent_pages(client, "space-1")
        assert result["data"]["items"][0]["title"] == "Page 1"


class TestDuplicatePage:
    def test_duplicate(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/duplicate",
            json={"id": "dup-page"},
        )
        with DocmostClient(api_key_settings) as client:
            result = duplicate_page(client, "page-1")
        assert result["id"] == "dup-page"


class TestCopyPage:
    def test_copy(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/copy",
            json={"id": "copy-page"},
        )
        with DocmostClient(api_key_settings) as client:
            result = copy_page(client, "page-1", "space-2")
        assert result["id"] == "copy-page"


class TestGetPageChildren:
    def test_children_with_space_id(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/sidebar-pages",
            json={"data": {"items": [{"id": "c1", "title": "Child"}]}},
        )
        with DocmostClient(api_key_settings) as client:
            result = get_page_children(client, "parent-1", space_id="s1")
        assert result["data"]["items"][0]["id"] == "c1"

    def test_children_resolves_space_id(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={"data": {"id": "parent-1", "spaceId": "s1"}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/sidebar-pages",
            json={"data": {"items": [{"id": "c1", "title": "Child"}]}},
        )
        with DocmostClient(api_key_settings) as client:
            result = get_page_children(client, "parent-1")
        assert result["data"]["items"][0]["id"] == "c1"


class TestGetPageHistory:
    def test_history(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/history",
            json={"data": {"items": [{"id": "v1", "createdAt": "2026-03-20"}]}},
        )
        with DocmostClient(api_key_settings) as client:
            result = get_page_history(client, "page-1")
        assert result["data"]["items"][0]["id"] == "v1"


class TestExportPage:
    def test_export(self, httpx_mock, api_key_settings) -> None:
        import io
        import zipfile

        # export_page() expects a ZIP response containing the exported content
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("export.md", "# Exported")
        zip_bytes = zip_buffer.getvalue()

        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/export",
            content=zip_bytes,
        )
        with DocmostClient(api_key_settings) as client:
            result = export_page(client, "page-1", fmt="md")
        assert result == "# Exported"

    def test_export_archive_requests_attachments(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/export",
            content=b"portable-zip",
        )
        with DocmostClient(api_key_settings) as client:
            result = export_page_archive(
                client,
                "page-1",
                fmt="md",
                include_children=True,
            )

        assert result == b"portable-zip"
        payload = json.loads(httpx_mock.get_requests()[0].content)
        assert payload["includeAttachments"] is True
        assert payload["includeChildren"] is True


class TestImportPageArchive:
    def test_starts_generic_zip_import(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/import-zip",
            json={"id": "file-task-1", "status": "processing"},
        )
        with DocmostClient(api_key_settings) as client:
            result = import_page_archive(
                client,
                space_id="space-1",
                file_name="portable.zip",
                file_bytes=b"zip-bytes",
            )

        assert result["id"] == "file-task-1"
        body = httpx_mock.get_requests()[0].read()
        assert b'name="source"' in body
        assert b"generic" in body
        assert b"portable.zip" in body


class TestImportPage:
    def test_parent_compatibility_moves_after_import(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/import",
            json={"id": "imported-page"},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/move",
            json={"id": "imported-page"},
        )

        with DocmostClient(api_key_settings) as client:
            result = import_page(
                client,
                space_id="space-1",
                file_name="page.md",
                file_bytes=b"# Page",
                parent_page_id="parent-1",
            )

        assert result["id"] == "imported-page"
        requests = httpx_mock.get_requests()
        assert [request.url.path for request in requests] == [
            "/api/pages/import",
            "/api/pages/move",
        ]
        assert b"parentPageId" not in requests[0].read()
        assert json.loads(requests[1].content) == {
            "pageId": "imported-page",
            "parentPageId": "parent-1",
            "position": "aaaaa",
        }

    def test_failed_parent_move_preserves_import_result(
        self,
        httpx_mock,
        api_key_settings,
        capsys,
    ) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/import",
            json={"id": "imported-page", "title": "Page"},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/move",
            status_code=404,
        )

        with (
            DocmostClient(api_key_settings) as client,
            pytest.raises(PageImportPlacementError) as exc,
        ):
            import_page(
                client,
                space_id="space-1",
                file_name="page.md",
                file_bytes=b"# Page",
                parent_page_id="missing-parent",
            )

        assert exc.value.code == 4
        assert exc.value.page_id == "imported-page"
        assert exc.value.result == {"id": "imported-page", "title": "Page"}
        captured = capsys.readouterr()
        assert captured.out == "imported-page\n"
        assert "failed to move it under the requested parent" in captured.err


class TestGetSidebarPages:
    def test_sidebar(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/sidebar-pages",
            json={"data": {"items": [{"id": "p1", "title": "Root", "children": []}]}},
        )
        with DocmostClient(api_key_settings) as client:
            result = get_sidebar_pages(client, "space-1")
        assert result["data"]["items"][0]["title"] == "Root"
