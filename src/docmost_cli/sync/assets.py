"""Attachment asset discovery, local storage, and Markdown rewriting for sync."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from docmost_cli.api.attachments import attachment_path, upload_attachment

ASSETS_DIRNAME = "files"

__all__ = [
    "ASSETS_DIRNAME",
    "LocalAssetReference",
    "asset_markdown_path",
    "asset_relative_path",
    "build_asset_entry",
    "collect_attachment_ids",
    "compute_file_hash",
    "discover_local_assets",
    "prepare_markdown_assets",
]


# Markdown image/link destinations. This intentionally targets inline links,
# which is the format emitted by the converter and Docmost's exporter.
_MARKDOWN_LINK_RE = re.compile(
    r"(?P<image>!)?\["
    r"(?P<label>(?:\\.|[^\[\]\\]|\[(?:\\.|[^\[\]\\])*\])*)\]\("
    r"(?P<destination><(?:\\.|[^>])+>|(?:\\.|[^\s()\\]|\((?:\\.|[^()\\])*\))+)"
    r"(?:\s+(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'))?\)"
)
_SERVER_ATTACHMENT_RE = re.compile(r"(?:^|/)(?:api/)?files/([^/?#]+)/")


@dataclass(frozen=True)
class LocalAssetReference:
    """A local file referenced by an inline Markdown image or link."""

    raw: str
    destination: str
    relative_path: str
    absolute_path: Path
    label: str
    is_image: bool


def _safe_file_name(file_name: str, attachment_id: str) -> str:
    name = Path(file_name.replace("\\", "/")).name.strip()
    return name if name not in {"", ".", ".."} else f"attachment-{attachment_id}"


def asset_relative_path(attachment_id: str, file_name: str) -> str:
    """Return the portable local path for an attachment asset."""
    return f"{ASSETS_DIRNAME}/{attachment_id}/{_safe_file_name(file_name, attachment_id)}"


def asset_markdown_path(relative_path: str) -> str:
    """URL-encode a portable asset path for use in Markdown."""
    return "/".join(quote(part, safe="") for part in Path(relative_path).as_posix().split("/"))


def compute_file_hash(path: Path) -> str:
    """Compute a streaming SHA-256 hash for a local asset."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def collect_attachment_ids(document: Any) -> list[str]:
    """Collect stable attachment IDs from a ProseMirror document."""
    found: list[str] = []
    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            attrs = value.get("attrs")
            if isinstance(attrs, dict):
                attachment_id = attrs.get("attachmentId")
                if not attachment_id:
                    for source_key in ("src", "url"):
                        source = attrs.get(source_key)
                        if not isinstance(source, str):
                            continue
                        match = _SERVER_ATTACHMENT_RE.search(source)
                        if match:
                            attachment_id = unquote(match.group(1))
                            break
                if isinstance(attachment_id, str) and attachment_id and attachment_id not in seen:
                    seen.add(attachment_id)
                    found.append(attachment_id)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document)
    return found


def build_asset_entry(info: dict[str, Any], relative_path: str, file_path: Path) -> dict[str, Any]:
    """Build a manifest entry from server metadata and a local file."""
    return {
        "file_name": str(info["fileName"]),
        "path": relative_path,
        "mime_type": str(info.get("mimeType") or "application/octet-stream"),
        "size": int(info.get("fileSize") or file_path.stat().st_size),
        "page_id": str(info.get("pageId") or ""),
        "content_hash": compute_file_hash(file_path),
        "server_path": str(
            info.get("path") or attachment_path(str(info["id"]), str(info["fileName"]))
        ),
    }


def _local_destination(
    destination: str,
    dir_path: Path,
) -> tuple[str, Path] | None:
    raw = (
        destination[1:-1]
        if destination.startswith("<") and destination.endswith(">")
        else destination
    )
    raw = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])", r"\1", raw)
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or raw.startswith(("#", "/")):
        return None

    decoded = unquote(parsed.path)
    if not decoded:
        return None

    candidate = (dir_path / decoded).resolve()
    root = dir_path.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None

    relative = candidate.relative_to(root).as_posix()
    if candidate.suffix.lower() in {".md", ".html", ".htm"} and not relative.startswith(
        f"{ASSETS_DIRNAME}/"
    ):
        return None
    if not candidate.is_file() and not relative.startswith(f"{ASSETS_DIRNAME}/"):
        return None
    return relative, candidate


def discover_local_assets(markdown: str, dir_path: Path) -> list[LocalAssetReference]:
    """Find local files/images referenced by Markdown content."""
    references: list[LocalAssetReference] = []
    for match in _MARKDOWN_LINK_RE.finditer(markdown):
        resolved = _local_destination(match.group("destination"), dir_path)
        if resolved is None:
            continue
        relative_path, absolute_path = resolved
        references.append(
            LocalAssetReference(
                raw=match.group(0),
                destination=match.group("destination"),
                relative_path=relative_path,
                absolute_path=absolute_path,
                label=match.group("label"),
                is_image=bool(match.group("image")),
            )
        )
    return references


