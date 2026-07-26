"""Manifest file handling for sync state tracking.

The manifest (.docmost-manifest.json) records which pages have been synced,
their content hashes, and filename mappings so that subsequent sync operations
can detect local and remote changes.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from docmost_cli.output.formatter import print_error

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "build_manifest",
    "build_page_entry",
    "build_server_revision",
    "compute_content_hash",
    "load_manifest",
    "sanitize_filename",
    "save_manifest",
]

MANIFEST_FILENAME = ".docmost-manifest.json"
MANIFEST_VERSION = 3
SERVER_REVISION_VERSION = 1

_UNSAFE_CHARS_RE = re.compile(r'[/\\:*?"<>|]')
_MULTI_DASH_RE = re.compile(r"-{2,}")
_MAX_TITLE_LENGTH = 80
_ID_PREFIX_LENGTH = 8


def sanitize_filename(title: str, page_id: str) -> str:
    """Generate a safe filename from page title and ID prefix.

    Format: ``{sanitized_title}--{id_prefix_8chars}.md``

    Rules applied to the title portion:
    - Replace ``/ \\ : * ? " < > |`` with ``-``
    - Collapse multiple consecutive dashes to a single dash
    - Strip leading/trailing dashes and whitespace
    - Limit title portion to 80 characters
    - Fall back to ``untitled`` if the title sanitizes to empty

    Args:
        title: The page title (may contain any characters).
        page_id: The full page UUID.

    Returns:
        A filesystem-safe filename ending in ``.md``.
    """
    sanitized = _UNSAFE_CHARS_RE.sub("-", title)
    sanitized = _MULTI_DASH_RE.sub("-", sanitized)
    sanitized = sanitized.strip("- \t\n\r")
    sanitized = sanitized[:_MAX_TITLE_LENGTH]
    # Re-strip in case truncation left a trailing dash
    sanitized = sanitized.rstrip("- ")
    if not sanitized:
        sanitized = "untitled"
    id_prefix = page_id[:_ID_PREFIX_LENGTH]
    return f"{sanitized}--{id_prefix}.md"


def compute_content_hash(content: str) -> str:
    """Compute SHA-256 hash of content for change detection.

    Strips trailing whitespace before hashing so that insignificant
    trailing newlines do not cause false-positive diffs.

    Args:
        content: The text content to hash.

    Returns:
        Hash string in the format ``sha256:{hex_digest}``.
    """
    normalized = content.rstrip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def build_server_revision(page: dict[str, Any]) -> dict[str, Any]:
    """Build a canonical fingerprint of server-controlled page state.

    The fingerprint is computed directly from the raw ``/pages/info`` JSON
    response. It intentionally does not use locally converted Markdown, so
    formatting normalization during ProseMirror-to-Markdown conversion cannot
    create false remote-change conflicts.

    ``updatedAt`` is retained for conflict diagnostics but is not part of the
    fingerprint. Docmost may advance that timestamp for a no-op metadata write;
    only a material change to state represented by sync should conflict.
    """
    state = {
        "id": page.get("id"),
        "spaceId": page.get("spaceId"),
        "parentPageId": page.get("parentPageId") or None,
        "title": page.get("title") or "",
        "icon": page.get("icon") or "",
        "content": page.get("content"),
        "deletedAt": page.get("deletedAt"),
    }
    canonical = json.dumps(
        state,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "version": SERVER_REVISION_VERSION,
        "fingerprint": f"sha256:{digest}",
        "updated_at": page.get("updatedAt"),
    }


def load_manifest(dir_path: Path) -> dict[str, Any] | None:
    """Load the sync manifest from a directory.

    Args:
        dir_path: Directory that should contain the manifest file.

    Returns:
        The parsed manifest dict, or ``None`` if the file does not exist.

    Raises:
        SystemExit: If the manifest version is newer than what this CLI supports.
    """
    manifest_path = dir_path / MANIFEST_FILENAME
    try:
        with manifest_path.open("r", encoding="utf-8") as f:
            manifest: dict[str, Any] = json.load(f)
    except FileNotFoundError:
        return None
    version = manifest.get("version", 0)
    if version > MANIFEST_VERSION:
        print_error(
            f"Manifest version {version} is not supported by this CLI "
            f"(max supported: {MANIFEST_VERSION}). Please upgrade docmost-cli."
        )
    return manifest


def save_manifest(dir_path: Path, manifest: dict[str, Any]) -> None:
    """Save the sync manifest to a directory as pretty-printed JSON.

    Uses atomic write (write to ``.tmp`` then rename) to avoid
    partial writes on crash.

    Args:
        dir_path: Directory where the manifest file will be written.
        manifest: The manifest dict to persist.
    """
    manifest_path = dir_path / MANIFEST_FILENAME
    tmp_path = dir_path / (MANIFEST_FILENAME + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp_path.replace(manifest_path)


def build_manifest(
    space_slug: str,
    space_id: str,
    pages: list[dict[str, Any]],
    assets: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a new manifest dict from page data.

    Args:
        space_slug: The space slug identifier (e.g. ``"eng"``).
        space_id: The space UUID.
        pages: List of dicts, each with keys:
            ``id``, ``title``, ``filename``, ``parent_id``, ``icon``,
            ``content_hash``.

    Returns:
        A complete manifest dict ready for :func:`save_manifest`.
    """
    now = datetime.now(UTC).isoformat()
    pages_by_id: dict[str, dict[str, Any]] = {}
    for page in pages:
        pages_by_id[page["id"]] = build_page_entry(
            title=page["title"],
            filename=page["filename"],
            parent_id=page.get("parent_id"),
            icon=page.get("icon", ""),
            content_hash=page["content_hash"],
            attachment_ids=page.get("attachment_ids", []),
            server_revision=page.get("server_revision"),
        )
    return {
        "version": MANIFEST_VERSION,
        "space_slug": space_slug,
        "space_id": space_id,
        "synced_at": now,
        "pages": pages_by_id,
        "assets": assets or {},
    }


def build_page_entry(
    title: str,
    filename: str,
    parent_id: str | None,
    icon: str,
    content_hash: str,
    attachment_ids: list[str] | None = None,
    server_revision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a single page entry for inclusion in the manifest.

    Args:
        title: Page title.
        filename: The sanitized local filename.
        parent_id: Parent page ID, or ``None`` for root pages.
        icon: Page icon (emoji or empty string).
        content_hash: Content hash from :func:`compute_content_hash`.
        attachment_ids: Attachment IDs referenced by the page.
        server_revision: Canonical revision from :func:`build_server_revision`.

    Returns:
        A dict representing one page in the manifest ``pages`` map.
    """
    entry: dict[str, Any] = {
        "title": title,
        "filename": filename,
        "parent_id": parent_id,
        "icon": icon,
        "content_hash": content_hash,
    }
    if attachment_ids:
        entry["attachment_ids"] = attachment_ids
    if server_revision:
        entry["server_revision"] = server_revision
    return entry
