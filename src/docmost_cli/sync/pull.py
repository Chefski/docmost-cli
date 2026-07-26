"""Pull space pages from Docmost server to local directory."""

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from docmost_cli.api.client import DocmostClient
from docmost_cli.output.formatter import _err_console as _err

__all__ = ["PullResult", "flatten_tree", "pull_space"]


@dataclass
class PullResult:
    """Result of a pull operation."""

    pages_pulled: int
    dir_path: Path
    attachments_pulled: int = 0


def _relative_managed_path(
    value: object,
    *,
    top_level: bool = False,
    assets_only: bool = False,
) -> Path:
    """Validate and normalize a manifest-owned relative path."""
    from docmost_cli.sync.assets import ASSETS_DIRNAME

    if not isinstance(value, str) or not value:
        raise ValueError("manifest contains an empty managed path")

    windows_path = PureWindowsPath(value)
    normalized = PurePosixPath(value.replace("\\", "/"))
    if (
        windows_path.is_absolute()
        or bool(windows_path.drive)
        or normalized.is_absolute()
        or ".." in normalized.parts
    ):
        raise ValueError(f"manifest contains an unsafe managed path: {value!r}")

    parts = tuple(part for part in normalized.parts if part not in {"", "."})
    if not parts:
        raise ValueError("manifest contains an empty managed path")
    if top_level and len(parts) != 1:
        raise ValueError(f"manifest page path must be a filename: {value!r}")
    if assets_only and (len(parts) < 3 or parts[0] != ASSETS_DIRNAME):
        raise ValueError(f"manifest asset path is outside '{ASSETS_DIRNAME}/': {value!r}")
    return Path(*parts)


def _remove_tree(path: Path) -> None:
    """Remove a temporary tree, including read-only files on Windows."""

    def make_writable_and_retry(
        function: Any,
        failed_path: str,
        _error: Any,
    ) -> None:
        current_mode = os.stat(failed_path).st_mode
        os.chmod(failed_path, current_mode | stat.S_IWUSR)
        function(failed_path)

    if path.exists() and not path.is_symlink():
        shutil.rmtree(path, onerror=make_writable_and_retry)
    elif path.is_symlink():
        path.unlink()


def _remove_managed_file(root: Path, relative_path: Path) -> None:
    """Remove one previously managed file without deleting unrelated content."""
    destination = root / relative_path
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    elif destination.exists():
        raise IsADirectoryError(
            f"managed file path is now a directory and cannot be replaced safely: {relative_path}"
        )
    else:
        return

    parent = destination.parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _remove_previous_managed_state(root: Path, manifest: dict[str, Any]) -> None:
    """Remove only files recorded as managed by the previous manifest."""
    pages = manifest.get("pages", {})
    if not isinstance(pages, dict):
        raise ValueError("manifest 'pages' must be an object")
    for entry in pages.values():
        if not isinstance(entry, dict):
            raise ValueError("manifest page entries must be objects")
        _remove_managed_file(
            root,
            _relative_managed_path(entry.get("filename"), top_level=True),
        )

    assets = manifest.get("assets", {})
    if not isinstance(assets, dict):
        raise ValueError("manifest 'assets' must be an object")
    for entry in assets.values():
        if not isinstance(entry, dict):
            raise ValueError("manifest asset entries must be objects")
        _remove_managed_file(
            root,
            _relative_managed_path(entry.get("path"), assets_only=True),
        )


def _prepare_destination(root: Path, relative_path: Path) -> Path:
    """Create safe parent directories and reject unrelated path collisions."""
    parent = root
    for part in relative_path.parts[:-1]:
        parent /= part
        if parent.is_symlink():
            raise FileExistsError(f"refusing to write through symlink: {relative_path}")
        if parent.exists() and not parent.is_dir():
            raise FileExistsError(f"managed path collides with a local file: {relative_path}")
        parent.mkdir(exist_ok=True)

    destination = root / relative_path
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(
            f"managed path collides with an unrelated local file: {relative_path}"
        )
    return destination


