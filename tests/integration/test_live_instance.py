"""Opt-in API behavior tests against a dedicated Docmost test instance."""

from __future__ import annotations

import io
import json
import os
import time
import uuid
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from docmost_cli.api.attachments import (
    download_attachment,
    get_attachment_info,
    search_attachments,
    upload_attachment,
)
from docmost_cli.api.comments import create_comment, list_comments, update_comment
from docmost_cli.api.pages import (
    POSITION_FIRST,
    copy_page,
    delete_page,
    duplicate_page,
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
from docmost_cli.api.pagination import extract_id, extract_items
from docmost_cli.api.search import search
from docmost_cli.api.spaces import create_space, get_space_info, list_spaces, update_space
from docmost_cli.api.users import get_current_user
from docmost_cli.api.workspace import get_workspace_info, list_workspace_members

from .conftest import CreatedResources, enabled

if TYPE_CHECKING:
    from docmost_cli.api.client import DocmostClient


pytestmark = pytest.mark.integration
CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "docmost-v0.95.0.json").read_text()
)


def _resource_id(response: dict[str, Any]) -> str:
    resource_id = extract_id(response)
    assert resource_id, f"response did not contain a resource ID: {response}"
    return resource_id


def _skip_known_drift(*selectors: tuple[str, str]) -> None:
    drift = {(entry["kind"], entry["path"]): entry for entry in CONTRACT["known_drift"]}
    blockers = [drift[selector] for selector in selectors if selector in drift]
    if blockers:
        owners = ", ".join(str(entry["owner"]) for entry in blockers)
        pytest.skip(f"blocked by explicit contract drift owned by {owners}")


def test_community_identity_and_workspace_reads(integration_client: DocmostClient) -> None:
    user = get_current_user(integration_client)
    workspace = get_workspace_info(integration_client)
    spaces = extract_items(list_spaces(integration_client, limit=5))

    assert user.get("id")
    assert workspace
    assert isinstance(spaces, list)


def test_community_space_reads(
    integration_client: DocmostClient,
    read_space_id: str,
) -> None:
    space = get_space_info(integration_client, space_id=read_space_id)
    recent = extract_items(list_recent_pages(integration_client, read_space_id, limit=5))
    sidebar = extract_items(get_sidebar_pages(integration_client, read_space_id))
    results = extract_items(search(integration_client, "contract", space_id=read_space_id, limit=5))

    assert space.get("id") == read_space_id
    assert isinstance(recent, list)
    assert isinstance(sidebar, list)
    assert isinstance(results, list)


def test_community_page_reads(integration_client: DocmostClient) -> None:
    page_id = os.getenv("DOCMOST_INTEGRATION_PAGE_ID")
    if not page_id:
        pytest.skip("DOCMOST_INTEGRATION_PAGE_ID is not set")

    info = get_page_info(integration_client, page_id)
    content = get_page_content(integration_client, page_id)
    history = extract_items(get_page_history(integration_client, page_id, limit=5))
    comments = extract_items(list_comments(integration_client, page_id))

    assert info.get("id") == page_id
    assert content.get("id") == page_id
    assert "content" in content
    assert isinstance(history, list)
    assert isinstance(comments, list)


def test_community_attachment_reads(integration_client: DocmostClient) -> None:
    attachment_id = os.getenv("DOCMOST_INTEGRATION_ATTACHMENT_ID")
    if not attachment_id:
        pytest.skip("DOCMOST_INTEGRATION_ATTACHMENT_ID is not set")

    info = get_attachment_info(integration_client, attachment_id)
    downloaded_info, content = download_attachment(integration_client, info)

    assert downloaded_info.get("id") == attachment_id
    assert isinstance(content, bytes)


def test_admin_workspace_member_read(integration_client: DocmostClient) -> None:
    if not enabled("DOCMOST_INTEGRATION_ALLOW_ADMIN_READS"):
        pytest.skip("DOCMOST_INTEGRATION_ALLOW_ADMIN_READS is not enabled")
    members = extract_items(list_workspace_members(integration_client, limit=5))
    assert isinstance(members, list)


def test_enterprise_attachment_search(integration_client: DocmostClient) -> None:
    if os.getenv("DOCMOST_INTEGRATION_EDITION", "").lower() != "enterprise":
        pytest.skip("DOCMOST_INTEGRATION_EDITION is not enterprise")
    if not enabled("DOCMOST_INTEGRATION_ATTACHMENT_SEARCH"):
        pytest.skip("DOCMOST_INTEGRATION_ATTACHMENT_SEARCH is not enabled")
    _skip_known_drift(("endpoint", "/search-attachments"))

    results = extract_items(search_attachments(integration_client, "contract"))
    assert isinstance(results, list)


