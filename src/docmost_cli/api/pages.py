"""Page API methods."""

from typing import Any

from docmost_cli.api.client import DocmostClient
from docmost_cli.api.pagination import build_body
from docmost_cli.output.formatter import print_error, print_result

__all__ = [
    "POSITION_FIRST",
    "PageImportOverrideError",
    "apply_import_overrides",
    "build_page_tree",
    "copy_page",
    "create_and_place_page",
    "create_page_via_import",
    "delete_page",
    "duplicate_page",
    "export_page",
    "export_page_archive",
    "get_page_children",
    "get_page_content",
    "get_page_history",
    "get_page_info",
    "get_sidebar_pages",
    "import_page",
    "import_page_archive",
    "list_recent_pages",
    "move_page",
    "try_update_page_content",
    "update_page_content",
    "update_page_meta",
]

# Fractional index string meaning "place at beginning" in Docmost's ordering.
POSITION_FIRST = "aaaaa"


class PageImportOverrideError(SystemExit):
    """Raised when a page is imported but a requested override fails.

    The original import response and page ID remain available so callers can
    recover the page without retrying the import and creating a duplicate.
    """

    def __init__(
        self,
        *,
        page_id: str,
        result: dict[str, Any],
        failures: tuple[SystemExit, ...],
    ) -> None:
        if not failures:
            raise ValueError("At least one override failure is required.")
        super().__init__(failures[0].code)
        self.page_id = page_id
        self.result = result
        self.failures = failures


def get_page_info(client: DocmostClient, page_id: str) -> dict[str, Any]:
    """Get page metadata by ID.

    Args:
        client: Authenticated Docmost client.
        page_id: Page UUID.

    Returns:
        Page info dict (unwrapped from data envelope).
    """
    result = client.post("/pages/info", json={"pageId": page_id})
    data = result.get("data", result)
    return data if isinstance(data, dict) else {}


def create_page_via_import(
    client: DocmostClient,
    *,
    space_id: str,
    title: str,
    content: str,
    parent_page_id: str | None = None,
) -> dict[str, Any]:
    """Create a page using the import endpoint (server-side MD→ProseMirror).

    Sends Markdown as a .md file via multipart upload. Available on both
    Community and Enterprise editions.

    Args:
        client: Authenticated Docmost client.
        space_id: Target space UUID.
        title: Page title.
        content: Markdown content.
        parent_page_id: Parent page UUID (optional).

    Returns:
        Raw API response dict (should contain page ID).
    """
    # Ensure content has the title as H1 if not already present
    md_content = content
    if md_content and not md_content.lstrip().startswith("#"):
        md_content = f"# {title}\n\n{md_content}"
    elif not md_content:
        md_content = f"# {title}\n"

    file_bytes = md_content.encode("utf-8")
    files = {"file": (f"{title}.md", file_bytes, "text/markdown")}
    data = build_body({"spaceId": space_id}, parentPageId=parent_page_id)

    return client.post_multipart("/pages/import", data=data, files=files)


def update_page_meta(
    client: DocmostClient,
    *,
    page_id: str,
    title: str | None = None,
    icon: str | None = None,
) -> dict[str, Any]:
    """Update page metadata (title, icon).

    Available on both Community and Enterprise editions.

    Args:
        client: Authenticated Docmost client.
        page_id: Page UUID.
        title: New title.
        icon: New icon emoji.

    Returns:
        Raw API response dict.
    """
    body = build_body({"pageId": page_id}, title=title, icon=icon)
    return client.post("/pages/update", json=body)


def update_page_content(
    client: DocmostClient,
    *,
    page_id: str,
    content: str,
    fmt: str = "markdown",
    operation: str = "replace",
) -> dict[str, Any]:
    """Update page content through Docmost's core page endpoint.

    Args:
        client: Authenticated Docmost client.
        page_id: Page UUID.
        content: Markdown or HTML content.
        fmt: Content format ("markdown" or "html").
        operation: ``replace``, ``append``, or ``prepend``.

    Returns:
        Raw API response dict.
    """
    return client.post(
        "/pages/update",
        json={
            "pageId": page_id,
            "content": content,
            "format": fmt,
            "operation": operation,
        },
    )