def _validate_staged_pull(
    staging_path: Path,
    manifest: dict[str, Any],
    expected_page_ids: set[str],
) -> None:
    """Verify every staged page, asset, and manifest entry before publication."""
    from docmost_cli.sync.assets import compute_file_hash
    from docmost_cli.sync.frontmatter import read_sync_file
    from docmost_cli.sync.manifest import MANIFEST_FILENAME, compute_content_hash, load_manifest

    manifest_path = staging_path / MANIFEST_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("staged pull is missing its manifest")
    if load_manifest(staging_path) != manifest:
        raise RuntimeError("staged pull manifest could not be read back")

    pages = manifest.get("pages", {})
    if not isinstance(pages, dict) or set(pages) != expected_page_ids:
        raise RuntimeError("staged pull does not contain the complete page set")

    seen_paths: set[Path] = set()
    assets = manifest.get("assets", {})
    if not isinstance(assets, dict):
        raise RuntimeError("staged pull assets are invalid")

    for page_id, entry in pages.items():
        if not isinstance(entry, dict):
            raise RuntimeError(f"staged manifest entry for page {page_id} is invalid")
        relative_path = _relative_managed_path(entry.get("filename"), top_level=True)
        if relative_path in seen_paths:
            raise RuntimeError(f"multiple pages map to the same local file: {relative_path}")
        seen_paths.add(relative_path)

        page_path = staging_path / relative_path
        if page_path.is_symlink() or not page_path.is_file():
            raise RuntimeError(f"staged page is missing: {relative_path}")
        metadata, markdown = read_sync_file(page_path)
        if metadata.get("id") != page_id:
            raise RuntimeError(f"staged page ID does not match manifest: {relative_path}")
        if compute_content_hash(markdown) != entry.get("content_hash"):
            raise RuntimeError(f"staged page hash does not match manifest: {relative_path}")
        attachment_ids = entry.get("attachment_ids", [])
        if not isinstance(attachment_ids, list) or any(
            attachment_id not in assets for attachment_id in attachment_ids
        ):
            raise RuntimeError(f"staged page has an untracked attachment: {relative_path}")

    for attachment_id, entry in assets.items():
        if not isinstance(entry, dict):
            raise RuntimeError(f"staged asset entry {attachment_id} is invalid")
        relative_path = _relative_managed_path(entry.get("path"), assets_only=True)
        if relative_path in seen_paths:
            raise RuntimeError(f"multiple managed items map to the same path: {relative_path}")
        seen_paths.add(relative_path)

        asset_path = staging_path / relative_path
        if asset_path.is_symlink() or not asset_path.is_file():
            raise RuntimeError(f"staged attachment is missing: {relative_path}")
        if compute_file_hash(asset_path) != entry.get("content_hash"):
            raise RuntimeError(f"staged attachment hash does not match manifest: {relative_path}")
        if asset_path.stat().st_size != entry.get("size"):
            raise RuntimeError(f"staged attachment size does not match manifest: {relative_path}")


def _temporary_sibling(target: Path, purpose: str) -> Path:
    """Create a unique temporary directory beside the target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    prefix = f".{target.name or 'docmost'}.{purpose}-"
    return Path(tempfile.mkdtemp(prefix=prefix, dir=target.parent))


def _publish_staged_pull(staging_path: Path, target: Path) -> None:
    """Publish a staged directory with rollback if the second rename fails."""
    if not target.exists() and not target.is_symlink():
        os.replace(staging_path, target)
        return

    backup_path = _temporary_sibling(target, "pull-backup")
    backup_path.rmdir()
    os.replace(target, backup_path)
    try:
        os.replace(staging_path, target)
    except BaseException:
        try:
            os.replace(backup_path, target)
        except OSError as rollback_error:
            raise RuntimeError(
                f"pull publication failed and rollback could not restore '{target}'; "
                f"the previous data remains at '{backup_path}'"
            ) from rollback_error
        raise

    try:
        _remove_tree(backup_path)
    except OSError as exc:
        _err.print(f"[yellow]Warning:[/yellow] could not remove backup '{backup_path}': {exc}")


def flatten_tree(
    pages: list[dict[str, Any]],
    parent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Flatten nested page tree into a flat list with parent_id.

    Args:
        pages: Nested page tree from build_page_tree.
        parent_id: Parent page ID for current level.

    Returns:
        Flat list of dicts with: id, title, icon, parent_id
    """
    result: list[dict[str, Any]] = []
    for page in pages:
        result.append(
            {
                "id": page["id"],
                "title": page.get("title", ""),
                "icon": page.get("icon") or "",
                "parent_id": parent_id,
            }
        )
        children = page.get("children", [])
        if children:
            result.extend(flatten_tree(children, parent_id=page["id"]))
    return result