def test_safe_page_comment_attachment_and_sync_primitives(
    integration_client: DocmostClient,
    mutation_space_id: str,
    created_resources: CreatedResources,
    tmp_path: Path,
) -> None:
    """Exercise mutations only inside the explicitly authorized test space."""
    suffix = uuid.uuid4().hex[:10]

    parent_response = import_page(
        integration_client,
        space_id=mutation_space_id,
        file_name=f"contract-parent-{suffix}.md",
        file_bytes=f"# Contract parent {suffix}\n\nInitial content".encode(),
    )
    parent_id = _resource_id(parent_response)
    created_resources.page_ids.append(parent_id)

    child_response = import_page(
        integration_client,
        space_id=mutation_space_id,
        file_name=f"contract-child-{suffix}.md",
        file_bytes=f"# Contract child {suffix}\n\nChild content".encode(),
    )
    child_id = _resource_id(child_response)
    created_resources.page_ids.append(child_id)
    move_page(
        integration_client,
        page_id=child_id,
        parent_page_id=parent_id,
        position=POSITION_FIRST,
    )
    child_info = get_page_info(integration_client, child_id)
    assert child_info.get("parentPageId") == parent_id

    update_page_content(
        integration_client,
        page_id=parent_id,
        content=f"# Contract parent {suffix}\n\nUpdated content",
    )
    title_before_remote_edit = str(get_page_info(integration_client, parent_id).get("title"))
    remote_title = f"Contract remote edit {suffix}"
    update_page_meta(integration_client, page_id=parent_id, title=remote_title, icon="🧪")
    remote_info = get_page_info(integration_client, parent_id)
    assert remote_info.get("title") == remote_title
    assert remote_info.get("title") != title_before_remote_edit

    move_page(
        integration_client,
        page_id=child_id,
        parent_page_id=None,
        position=POSITION_FIRST,
    )
    assert get_page_info(integration_client, child_id).get("parentPageId") is None

    duplicate_response = duplicate_page(integration_client, parent_id)
    duplicate_id = _resource_id(duplicate_response)
    created_resources.page_ids.append(duplicate_id)
    assert get_page_info(integration_client, duplicate_id).get("id") == duplicate_id

    comment = create_comment(
        integration_client,
        page_id=parent_id,
        content=f"Contract comment {suffix}",
    )
    comment_id = _resource_id(comment)
    updated_comment = update_comment(
        integration_client,
        comment_id=comment_id,
        content=f"Updated contract comment {suffix}",
    )
    assert _resource_id(updated_comment) == comment_id

    upload_path = tmp_path / f"contract-{suffix}.txt"
    upload_path.write_text(f"contract attachment {suffix}")
    attachment = upload_attachment(
        integration_client,
        page_id=parent_id,
        file_path=upload_path,
    )
    attachment_id = _resource_id(attachment)
    attachment_info = get_attachment_info(integration_client, attachment_id)
    _, attachment_bytes = download_attachment(integration_client, attachment_info)
    assert attachment_bytes == upload_path.read_bytes()

    delete_page(integration_client, child_id)
    created_resources.page_ids.remove(child_id)


def test_safe_cross_space_copy_and_move(
    integration_client: DocmostClient,
    mutation_space_id: str,
    created_resources: CreatedResources,
) -> None:
    second_space_id = os.getenv("DOCMOST_INTEGRATION_SECOND_MUTATION_SPACE_ID")
    if not second_space_id:
        pytest.skip("DOCMOST_INTEGRATION_SECOND_MUTATION_SPACE_ID is not set")
    _skip_known_drift(
        ("endpoint", "/pages/copy"),
        ("request-fields", "/pages/move"),
    )

    suffix = uuid.uuid4().hex[:10]
    response = import_page(
        integration_client,
        space_id=mutation_space_id,
        file_name=f"contract-cross-space-{suffix}.md",
        file_bytes=f"# Contract cross-space {suffix}".encode(),
    )
    page_id = _resource_id(response)
    created_resources.page_ids.append(page_id)

    copied = copy_page(integration_client, page_id, second_space_id)
    copied_id = _resource_id(copied)
    created_resources.page_ids.append(copied_id)
    assert get_page_info(integration_client, copied_id).get("spaceId") == second_space_id

    move_page(integration_client, page_id=page_id, space_id=second_space_id)
    assert get_page_info(integration_client, page_id).get("spaceId") == second_space_id


def test_safe_async_zip_import(
    integration_client: DocmostClient,
    mutation_space_id: str,
) -> None:
    if not enabled("DOCMOST_INTEGRATION_ALLOW_ASYNC_IMPORT"):
        pytest.skip("DOCMOST_INTEGRATION_ALLOW_ASYNC_IMPORT is not enabled")

    suffix = uuid.uuid4().hex[:10]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr(f"contract-{suffix}.md", f"# Contract archive {suffix}")
    response = import_page_archive(
        integration_client,
        space_id=mutation_space_id,
        file_name=f"contract-{suffix}.zip",
        file_bytes=archive.getvalue(),
    )
    task_id = _resource_id(response)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        task_response = integration_client.post(
            "/file-tasks/info",
            json={"fileTaskId": task_id},
            retry_safe=True,
        )
        task = task_response.get("data", task_response)
        assert isinstance(task, dict), f"unexpected file-task response: {task_response}"
        status = str(task.get("status", "")).lower()
        if status == "success":
            return
        if status == "failed":
            pytest.fail(f"async ZIP import failed: {task}")
        time.sleep(1)
    pytest.fail(f"async ZIP import did not complete within 60 seconds: {task_id}")


def test_safe_space_create_and_update(
    integration_client: DocmostClient,
    created_resources: CreatedResources,
) -> None:
    if not enabled("DOCMOST_INTEGRATION_ALLOW_SPACE_MUTATIONS"):
        pytest.skip("DOCMOST_INTEGRATION_ALLOW_SPACE_MUTATIONS is not enabled")
    if not enabled("DOCMOST_INTEGRATION_ALLOW_MUTATIONS"):
        pytest.skip("DOCMOST_INTEGRATION_ALLOW_MUTATIONS is not enabled")

    suffix = uuid.uuid4().hex[:10]
    created = create_space(
        integration_client,
        name=f"CLI contract {suffix}",
        slug=f"cli-contract-{suffix}",
        description="Disposable integration-test space",
    )
    space_id = _resource_id(created)
    created_resources.space_ids.append(space_id)

    updated = update_space(
        integration_client,
        space_id=space_id,
        name=f"CLI contract updated {suffix}",
    )
    assert _resource_id(updated) == space_id
