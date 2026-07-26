"""Loss-prevention helpers for Markdown-based page synchronization.

Docmost stores pages as ProseMirror JSON. Markdown is intentionally a smaller
format, so some editor nodes, marks, and attributes cannot survive a
Markdown -> ProseMirror replacement. This module records the source document
shape during pull and lets push reject replacements that would be lossy.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Mapping

    from docmost_cli.api.client import DocmostClient
    from docmost_cli.sync.diff import SyncDiff

__all__ = [
    "RichContentConflict",
    "analyze_prosemirror",
    "build_pulled_rich_content_state",
    "fetch_canonical_markdown",
    "find_rich_content_conflicts",
    "markdown_rich_content_state",
    "rewrite_attachment_urls",
]

_GUARD_VERSION = 1
_SNAPSHOT_DIR = Path(".docmost") / "raw-pages"

# These are the nodes represented by Docmost's current server-side
# Markdown serializer and parser. Attributes still require separate checks.
_MARKDOWN_NODES = {
    "doc",
    "paragraph",
    "text",
    "heading",
    "blockquote",
    "bulletList",
    "orderedList",
    "listItem",
    "taskList",
    "taskItem",
    "codeBlock",
    "horizontalRule",
    "table",
    "tableRow",
    "tableHeader",
    "tableCell",
    "image",
    "callout",
    "mathInline",
    "mathBlock",
}
_MARKDOWN_MARKS = {"bold", "italic", "strike", "code", "link"}


@dataclass(frozen=True)
class RichContentConflict:
    """A local replacement that would discard source ProseMirror features."""

    page_id: str
    filename: str
    title: str
    features: tuple[str, ...]
    snapshot_path: str | None


def analyze_prosemirror(content: object) -> tuple[str, ...]:
    """Return author-visible features that cannot round-trip through Markdown.

    Generated paragraph/heading IDs are deliberately ignored: Docmost
    regenerates those IDs when it imports Markdown. The guard focuses on
    author-visible structure, formatting, embedded content, and references.
    """
    if not isinstance(content, dict):
        return ("content:invalid-prosemirror",)

    features: set[str] = set()

    def walk(node: object) -> None:
        if not isinstance(node, dict):
            features.add("content:invalid-node")
            return

        node_type = node.get("type")
        if not isinstance(node_type, str) or not node_type:
            features.add("content:missing-node-type")
        elif node_type not in _MARKDOWN_NODES:
            features.add(f"node:{node_type}")
        else:
            _check_node_attributes(node_type, node.get("attrs"), features)
            _check_node_structure(node_type, node.get("content"), features)

        marks = node.get("marks", [])
        if marks is not None:
            if not isinstance(marks, list):
                features.add("content:invalid-marks")
            else:
                for mark in marks:
                    _check_mark(mark, features)

        children = node.get("content", [])
        if children is not None:
            if not isinstance(children, list):
                features.add("content:invalid-children")
            else:
                for child in children:
                    walk(child)

    walk(content)
    return tuple(sorted(features))


def fetch_canonical_markdown(client: DocmostClient, page_id: str) -> str | None:
    """Ask Docmost to serialize a page with its canonical Markdown converter.

    ``None`` indicates an older server or an invalid response. Callers may
    fall back to the local converter for known-safe documents.
    """
    response = client.post_raw(
        "/pages/info",
        json={"pageId": page_id, "format": "markdown"},
        raise_on_error=False,
    )
    if not response.is_success:
        return None

    try:
        payload = response.json()
    except ValueError:
        return None
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    markdown = data.get("content")
    return markdown if isinstance(markdown, str) else None


def rewrite_attachment_urls(markdown: str, attachment_paths: Mapping[str, str]) -> str:
    """Rewrite Docmost attachment URLs in canonical Markdown to local paths."""
    rewritten = markdown
    for attachment_id, local_path in attachment_paths.items():
        identifiers = {attachment_id, quote(attachment_id, safe="")}
        for identifier in identifiers:
            pattern = re.compile(
                rf"(?:https?://[^/\s)>]+)?/(?:api/)?files/{re.escape(identifier)}/"
                r"[^\s)>\"']+"
            )
            rewritten = pattern.sub(local_path, rewritten)
    return rewritten


def build_pulled_rich_content_state(
    dir_path: Path,
    page_id: str,
    content: object,
) -> dict[str, Any]:
    """Persist a raw ProseMirror recovery snapshot and return guard metadata."""
    snapshot_path = _snapshot_path(page_id)
    serialized = _serialize_snapshot(content)
    destination = dir_path / snapshot_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_suffix(destination.suffix + ".tmp")
    tmp_path.write_text(serialized + "\n", encoding="utf-8")
    tmp_path.replace(destination)

    return {
        "guard_version": _GUARD_VERSION,
        "source": "prosemirror",
        "snapshot_path": snapshot_path.as_posix(),
        "snapshot_hash": _snapshot_hash(serialized),
        "unsafe_features": list(analyze_prosemirror(content)),
    }


def markdown_rich_content_state() -> dict[str, Any]:
    """Return guard metadata for content known to originate as Markdown."""
    return {
        "guard_version": _GUARD_VERSION,
        "source": "markdown",
        "unsafe_features": [],
    }


def find_rich_content_conflicts(
    diff: SyncDiff,
) -> list[RichContentConflict]:
    """Find content/attachment replacements blocked by pull-time metadata.

    Manifests created before guard version 1 remain compatible: entries with
    no ``rich_content`` field are not blocked. A fresh pull enables protection.
    """
    from docmost_cli.sync.diff import ChangeType

    conflicts: list[RichContentConflict] = []
    replacement_changes = {ChangeType.CONTENT_CHANGED, ChangeType.ATTACHMENT_CHANGED}
    for change in diff.modified:
        if not change.changes & replacement_changes:
            continue

        entry = change.manifest_entry or {}
        state = entry.get("rich_content")
        if state is None:
            continue
        if not isinstance(state, dict) or state.get("guard_version") != _GUARD_VERSION:
            features = ("guard:invalid-metadata",)
            snapshot_path = None
        else:
            raw_features = state.get("unsafe_features", [])
            if not isinstance(raw_features, list) or not all(
                isinstance(feature, str) for feature in raw_features
            ):
                features = ("guard:invalid-metadata",)
            else:
                features = tuple(sorted(set(raw_features)))
            raw_snapshot_path = state.get("snapshot_path")
            snapshot_path = raw_snapshot_path if isinstance(raw_snapshot_path, str) else None

        if features:
            meta = change.local_meta or {}
            conflicts.append(
                RichContentConflict(
                    page_id=change.page_id,
                    filename=change.filename,
                    title=meta.get("title") or str(entry.get("title") or change.page_id),
                    features=features,
                    snapshot_path=snapshot_path,
                )
            )
    return conflicts


def _check_mark(mark: object, features: set[str]) -> None:
    if not isinstance(mark, dict):
        features.add("content:invalid-mark")
        return
    mark_type = mark.get("type")
    if not isinstance(mark_type, str) or not mark_type:
        features.add("content:missing-mark-type")
        return
    if mark_type not in _MARKDOWN_MARKS:
        features.add(f"mark:{mark_type}")
        return
    if mark_type == "link":
        attrs = mark.get("attrs")
        if isinstance(attrs, dict):
            _flag_extra_attributes(
                "mark:link",
                attrs,
                allowed={"href", "title"},
                features=features,
            )
        elif attrs is not None:
            features.add("content:invalid-mark-attributes")
    elif _has_meaningful_attributes(mark.get("attrs")):
        features.add(f"mark:{mark_type}.attributes")


def _check_node_attributes(
    node_type: str,
    raw_attrs: object,
    features: set[str],
) -> None:
    if raw_attrs is None:
        return
    if not isinstance(raw_attrs, dict):
        features.add("content:invalid-node-attributes")
        return

    allowed: set[str] = set()
    if node_type == "paragraph":
        allowed = {"id", "textAlign", "indent"}
        _flag_nondefault(raw_attrs, "textAlign", {None, "", "left"}, node_type, features)
        _flag_nondefault(raw_attrs, "indent", {None, 0}, node_type, features)
    elif node_type == "heading":
        allowed = {"id", "level", "textAlign", "indent"}
        level = raw_attrs.get("level", 1)
        if not isinstance(level, int) or not 1 <= level <= 6:
            features.add("attribute:heading.level")
        _flag_nondefault(raw_attrs, "textAlign", {None, "", "left"}, node_type, features)
        _flag_nondefault(raw_attrs, "indent", {None, 0}, node_type, features)
    elif node_type == "orderedList":
        allowed = {"start"}
        start = raw_attrs.get("start", 1)
        if not isinstance(start, int):
            features.add("attribute:orderedList.start")
    elif node_type == "taskItem":
        allowed = {"checked"}
        if "checked" in raw_attrs and not isinstance(raw_attrs["checked"], bool):
            features.add("attribute:taskItem.checked")
    elif node_type == "codeBlock":
        allowed = {"language"}
    elif node_type == "image":
        allowed = {
            "src",
            "alt",
            "title",
            "attachmentId",
            "size",
            "align",
            "width",
            "height",
            "aspectRatio",
            "placeholder",
        }
        _flag_nondefault(raw_attrs, "align", {None, "", "center"}, node_type, features)
        for attr in ("width", "height", "aspectRatio", "placeholder"):
            _flag_nondefault(raw_attrs, attr, {None, "", 0}, node_type, features)
    elif node_type == "callout":
        allowed = {"type", "icon"}
        _flag_nondefault(raw_attrs, "icon", {None, ""}, node_type, features)
    elif node_type in {"mathInline", "mathBlock"}:
        allowed = {"text"}
    elif node_type in {"tableCell", "tableHeader"}:
        allowed = {
            "colspan",
            "rowspan",
            "colwidth",
            "backgroundColor",
            "backgroundColorName",
        }
        _flag_nondefault(raw_attrs, "colspan", {None, 1}, node_type, features)
        _flag_nondefault(raw_attrs, "rowspan", {None, 1}, node_type, features)
        for attr in ("colwidth", "backgroundColor", "backgroundColorName"):
            _flag_nondefault(raw_attrs, attr, {None, "", ()}, node_type, features)

    _flag_extra_attributes(f"node:{node_type}", raw_attrs, allowed, features)


def _check_node_structure(
    node_type: str,
    raw_content: object,
    features: set[str],
) -> None:
    """Flag valid ProseMirror structures that GFM flattens during import."""
    if node_type not in {"taskItem", "tableCell", "tableHeader"}:
        return
    if not isinstance(raw_content, list):
        return
    if len(raw_content) != 1:
        features.add(f"structure:{node_type}.content")
        return
    child = raw_content[0]
    if not isinstance(child, dict) or child.get("type") != "paragraph":
        features.add(f"structure:{node_type}.content")


def _flag_nondefault(
    attrs: dict[str, Any],
    name: str,
    defaults: set[object],
    node_type: str,
    features: set[str],
) -> None:
    if name not in attrs:
        return
    value = attrs[name]
    normalized: object = tuple(value) if isinstance(value, list) else value
    if not any(normalized == default for default in defaults):
        features.add(f"attribute:{node_type}.{name}")


def _flag_extra_attributes(
    owner: str,
    attrs: dict[str, Any],
    allowed: set[str],
    features: set[str],
) -> None:
    for name, value in attrs.items():
        if name not in allowed and _is_meaningful(value):
            features.add(f"attribute:{owner}.{name}")


def _has_meaningful_attributes(raw_attrs: object) -> bool:
    if raw_attrs is None:
        return False
    if not isinstance(raw_attrs, dict):
        return True
    return any(_is_meaningful(value) for value in raw_attrs.values())


def _is_meaningful(value: object) -> bool:
    return value not in (None, False, "", 0, [], {})


def _snapshot_path(page_id: str) -> Path:
    return _SNAPSHOT_DIR / f"{quote(page_id, safe='')}.json"


def _serialize_snapshot(content: object) -> str:
    return json.dumps(content, indent=2, sort_keys=True, ensure_ascii=False)


def _snapshot_hash(serialized: str) -> str:
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