def _attachment_html(reference: LocalAssetReference, info: dict[str, Any]) -> str:
    attachment_id = escape(str(info["id"]), quote=True)
    file_name = escape(str(info["fileName"]), quote=True)
    mime_type = escape(str(info.get("mimeType") or "application/octet-stream"), quote=True)
    file_size = escape(
        str(info.get("fileSize") or reference.absolute_path.stat().st_size),
        quote=True,
    )
    server_path = escape(
        str(info.get("path") or attachment_path(str(info["id"]), str(info["fileName"]))),
        quote=True,
    )
    label = escape(reference.label or str(info["fileName"]), quote=True)

    if reference.is_image:
        return (
            f'<img src="{server_path}" alt="{label}" '
            f'data-attachment-id="{attachment_id}" data-size="{file_size}">'
        )
    return (
        f'<div data-type="attachment" data-attachment-url="{server_path}" '
        f'data-attachment-name="{file_name}" data-attachment-mime="{mime_type}" '
        f'data-attachment-size="{file_size}" data-attachment-id="{attachment_id}"></div>'
    )


def prepare_markdown_assets(
    client: Any,
    *,
    page_id: str,
    markdown: str,
    dir_path: Path,
    manifest: dict[str, Any],
) -> tuple[str, dict[str, dict[str, Any]], list[str]]:
    """Upload changed local assets and rewrite references to Docmost HTML.

    Returns the Markdown/HTML hybrid to send to Docmost, updated manifest asset
    entries keyed by attachment ID, and the IDs referenced by this page.
    """
    references = discover_local_assets(markdown, dir_path)
    if not references:
        return markdown, {}, []

    manifest_assets = manifest.get("assets", {})
    assets_by_path: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for attachment_id, entry in manifest_assets.items():
        if isinstance(entry, dict) and entry.get("path"):
            assets_by_path.setdefault(str(entry["path"]), []).append((attachment_id, entry))
    info_by_path: dict[str, dict[str, Any]] = {}
    updated_entries: dict[str, dict[str, Any]] = {}

    for reference in references:
        if reference.relative_path in info_by_path:
            continue
        if not reference.absolute_path.is_file():
            raise FileNotFoundError(reference.absolute_path)

        candidates = assets_by_path.get(reference.relative_path, [])
        existing = next(
            (candidate for candidate in candidates if candidate[1].get("page_id") == page_id),
            candidates[0] if candidates else None,
        )
        existing_id = existing[0] if existing else None
        existing_entry = existing[1] if existing else {}
        current_hash = compute_file_hash(reference.absolute_path)
        same_bytes = bool(existing and current_hash == existing_entry.get("content_hash"))
        same_owner = bool(existing and existing_entry.get("page_id") == page_id)

        if existing and same_bytes and same_owner:
            info = {
                "id": existing_id,
                "fileName": existing_entry.get("file_name") or reference.absolute_path.name,
                "mimeType": existing_entry.get("mime_type") or "application/octet-stream",
                "fileSize": existing_entry.get("size") or reference.absolute_path.stat().st_size,
                "pageId": existing_entry.get("page_id") or page_id,
                "path": existing_entry.get("server_path")
                or attachment_path(
                    str(existing_id),
                    str(existing_entry.get("file_name") or reference.absolute_path.name),
                ),
            }
        else:
            replacement_id = str(existing_id) if existing and same_owner else None
            upload_name = (
                str(existing_entry.get("file_name"))
                if replacement_id and existing_entry.get("file_name")
                else None
            )
            info = upload_attachment(
                client,
                page_id=page_id,
                file_path=reference.absolute_path,
                attachment_id=replacement_id,
                upload_name=upload_name,
            )

        attachment_id = str(info["id"])
        info_by_path[reference.relative_path] = info
        updated_entries[attachment_id] = build_asset_entry(
            info,
            reference.relative_path,
            reference.absolute_path,
        )

    attachment_ids: list[str] = []

    def replace_reference(match: re.Match[str]) -> str:
        resolved = _local_destination(match.group("destination"), dir_path)
        if resolved is None:
            return match.group(0)
        relative_path, absolute_path = resolved
        info = info_by_path.get(relative_path)
        if info is None:
            return match.group(0)
        reference = LocalAssetReference(
            raw=match.group(0),
            destination=match.group("destination"),
            relative_path=relative_path,
            absolute_path=absolute_path,
            label=match.group("label"),
            is_image=bool(match.group("image")),
        )
        attachment_id = str(info["id"])
        if attachment_id not in attachment_ids:
            attachment_ids.append(attachment_id)
        return _attachment_html(reference, info)

    rewritten = _MARKDOWN_LINK_RE.sub(replace_reference, markdown)
    return rewritten, updated_entries, attachment_ids
