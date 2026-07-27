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
from urllib.parse import quote, unquote, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from docmost_cli.api.client import DocmostClient
    from docmost_cli.sync.diff import PageChange, SyncDiff

__all__ = [
    "PageRevisionChangedError",
    "RichContentConflict",
    "analyze_prosemirror",
    "build_pulled_rich_content_state",
    "fetch_canonical_markdown",
    "find_current_rich_content_conflict",
    "find_rich_content_conflicts",
    "markdown_rich_content_state",
    "rewrite_attachment_urls",
    "sub_markdown_outside_code",
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
    "attachment",
}
_MARKDOWN_MARKS = {"bold", "italic", "strike", "code", "link"}
_MARKDOWN_LINK_RE = re.compile(
    r"(?P<prefix>!?\[(?:\\.|[^\[\]\\]|\[(?:\\.|[^\[\]\\])*\])*\]\()"
    r"(?P<destination><(?:\\.|[^>])+>|(?:\\.|[^\s()\\]|\((?:\\.|[^()\\])*\))+)"
    r"(?P<suffix>(?:\s+(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'))?\))"
)
_SERVER_ATTACHMENT_URL_RE = re.compile(
    r"^(?:https?://[^/\s>]+)?/(?:api/)?files/(?P<attachment_id>[^/]+)/[^\s>\"']+$"
)
_CANONICAL_MARKDOWN_UNAVAILABLE = frozenset({400, 404})
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)$")


@dataclass(frozen=True)
class RichContentConflict:
    """A local replacement that would discard source ProseMirror features."""

    page_id: str
    filename: str
    title: str
    features: tuple[str, ...]
    snapshot_path: str | None


class PageRevisionChangedError(RuntimeError):
    """The raw and canonical page responses describe different revisions."""


def analyze_prosemirror(content: object) -> tuple[str, ...]:
    """Return author-visible features that cannot round-trip through Markdown.

    Generated paragraph/heading IDs are deliberately ignored: Docmost
    regenerates those IDs when it imports Markdown. The guard focuses on
    author-visible structure, formatting, embedded content, and references.
    """
    if content is None:
        return ()
    if not isinstance(content, dict):
        return ("content:invalid-prosemirror",)

    features: set[str] = set()
    generated_node_ids: set[str] = set()
    fragment_targets: set[str] = set()

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
            if node_type in {"paragraph", "heading"}:
                attrs = node.get("attrs")
                node_id = attrs.get("id") if isinstance(attrs, dict) else None
                if isinstance(node_id, str) and node_id:
                    generated_node_ids.add(node_id)
            _check_node_attributes(node_type, node.get("attrs"), features)
            _check_node_structure(node_type, node.get("content"), features)

        marks = node.get("marks", [])
        if marks is not None:
            if not isinstance(marks, list):
                features.add("content:invalid-marks")
            else:
                for mark in marks:
                    _check_mark(mark, features, fragment_targets)

        children = node.get("content", [])
        if children is not None:
            if not isinstance(children, list):
                features.add("content:invalid-children")
            else:
                for child in children:
                    walk(child)

    walk(content)
    if generated_node_ids & fragment_targets:
        features.add("reference:generated-node-id")
    return tuple(sorted(features))


def fetch_canonical_markdown(
    client: DocmostClient,
    page_id: str,
    *,
    expected_updated_at: str | None = None,
) -> str | None:
    """Ask Docmost to serialize a page with its canonical Markdown converter.

    This read-only POST uses the client's session-refresh and replay-safe retry
    path. When ``expected_updated_at`` is supplied, the response must describe
    that same page revision. ``None`` indicates a successful response without
    Markdown content.

    Raises:
        PageRevisionChangedError: If the response revision is missing or changed.
    """
    response = client.post_raw(
        "/pages/info",
        json={"pageId": page_id, "format": "markdown"},
        retry_safe=True,
        allowed_error_statuses=_CANONICAL_MARKDOWN_UNAVAILABLE,
    )
    if response.status_code in _CANONICAL_MARKDOWN_UNAVAILABLE:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    if expected_updated_at is not None and data.get("updatedAt") != expected_updated_at:
        raise PageRevisionChangedError(page_id)
    markdown = data.get("content")
    return markdown if isinstance(markdown, str) else None


