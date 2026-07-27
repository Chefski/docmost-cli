"""Remote revision checks for safe sync pushes."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from docmost_cli.sync.manifest import SERVER_REVISION_VERSION, build_server_revision

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from docmost_cli.api.client import DocmostClient
    from docmost_cli.sync.diff import PageChange

__all__ = [
    "RemoteConflict",
    "RemotePreflight",
    "fetch_server_attachment",
    "fetch_server_page",
    "format_reconciliation_guidance",
    "server_page_can_use_canonical_revision",
    "server_page_revision_is_verified",
    "verify_remote_revisions",
]

_REVISION_ALLOWED_STATUSES = frozenset({404, 429, 500, 502, 503, 504})
_REVISION_MODE_KEY = "_docmost_cli_revision_mode"
_VERIFIED_REVISION_MODES = frozenset({"atomic", "token", "stable"})


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
    missing_attachment_ids: set[str] = field(default_factory=set)
    reassigned_attachment_ids: set[str] = field(default_factory=set)

    @property
    def conflict_page_ids(self) -> set[str]:
        """Return IDs of pages with stale or unavailable baselines."""
        return {conflict.page_id for conflict in self.conflicts}


def fetch_server_page(
    client: DocmostClient,
    page_id: str,
    *,
    failure_suffix: str = "No changes were pushed.",
    _stability_samples: int = 3,
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
        retry_safe=True,
        allowed_error_statuses=_REVISION_ALLOWED_STATUSES,
    )
    if response.status_code == 404:
        return None
    if not response.is_success:
        print_error(
            f"Could not verify the remote revision for page {page_id} "
            f"(HTTP {response.status_code}). {failure_suffix}"
        )

    try:
        result = response.json()
    except ValueError:
        print_error(
            f"Could not verify the remote revision for page {page_id}: "
            f"the server returned invalid JSON. {failure_suffix}"
        )

    raw_data = result.get("data", result) if isinstance(result, dict) else {}
    if not isinstance(raw_data, dict) or raw_data.get("id") != page_id:
        print_error(
            f"Could not verify the remote revision for page {page_id}: "
            f"the server response did not contain page state. {failure_suffix}"
        )
    data = {**raw_data, _REVISION_MODE_KEY: "atomic"}

    # Older Docmost releases may expose content separately from page metadata.
    # Enrich both pull and preflight through the same path so their fingerprints
    # remain comparable across server versions.
    if "content" not in data:
        content_response = client.post_raw(
            "/pages/content",
            json={"pageId": page_id},
            retry_safe=True,
            allowed_error_statuses=_REVISION_ALLOWED_STATUSES,
        )
        if content_response.status_code == 404:
            print_error(
                f"Could not verify the remote revision for page {page_id}: "
                f"page content is unavailable from this Docmost instance. {failure_suffix}"
            )
        if not content_response.is_success:
            print_error(
                f"Could not verify the remote revision for page {page_id} "
                f"(HTTP {content_response.status_code}). {failure_suffix}"
            )
        try:
            content_result = content_response.json()
        except ValueError:
            print_error(
                f"Could not verify the remote revision for page {page_id}: "
                f"the server returned invalid content JSON. {failure_suffix}"
            )
        content_data = (
            content_result.get("data", content_result) if isinstance(content_result, dict) else {}
        )
        if not isinstance(content_data, dict) or "content" not in content_data:
            print_error(
                f"Could not verify the remote revision for page {page_id}: "
                f"the server response did not contain page content. {failure_suffix}"
            )
        content_page_id = content_data.get("id") or content_data.get("pageId")
        if content_page_id is not None and content_page_id != page_id:
            print_error(
                f"Could not verify the remote revision for page {page_id}: "
                f"the server returned content for a different page. {failure_suffix}"
            )
        metadata_updated_at = data.get("updatedAt")
        content_updated_at = content_data.get("updatedAt")
        revision_token_matches = (
            isinstance(metadata_updated_at, str)
            and isinstance(content_updated_at, str)
            and metadata_updated_at == content_updated_at
        )
        data = {
            **data,
            "content": content_data["content"],
            _REVISION_MODE_KEY: "token" if revision_token_matches else "unverified",
        }

        if not revision_token_matches and _stability_samples > 1:
            previous_fingerprint = build_server_revision(data)["fingerprint"]
            for _ in range(_stability_samples - 1):
                sample = fetch_server_page(
                    client,
                    page_id,
                    failure_suffix=failure_suffix,
                    _stability_samples=1,
                )
                if sample is None:
                    return None
                if server_page_revision_is_verified(sample):
                    return sample
                current_fingerprint = build_server_revision(sample)["fingerprint"]
                if current_fingerprint == previous_fingerprint:
                    sample[_REVISION_MODE_KEY] = "stable"
                    return sample
                previous_fingerprint = current_fingerprint
            print_error(
                f"Could not verify the remote revision for page {page_id}: "
                f"page metadata and content changed repeatedly. {failure_suffix}"
            )
    return data


def server_page_revision_is_verified(page: dict[str, Any]) -> bool:
    """Return whether page metadata and content came from one verified revision."""
    return page.get(_REVISION_MODE_KEY) in _VERIFIED_REVISION_MODES


def server_page_can_use_canonical_revision(page: dict[str, Any]) -> bool:
    """Return whether canonical Markdown can be tied to the accepted raw revision."""
    return page.get(_REVISION_MODE_KEY) in {"atomic", "token"} and isinstance(
        page.get("updatedAt"), str
    )


def fetch_server_attachment(
    client: DocmostClient,
    attachment_id: str,
    *,
    failure_suffix: str = "No changes were pushed.",
) -> dict[str, Any] | None:
    """Fetch attachment metadata while preserving retry, 404, and validation semantics."""
    from docmost_cli.output.formatter import print_error

    response = client.post_raw(
        "/files/info",
        json={"attachmentId": attachment_id},
        retry_safe=True,
        allowed_error_statuses=_REVISION_ALLOWED_STATUSES,
    )
    if response.status_code == 404:
        return None
    if not response.is_success:
        print_error(
            f"Could not verify the remote revision for attachment {attachment_id} "
            f"(HTTP {response.status_code}). {failure_suffix}"
        )
    try:
        result = response.json()
    except ValueError:
        print_error(
            f"Could not verify the remote revision for attachment {attachment_id}: "
            f"the server returned invalid JSON. {failure_suffix}"
        )
    data = result.get("data", result) if isinstance(result, dict) else {}
    if not isinstance(data, dict) or data.get("id") != attachment_id:
        print_error(
            f"Could not verify the remote revision for attachment {attachment_id}: "
            f"the server response did not contain matching attachment state. {failure_suffix}"
        )
    return data


def verify_remote_revisions(
    client: DocmostClient,
    changes: Iterable[PageChange],
    *,
    space_slug: str = "<space>",
    dir_path: Path | None = None,
    manifest: dict[str, Any] | None = None,
    force: bool = False,
) -> RemotePreflight:
    """Compare manifest baselines with current raw ``/pages/info`` state.

    All pages are checked before the caller starts mutating the server. This is
    a best-effort preflight, not atomic compare-and-swap: current Docmost page
    mutations do not accept a conditional revision token. Older manifests
    remain readable, but a page without a revision baseline is treated as a
    conflict rather than silently assuming the current server value was present
    at the old pull.
    """
    from docmost_cli.output.formatter import print_error

    change_list = list(changes)
    result = RemotePreflight()
    seen_ids: set[str] = set()

    for change in change_list:
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

        result.pages[change.page_id] = {
            key: value for key, value in current_page.items() if key != _REVISION_MODE_KEY
        }
        current = build_server_revision(current_page)

        if not server_page_revision_is_verified(current_page):
            result.conflicts.append(
                RemoteConflict(
                    page_id=change.page_id,
                    title=title,
                    filename=filename,
                    reason="server could not provide a revision-consistent page snapshot",
                    expected_updated_at=_updated_at(expected),
                    current_updated_at=_updated_at(current),
                )
            )
        elif not _is_supported_revision(expected):
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

    if manifest is not None and dir_path is not None:
        _verify_changed_attachment_revisions(
            client,
            change_list,
            manifest=manifest,
            dir_path=dir_path,
            result=result,
        )

    if result.conflicts and not force:
        print_error(
            _format_conflicts(
                result.conflicts,
                space_slug=space_slug,
                dir_path=dir_path,
            )
        )

    return result


def _verify_changed_attachment_revisions(
    client: DocmostClient,
    changes: Iterable[PageChange],
    *,
    manifest: dict[str, Any],
    dir_path: Path,
    result: RemotePreflight,
) -> None:
    """Check remote revisions for locally changed in-place attachment replacements."""
    from docmost_cli.api.attachments import download_attachment
    from docmost_cli.sync.assets import (
        compute_bytes_hash,
        compute_file_hash,
        discover_local_assets,
    )
    from docmost_cli.sync.diff import ChangeType

    manifest_assets = manifest.get("assets", {})
    if not isinstance(manifest_assets, dict):
        return

    seen_ids: set[str] = set()
    for change in changes:
        if ChangeType.ATTACHMENT_CHANGED not in change.changes:
            continue
        entry = change.manifest_entry or {}
        referenced_paths = {
            reference.relative_path
            for reference in discover_local_assets(change.local_body or "", dir_path)
        }
        for raw_attachment_id in entry.get("attachment_ids", []):
            attachment_id = str(raw_attachment_id)
            if not attachment_id or attachment_id in seen_ids:
                continue
            asset = manifest_assets.get(attachment_id)
            if not isinstance(asset, dict) or not asset.get("path"):
                continue
            if str(asset["path"]) not in referenced_paths:
                continue
            seen_ids.add(attachment_id)
            root = dir_path.resolve()
            local_path = (root / str(asset["path"])).resolve()
            try:
                local_path.relative_to(root)
            except ValueError:
                continue
            if not local_path.is_file():
                continue
            if compute_file_hash(local_path) == asset.get("content_hash"):
                continue

            current = fetch_server_attachment(client, attachment_id)
            title = str(entry.get("title") or change.page_id)
            file_name = str(asset.get("file_name") or asset["path"])
            if current is None:
                result.missing_attachment_ids.add(attachment_id)
                result.conflicts.append(
                    RemoteConflict(
                        page_id=attachment_id,
                        title=f"{title} attachment {file_name}",
                        filename=str(asset["path"]),
                        reason="attachment no longer exists on the server",
                        expected_updated_at=_asset_updated_at(asset),
                    )
                )
                continue

            expected_updated_at = _asset_updated_at(asset)
            current_updated_at = _asset_updated_at(current)
            expected_page_id = str(asset.get("page_id") or "")
            current_page_id = str(current.get("pageId") or "")
            if current_page_id != expected_page_id:
                result.reassigned_attachment_ids.add(attachment_id)
                result.conflicts.append(
                    RemoteConflict(
                        page_id=attachment_id,
                        title=f"{title} attachment {file_name}",
                        filename=str(asset["path"]),
                        reason=(
                            "attachment ownership changed since the last pull"
                            if expected_page_id
                            else "manifest has no compatible attachment owner"
                        ),
                        expected_updated_at=expected_updated_at,
                        current_updated_at=current_updated_at,
                    )
                )
                continue

            expected_hash = asset.get("content_hash")
            if not isinstance(expected_hash, str) or not expected_hash.startswith("sha256:"):
                result.conflicts.append(
                    RemoteConflict(
                        page_id=attachment_id,
                        title=f"{title} attachment {file_name}",
                        filename=str(asset["path"]),
                        reason="manifest has no compatible attachment fingerprint",
                        current_updated_at=current_updated_at,
                    )
                )
                continue

            _, remote_bytes = download_attachment(client, current)
            current_hash = compute_bytes_hash(remote_bytes)
            if current_hash != expected_hash:
                result.conflicts.append(
                    RemoteConflict(
                        page_id=attachment_id,
                        title=f"{title} attachment {file_name}",
                        filename=str(asset["path"]),
                        reason="attachment bytes changed since the last pull",
                        expected_updated_at=expected_updated_at,
                        current_updated_at=current_updated_at,
                    )
                )


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


def _asset_updated_at(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    updated_at = value.get("server_updated_at") or value.get("updatedAt")
    return str(updated_at) if updated_at else None


def _format_conflicts(
    conflicts: list[RemoteConflict],
    *,
    space_slug: str,
    dir_path: Path | None,
) -> str:
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
    lines.append(
        format_reconciliation_guidance(
            space_slug=space_slug,
            dir_path=dir_path,
            local_files_unchanged=True,
        )
    )
    lines.append("To deliberately apply the local changes anyway, retry with 'sync push --force'.")
    return "\n".join(lines)


def format_reconciliation_guidance(
    *,
    space_slug: str,
    dir_path: Path | None,
    local_files_unchanged: bool,
) -> str:
    """Describe a non-destructive way to reconcile local and remote state."""
    from rich.markup import escape

    lines: list[str] = []
    if local_files_unchanged and dir_path is not None:
        lines.append(f"Local files in '{escape(str(dir_path))}' were not changed.")
    lines.append("Do not force-pull over local edits before committing or backing them up.")
    quoted_space = escape(shlex.quote(space_slug))
    lines.append(
        "Pull the remote space into a separate directory with "
        f"'docmost-cli sync pull {quoted_space} --dir <new-directory>', "
        "then merge your local edits there."
    )
    return "\n".join(lines)
