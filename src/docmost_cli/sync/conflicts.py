"""Remote revision checks for safe sync pushes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from docmost_cli.sync.manifest import SERVER_REVISION_VERSION, build_server_revision

if TYPE_CHECKING:
    from collections.abc import Iterable

    from docmost_cli.api.client import DocmostClient
    from docmost_cli.sync.diff import PageChange

__all__ = [
    "RemoteConflict",
    "RemotePreflight",
    "fetch_server_page",
    "verify_remote_revisions",
]


@dataclass(frozen=True)
class RemoteConflict:
    """A page whose current server state no longer matches the manifest."""

    page_id: str
    title: str
    filename: str
    reason: str
    expected_updated_at: str | None = None
    current_updated_at: str | None = None


@dataclass
class RemotePreflight:
    """Result of checking every existing page before a push."""

    pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    conflicts: list[RemoteConflict] = field(default_factory=list)
    missing_page_ids: set[str] = field(default_factory=set)

    @property
    def conflict_page_ids(self) -> set[str]:
        """Return IDs of pages with stale or unavailable baselines."""
        return {conflict.page_id for conflict in self.conflicts}


def fetch_server_page(
    client: DocmostClient,
    page_id: str,
    *,
    failure_suffix: str = "No changes were pushed.",
) -> dict[str, Any] | None:
    """Fetch raw page state without turning a missing page into an early CLI exit.

    Returns ``None`` for a server-side 404. Other verification failures abort
    with ``failure_suffix`` appended so callers can accurately describe whether
    the fetch happened before or after a mutation.
    """
    from docmost_cli.output.formatter import print_error

    response = client.post_raw(
        "/pages/info",
        json={"pageId": page_id},
        raise_on_error=False,
    )
    if response.status_code == 401:
        # Route session-auth expiry through the client's normal refresh path.
        from docmost_cli.api.pages import get_page_info

        return get_page_info(client, page_id)
    if response.status_code == 404:
        return None
    if not response.is_success:
        status = f"HTTP {response.status_code}" if response.status_code else "network error"
        print_error(
            f"Could not verify the remote revision for page {page_id} ({status}). {failure_suffix}"
        )

    try:
        result = response.json()
    except ValueError:
        print_error(
            f"Could not verify the remote revision for page {page_id}: "
            f"the server returned invalid JSON. {failure_suffix}"
        )

    data = result.get("data", result) if isinstance(result, dict) else {}
    if not isinstance(data, dict) or not data.get("id"):
        print_error(
            f"Could not verify the remote revision for page {page_id}: "
            f"the server response did not contain page state. {failure_suffix}"
        )
    return data


def verify_remote_revisions(
    client: DocmostClient,
    changes: Iterable[PageChange],
    *,
    force: bool = False,
) -> RemotePreflight:
    """Compare manifest baselines with current raw ``/pages/info`` state.

    All pages are checked before the caller starts mutating the server. Older
    manifests remain readable, but a page without a revision baseline is
    treated as a conflict rather than silently assuming the current server
    value was present at the old pull.
    """
    from docmost_cli.output.formatter import print_error

    result = RemotePreflight()
    seen_ids: set[str] = set()

    for change in changes:
        if not change.page_id or change.page_id in seen_ids:
            continue
        seen_ids.add(change.page_id)

        entry = change.manifest_entry or {}
        title = str(entry.get("title") or change.page_id)
        filename = str(entry.get("filename") or change.filename)
        expected = entry.get("server_revision")
        current_page = fetch_server_page(client, change.page_id)

        if current_page is None:
            result.missing_page_ids.add(change.page_id)
            result.conflicts.append(
                RemoteConflict(
                    page_id=change.page_id,
                    title=title,
                    filename=filename,
                    reason="page no longer exists on the server",
                    expected_updated_at=_updated_at(expected),
                )
            )
            continue

        result.pages[change.page_id] = current_page
        current = build_server_revision(current_page)

        if not _is_supported_revision(expected):
            result.conflicts.append(
                RemoteConflict(
                    page_id=change.page_id,
                    title=title,
                    filename=filename,
                    reason="manifest has no compatible server revision",
                    current_updated_at=_updated_at(current),
                )
            )
        elif isinstance(expected, dict) and expected.get("fingerprint") != current["fingerprint"]:
            result.conflicts.append(
                RemoteConflict(
                    page_id=change.page_id,
                    title=title,
                    filename=filename,
                    reason="server content or metadata changed since the last pull",
                    expected_updated_at=_updated_at(expected),
                    current_updated_at=_updated_at(current),
                )
            )

    if result.conflicts and not force:
        print_error(_format_conflicts(result.conflicts))

    return result


def _is_supported_revision(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("version") == SERVER_REVISION_VERSION
        and isinstance(value.get("fingerprint"), str)
    )


def _updated_at(revision: Any) -> str | None:
    if not isinstance(revision, dict):
        return None
    updated_at = revision.get("updated_at")
    return str(updated_at) if updated_at else None


def _format_conflicts(conflicts: list[RemoteConflict]) -> str:
    from rich.markup import escape

    lines = ["Remote changes detected; no changes were pushed:"]
    for conflict in conflicts:
        detail = conflict.reason
        if conflict.expected_updated_at and conflict.current_updated_at:
            detail += (
                f" (pulled {conflict.expected_updated_at}, current {conflict.current_updated_at})"
            )
        lines.append(
            f"  - {escape(conflict.title)} ({conflict.page_id}; "
            f"{escape(conflict.filename)}): {detail}"
        )
    lines.extend(
        [
            "Run 'docmost-cli sync pull <space> --force' to reconcile the remote changes.",
            "To deliberately apply the local changes anyway, retry with 'sync push --force'.",
        ]
    )
    return "\n".join(lines)