def rewrite_attachment_urls(
    markdown: str,
    attachment_paths: Mapping[str, str],
    *,
    docmost_origin: str | None = None,
) -> str:
    """Rewrite Markdown attachment destinations outside literal code contexts."""

    def replace_destination(match: re.Match[str]) -> str:
        raw_destination = match.group("destination")
        destination = (
            raw_destination[1:-1]
            if raw_destination.startswith("<") and raw_destination.endswith(">")
            else raw_destination
        )
        normalized_destination = re.sub(
            r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])",
            r"\1",
            destination,
        )
        server_match = _SERVER_ATTACHMENT_URL_RE.fullmatch(normalized_destination)
        if server_match is None:
            return match.group(0)
        parsed_destination = urlsplit(normalized_destination)
        if parsed_destination.netloc:
            if docmost_origin is None:
                return match.group(0)
            parsed_origin = urlsplit(docmost_origin)
            if (
                parsed_destination.scheme.lower(),
                parsed_destination.netloc.lower(),
            ) != (parsed_origin.scheme.lower(), parsed_origin.netloc.lower()):
                return match.group(0)
        decoded_id = unquote(server_match.group("attachment_id"))
        local_path = attachment_paths.get(decoded_id)
        if local_path is None:
            return match.group(0)
        return f"{match.group('prefix')}{local_path}{match.group('suffix')}"

    return sub_markdown_outside_code(markdown, _MARKDOWN_LINK_RE, replace_destination)


def sub_markdown_outside_code(
    markdown: str,
    pattern: re.Pattern[str],
    replacement: Callable[[re.Match[str]], str],
) -> str:
    """Apply a regex substitution only outside Markdown code contexts."""
    protected_ranges = _markdown_code_ranges(markdown)
    if not protected_ranges:
        return pattern.sub(replacement, markdown)

    rewritten: list[str] = []
    cursor = 0
    for start, end in protected_ranges:
        rewritten.append(pattern.sub(replacement, markdown[cursor:start]))
        rewritten.append(markdown[start:end])
        cursor = end
    rewritten.append(pattern.sub(replacement, markdown[cursor:]))
    return "".join(rewritten)


def find_current_rich_content_conflict(
    client: DocmostClient,
    change: PageChange,
) -> RichContentConflict | None:
    """Re-check the current server document immediately before replacement.

    Legacy manifest entries without rich-content guard metadata remain
    compatible. Guarded entries are re-fetched so rich structures added by a
    concurrent Docmost editor cannot be overwritten from a stale safe snapshot.
    """
    state = (change.manifest_entry or {}).get("rich_content")
    if state is None:
        return None

    from docmost_cli.api.pages import get_page_content

    current = get_page_content(client, change.page_id)
    features = analyze_prosemirror(current.get("content"))
    if not features:
        return None

    entry = change.manifest_entry or {}
    meta = change.local_meta or {}
    raw_snapshot_path = state.get("snapshot_path") if isinstance(state, dict) else None
    snapshot_path = raw_snapshot_path if isinstance(raw_snapshot_path, str) else None
    return RichContentConflict(
        page_id=change.page_id,
        filename=change.filename,
        title=meta.get("title") or str(entry.get("title") or change.page_id),
        features=features,
        snapshot_path=snapshot_path,
    )


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
            raw_features = state.get("unsafe_features")
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


def _check_mark(
    mark: object,
    features: set[str],
    fragment_targets: set[str],
) -> None:
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
            href = attrs.get("href")
            if isinstance(href, str) and href.startswith("#") and len(href) > 1:
                fragment_targets.add(unquote(href[1:]))
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


