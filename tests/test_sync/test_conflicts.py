"""Tests for remote revision conflict detection."""

from __future__ import annotations

import pytest

from docmost_cli.api.client import DocmostClient
from docmost_cli.config.settings import DocmostSettings
from docmost_cli.sync.conflicts import verify_remote_revisions
from docmost_cli.sync.diff import PageChange
from docmost_cli.sync.manifest import build_server_revision

_TEST_URL = "https://docs.example.com"
_PAGE_ID = "019a2a69-bbbb-cccc-dddd-eeeeeeeeeeee"


def _make_client() -> DocmostClient:
    return DocmostClient(DocmostSettings(url=_TEST_URL, api_key="dm_test1234567890"))


def _server_page(**overrides: object) -> dict:
    page = {
        "id": _PAGE_ID,
        "title": "Page",
        "icon": "",
        "parentPageId": None,
        "spaceId": "space-1",
        "content": {
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]}],
        },
        "deletedAt": None,
        "updatedAt": "2026-01-01T00:00:00.000Z",
    }
    page.update(overrides)
    return page


def _change(
    revision: dict | None,
    *,
    title: str = "Page",
    filename: str = "Page--019a2a69.md",
) -> PageChange:
    entry = {
        "title": title,
        "filename": filename,
    }
    if revision is not None:
        entry["server_revision"] = revision
    return PageChange(
        page_id=_PAGE_ID,
        filename=filename,
        manifest_entry=entry,
    )


def _mock_page(httpx_mock, page: dict, *, status_code: int = 200) -> None:
    httpx_mock.add_response(
        url=f"{_TEST_URL}/api/pages/info",
        status_code=status_code,
        json=page,
    )


class TestVerifyRemoteRevisions:
    def test_matching_raw_server_state_has_no_conflict(self, httpx_mock) -> None:
        page = _server_page()
        _mock_page(httpx_mock, page)

        with _make_client() as client:
            result = verify_remote_revisions(
                client,
                [_change(build_server_revision(page))],
            )

        assert result.conflicts == []
        assert result.pages[_PAGE_ID] == page

    def test_updated_at_only_does_not_create_false_conflict(self, httpx_mock) -> None:
        pulled = _server_page(updatedAt="2026-01-01T00:00:00.000Z")
        current = _server_page(updatedAt="2026-01-02T00:00:00.000Z")
        _mock_page(httpx_mock, current)

        with _make_client() as client:
            result = verify_remote_revisions(
                client,
                [_change(build_server_revision(pulled))],
            )

        assert result.conflicts == []

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("title", "Changed remotely"),
            ("icon", "📄"),
            ("parentPageId", "parent-2"),
            ("spaceId", "space-2"),
            ("content", {"type": "doc", "content": [{"type": "heading"}]}),
        ],
    )
    def test_material_remote_change_aborts(
        self,
        httpx_mock,
        capsys,
        field: str,
        value: object,
    ) -> None:
        pulled = _server_page()
        current = _server_page(**{field: value}, updatedAt="2026-01-02T00:00:00.000Z")
        _mock_page(httpx_mock, current)

        with _make_client() as client, pytest.raises(SystemExit):
            verify_remote_revisions(
                client,
                [_change(build_server_revision(pulled))],
            )

        error = capsys.readouterr().err
        assert _PAGE_ID in error
        assert "no changes were pushed" in error
        assert "sync pull <space> --force" in error
        assert "sync push --force" in error

    def test_old_manifest_requires_pull_or_explicit_force(self, httpx_mock, capsys) -> None:
        _mock_page(httpx_mock, _server_page())

        with _make_client() as client, pytest.raises(SystemExit):
            verify_remote_revisions(client, [_change(None)])

        error = " ".join(capsys.readouterr().err.split())
        assert "manifest has no compatible server revision" in error

    def test_force_returns_conflicts_for_caller_to_handle(self, httpx_mock) -> None:
        pulled = _server_page()
        current = _server_page(title="Changed remotely")
        _mock_page(httpx_mock, current)

        with _make_client() as client:
            result = verify_remote_revisions(
                client,
                [_change(build_server_revision(pulled))],
                force=True,
            )

        assert result.conflict_page_ids == {_PAGE_ID}
        assert result.pages[_PAGE_ID] == current

    def test_missing_remote_page_is_reported_as_conflict(self, httpx_mock, capsys) -> None:
        _mock_page(httpx_mock, {}, status_code=404)

        with _make_client() as client, pytest.raises(SystemExit):
            verify_remote_revisions(
                client,
                [_change(build_server_revision(_server_page()))],
            )

        error = " ".join(capsys.readouterr().err.split())
        assert "no longer exists on the server" in error
        assert _PAGE_ID in error

    def test_conflict_details_preserve_titles_with_rich_markup(self, httpx_mock, capsys) -> None:
        pulled = _server_page()
        _mock_page(httpx_mock, _server_page(title="Changed remotely"))

        with _make_client() as client, pytest.raises(SystemExit):
            verify_remote_revisions(
                client,
                [
                    _change(
                        build_server_revision(pulled),
                        title="[Draft] Page",
                        filename="[Draft]--019a2a69.md",
                    )
                ],
            )

        error = capsys.readouterr().err
        assert "[Draft] Page" in error
        assert "[Draft]--019a2a69.md" in error

    def test_verification_failure_aborts_before_mutation(self, httpx_mock, capsys) -> None:
        _mock_page(httpx_mock, {"message": "unavailable"}, status_code=503)

        with _make_client() as client, pytest.raises(SystemExit):
            verify_remote_revisions(
                client,
                [_change(build_server_revision(_server_page()))],
            )

        assert "Could not verify the remote revision" in capsys.readouterr().err
