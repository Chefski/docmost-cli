"""Contract tests that tie API helpers to the pinned Docmost source contract."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from docmost_cli.api.attachments import (
    download_attachment,
    get_attachment_info,
    search_attachments,
    upload_attachment,
)
from docmost_cli.api.auth import SessionAuth
from docmost_cli.api.comments import create_comment, list_comments, update_comment
from docmost_cli.api.pages import (
    create_page,
    delete_page,
    duplicate_page,
    export_page,
    export_page_archive,
    get_page_children,
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
from docmost_cli.api.search import search
from docmost_cli.api.spaces import create_space, get_space_info, list_spaces, update_space
from docmost_cli.api.users import get_current_user
from docmost_cli.api.workspace import get_workspace_info, list_workspace_members

if TYPE_CHECKING:
    from collections.abc import Callable


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = Path(__file__).with_name("docmost-v0.95.0.json")
CONTRACT = json.loads(CONTRACT_PATH.read_text())


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    fields: frozenset[str]
    file_fields: frozenset[str] = frozenset()


class StubResponse:
    """Small response double for raw API helpers."""

    content = b"# exported"
    is_success = True

    def json(self) -> dict[str, Any]:
        return {"content": {"type": "doc", "content": []}}


class RecordingClient:
    """Record helper requests without claiming that a server accepted them."""

    def __init__(self) -> None:
        self.requests: list[Request] = []

    def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        *,
        error_messages: dict[int, str] | None = None,
        retry_safe: bool = False,
    ) -> dict[str, Any]:
        del error_messages, retry_safe
        self.requests.append(Request("POST", path, frozenset((json or {}).keys())))
        return {
            "id": "00000000-0000-4000-8000-000000000001",
            "spaceId": "00000000-0000-4000-8000-000000000002",
            "content": {"type": "doc", "content": []},
        }

    def post_raw(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        *,
        raise_on_error: bool = True,
        retry_safe: bool = False,
    ) -> StubResponse:
        del raise_on_error, retry_safe
        self.requests.append(Request("POST", path, frozenset((json or {}).keys())))
        return StubResponse()

    def post_multipart(
        self,
        path: str,
        data: dict[str, str] | None = None,
        files: dict[str, Any] | None = None,
        *,
        retry_safe: bool = False,
    ) -> dict[str, Any]:
        del retry_safe
        self.requests.append(
            Request(
                "POST",
                path,
                frozenset((data or {}).keys()),
                frozenset((files or {}).keys()),
            )
        )
        return {"id": "00000000-0000-4000-8000-000000000001"}

    def get_raw(self, path: str, *, raise_on_error: bool = True) -> StubResponse:
        del raise_on_error
        self.requests.append(Request("GET", path, frozenset()))
        return StubResponse()

    def attachment_url(self, attachment_id: str, file_name: str) -> str:
        return f"https://docs.example.test/api/files/{attachment_id}/{file_name}"


def _literal_client_requests(source: str, source_name: str) -> set[tuple[str, str, str]]:
    method_names = {
        "post": "POST",
        "post_raw": "POST",
        "post_multipart": "POST",
        "get": "GET",
        "get_raw": "GET",
    }
    requests: set[tuple[str, str, str]] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = method_names.get(node.func.attr)
        if not method:
            continue
        path_node = (
            node.args[0]
            if node.args
            else next(
                (keyword.value for keyword in node.keywords if keyword.arg == "path"),
                None,
            )
        )
        if not isinstance(path_node, ast.Constant) or not isinstance(path_node.value, str):
            continue
        if path_node.value.startswith("/"):
            requests.add((source_name, method, path_node.value))
    return requests


def _source_requests() -> set[tuple[str, str, str]]:
    requests: set[tuple[str, str, str]] = set()
    api_root = ROOT / "src" / "docmost_cli" / "api"
    for source_path in sorted(api_root.glob("*.py")):
        requests.update(
            _literal_client_requests(
                source_path.read_text(),
                str(source_path.relative_to(ROOT)),
            )
        )
    return requests


def _operation_for(request: Request) -> tuple[str, dict[str, Any]] | None:
    for name, operation in CONTRACT["operations"].items():
        if operation["method"] != request.method:
            continue
        template = operation["path"]
        pattern = re.sub(r":[A-Za-z_]\w*", r"[^/]+", re.escape(template))
        if re.fullmatch(pattern, request.path):
            return name, operation
        if template == request.path:
            return name, operation
    return None


def _assert_request_matches(operation_name: str, request: Request) -> None:
    operation = CONTRACT["operations"][operation_name]
    assert request.method == operation["method"]
    assert request.path == operation["path"] or _operation_for(request) == (
        operation_name,
        operation,
    )
    allowed = set(operation["allowed_fields"])
    required = set(operation["required_fields"])
    assert set(request.fields) <= allowed
    assert required <= set(request.fields)
    assert set(request.file_fields) == set(operation.get("file_fields", []))


def _single_request(invoke: Callable[[RecordingClient], Any]) -> Request:
    client = RecordingClient()
    invoke(client)
    assert len(client.requests) == 1
    return client.requests[0]


def test_every_literal_cli_endpoint_is_registered_or_explicit_drift() -> None:
    registered = {
        (operation["method"], operation["path"]) for operation in CONTRACT["operations"].values()
    }
    drift = {
        (entry["source"], entry["method"], entry["path"])
        for entry in CONTRACT["known_drift"]
        if entry["kind"] == "endpoint"
    }
    source_requests = _source_requests()

    unknown = {
        item
        for item in source_requests
        if (item[1], item[2]) not in registered and item not in drift
    }
    assert unknown == set()

    stale_allowlist = drift - source_requests
    assert stale_allowlist == set()


def test_literal_endpoint_inventory_handles_keyword_paths() -> None:
    assert _literal_client_requests(
        'client.post(path="/new-endpoint", json={"value": 1})',
        "example.py",
    ) == {("example.py", "POST", "/new-endpoint")}


def test_known_drift_excludes_retired_entries_and_remains_actionable() -> None:
    retired = {
        ("endpoint", "POST", "/pages/content", ()),
        ("endpoint", "POST", "/pages/copy", ()),
        ("request-fields", "POST", "/pages/import", ("parentPageId",)),
        ("request-fields", "POST", "/pages/move", ("spaceId",)),
    }
    actual = {
        (
            entry["kind"],
            entry["method"],
            entry["path"],
            tuple(entry.get("fields", [])),
        )
        for entry in CONTRACT["known_drift"]
    }
    assert actual.isdisjoint(retired)
    for entry in CONTRACT["known_drift"]:
        assert entry["owner"].startswith("high-priority fix ")
        assert entry["replacement"]
        assert (ROOT / entry["source"]).is_file()
        if entry["kind"] == "endpoint":
            assert entry["upstream_absence"]


def test_no_known_request_field_drift_remains() -> None:
    assert not [entry for entry in CONTRACT["known_drift"] if entry["kind"] == "request-fields"]


def test_session_login_request_matches_pinned_contract(
    httpx_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    httpx_mock.add_response(
        url="https://docs.example.test/api/auth/login",
        json={"token": "test-token"},
    )
    auth = SessionAuth(
        "https://docs.example.test",
        "contract@example.test",
        "secret",
    )
    with httpx.Client() as client:
        auth.refresh(client)

    recorded = httpx_mock.get_requests()
    assert len(recorded) == 1
    login_request = recorded[0]
    request = Request(
        login_request.method,
        login_request.url.path.removeprefix("/api"),
        frozenset(json.loads(login_request.content).keys()),
    )
    _assert_request_matches("auth.login", request)


@pytest.mark.parametrize(
    ("operation_name", "invoke"),
    [
        (
            "attachments.search",
            lambda client: search_attachments(
                client,
                "roadmap",
                space_id="00000000-0000-4000-8000-000000000002",
            ),
        ),
        (
            "attachments.download",
            lambda client: download_attachment(
                client,
                {
                    "id": "00000000-0000-4000-8000-000000000001",
                    "fileName": "report.pdf",
                },
            ),
        ),
        (
            "attachments.info",
            lambda client: get_attachment_info(
                client,
                "00000000-0000-4000-8000-000000000001",
            ),
        ),
        (
            "comments.list",
            lambda client: list_comments(
                client,
                "00000000-0000-4000-8000-000000000001",
            ),
        ),
        (
            "comments.create",
            lambda client: create_comment(
                client,
                page_id="00000000-0000-4000-8000-000000000001",
                content="Contract test",
            ),
        ),
        (
            "comments.update",
            lambda client: update_comment(
                client,
                comment_id="00000000-0000-4000-8000-000000000003",
                content="Updated",
            ),
        ),
        (
            "file-tasks.info",
            lambda client: client.post(
                path="/file-tasks/info",
                json={"fileTaskId": "00000000-0000-4000-8000-000000000004"},
                retry_safe=True,
            ),
        ),
        (
            "pages.delete",
            lambda client: delete_page(
                client,
                "00000000-0000-4000-8000-000000000001",
            ),
        ),
        (
            "pages.create",
            lambda client: create_page(
                client,
                space_id="00000000-0000-4000-8000-000000000002",
                title="Contract",
                content="# Contract",
            ),
        ),
        (
            "pages.duplicate",
            lambda client: duplicate_page(
                client,
                "00000000-0000-4000-8000-000000000001",
            ),
        ),
        pytest.param(
            "pages.export",
            lambda client: export_page(
                client,
                "00000000-0000-4000-8000-000000000001",
            ),
            id="pages-export-content",
        ),
        pytest.param(
            "pages.export",
            lambda client: export_page_archive(
                client,
                "00000000-0000-4000-8000-000000000001",
                include_children=True,
            ),
            id="pages-export-archive",
        ),
        (
            "pages.history",
            lambda client: get_page_history(
                client,
                "00000000-0000-4000-8000-000000000001",
                limit=5,
                cursor="cursor-1",
            ),
        ),
        (
            "pages.info",
            lambda client: get_page_info(
                client,
                "00000000-0000-4000-8000-000000000001",
            ),
        ),
        (
            "pages.move",
            lambda client: move_page(
                client,
                page_id="00000000-0000-4000-8000-000000000001",
                parent_page_id="00000000-0000-4000-8000-000000000003",
                position="aaaaa",
            ),
        ),
        (
            "pages.move-to-space",
            lambda client: move_page(
                client,
                page_id="00000000-0000-4000-8000-000000000001",
                space_id="00000000-0000-4000-8000-000000000002",
            ),
        ),
        (
            "pages.recent",
            lambda client: list_recent_pages(
                client,
                "00000000-0000-4000-8000-000000000002",
                limit=5,
                cursor="cursor-1",
            ),
        ),
        pytest.param(
            "pages.sidebar",
            lambda client: get_page_children(
                client,
                "00000000-0000-4000-8000-000000000001",
                space_id="00000000-0000-4000-8000-000000000002",
            ),
            id="pages-sidebar-children",
        ),
        pytest.param(
            "pages.sidebar",
            lambda client: get_sidebar_pages(
                client,
                "00000000-0000-4000-8000-000000000002",
            ),
            id="pages-sidebar-space",
        ),
        pytest.param(
            "pages.update",
            lambda client: update_page_meta(
                client,
                page_id="00000000-0000-4000-8000-000000000001",
                title="Updated",
                icon="📚",
            ),
            id="pages-update-metadata",
        ),
        pytest.param(
            "pages.update",
            lambda client: update_page_content(
                client,
                page_id="00000000-0000-4000-8000-000000000001",
                content="# Updated",
            ),
            id="pages-update-content",
        ),
        (
            "search.pages",
            lambda client: search(
                client,
                "roadmap",
                space_id="00000000-0000-4000-8000-000000000002",
                limit=5,
                offset=10,
            ),
        ),
        (
            "spaces.list",
            lambda client: list_spaces(client, limit=5, cursor="cursor-1"),
        ),
        (
            "spaces.info",
            lambda client: get_space_info(
                client,
                space_id="00000000-0000-4000-8000-000000000002",
            ),
        ),
        (
            "spaces.create",
            lambda client: create_space(
                client,
                name="Contract Tests",
                description="Disposable",
            ),
        ),
        (
            "spaces.update",
            lambda client: update_space(
                client,
                space_id="00000000-0000-4000-8000-000000000002",
                name="Updated",
                description="Disposable",
            ),
        ),
        ("users.me", get_current_user),
        ("workspace.info", get_workspace_info),
        (
            "workspace.members",
            lambda client: list_workspace_members(
                client,
                limit=5,
                cursor="cursor-1",
            ),
        ),
    ],
)
def test_json_request_bodies_match_pinned_contract(
    operation_name: str,
    invoke: Callable[[RecordingClient], Any],
) -> None:
    request = _single_request(invoke)
    _assert_request_matches(operation_name, request)


def test_upload_request_matches_pinned_contract(tmp_path: Path) -> None:
    source = tmp_path / "contract.txt"
    source.write_text("contract")
    request = _single_request(
        lambda client: upload_attachment(
            client,
            page_id="00000000-0000-4000-8000-000000000001",
            file_path=source,
        )
    )
    _assert_request_matches("attachments.upload", request)


@pytest.mark.parametrize(
    ("operation_name", "invoke"),
    [
        (
            "pages.import",
            lambda client: import_page(
                client,
                space_id="00000000-0000-4000-8000-000000000002",
                file_name="contract.md",
                file_bytes=b"# Contract",
            ),
        ),
        (
            "pages.import-zip",
            lambda client: import_page_archive(
                client,
                space_id="00000000-0000-4000-8000-000000000002",
                file_name="contract.zip",
                file_bytes=b"PK",
            ),
        ),
    ],
)
def test_import_request_bodies_match_pinned_contract(
    operation_name: str,
    invoke: Callable[[RecordingClient], Any],
) -> None:
    request = _single_request(invoke)
    _assert_request_matches(operation_name, request)