def try_update_page_content(
    client: DocmostClient,
    *,
    page_id: str,
    content: str,
    fmt: str = "markdown",
) -> bool:
    """Try updating page content without raising on an unavailable endpoint.

    Args:
        client: Authenticated Docmost client.
        page_id: Page UUID.
        content: Markdown or HTML content.
        fmt: Content format ("markdown" or "html").

    Returns:
        True if the update succeeded, False if the endpoint is unavailable.
    """
    response = client.post_raw(
        "/pages/update",
        json={
            "pageId": page_id,
            "content": content,
            "format": fmt,
            "operation": "replace",
        },
        raise_on_error=False,
    )
    return response.is_success


def create_and_place_page(
    client: DocmostClient,
    *,
    space_id: str,
    title: str,
    content: str,
    parent_page_id: str | None = None,
    icon: str | None = None,
) -> str:
    """Create a page via import, then move to parent and set icon.

    Combines the three-step create+move+icon workflow that the import
    endpoint requires (it ignores parentPageId and icon).

    Args:
        client: Authenticated Docmost client.
        space_id: Target space UUID.
        title: Page title.
        content: Markdown content.
        parent_page_id: Parent page UUID (optional).
        icon: Page icon emoji (optional).

    Returns:
        The new page's UUID.
    """
    from docmost_cli.api.pagination import extract_id

    result = create_page_via_import(client, space_id=space_id, title=title, content=content)
    page_id = extract_id(result)

    if parent_page_id:
        move_page(
            client,
            page_id=page_id,
            parent_page_id=parent_page_id,
            position=POSITION_FIRST,
        )
    if icon:
        update_page_meta(client, page_id=page_id, icon=icon)

    return page_id


def delete_page(client: DocmostClient, page_id: str) -> dict[str, Any]:
    """Delete a page by ID.

    Available on both Community and Enterprise editions.

    Args:
        client: Authenticated Docmost client.
        page_id: Page UUID.

    Returns:
        Raw API response dict.
    """
    return client.post("/pages/delete", json={"pageId": page_id})


def move_page(
    client: DocmostClient,
    *,
    page_id: str,
    parent_page_id: str | None = None,
    space_id: str | None = None,
    position: str | int | None = None,
) -> dict[str, Any]:
    """Move a page to a new location.

    Available on both Community and Enterprise editions.

    Args:
        client: Authenticated Docmost client.
        page_id: Page UUID.
        parent_page_id: New parent page UUID (omit for root).
        space_id: Target space UUID (for cross-space moves).
        position: Position among siblings (fractional index string, 5-12 chars).

    Returns:
        Raw API response dict.
    """
    body = build_body(
        {"pageId": page_id},
        parentPageId=parent_page_id,
        spaceId=space_id,
        position=position,
    )
    return client.post("/pages/move", json=body)


def get_page_content(client: DocmostClient, page_id: str) -> dict[str, Any]:
    """Get page content and metadata.

    Tries POST /pages/content (Enterprise v0.70+) first, then falls back
    to POST /pages/info which may include content on both editions.

    Args:
        client: Authenticated Docmost client.
        page_id: Page UUID.

    Returns:
        Dict with page metadata and content (ProseMirror JSON).
    """
    # Get page info first (needed for metadata and fallback content)
    info = get_page_info(client, page_id)

    # Try Enterprise content endpoint (silently — may not exist on Community)
    response = client.post_raw("/pages/content", json={"pageId": page_id}, raise_on_error=False)
    if response.is_success:
        try:
            content_data = response.json()
            data = content_data.get("data", content_data)
            info["content"] = data.get("content", data)
            return info
        except (ValueError, KeyError):
            pass

    # Fall back to content from /pages/info (already fetched)
    if "content" in info and info["content"]:
        return info

    print_error(
        "Page content not available via REST on this instance. "
        "This may require Enterprise edition (v0.70+). "
        "Try 'docmost-cli page get <id> --raw' or access the page in the web UI.",
        exit_code=1,
    )


