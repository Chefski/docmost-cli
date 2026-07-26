"""Push local changes to Docmost server."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from docmost_cli.output.formatter import _err_console as _err
from docmost_cli.sync.diff import ChangeType, PageChange, SyncDiff

if TYPE_CHECKING:
    from pathlib import Path

    from docmost_cli.api.client import DocmostClient

__all__ = ["PushResult", "push_space"]


@dataclass
class PushResult:
    """Result of a push operation."""

    created: int = 0
    updated: int = 0
    moved: int = 0
    deleted: int = 0
    unchanged: int = 0
    id_remaps: dict[str, str] = field(default_factory=dict)  # old_id -> new_id


def push_space(
    client: DocmostClient,
    space_slug: str,
    dir_path: Path,
    *,
    dry_run: bool = False,
    delete: bool = False,
    force: bool = False,
    diff: SyncDiff | None = None,
) -> PushResult:
    """Push local changes to Docmost server.

    Args:
        client: Authenticated Docmost client.
        space_slug: Space slug identifier.
        dir_path: Directory containing synced files.
        dry_run: If True, show plan without executing changes.
        delete: If True, delete server pages not found locally.
        force: Apply local changes despite stale remote revision baselines.
        diff: Pre-computed diff (avoids recomputing if caller already has it).

    Returns:
        PushResult with counts and any ID remaps.
    """
    from docmost_cli.api.pages import (
        POSITION_FIRST,
        create_and_place_page,
        delete_page,
        move_page,
        update_page_content,
        update_page_meta,
    )
    from docmost_cli.api.spaces import resolve_space_id
    from docmost_cli.output.formatter import print_error
    from docmost_cli.sync.assets import prepare_markdown_assets
    from docmost_cli.sync.conflicts import (
        fetch_server_page,
        format_reconciliation_guidance,
        verify_remote_revisions,
    )
    from docmost_cli.sync.diff import compute_diff
    from docmost_cli.sync.frontmatter import write_sync_file
    from docmost_cli.sync.manifest import (
        build_page_entry,
        build_server_revision,
        compute_content_hash,
        load_manifest,
        save_manifest,
    )

    space_id = resolve_space_id(client, space_slug)

    manifest = load_manifest(dir_path)
    if manifest is None:
        print_error(f"No manifest found in '{dir_path}'. Run 'sync pull' first.")

    if diff is None:
        diff = compute_diff(manifest, dir_path)
    result = PushResult(unchanged=diff.unchanged)

    if not diff.has_changes:
        _err.print("No changes to push.")
        return result

    # Display summary
    _print_summary(diff)

    if dry_run:
        _print_dry_run(diff)
        return result

    # Check every existing page before making any mutation. This keeps a
    # conflict from being discovered after an unrelated new page was created.
    existing_changes = [*diff.modified, *diff.moved]
    if delete:
        existing_changes.extend(diff.deleted)
    preflight = verify_remote_revisions(
        client,
        existing_changes,
        space_slug=space_slug,
        dir_path=dir_path,
        manifest=manifest,
        force=force,
    )

    # A missing page cannot be overwritten. A missing page that was already
    # deleted locally is treated as an idempotent deletion when --force is set.
    deleted_ids = {change.page_id for change in diff.deleted} if delete else set()
    unforceable_missing = preflight.missing_page_ids - deleted_ids
    if unforceable_missing:
        missing = ", ".join(sorted(unforceable_missing))
        print_error(
            "Cannot force updates for pages that no longer exist on the server "
            f"({missing}). No changes were pushed.\n"
            + format_reconciliation_guidance(
                space_slug=space_slug,
                dir_path=dir_path,
                local_files_unchanged=True,
            )
        )

    if preflight.missing_attachment_ids:
        missing = ", ".join(sorted(preflight.missing_attachment_ids))
        print_error(
            "Cannot replace attachments that no longer exist on the server "
            f"({missing}). No changes were pushed.\n"
            + format_reconciliation_guidance(
                space_slug=space_slug,
                dir_path=dir_path,
                local_files_unchanged=True,
            )
        )

    # --- Execute changes ---

    id_remap: dict[str, str] = {}  # old_id -> new_id
    manifest.setdefault("assets", {})
    revision_refresh_ids: set[str] = set()
    forced_conflict_ids = preflight.conflict_page_ids if force else set()

    # Phase A: Create new pages (topological order)
    existing_ids = set(manifest.get("pages", {}).keys())
    sorted_new = _topological_sort(diff.new, existing_ids)

    for change in sorted_new:
        meta = change.local_meta or {}
        body = change.local_body or ""
        title = meta.get("title", "Untitled")
        parent_id = meta.get("parent_id", "").strip() or None
        icon = meta.get("icon", "").strip()

        # Resolve parent_id through remap table
        if parent_id and parent_id in id_remap:
            parent_id = id_remap[parent_id]

        _err.print(f"  Creating: {title}")
        new_id = create_and_place_page(
            client,
            space_id=space_id,
            title=title,
            content=body,
            parent_page_id=parent_id,
            icon=icon,
        )

        try:
            server_body, asset_entries, attachment_ids = prepare_markdown_assets(
                client,
                page_id=new_id,
                markdown=body,
                dir_path=dir_path,
                manifest=manifest,
            )
        except FileNotFoundError as exc:
            print_error(f"Attachment file not found: {exc.filename or exc}")

        if attachment_ids:
            update_page_content(client, page_id=new_id, content=server_body)
            manifest["assets"].update(asset_entries)

        # Write ID back to frontmatter
        meta["id"] = new_id
        write_sync_file(dir_path / change.filename, meta, body)

        # Update manifest
        content_hash = compute_content_hash(body)
        manifest["pages"][new_id] = build_page_entry(
            title=title,
            filename=change.filename,
            parent_id=parent_id,
            icon=icon,
            content_hash=content_hash,
            attachment_ids=attachment_ids,
        )
        revision_refresh_ids.add(new_id)
        existing_ids.add(new_id)
        result.created += 1

    # Phase B: Update modified pages
    for change in diff.modified:
        meta = change.local_meta or {}
        body = change.local_body or ""
        page_id = change.page_id
        title = meta.get("title", "")
        parent_id = meta.get("parent_id", "").strip() or None
        icon = meta.get("icon", "").strip()

        has_content_change = bool(
            change.changes & {ChangeType.CONTENT_CHANGED, ChangeType.ATTACHMENT_CHANGED}
        )
        has_meta_change = bool(change.changes & {ChangeType.TITLE_CHANGED, ChangeType.ICON_CHANGED})

        # Content update
        if has_content_change:
            try:
                server_body, asset_entries, attachment_ids = prepare_markdown_assets(
                    client,
                    page_id=page_id,
                    markdown=body,
                    dir_path=dir_path,
                    manifest=manifest,
                )
            except FileNotFoundError as exc:
                print_error(f"Attachment file not found: {exc.filename or exc}")
            update_page_content(client, page_id=page_id, content=server_body)
            manifest["assets"].update(asset_entries)
            _err.print(f"  Updated: {title}")
        else:
            attachment_ids = list((change.manifest_entry or {}).get("attachment_ids", []))

        # Metadata changes use the same core page update endpoint.
        if has_meta_change:
            _err.print(f"  Metadata: {title}")
            update_page_meta(
                client,
                page_id=page_id,
                title=title if ChangeType.TITLE_CHANGED in change.changes else None,
                icon=icon if ChangeType.ICON_CHANGED in change.changes else None,
            )

        # Record successful content/metadata updates, but preserve the previous
        # parent until the separate move request succeeds.
        manifest_parent_id = parent_id
        if ChangeType.MOVED in change.changes:
            manifest_parent_id = (change.manifest_entry or {}).get("parent_id") or None

        content_hash = compute_content_hash(body)
        manifest["pages"][page_id] = build_page_entry(
            title=title,
            filename=change.filename,
            parent_id=manifest_parent_id,
            icon=icon,
            content_hash=content_hash,
            attachment_ids=attachment_ids,
            server_revision=(change.manifest_entry or {}).get("server_revision"),
        )
        if page_id not in forced_conflict_ids:
            revision_refresh_ids.add(page_id)
        result.updated += 1

    # Phase B2: Move pages after any content/metadata updates have succeeded.
    for change in diff.moved:
        meta = change.local_meta or {}
        page_id = change.page_id
        parent_id = meta.get("parent_id", "").strip() or None
        title = meta.get("title", page_id)

        # Check remap
        if page_id in id_remap:
            page_id = id_remap[page_id]
        if parent_id and parent_id in id_remap:
            parent_id = id_remap[parent_id]

        _err.print(f"  Moving: {title}")
        move_page(
            client,
            page_id=page_id,
            parent_page_id=parent_id,
            position=POSITION_FIRST,
        )

        # Update manifest
        if page_id in manifest["pages"]:
            manifest["pages"][page_id]["parent_id"] = parent_id
        if page_id not in forced_conflict_ids:
            revision_refresh_ids.add(page_id)
        result.moved += 1

    # Phase C: Deletions
    if diff.deleted:
        if delete:
            for change in diff.deleted:
                entry = change.manifest_entry or {}
                _err.print(f"  Deleting: {entry.get('title', change.page_id)}")
                if change.page_id not in preflight.missing_page_ids:
                    delete_page(client, change.page_id)
                manifest["pages"].pop(change.page_id, None)
                result.deleted += 1
        else:
            _err.print(
                f"  [yellow]{len(diff.deleted)} page(s) on server not found locally. "
                "Use --delete to remove.[/yellow]"
            )

    # Legacy field retained in the result contract; core page updates preserve IDs.
    result.id_remaps = id_remap

    # Persist the canonical post-write state. Pages deliberately forced through
    # a conflict keep their old baseline so a later non-forced push still
    # requires a pull rather than silently treating a mixed local/remote state
    # as fully reconciled.
    for page_id in sorted(revision_refresh_ids):
        page = fetch_server_page(
            client,
            page_id,
            failure_suffix=(
                "The page may have been updated, but the manifest was not saved.\n"
                + format_reconciliation_guidance(
                    space_slug=space_slug,
                    dir_path=dir_path,
                    local_files_unchanged=False,
                )
            ),
        )
        if page is None:
            print_error(
                f"Page {page_id} disappeared after it was updated. "
                "The manifest was not saved.\n"
                + format_reconciliation_guidance(
                    space_slug=space_slug,
                    dir_path=dir_path,
                    local_files_unchanged=False,
                )
            )
        entry = manifest["pages"].get(page_id)
        if entry is not None:
            entry["server_revision"] = build_server_revision(page)

    # Save manifest
    save_manifest(dir_path, manifest)

    _err.print(
        f"Pushed to '{space_slug}': "
        f"{result.created} created, {result.updated} updated, "
        f"{result.moved} moved, {result.deleted} deleted"
    )
    return result


def _community_replace(
    client: DocmostClient,
    *,
    space_id: str,
    old_page_id: str,
    title: str,
    content: str,
    parent_id: str | None,
    icon: str,
) -> str:
    """Safe content update for Community edition: create new, then delete old.

    The old page is only deleted after the new one is confirmed created.

    Returns:
        New page ID.
    """
    from docmost_cli.api.pages import create_and_place_page, delete_page

    new_id = create_and_place_page(
        client,
        space_id=space_id,
        title=title,
        content=content,
        parent_page_id=parent_id,
        icon=icon,
    )
    delete_page(client, old_page_id)
    return new_id


def _topological_sort(
    new_changes: list[PageChange],
    existing_ids: set[str],
) -> list[PageChange]:
    """Sort new pages so parents are created before children.

    Pages with no parent or whose parent already exists on the server
    are placed first. Pages whose parent_id references a server ID not
    yet in the resolved set are deferred. Note: new pages have empty
    page_id, so cross-references between new pages are not supported —
    only references to existing server IDs are resolved.

    Args:
        new_changes: List of PageChange with NEW type.
        existing_ids: Set of page IDs already on the server.

    Returns:
        Sorted list of PageChange.
    """
    result = []
    remaining = list(new_changes)
    resolved = set(existing_ids)

    max_iterations = len(remaining) + 1
    for _ in range(max_iterations):
        if not remaining:
            break
        next_remaining = []
        for change in remaining:
            meta = change.local_meta or {}
            parent_id = meta.get("parent_id", "").strip() or None
            if parent_id is None or parent_id in resolved:
                result.append(change)
            else:
                next_remaining.append(change)
        if len(next_remaining) == len(remaining):
            # No progress — circular or broken parent reference — add remaining
            result.extend(next_remaining)
            break
        remaining = next_remaining

    return result


def _print_summary(diff: SyncDiff) -> None:
    """Print change summary to stderr."""
    lines: list[str] = []
    if diff.new:
        lines.append(f"  Create:    {len(diff.new)} page(s)")
    if diff.modified:
        lines.append(f"  Update:    {len(diff.modified)} page(s)")
    if diff.moved:
        lines.append(f"  Move:      {len(diff.moved)} page(s)")
    if diff.deleted:
        lines.append(f"  Delete:    {len(diff.deleted)} page(s)")
    lines.append(f"  Unchanged: {diff.unchanged} page(s)")
    _err.print("Push plan:")
    for line in lines:
        _err.print(line)


def _print_dry_run(diff: SyncDiff) -> None:
    """Print detailed plan to stdout for scripting."""
    import sys

    for change in diff.new:
        meta = change.local_meta or {}
        sys.stdout.write(f"CREATE {change.filename} ({meta.get('title', '?')})\n")
    for change in diff.modified:
        types = ", ".join(c.value for c in change.changes if c != ChangeType.MOVED)
        sys.stdout.write(f"UPDATE {change.filename} ({types})\n")
    for change in diff.moved:
        meta = change.local_meta or {}
        sys.stdout.write(f"MOVE   {change.filename} -> parent:{meta.get('parent_id', 'root')}\n")
    for change in diff.deleted:
        entry = change.manifest_entry or {}
        sys.stdout.write(f"DELETE {entry.get('filename', '?')} ({entry.get('title', '?')})\n")