def pull_space(
    client: DocmostClient,
    space_slug: str,
    dir_path: Path,
    *,
    force: bool = False,
) -> PullResult:
    """Pull all pages from a space to a local directory.

    Algorithm:
    1. Resolve space slug to ID
    2. Build full page tree
    3. Flatten tree to list with parent_id
    4. Copy unrelated local files into a sibling staging directory
    5. Remove only files managed by the previous manifest from staging
    6. Fetch every page and attachment into staging and write the manifest
    7. Validate the complete staged snapshot
    8. Replace the target directory, rolling back if publication fails

    Args:
        client: Authenticated Docmost client.
        space_slug: Space slug identifier.
        dir_path: Target directory path.
        force: Overwrite existing files without warning.

    Returns:
        PullResult with count and path.
    """
    from docmost_cli.api.attachments import download_attachment
    from docmost_cli.api.pages import build_page_tree, get_page_content
    from docmost_cli.api.spaces import resolve_space_id
    from docmost_cli.convert.prosemirror_to_md import convert_to_markdown
    from docmost_cli.output.formatter import print_error
    from docmost_cli.sync.assets import (
        asset_markdown_path,
        asset_relative_path,
        build_asset_entry,
        collect_attachment_ids,
    )
    from docmost_cli.sync.frontmatter import write_sync_file
    from docmost_cli.sync.manifest import (
        MANIFEST_FILENAME,
        build_manifest,
        build_page_entry,
        compute_content_hash,
        load_manifest,
        sanitize_filename,
        save_manifest,
    )

    target_path = Path(os.path.abspath(dir_path))
    if target_path == target_path.parent:
        print_error("The sync target must not be a filesystem root.")
    if target_path.is_symlink():
        print_error(f"Target directory '{dir_path}' must not be a symbolic link.")
    if target_path.exists() and not target_path.is_dir():
        print_error(f"Target path '{dir_path}' is not a directory.")

    existing_manifest = load_manifest(target_path)
    if existing_manifest is not None and not force:
        print_error(f"Directory '{dir_path}' already has synced data. Use --force to overwrite.")

    # 1. Resolve space
    space_id = resolve_space_id(client, space_slug)

    # 2. Build page tree
    _err.print(f"Fetching page tree for '{space_slug}'...")
    tree = build_page_tree(client, space_id)

    # 3. Flatten
    flat_pages = flatten_tree(tree)
    total = len(flat_pages)

    # 4. Prepare a same-filesystem staging directory so publication uses renames.
    staging_path = _temporary_sibling(target_path, "pull-staging")
    published = False
    try:
        if target_path.exists():
            shutil.copytree(
                target_path,
                staging_path,
                dirs_exist_ok=True,
                symlinks=True,
            )
        if existing_manifest is not None:
            _remove_previous_managed_state(staging_path, existing_manifest)

        manifest_path = staging_path / MANIFEST_FILENAME
        if manifest_path.is_symlink() or manifest_path.is_file():
            manifest_path.unlink()
        elif manifest_path.exists():
            raise IsADirectoryError(f"manifest path is a directory: {manifest_path}")

        # 5. Fetch content and write files into staging.
        page_entries: list[dict[str, Any]] = []
        assets: dict[str, dict[str, Any]] = {}
        for i, page_info in enumerate(flat_pages, 1):
            page_id = page_info["id"]
            title = page_info["title"]
            _err.print(f"Pulling {i}/{total}: {title}")

            content_data = get_page_content(client, page_id)
            pm_content = content_data.get("content")

            attachment_ids = collect_attachment_ids(pm_content)
            attachment_paths: dict[str, str] = {}
            for attachment_id in attachment_ids:
                if attachment_id not in assets:
                    attachment_info, attachment_bytes = download_attachment(client, attachment_id)
                    relative_path = asset_relative_path(
                        attachment_id,
                        str(attachment_info["fileName"]),
                    )
                    destination = _prepare_destination(
                        staging_path,
                        _relative_managed_path(relative_path, assets_only=True),
                    )
                    destination.write_bytes(attachment_bytes)
                    assets[attachment_id] = build_asset_entry(
                        attachment_info,
                        relative_path,
                        destination,
                    )
                attachment_paths[attachment_id] = asset_markdown_path(
                    str(assets[attachment_id]["path"])
                )

            markdown = (
                convert_to_markdown(pm_content, attachment_paths=attachment_paths)
                if pm_content
                else ""
            )

            filename = sanitize_filename(title, page_id)
            destination = _prepare_destination(
                staging_path,
                _relative_managed_path(filename, top_level=True),
            )
            metadata = {
                "id": page_id,
                "title": title,
                "parent_id": page_info["parent_id"] or "",
                "icon": page_info["icon"],
            }
            write_sync_file(destination, metadata, markdown)

            content_hash = compute_content_hash(markdown)
            entry = build_page_entry(
                title=title,
                filename=filename,
                parent_id=page_info["parent_id"],
                icon=page_info["icon"],
                content_hash=content_hash,
                attachment_ids=attachment_ids,
            )
            page_entries.append({"id": page_id, **entry})

        # 6. Commit the staged metadata only after all downloads have completed.
        manifest = build_manifest(space_slug, space_id, page_entries, assets)
        save_manifest(staging_path, manifest)

        # 7. Read every managed file back before replacing the live directory.
        _validate_staged_pull(
            staging_path,
            manifest,
            {str(page["id"]) for page in flat_pages},
        )

        # 8. Publish as one directory snapshot, with rollback on rename failure.
        _publish_staged_pull(staging_path, target_path)
        published = True
    finally:
        if not published and (staging_path.exists() or staging_path.is_symlink()):
            try:
                _remove_tree(staging_path)
            except OSError as exc:
                _err.print(
                    f"[yellow]Warning:[/yellow] could not remove staging directory "
                    f"'{staging_path}': {exc}"
                )

    _err.print(
        f"Pulled {total} pages and {len(assets)} attachments from '{space_slug}' -> {dir_path}"
    )
    return PullResult(
        pages_pulled=total,
        dir_path=dir_path,
        attachments_pulled=len(assets),
    )
