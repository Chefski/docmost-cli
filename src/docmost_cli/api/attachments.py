"""Attachment API methods and stable attachment references."""

from __future__ import annotations

import mimetypes
from typing import TYPE_CHECKING, Any

from docmost_cli.api.pagination import build_body

if TYPE_CHECKING:
    from pathlib import Path

    from docmost_cli.api.client import DocmostClient

__all__ = [
    "attachment_path",
    "download_attachment",
    "get_attachment_info",
    "search_attachments",
    "upload_attachment",
]


def attachment_path(attachment_id: str, file_name: str) -> str:
    """Return the canonical server-relative path for an attachment."""
    from urllib.parse import quote

    return f"/api/files/{attachment_id}/{quote(file_name, safe='')}"


def _with_urls(client: DocmostClient, attachment: dict[str, Any]) -> dict[str, Any]:
    """Add canonical relative and absolute URLs to an attachment response."""
    result = dict(attachment)
    attachment_id = str(result.get("id", ""))
    file_name = str(result.get("fileName", ""))
    if attachment_id and file_name:
        result["path"] = attachment_path(attachment_id, file_name)
        result["url"] = client.attachment_url(attachment_id, file_name)
    return result


def upload_attachment(
    client: DocmostClient,
    *,
    page_id: str,
    file_path: Path,
    attachment_id: str | None = None,
    upload_name: str | None = None,
) -> dict[str, Any]:
    """Upload a file owned by a page.

    Supplying ``attachment_id`` replaces the bytes of that attachment in place,
    preserving its stable ID and URL. Docmost requires the replacement to have
    the same extension as the original attachment.
    """
    if not file_path.is_file():
        raise FileNotFoundError(file_path)

    if attachment_id and upload_name is None:
        existing = get_attachment_info(client, attachment_id)
        upload_name = str(existing["fileName"])
    file_name = upload_name or file_path.name
    mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    data = {"pageId": page_id}
    if attachment_id:
        data["attachmentId"] = attachment_id
    files = {"file": (file_name, file_path.read_bytes(), mime_type)}
    response = client.post_multipart("/files/upload", data=data, files=files)
    attachment = response.get("data", response)
    attachment.setdefault("pageId", page_id)
    return _with_urls(client, attachment)


def get_attachment_info(client: DocmostClient, attachment_id: str) -> dict[str, Any]:
    """Return attachment metadata plus stable relative and absolute URLs."""
    response = client.post("/files/info", json={"attachmentId": attachment_id})
    attachment = response.get("data", response)
    return _with_urls(client, attachment)


def download_attachment(
    client: DocmostClient,
    attachment: dict[str, Any] | str,
) -> tuple[dict[str, Any], bytes]:
    """Download an attachment and return its normalized metadata and bytes."""
    info = (
        get_attachment_info(client, attachment)
        if isinstance(attachment, str)
        else _with_urls(client, attachment)
    )
    api_path = attachment_path(str(info["id"]), str(info["fileName"])).removeprefix("/api")
    response = client.get_raw(api_path)
    return info, response.content


def search_attachments(
    client: DocmostClient,
    query: str,
    *,
    space_id: str | None = None,
) -> dict[str, Any]:
    """Search attachments by query string.

    Attachment search requires Docmost's attachment-indexing feature.
    Direct upload/info/download use the core attachment endpoints.
    """
    body = build_body({"query": query}, spaceId=space_id)
    return client.post(
        "/search-attachments",
        json=body,
        error_messages={
            403: (
                "Attachment search is unavailable or permission was denied. "
                "Verify that Enterprise attachment indexing is enabled and that "
                "you can access the requested space."
            ),
            404: (
                "Attachment search is unavailable on this Docmost instance. "
                "This command requires the Enterprise attachment-indexing feature."
            ),
        },
    )
