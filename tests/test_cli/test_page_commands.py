"""Tests for page CLI commands."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from docmost_cli.cli.main import app
from docmost_cli.cli.page import _resolve_content

runner = CliRunner()


class TestResolveContent:
    def test_inline_content(self) -> None:
        result = _resolve_content("hello", None, False)
        assert result == "hello"

    def test_file_content(self, tmp_path: Path) -> None:
        f = tmp_path / "test.md"
        f.write_text("# File Content")
        result = _resolve_content(None, f, False)
        assert result == "# File Content"

    def test_no_content(self) -> None:
        result = _resolve_content(None, None, False)
        assert result is None

    def test_multiple_sources_exits(self) -> None:
        with pytest.raises(SystemExit):
            _resolve_content("inline", Path("file.md"), False)

    def test_file_not_found_exits(self) -> None:
        with pytest.raises(SystemExit):
            _resolve_content(None, Path("/nonexistent/file.md"), False)

    def test_content_escape_sequences(self) -> None:
        """Backslash-n in --content should become actual newline."""
        result = _resolve_content("Line 1\\n\\nLine 2", None, False)
        assert result == "Line 1\n\nLine 2"

    def test_content_escape_tab(self) -> None:
        """Backslash-t in --content should become actual tab."""
        result = _resolve_content("Col1\\tCol2", None, False)
        assert result == "Col1\tCol2"


class TestPageCreate:
    def test_create_with_content(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "space-1", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/create",
            json={"id": "page-new"},
        )
        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config),
                "page",
                "create",
                "eng",
                "--title",
                "Test Page",
                "--content",
                "Hello world",
            ],
        )
        assert result.exit_code == 0
        assert "page-new" in result.output

    def test_create_with_parent(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "space-1", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/create",
            json={"id": "child-page"},
        )
        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config),
                "page",
                "create",
                "eng",
                "--title",
                "Child",
                "--content",
                "Content",
                "--parent",
                "parent-1",
            ],
        )
        assert result.exit_code == 0
        assert "child-page" in result.output
        import json as json_mod

        create_requests = [r for r in httpx_mock.get_requests() if "/pages/create" in str(r.url)]
        assert len(create_requests) == 1
        create_body = json_mod.loads(create_requests[0].content)
        assert create_body["parentPageId"] == "parent-1"
        assert create_body["format"] == "markdown"

    def test_create_empty_page(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "space-1", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/create",
            json={"id": "empty-page"},
        )
        result = runner.invoke(
            app,
            ["--config", str(tmp_config), "page", "create", "eng", "--title", "Empty"],
        )
        assert result.exit_code == 0
        assert "empty-page" in result.output

    def test_create_from_file(self, tmp_config, tmp_path, httpx_mock) -> None:
        content_file = tmp_path / "content.md"
        content_file.write_text("# From File\n\nContent here")

        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "space-1", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/create",
            json={"id": "file-page"},
        )
        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config),
                "page",
                "create",
                "eng",
                "--title",
                "File Page",
                "--file",
                str(content_file),
            ],
        )
        assert result.exit_code == 0
        assert "file-page" in result.output


class TestPageUpdate:
    def test_update_title(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={"id": "page-1", "title": "Old Title"},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/update",
            json={"id": "page-1", "title": "New Title"},
        )
        result = runner.invoke(
            app,
            ["--config", str(tmp_config), "page", "update", "page-1", "--title", "New Title"],
        )
        assert result.exit_code == 0
        assert "page-1" in result.output

    def test_update_no_flags(self, tmp_config) -> None:
        result = runner.invoke(app, ["--config", str(tmp_config), "page", "update", "page-1"])
        assert result.exit_code != 0


class TestPageDelete:
    def test_delete_with_yes(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={"id": "page-1", "title": "Doomed Page"},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/delete",
            json={"id": "page-1"},
        )
        result = runner.invoke(app, ["--config", str(tmp_config), "-y", "page", "delete", "page-1"])
        assert result.exit_code == 0
        assert "page-1" in result.output

    def test_delete_aborted(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={"id": "page-1", "title": "Safe Page"},
        )
        result = runner.invoke(
            app,
            ["--config", str(tmp_config), "page", "delete", "page-1"],
            input="n\n",
        )
        assert result.exit_code != 0  # Aborted


class TestPageMove:
    def test_move_to_space(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "space-2", "slug": "staging", "name": "Staging"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/move-to-space",
        )
        result = runner.invoke(
            app,
            ["--config", str(tmp_config), "page", "move", "page-1", "--space", "staging"],
        )
        assert result.exit_code == 0
        assert "page-1" in result.output

    def test_move_no_flags(self, tmp_config) -> None:
        result = runner.invoke(app, ["--config", str(tmp_config), "page", "move", "page-1"])
        assert result.exit_code != 0

    def test_move_to_space_rejects_parent(self, tmp_config) -> None:
        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config),
                "page",
                "move",
                "page-1",
                "--space",
                "staging",
                "--parent",
                "parent-2",
            ],
        )
        assert result.exit_code != 0


class TestPageList:
    def test_list_json(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "s1", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/recent",
            json={
                "data": {
                    "items": [
                        {"id": "p1", "title": "Page One", "updatedAt": "2026-03-20"},
                    ]
                }
            },
        )
        result = runner.invoke(app, ["--config", str(tmp_config), "page", "list", "eng", "--json"])
        assert result.exit_code == 0
        assert "Page One" in result.output
        assert "p1" in result.output

    def test_list_table(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "s1", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/recent",
            json={
                "data": {
                    "items": [
                        {"id": "p1", "title": "Page One", "updatedAt": "2026-03-20"},
                    ]
                }
            },
        )
        result = runner.invoke(app, ["--config", str(tmp_config), "page", "list", "eng"])
        assert result.exit_code == 0
        assert "Page One" in result.output


class TestPageGet:
    def test_get_markdown(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={
                "id": "page-1",
                "title": "Hello",
                "spaceId": "s1",
                "content": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "heading",
                            "attrs": {"level": 1},
                            "content": [{"type": "text", "text": "Hello"}],
                        },
                        {"type": "paragraph", "content": [{"type": "text", "text": "World"}]},
                    ],
                },
            },
        )
        result = runner.invoke(app, ["--config", str(tmp_config), "page", "get", "page-1"])
        assert result.exit_code == 0
        assert "# Hello" in result.output
        assert "World" in result.output

    def test_get_raw(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={
                "id": "page-1",
                "title": "Hello",
                "spaceId": "s1",
                "content": {"type": "doc", "content": []},
            },
        )
        result = runner.invoke(app, ["--config", str(tmp_config), "page", "get", "page-1", "--raw"])
        assert result.exit_code == 0
        assert '"type"' in result.output

    def test_get_null_content_outputs_empty_markdown(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={"id": "page-1", "title": "Empty", "spaceId": "s1", "content": None},
        )
        result = runner.invoke(app, ["--config", str(tmp_config), "page", "get", "page-1"])
        assert result.exit_code == 0
        assert result.output == ""

    def test_get_raw_null_content_outputs_json_null(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={"id": "page-1", "title": "Empty", "spaceId": "s1", "content": None},
        )
        result = runner.invoke(
            app,
            ["--config", str(tmp_config), "page", "get", "page-1", "--raw"],
        )
        assert result.exit_code == 0
        assert result.output == "null\n"

    def test_get_meta(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={
                "id": "page-1",
                "title": "Test",
                "spaceId": "s1",
                "createdAt": "2026-01-01",
                "updatedAt": "2026-03-20",
                "content": {
                    "type": "doc",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Content"}]},
                    ],
                },
            },
        )
        result = runner.invoke(
            app, ["--config", str(tmp_config), "page", "get", "page-1", "--meta"]
        )
        assert result.exit_code == 0
        assert "---" in result.output
        assert "id: page-1" in result.output
        assert "Content" in result.output

    def test_get_with_emoji_content(self, tmp_config, httpx_mock) -> None:
        """Emoji in page content should not crash (Windows cp1252 fix)."""
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={
                "id": "page-emoji",
                "title": "Test",
                "spaceId": "s1",
                "content": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "Status: "},
                                {"type": "text", "text": "\u2705 Done"},
                            ],
                        },
                    ],
                },
            },
        )
        result = runner.invoke(app, ["--config", str(tmp_config), "page", "get", "page-emoji"])
        assert result.exit_code == 0
        assert "Status:" in result.output
        assert "Done" in result.output


class TestPageDuplicate:
    def test_duplicate(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={"id": "page-1", "title": "Original"},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/duplicate",
            json={"id": "page-dup"},
        )
        result = runner.invoke(app, ["--config", str(tmp_config), "page", "duplicate", "page-1"])
        assert result.exit_code == 0
        assert "page-dup" in result.output


class TestPageCopy:
    def test_copy(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={"id": "page-1", "title": "Source Page"},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "space-2", "slug": "target", "name": "Target"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/duplicate",
            json={"id": "page-copy"},
        )
        result = runner.invoke(
            app, ["--config", str(tmp_config), "page", "copy", "page-1", "--space", "target"]
        )
        assert result.exit_code == 0
        assert "page-copy" in result.output


class TestPageChildren:
    def test_children_json(self, tmp_config, httpx_mock) -> None:
        # page children resolves space_id from page info first
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            json={"data": {"id": "parent-1", "spaceId": "s1"}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/sidebar-pages",
            json={
                "data": {
                    "items": [
                        {"id": "child-1", "title": "Child One", "updatedAt": "2026-03-20"},
                    ]
                }
            },
        )
        result = runner.invoke(
            app, ["--config", str(tmp_config), "page", "children", "parent-1", "--json"]
        )
        assert result.exit_code == 0
        assert "Child One" in result.output


class TestPageHistory:
    def test_history_json(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/history",
            json={
                "data": {
                    "items": [
                        {"id": "v1", "creatorId": "user-1", "createdAt": "2026-03-20"},
                    ]
                }
            },
        )
        result = runner.invoke(
            app, ["--config", str(tmp_config), "page", "history", "page-1", "--json"]
        )
        assert result.exit_code == 0
        assert "v1" in result.output


class TestPageExport:
    @staticmethod
    def _make_zip(content: str) -> bytes:
        """Create a ZIP file in memory containing a single markdown file."""
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("export.md", content)
        return buf.getvalue()

    def test_export_stdout(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/export",
            content=self._make_zip("# Exported Content\n\nHello world"),
        )
        result = runner.invoke(app, ["--config", str(tmp_config), "page", "export", "page-1"])
        assert result.exit_code == 0
        assert "Exported Content" in result.output

    def test_export_to_file(self, tmp_config, tmp_path, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/export",
            content=self._make_zip("# File Content"),
        )
        output_file = tmp_path / "export.md"
        result = runner.invoke(
            app,
            ["--config", str(tmp_config), "page", "export", "page-1", "--output", str(output_file)],
        )
        assert result.exit_code == 0
        assert output_file.exists()
        assert "File Content" in output_file.read_text()

    def test_export_attachment_archive(self, tmp_config, tmp_path, httpx_mock) -> None:
        archive = self._make_zip("![Diagram](files/attachment-id/diagram.png)")
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/export",
            content=archive,
        )
        output_file = tmp_path / "portable.zip"

        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config),
                "page",
                "export",
                "page-1",
                "--include-attachments",
                "--output",
                str(output_file),
            ],
        )

        assert result.exit_code == 0
        assert output_file.read_bytes() == archive
        import json

        payload = json.loads(httpx_mock.get_requests()[0].content)
        assert payload["includeAttachments"] is True


class TestPageImport:
    def test_import_zip_preserves_attachments(
        self,
        tmp_config,
        tmp_path,
        httpx_mock,
    ) -> None:
        archive = tmp_path / "portable.zip"
        archive.write_bytes(b"zip-bytes")
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "s1", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/import-zip",
            json={"id": "file-task-1", "status": "processing"},
        )

        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config),
                "page",
                "import",
                "eng",
                "--file",
                str(archive),
            ],
        )

        assert result.exit_code == 0
        assert "file-task-1" in result.output
        body = httpx_mock.get_requests()[1].read()
        assert b"generic" in body

    @pytest.mark.parametrize(
        ("file_name", "file_content", "mime_type"),
        [
            ("doc.md", "# Auto Title\n\nSome content", b"text/markdown"),
            ("doc.html", "<h1>Auto Title</h1><p>Some content</p>", b"text/html"),
        ],
    )
    def test_import_applies_title_and_parent_overrides(
        self,
        tmp_config,
        tmp_path,
        httpx_mock,
        file_name,
        file_content,
        mime_type,
    ) -> None:
        import_file = tmp_path / file_name
        import_file.write_text(file_content)

        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "s1", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/import",
            json={"id": "imported-page"},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/update",
            json={"id": "imported-page", "title": "Custom Title"},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/move",
            json={"id": "imported-page", "parentPageId": "parent-1"},
        )
        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config),
                "page",
                "import",
                "eng",
                "--file",
                str(import_file),
                "--title",
                "Custom Title",
                "--parent",
                "parent-1",
            ],
        )
        assert result.exit_code == 0
        assert "imported-page" in result.output

        import json

        requests = httpx_mock.get_requests()
        assert [request.url.path for request in requests] == [
            "/api/spaces",
            "/api/pages/import",
            "/api/pages/update",
            "/api/pages/move",
        ]
        assert mime_type in requests[1].read()
        assert b"parentPageId" not in requests[1].read()
        assert json.loads(requests[2].content) == {
            "pageId": "imported-page",
            "title": "Custom Title",
        }
        assert json.loads(requests[3].content) == {
            "pageId": "imported-page",
            "parentPageId": "parent-1",
            "position": "aaaaa",
        }

    @pytest.mark.parametrize("override", [["--title", "Title"], ["--parent", "parent-1"]])
    def test_import_zip_rejects_metadata_overrides(
        self,
        tmp_config,
        tmp_path,
        httpx_mock,
        override,
    ) -> None:
        archive = tmp_path / "portable.zip"
        archive.write_bytes(b"zip-bytes")

        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config),
                "page",
                "import",
                "eng",
                "--file",
                str(archive),
                *override,
            ],
        )

        assert result.exit_code != 0
        assert "cannot override metadata in a ZIP import" in result.output
        assert httpx_mock.get_requests() == []

    def test_import_auto_title_from_h1(self, tmp_config, tmp_path, httpx_mock) -> None:
        md_file = tmp_path / "doc.md"
        md_file.write_text("# My Page Title\n\nContent here")

        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "s1", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/import",
            json={"id": "imported-page"},
        )
        result = runner.invoke(
            app,
            ["--config", str(tmp_config), "page", "import", "eng", "--file", str(md_file)],
        )
        assert result.exit_code == 0
        assert "imported-page" in result.output

    def test_import_reports_page_id_when_parent_override_fails(
        self,
        tmp_config,
        tmp_path,
        httpx_mock,
    ) -> None:
        md_file = tmp_path / "doc.md"
        md_file.write_text("# Imported Page\n\nContent")

        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "s1", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/import",
            json={"id": "imported-page"},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/move",
            status_code=404,
        )

        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config),
                "page",
                "import",
                "eng",
                "--file",
                str(md_file),
                "--parent",
                "missing-parent",
            ],
        )

        assert result.exit_code == 4
        assert result.stdout == "imported-page\n"
        assert "failed to apply" in result.stderr
        assert "requested override" in result.stderr

    def test_import_attempts_parent_move_when_title_override_fails(
        self,
        tmp_config,
        tmp_path,
        httpx_mock,
    ) -> None:
        md_file = tmp_path / "doc.md"
        md_file.write_text("# Imported Page\n\nContent")

        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "s1", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/import",
            json={"id": "imported-page"},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/update",
            status_code=422,
            json={"message": "Invalid title"},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/move",
            json={"id": "imported-page", "parentPageId": "parent-1"},
        )

        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config),
                "page",
                "import",
                "eng",
                "--file",
                str(md_file),
                "--title",
                "Rejected Title",
                "--parent",
                "parent-1",
            ],
        )

        assert result.exit_code == 1
        assert result.stdout == "imported-page\n"
        assert [request.url.path for request in httpx_mock.get_requests()] == [
            "/api/spaces",
            "/api/pages/import",
            "/api/pages/update",
            "/api/pages/move",
        ]
        assert "Validation error: Invalid title" in result.stderr
        assert "failed to apply" in result.stderr


class TestPageListTree:
    def test_tree(self, tmp_config, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/spaces",
            json={"data": {"items": [{"id": "s1", "slug": "eng", "name": "Eng"}]}},
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/sidebar-pages",
            json={
                "data": {
                    "items": [
                        {
                            "id": "p1",
                            "title": "Root Page",
                            "children": [
                                {"id": "p2", "title": "Child Page", "children": []},
                            ],
                        },
                    ]
                }
            },
        )
        result = runner.invoke(app, ["--config", str(tmp_config), "page", "list", "eng", "--tree"])
        assert result.exit_code == 0
        assert "Root Page" in result.output
        assert "Child Page" in result.output
