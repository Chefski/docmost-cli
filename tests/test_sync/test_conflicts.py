"""Tests for remote revision conflict detection."""

from __future__ import annotations

import pytest

from docmost_cli.api.client import DocmostClient
from docmost_cli.config.settings import DocmostSettings
from docmost_cli.sync.assets import compute_bytes_hash
from docmost_cli.sync.conflicts import verify_remote_revisions
from docmost_cli.sync.diff import ChangeType, PageChange
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
        assert "--dir <new-directory>" in error
        assert "Do not force-pull over local edits" in error
        assert "sync pull --force" not in error
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

    def test_transient_verification_failure_retries_then_succeeds(
        self,
        httpx_mock,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("time.sleep", lambda _: None)
        page = _server_page()
        _mock_page(httpx_mock, {"message": "unavailable"}, status_code=503)
        _mock_page(httpx_mock, page)

        with _make_client() as client:
            result = verify_remote_revisions(
                client,
                [_change(build_server_revision(page))],
            )

        assert result.conflicts == []
        assert len(httpx_mock.get_requests()) == 2

    def test_exhausted_verification_retries_abort_before_mutation(
        self,
        httpx_mock,
        capsys,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("time.sleep", lambda _: None)
        for _ in range(4):
            _mock_page(httpx_mock, {"message": "unavailable"}, status_code=503)

        with _make_client() as client, pytest.raises(SystemExit):
            verify_remote_revisions(
                client,
                [_change(build_server_revision(_server_page()))],
            )

        assert "Could not verify the remote revision" in capsys.readouterr().err

    def test_legacy_content_endpoint_is_used_in_revision(self, httpx_mock) -> None:
        page = _server_page()
        page_without_content = {key: value for key, value in page.items() if key != "content"}
        _mock_page(httpx_mock, page_without_content)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/content",
            json={"data": {"content": page["content"]}},
        )

        with _make_client() as client:
            result = verify_remote_revisions(
                client,
                [_change(build_server_revision(page))],
            )

        assert result.conflicts == []
        assert result.pages[_PAGE_ID]["content"] == page["content"]

    def test_missing_content_endpoint_aborts_instead_of_fingerprinting_blank_page(
        self,
        httpx_mock,
        capsys,
    ) -> None:
        page = _server_page()
        page_without_content = {key: value for key, value in page.items() if key != "content"}
        _mock_page(httpx_mock, page_without_content)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/pages/content",
            status_code=404,
        )

        with _make_client() as client, pytest.raises(SystemExit):
            verify_remote_revisions(
                client,
                [_change(build_server_revision(page))],
            )

        assert "page content is unavailable" in " ".join(capsys.readouterr().err.split())

    def test_changed_local_attachment_conflicts_when_remote_bytes_were_replaced(
        self,
        httpx_mock,
        tmp_path,
        capsys,
    ) -> None:
        attachment_id = "019c0000-1111-7222-8333-444444444444"
        relative_path = f"files/{attachment_id}/diagram.png"
        local_file = tmp_path / relative_path
        local_file.parent.mkdir(parents=True)
        local_file.write_bytes(b"changed locally")
        page = _server_page()
        change = _change(build_server_revision(page))
        change.changes.add(ChangeType.ATTACHMENT_CHANGED)
        change.local_body = f"![Architecture]({relative_path})\n"
        assert change.manifest_entry is not None
        change.manifest_entry["attachment_ids"] = [attachment_id]
        manifest = {
            "assets": {
                attachment_id: {
                    "file_name": "diagram.png",
                    "path": relative_path,
                    "page_id": _PAGE_ID,
                    "content_hash": compute_bytes_hash(b"pulled remote bytes"),
                    "server_updated_at": "2026-01-01T00:00:00.000Z",
                }
            }
        }
        _mock_page(httpx_mock, page)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/files/info",
            json={
                "id": attachment_id,
                "fileName": "diagram.png",
                "pageId": _PAGE_ID,
                "updatedAt": "2026-01-02T00:00:00.000Z",
            },
        )
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/files/{attachment_id}/diagram.png",
            content=b"changed remote bytes",
        )

        with _make_client() as client, pytest.raises(SystemExit):
            verify_remote_revisions(
                client,
                [change],
                manifest=manifest,
                dir_path=tmp_path,
            )

        error = " ".join(capsys.readouterr().err.split())
        assert attachment_id in error
        assert "attachment bytes changed since the last pull" in error

    def test_session_reauthentication_still_validates_page_shape(
        self,
        httpx_mock,
        session_settings,
        monkeypatch,
        tmp_path,
        capsys,
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        url = f"{_TEST_URL}/api/pages/info"
        httpx_mock.add_response(url=url, status_code=401)
        httpx_mock.add_response(
            url=f"{_TEST_URL}/api/auth/login",
            json={"token": "new_jwt"},
        )
        httpx_mock.add_response(url=url, json={"message": "not a page"})

        with DocmostClient(session_settings) as client, pytest.raises(SystemExit):
            verify_remote_revisions(
                client,
                [_change(build_server_revision(_server_page()))],
            )

        assert "did not contain page state" in " ".join(capsys.readouterr().err.split())