def list_recent_pages(
    client: DocmostClient,
    space_id: str,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List recent pages in a space with cursor-based pagination.

    Args:
        client: Authenticated Docmost client.
        space_id: Space UUID.
        limit: Max results to return.
        cursor: Pagination cursor.

    Returns:
        Raw API response dict.
    """
    body = build_body({"spaceId": space_id}, limit=limit, cursor=cursor)
    return client.post("/pages/recent", json=body)


def duplicate_page(client: DocmostClient, page_id: str) -> dict[str, Any]:
    """Duplicate a page.

    Args:
        client: Authenticated Docmost client.
        page_id: Page UUID to duplicate.

    Returns:
        Raw API response dict (should contain new page ID).
    """
    return client.post("/pages/duplicate", json={"pageId": page_id})


def copy_page(client: DocmostClient, page_id: str, space_id: str) -> dict[str, Any]:
    """Copy a page to a different space.

    Args:
        client: Authenticated Docmost client.
        page_id: Page UUID to copy.
        space_id: Target space UUID.

    Returns:
        Raw API response dict (should contain new page ID).
    """
    return client.post("/pages/copy", json={"pageId": page_id, "spaceId": space_id})


def get_page_children(
    client: DocmostClient,
    page_id: str,
    *,
    space_id: str | None = None,
) -> dict[str, Any]:
    """List direct child pages.

    Uses /pages/sidebar-pages with pageId (works on Community edition).
    If space_id is not provided, resolves it from the page's metadata.

    Args:
        client: Authenticated Docmost client.
        page_id: Parent page UUID.
        space_id: Space UUID (resolved from page info if not provided).

    Returns:
        Raw API response dict.
    """
    if not space_id:
        info = get_page_info(client, page_id)
        space_id = info.get("spaceId", "")
    return client.post("/pages/sidebar-pages", json={"spaceId": space_id, "pageId": page_id})


def get_page_history(
    client: DocmostClient,
    page_id: str,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Get page version history.

    Args:
        client: Authenticated Docmost client.
        page_id: Page UUID.
        limit: Max results.
        cursor: Pagination cursor.

    Returns:
        Raw API response dict.
    """
    body = build_body({"pageId": page_id}, limit=limit, cursor=cursor)
    return client.post("/pages/history", json=body)


def export_page(client: DocmostClient, page_id: str, fmt: str = "md") -> str:
    """Export page content.

    Current Docmost versions return a plain file for a single page, while
    older versions returned a one-file ZIP. Both response shapes are accepted.

    Args:
        client: Authenticated Docmost client.
        page_id: Page UUID.
        fmt: Export format ("md" or "html"). Accepts "md" as alias for "markdown".

    Returns:
        Exported content as a string.
    """
    import io
    import zipfile

    # Docmost expects "markdown" not "md"
    api_format = "markdown" if fmt == "md" else fmt
    response = client.post_raw("/pages/export", json={"pageId": page_id, "format": api_format})

    buffer = io.BytesIO(response.content)
    if not zipfile.is_zipfile(buffer):
        return response.content.decode("utf-8")

    # Compatibility with older Docmost versions that zipped single-page exports.
    with zipfile.ZipFile(buffer) as zf:
        expected_suffix = ".md" if api_format == "markdown" else ".html"
        names = [name for name in zf.namelist() if name.lower().endswith(expected_suffix)]
        if not names:
            print_error("Export ZIP contains no page content.", exit_code=1)
        return zf.read(names[0]).decode("utf-8")


def export_page_archive(
    client: DocmostClient,
    page_id: str,
    *,
    fmt: str = "md",
    include_children: bool = False,
) -> bytes:
    """Export a page and its attachment files as a portable ZIP archive."""
    api_format = "markdown" if fmt == "md" else fmt
    response = client.post_raw(
        "/pages/export",
        json={
            "pageId": page_id,
            "format": api_format,
            "includeAttachments": True,
            "includeChildren": include_children,
        },
    )
    return response.content


def apply_import_overrides(
    client: DocmostClient,
    *,
    result: dict[str, Any],
    title: str | None = None,
    parent_page_id: str | None = None,
) -> None:
    """Apply metadata overrides after a successful single-page import.

    Docmost's import controller ignores title and parent fields. This helper
    keeps the post-import update/move sequence and partial-import recovery
    consistent for CLI and API callers.
    """
    from docmost_cli.api.pagination import extract_id

    page_id = extract_id(result)
    failures: list[SystemExit] = []
    if title is not None:
        try:
            update_page_meta(client, page_id=page_id, title=title)
        except SystemExit as exc:
            failures.append(exc)
    if parent_page_id is not None:
        try:
            move_page(
                client,
                page_id=page_id,
                parent_page_id=parent_page_id,
                position=POSITION_FIRST,
            )
        except SystemExit as exc:
            failures.append(exc)

    if failures:
        print_result(
            page_id,
            f"Imported page {page_id}, but failed to apply the requested override(s).",
        )
        raise PageImportOverrideError(
            page_id=page_id,
            result=result,
            failures=tuple(failures),
        ) from failures[0]


def get_sidebar_pages(client: DocmostClient, space_id: str) -> dict[str, Any]:
    """Get page tree structure for a space.

    Returns nested structure with children arrays, used for --tree view.

    Args:
        client: Authenticated Docmost client.
        space_id: Space UUID.

    Returns:
        Raw API response dict with nested page tree.
    """
    return client.post("/pages/sidebar-pages", json={"spaceId": space_id})


def import_page(
    client: DocmostClient,
    *,
    space_id: str,
    file_name: str,
    file_bytes: bytes,
    parent_page_id: str | None = None,
) -> dict[str, Any]:
    """Import a file as a new page via multipart upload.

    Docmost's single-page import endpoint only consumes the uploaded file and
    ``spaceId``. The optional parent remains supported for API compatibility
    and is applied through the page move endpoint after the import returns.

    Args:
        client: Authenticated Docmost client.
        space_id: Target space UUID.
        file_name: Original filename (used for MIME detection and upload).
        file_bytes: Raw file content bytes.
        parent_page_id: Parent page UUID applied after import (optional).

    Returns:
        Raw API response dict (should contain new page ID).
    """
    mime = "text/html" if file_name.lower().endswith((".html", ".htm")) else "text/markdown"
    files = {"file": (file_name, file_bytes, mime)}
    result = client.post_multipart("/pages/import", data={"spaceId": space_id}, files=files)

    if parent_page_id is not None:
        apply_import_overrides(
            client,
            result=result,
            parent_page_id=parent_page_id,
        )

    return result


def import_page_archive(
    client: DocmostClient,
    *,
    space_id: str,
    file_name: str,
    file_bytes: bytes,
) -> dict[str, Any]:
    """Import a Docmost/generic ZIP, preserving included attachment files.

    ZIP imports are processed asynchronously by Docmost. The response is a
    file-task record whose stable ID can be queried through ``/file-tasks/info``.
    """
    files = {"file": (file_name, file_bytes, "application/zip")}
    data = {"spaceId": space_id, "source": "generic"}
    return client.post_multipart("/pages/import-zip", data=data, files=files)


def build_page_tree(
    client: DocmostClient,
    space_id: str,
    *,
    max_depth: int = 10,
) -> list[dict[str, Any]]:
    """Build full page tree, filling in missing children recursively.

    Starts with /pages/sidebar-pages, then uses /pages/children to
    fill in any empty children arrays (sidebar API may not return them).

    Args:
        client: Authenticated Docmost client.
        space_id: Space UUID.
        max_depth: Maximum recursion depth to prevent runaway.

    Returns:
        List of page dicts with populated children arrays.
    """
    from docmost_cli.api.pagination import extract_items

    result = get_sidebar_pages(client, space_id)
    pages = extract_items(result)

    for page in pages:
        _fill_children(client, page, space_id=space_id, depth=0, max_depth=max_depth)

    return pages


def _fill_children(
    client: DocmostClient,
    page: dict[str, Any],
    *,
    space_id: str,
    depth: int,
    max_depth: int,
) -> None:
    """Recursively fetch children if the sidebar API returned them empty."""
    if depth >= max_depth:
        return

    children = page.get("children", [])

    # If sidebar returned empty children, fetch via sidebar-pages with pageId
    if not children and page.get("hasChildren", False):
        try:
            from docmost_cli.api.pagination import extract_items

            result = get_page_children(client, page["id"], space_id=space_id)
            children = extract_items(result)
            page["children"] = children
        except SystemExit as exc:
            if exc.code not in (4,):
                raise
            page["children"] = []
            return

    for child in children:
        _fill_children(client, child, space_id=space_id, depth=depth + 1, max_depth=max_depth)