def _markdown_code_ranges(markdown: str) -> list[tuple[int, int]]:
    """Return fenced-block and inline-code ranges in source order."""
    fenced: list[tuple[int, int]] = []
    lines = markdown.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)

    line_index = 0
    while line_index < len(lines):
        line_text = lines[line_index].rstrip("\r\n")
        opening = _FENCE_OPEN_RE.match(line_text)
        if opening is None:
            line_index += 1
            continue

        fence = opening.group("fence")
        if fence.startswith("`") and "`" in opening.group("info"):
            line_index += 1
            continue

        marker = fence[0]
        minimum_length = len(fence)
        end_index = len(lines)
        for candidate_index in range(line_index + 1, len(lines)):
            candidate = lines[candidate_index].rstrip("\r\n")
            stripped = candidate.lstrip(" ")
            indent = len(candidate) - len(stripped)
            marker_length = len(stripped) - len(stripped.lstrip(marker))
            trailing = stripped[marker_length:]
            if indent <= 3 and marker_length >= minimum_length and trailing.strip(" \t") == "":
                end_index = candidate_index + 1
                break

        start = offsets[line_index]
        end = offsets[end_index] if end_index < len(offsets) else len(markdown)
        fenced.append((start, end))
        line_index = end_index

    indented: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        start = offsets[index]
        if any(fence_start <= start < fence_end for fence_start, fence_end in fenced):
            continue
        if line.startswith("    ") or line.startswith("\t"):
            indented.append((start, start + len(line)))

    blocks = sorted([*fenced, *indented])
    protected = list(blocks)
    gap_start = 0
    for block_start, block_end in [*blocks, (len(markdown), len(markdown))]:
        protected.extend(_inline_code_ranges(markdown, gap_start, block_start))
        gap_start = max(gap_start, block_end)
    return sorted(protected)


def _inline_code_ranges(markdown: str, start: int, end: int) -> list[tuple[int, int]]:
    """Return closed CommonMark backtick-code spans inside one non-fenced range."""
    ranges: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        opening = markdown.find("`", cursor, end)
        if opening < 0:
            break
        if _is_escaped(markdown, opening):
            cursor = opening + 1
            continue

        opening_end = opening
        while opening_end < end and markdown[opening_end] == "`":
            opening_end += 1
        run_length = opening_end - opening

        candidate = opening_end
        closing_end: int | None = None
        while candidate < end:
            candidate = markdown.find("`", candidate, end)
            if candidate < 0:
                break
            candidate_end = candidate
            while candidate_end < end and markdown[candidate_end] == "`":
                candidate_end += 1
            if candidate_end - candidate == run_length and not _is_escaped(markdown, candidate):
                closing_end = candidate_end
                break
            candidate = candidate_end

        if closing_end is None:
            cursor = opening_end
            continue
        ranges.append((opening, closing_end))
        cursor = closing_end
    return ranges


def _is_escaped(value: str, index: int) -> bool:
    """Return whether the character at ``index`` has an odd backslash prefix."""
    backslashes = 0
    index -= 1
    while index >= 0 and value[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


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
        attachment_id = raw_attrs.get("attachmentId")
        source = raw_attrs.get("src")
        is_attachment = (
            isinstance(attachment_id, str)
            and bool(attachment_id)
            or isinstance(source, str)
            and _SERVER_ATTACHMENT_URL_RE.fullmatch(source) is not None
        )
        if is_attachment:
            _flag_nondefault(raw_attrs, "title", {None, ""}, node_type, features)
    elif node_type == "attachment":
        allowed = {"url", "name", "mime", "size", "attachmentId", "placeholder"}
        _flag_nondefault(raw_attrs, "placeholder", {None, ""}, node_type, features)
        attachment_id = raw_attrs.get("attachmentId")
        attachment_url = raw_attrs.get("url")
        server_match = (
            _SERVER_ATTACHMENT_URL_RE.fullmatch(attachment_url)
            if isinstance(attachment_url, str)
            else None
        )
        if server_match is None or (
            isinstance(attachment_id, str)
            and attachment_id
            and unquote(server_match.group("attachment_id")) != attachment_id
        ):
            features.add("attribute:attachment.reference")
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
    if node_type == "table":
        _check_table_headers(raw_content, features)
        return
    if node_type not in {"listItem", "taskItem", "tableCell", "tableHeader"}:
        return
    if not isinstance(raw_content, list):
        return
    if len(raw_content) != 1:
        features.add(f"structure:{node_type}.content")
        return
    child = raw_content[0]
    if not isinstance(child, dict) or child.get("type") != "paragraph":
        features.add(f"structure:{node_type}.content")


def _check_table_headers(raw_content: object, features: set[str]) -> None:
    """Require the single header-row shape representable by GFM tables."""
    if not isinstance(raw_content, list) or not raw_content:
        return
    for row_index, row in enumerate(raw_content):
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            features.add("structure:table.headers")
            return
        cells = row.get("content")
        if not isinstance(cells, list):
            features.add("structure:table.headers")
            return
        expected_type = "tableHeader" if row_index == 0 else "tableCell"
        if any(not isinstance(cell, dict) or cell.get("type") != expected_type for cell in cells):
            features.add("structure:table.headers")
            return


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
