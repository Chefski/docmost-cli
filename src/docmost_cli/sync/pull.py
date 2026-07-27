"""Pull space pages from Docmost server to local directory."""

import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from docmost_cli.api.client import DocmostClient
from docmost_cli.output.formatter import _err_console as _err

__all__ = ["PullResult", "flatten_tree", "pull_space"]

_SNAPSHOT_UNSET = object()
_ACTIVE_PUBLISH_TOKENS: set[str] = set()


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
    parent = root
    for part in relative_path.parts[:-1]:
        parent /= part
        if parent.is_symlink():
            raise FileExistsError(
                f"refusing to remove a managed file through a symlink: {relative_path}"
            )
        if not parent.exists():
            return
        if not parent.is_dir():
            raise NotADirectoryError(f"managed path parent is not a directory: {relative_path}")

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
        rich_content = entry.get("rich_content")
        if isinstance(rich_content, dict) and rich_content.get("snapshot_path") is not None:
            _remove_managed_file(
                root,
                _relative_snapshot_path(rich_content.get("snapshot_path")),
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


def _relative_snapshot_path(value: object) -> Path:
    """Validate a manifest-owned raw ProseMirror snapshot path."""
    relative_path = _relative_managed_path(value)
    if (
        len(relative_path.parts) != 3
        or relative_path.parts[:2] != (".docmost", "raw-pages")
        or relative_path.suffix != ".json"
    ):
        raise ValueError(f"manifest snapshot path is outside '.docmost/raw-pages/': {value!r}")
    return relative_path


def _ensure_managed_directory(root: Path, relative_path: Path) -> Path:
    """Create a managed directory without traversing preserved symlinks."""
    destination = root
    for part in relative_path.parts:
        destination /= part
        if destination.is_symlink():
            raise FileExistsError(f"refusing to write through symlink: {relative_path}")
        if destination.exists() and not destination.is_dir():
            raise FileExistsError(f"managed directory collides with a local file: {relative_path}")
        destination.mkdir(exist_ok=True)
    return destination


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

        rich_content = entry.get("rich_content")
        if not isinstance(rich_content, dict):
            raise RuntimeError(
                f"staged page is missing rich-content recovery state: {relative_path}"
            )
        snapshot_path = staging_path / _relative_snapshot_path(rich_content.get("snapshot_path"))
        if snapshot_path.is_symlink() or not snapshot_path.is_file():
            raise RuntimeError(f"staged page snapshot is missing: {snapshot_path}")
        serialized_snapshot = snapshot_path.read_text(encoding="utf-8").removesuffix("\n")
        snapshot_hash = f"sha256:{hashlib.sha256(serialized_snapshot.encode()).hexdigest()}"
        if snapshot_hash != rich_content.get("snapshot_hash"):
            raise RuntimeError(f"staged page snapshot hash does not match: {snapshot_path}")

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


def _apply_default_directory_mode(path: Path) -> None:
    """Apply the mode that a normal mkdir would receive under the current umask."""
    probe = path / ".docmost-directory-mode-probe"
    probe.mkdir()
    mode = stat.S_IMODE(probe.stat().st_mode)
    probe.rmdir()
    path.chmod(mode)


def _snapshot_target(target: Path) -> dict[str, tuple[Any, ...]] | None:
    """Capture target state without following symlinks."""
    if not target.exists() and not target.is_symlink():
        return None

    entries: dict[str, tuple[Any, ...]] = {}

    def record(path: Path, relative_path: str) -> None:
        path_stat = path.lstat()
        file_type = stat.S_IFMT(path_stat.st_mode)
        link_target = os.readlink(path) if stat.S_ISLNK(path_stat.st_mode) else None
        entries[relative_path] = (
            file_type,
            stat.S_IMODE(path_stat.st_mode),
            path_stat.st_size,
            path_stat.st_mtime_ns,
            path_stat.st_ino,
            link_target,
        )

    record(target, ".")
    for current_root, dir_names, file_names in os.walk(target, followlinks=False):
        dir_names.sort()
        file_names.sort()
        current = Path(current_root)
        for name in (*dir_names, *file_names):
            path = current / name
            record(path, path.relative_to(target).as_posix())
    return entries


def _assert_target_unchanged(
    target: Path,
    expected_snapshot: dict[str, tuple[Any, ...]] | None,
) -> None:
    """Abort rather than overwrite local changes made while a pull was staging."""
    if _snapshot_target(target) != expected_snapshot:
        raise RuntimeError(
            f"Target directory '{target}' changed while the pull was staging; "
            "the previous sync and intervening local changes were preserved."
        )


def _snapshot_digest(snapshot: dict[str, tuple[Any, ...]] | None) -> str:
    """Return a deterministic digest for a target snapshot."""
    serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _atomic_exchange_directories(left: Path, right: Path) -> bool:
    """Atomically exchange two directories when the operating system supports it."""
    if sys.platform not in {"darwin", "linux"}:
        return False

    libc = ctypes.CDLL(None, use_errno=True)
    left_bytes = os.fsencode(left)
    right_bytes = os.fsencode(right)
    if sys.platform == "darwin":
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            return False
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(left_bytes, right_bytes, 0x00000002)  # RENAME_SWAP
    else:
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            return False
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,  # AT_FDCWD
            left_bytes,
            -100,  # AT_FDCWD
            right_bytes,
            0x00000002,  # RENAME_EXCHANGE
        )

    if result == 0:
        return True
    error_number = ctypes.get_errno()
    unsupported_errors = {
        errno.EINVAL,
        errno.ENOSYS,
        errno.EXDEV,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    if error_number in unsupported_errors:
        return False
    raise OSError(error_number, os.strerror(error_number))


def _rename_directory_noreplace(source: Path, target: Path) -> None:
    """Rename a directory only if the target is still absent."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if sys.platform == "darwin":
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is not None:
            renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            renamex_np.restype = ctypes.c_int
            result = renamex_np(source_bytes, target_bytes, 0x00000004)  # RENAME_EXCL
            if result == 0:
                return
            error_number = ctypes.get_errno()
            if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
                raise FileExistsError(error_number, os.strerror(error_number), target)
            if error_number not in {
                errno.EINVAL,
                errno.ENOSYS,
                getattr(errno, "ENOTSUP", errno.EINVAL),
                getattr(errno, "EOPNOTSUPP", errno.EINVAL),
            }:
                raise OSError(error_number, os.strerror(error_number))
    elif sys.platform == "linux":
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                -100,
                source_bytes,
                -100,
                target_bytes,
                0x00000001,  # RENAME_NOREPLACE
            )
            if result == 0:
                return
            error_number = ctypes.get_errno()
            if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
                raise FileExistsError(error_number, os.strerror(error_number), target)
            if error_number not in {
                errno.EINVAL,
                errno.ENOSYS,
                getattr(errno, "ENOTSUP", errno.EINVAL),
                getattr(errno, "EOPNOTSUPP", errno.EINVAL),
            }:
                raise OSError(error_number, os.strerror(error_number))
    elif os.name == "nt":
        os.rename(source, target)
        return

    # Reserve the absent name atomically on platforms without no-replace rename.
    target.mkdir()
    placeholder_identity = _path_identity(target)
    try:
        if _path_identity(target) != placeholder_identity:
            raise FileExistsError(f"target changed while reserving '{target}'")
        os.replace(source, target)
    except BaseException:
        if _path_identity(target) == placeholder_identity:
            with suppress(OSError):
                target.rmdir()
        raise


def _publish_journal_path(target: Path) -> Path:
    """Return the durable recovery journal path for a target."""
    return target.parent / f".{target.name}.pull-transaction.json"


def _sync_directory(path: Path) -> None:
    """Durably flush directory-entry changes where the platform supports it."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_tree(path: Path) -> None:
    """Flush staged file contents and directories before publication."""
    for current_root, _dir_names, file_names in os.walk(path, topdown=False):
        current = Path(current_root)
        for file_name in file_names:
            file_path = current / file_name
            file_stat = file_path.lstat()
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            descriptor = os.open(file_path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _sync_directory(current)


def _path_identity(path: Path) -> tuple[int, int] | None:
    """Return a stable filesystem identity without following symlinks."""
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return None
    return path_stat.st_dev, path_stat.st_ino


def _payload_identity(payload: dict[str, Any], key: str) -> tuple[int, int] | None:
    """Read a validated filesystem identity from a recovery payload."""
    value = payload.get(key)
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(part, int) for part in value)
    ):
        raise RuntimeError(f"invalid {key} in pull recovery journal")
    return value[0], value[1]


def _identity_payload(path: Path) -> list[int] | None:
    """Serialize a path identity for a recovery payload."""
    identity = _path_identity(path)
    return list(identity) if identity is not None else None


def _process_is_running(process_id: int) -> bool:
    """Return whether a process ID still refers to a live process."""
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_json_file(path: Path, payload: dict[str, Any], *, exclusive: bool) -> None:
    """Write and fsync one JSON object, optionally requiring a new file."""
    flags = os.O_WRONLY | os.O_CREAT
    if exclusive:
        flags |= os.O_EXCL
    else:
        flags |= os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as journal:
            json.dump(payload, journal, sort_keys=True)
            journal.write("\n")
            journal.flush()
            os.fsync(journal.fileno())
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise


def _write_publish_journal(
    target: Path,
    staging_path: Path,
    backup_path: Path | None,
) -> dict[str, Any]:
    """Persist enough sibling names to recover an interrupted publication."""
    journal_path = _publish_journal_path(target)
    token = uuid.uuid4().hex
    payload = {
        "version": 2,
        "target": target.name,
        "staging": staging_path.name,
        "backup": backup_path.name if backup_path else None,
        "phase": "prepared",
        "owner_pid": os.getpid(),
        "owner_token": token,
        "target_identity": _identity_payload(target),
        "target_snapshot_digest": _snapshot_digest(_snapshot_target(target)),
        "staging_identity": _identity_payload(staging_path),
        "backup_identity": _identity_payload(backup_path) if backup_path is not None else None,
    }
    try:
        _write_json_file(journal_path, payload, exclusive=True)
        _sync_directory(target.parent)
    except FileExistsError as exc:
        raise RuntimeError(
            f"another pull publication or recovery is active for '{target}'"
        ) from exc
    except BaseException:
        with suppress(FileNotFoundError):
            journal_path.unlink()
        raise
    _ACTIVE_PUBLISH_TOKENS.add(token)
    return payload


def _update_publish_journal(target: Path, payload: dict[str, Any], phase: str) -> None:
    """Advance and durably persist a publication phase."""
    journal_path = _publish_journal_path(target)
    temporary_path = journal_path.with_name(f"{journal_path.name}.tmp-{uuid.uuid4().hex}")
    updated = {**payload, "phase": phase}
    try:
        _write_json_file(temporary_path, updated, exclusive=True)
        os.replace(temporary_path, journal_path)
        _sync_directory(target.parent)
    finally:
        with suppress(FileNotFoundError):
            temporary_path.unlink()
    payload.clear()
    payload.update(updated)


def _remove_publish_journal(target: Path) -> None:
    """Remove a completed publication journal durably."""
    journal_path = _publish_journal_path(target)
    token: str | None = None
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("owner_token"), str):
            token = payload["owner_token"]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    try:
        journal_path.unlink()
    except FileNotFoundError:
        return
    _sync_directory(target.parent)
    if token is not None:
        _ACTIVE_PUBLISH_TOKENS.discard(token)


def _release_current_publish_ownership(target: Path) -> None:
    """Release an in-process ownership marker after publication unwinds."""
    try:
        payload = json.loads(_publish_journal_path(target).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    if (
        isinstance(payload, dict)
        and payload.get("owner_pid") == os.getpid()
        and isinstance(payload.get("owner_token"), str)
    ):
        _ACTIVE_PUBLISH_TOKENS.discard(payload["owner_token"])


def _staging_is_recovery_data(target: Path, staging_path: Path) -> bool:
    """Return whether a failed transaction still needs the staging directory."""
    try:
        payload = json.loads(_publish_journal_path(target).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (json.JSONDecodeError, OSError):
        return True
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 2
        or payload.get("target") != target.name
        or payload.get("staging") != staging_path.name
    ):
        return True
    try:
        target_identity = _payload_identity(payload, "target_identity")
    except RuntimeError:
        return True
    return target_identity is not None


def _journal_sibling(
    target: Path,
    value: object,
    *,
    purpose: str,
    optional: bool = False,
) -> Path | None:
    """Validate a journal-owned sibling path before recovery."""
    if optional and value is None:
        return None
    if not isinstance(value, str) or Path(value).name != value:
        raise RuntimeError(f"invalid {purpose} path in pull recovery journal")
    expected_prefix = f".{target.name}.{purpose}-"
    if not value.startswith(expected_prefix):
        raise RuntimeError(f"unexpected {purpose} path in pull recovery journal")
    return target.parent / value


def _recover_interrupted_publish(target: Path) -> None:
    """Restore or finish a publication described by a durable sibling journal."""
    journal_path = _publish_journal_path(target)
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cannot read pull recovery journal '{journal_path}'") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot read pull recovery journal '{journal_path}'") from exc

    if (
        not isinstance(payload, dict)
        or payload.get("version") != 2
        or payload.get("target") != target.name
        or payload.get("phase") not in {"prepared", "target-moved", "published", "conflict"}
    ):
        raise RuntimeError(f"invalid pull recovery journal '{journal_path}'")
    target_snapshot_digest = payload.get("target_snapshot_digest")
    if not isinstance(target_snapshot_digest, str):
        raise RuntimeError(f"invalid pull recovery journal '{journal_path}'")
    owner_pid = payload.get("owner_pid")
    owner_token = payload.get("owner_token")
    if not isinstance(owner_pid, int) or not isinstance(owner_token, str):
        raise RuntimeError(f"invalid pull recovery journal '{journal_path}'")
    if owner_token in _ACTIVE_PUBLISH_TOKENS or (
        owner_pid != os.getpid() and _process_is_running(owner_pid)
    ):
        raise RuntimeError(f"another pull publication is active for '{target}'")

    staging_path = _journal_sibling(target, payload.get("staging"), purpose="pull-staging")
    backup_path = _journal_sibling(
        target,
        payload.get("backup"),
        purpose="pull-backup",
        optional=True,
    )
    assert staging_path is not None
    target_identity = _payload_identity(payload, "target_identity")
    staging_identity = _payload_identity(payload, "staging_identity")
    backup_identity = _payload_identity(payload, "backup_identity")
    actual_target = _path_identity(target)
    actual_staging = _path_identity(staging_path)
    actual_backup = _path_identity(backup_path) if backup_path is not None else None

    if target_identity is None or staging_identity is None:
        raise RuntimeError(f"incomplete pull recovery journal '{journal_path}'")
    if payload["phase"] == "conflict":
        raise RuntimeError(
            f"pull recovery found a preserved publication conflict for '{target}'; "
            f"inspect '{journal_path}' and its staging/backup paths"
        )

    if actual_target is None:
        if backup_path is None or actual_backup != target_identity:
            raise RuntimeError(
                f"pull recovery cannot identify the previous target for '{target}'; "
                f"preserving all transaction data for inspection"
            )
        try:
            _rename_directory_noreplace(backup_path, target)
        except FileExistsError as exc:
            raise RuntimeError(
                f"pull recovery found an unexpected replacement at '{target}'; "
                f"preserving all transaction data for inspection"
            ) from exc
        _sync_directory(target.parent)
        actual_target = target_identity
        actual_backup = None

    if actual_target == staging_identity:
        old_snapshot_is_identified = (
            actual_staging == target_identity or actual_backup == target_identity
        )
        if payload["phase"] != "published" and not old_snapshot_is_identified:
            raise RuntimeError(
                f"pull recovery cannot identify the previous snapshot for '{target}'; "
                f"preserving all transaction data for inspection"
            )
        old_snapshot_path = staging_path if actual_staging == target_identity else backup_path
        if (
            old_snapshot_path is not None
            and _snapshot_digest(_snapshot_target(old_snapshot_path)) != target_snapshot_digest
        ):
            raise RuntimeError(
                f"pull recovery found local changes in the previous snapshot for '{target}'; "
                f"preserving all transaction data for inspection"
            )
        if payload["phase"] != "published":
            _sync_directory(target.parent)
            _update_publish_journal(target, payload, "published")
    elif actual_target != target_identity:
        raise RuntimeError(
            f"pull recovery found an unexpected replacement at '{target}'; "
            f"preserving all transaction data for inspection"
        )

    if actual_staging is not None:
        if actual_staging not in {target_identity, staging_identity}:
            raise RuntimeError(
                f"pull recovery found an unexpected staging directory '{staging_path}'"
            )
        _remove_tree(staging_path)
    if backup_path is not None and actual_backup is not None:
        if actual_backup not in {target_identity, backup_identity}:
            raise RuntimeError(
                f"pull recovery found an unexpected backup directory '{backup_path}'"
            )
        _remove_tree(backup_path)
    _sync_directory(target.parent)
    _remove_publish_journal(target)


def _publish_staged_pull(
    staging_path: Path,
    target: Path,
    *,
    expected_snapshot: dict[str, tuple[Any, ...]] | None | object = _SNAPSHOT_UNSET,
) -> None:
    """Publish staged state atomically, with a recoverable portable fallback."""
    if expected_snapshot is not _SNAPSHOT_UNSET:
        assert expected_snapshot is None or isinstance(expected_snapshot, dict)
        _assert_target_unchanged(target, expected_snapshot)
    if not target.exists() and not target.is_symlink():
        try:
            _rename_directory_noreplace(staging_path, target)
        except FileExistsError as exc:
            raise RuntimeError(
                f"Target directory '{target}' appeared while the pull was staging; "
                "the intervening target was preserved."
            ) from exc
        _sync_directory(target.parent)
        return

    backup_path = _temporary_sibling(target, "pull-backup")
    try:
        journal = _write_publish_journal(target, staging_path, backup_path)
    except BaseException:
        _remove_tree(backup_path)
        raise
    if expected_snapshot is not _SNAPSHOT_UNSET:
        try:
            assert expected_snapshot is None or isinstance(expected_snapshot, dict)
            _assert_target_unchanged(target, expected_snapshot)
        except BaseException:
            _remove_tree(backup_path)
            _remove_publish_journal(target)
            raise

    staged_snapshot = _snapshot_target(staging_path)
    if _atomic_exchange_directories(staging_path, target):
        _sync_directory(target.parent)
        if expected_snapshot is not _SNAPSHOT_UNSET:
            try:
                assert expected_snapshot is None or isinstance(expected_snapshot, dict)
                _assert_target_unchanged(staging_path, expected_snapshot)
            except BaseException:
                with suppress(OSError):
                    _update_publish_journal(target, journal, "conflict")
                try:
                    can_rollback = (
                        _path_identity(target) == _payload_identity(journal, "staging_identity")
                        and _snapshot_target(target) == staged_snapshot
                    )
                    if can_rollback and _atomic_exchange_directories(staging_path, target):
                        _sync_directory(target.parent)
                        if _snapshot_target(staging_path) == staged_snapshot:
                            _remove_publish_journal(target)
                except OSError:
                    pass
                raise
        _update_publish_journal(target, journal, "published")
        try:
            _remove_tree(staging_path)
        except OSError as exc:
            _err.print(
                f"[yellow]Warning:[/yellow] could not remove previous sync '{staging_path}': {exc}"
            )
        try:
            _remove_tree(backup_path)
        except OSError as exc:
            _err.print(
                f"[yellow]Warning:[/yellow] could not remove reserved backup '{backup_path}': {exc}"
            )
        _remove_publish_journal(target)
        return

    try:
        backup_path.rmdir()
    except OSError:
        _remove_publish_journal(target)
        raise
    os.replace(target, backup_path)
    try:
        _sync_directory(target.parent)
        _update_publish_journal(target, journal, "target-moved")
    except BaseException:
        try:
            _rename_directory_noreplace(backup_path, target)
            _sync_directory(target.parent)
            _remove_publish_journal(target)
        except OSError:
            pass
        raise
    if expected_snapshot is not _SNAPSHOT_UNSET:
        try:
            assert expected_snapshot is None or isinstance(expected_snapshot, dict)
            _assert_target_unchanged(backup_path, expected_snapshot)
        except BaseException:
            _rename_directory_noreplace(backup_path, target)
            _sync_directory(target.parent)
            _remove_publish_journal(target)
            raise
    try:
        _rename_directory_noreplace(staging_path, target)
        _sync_directory(target.parent)
        _update_publish_journal(target, journal, "published")
    except BaseException:
        try:
            if not target.exists() and _path_identity(backup_path) == _payload_identity(
                journal, "target_identity"
            ):
                _rename_directory_noreplace(backup_path, target)
                _sync_directory(target.parent)
                _remove_publish_journal(target)
        except OSError as rollback_error:
            raise RuntimeError(
                f"pull publication failed and rollback could not restore '{target}'; "
                f"the previous data and recovery journal remain at '{backup_path}'"
            ) from rollback_error
        raise

    try:
        _remove_tree(backup_path)
        _sync_directory(target.parent)
    except OSError as exc:
        _err.print(f"[yellow]Warning:[/yellow] could not remove backup '{backup_path}': {exc}")
    _remove_publish_journal(target)


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
    stack = [(page, parent_id) for page in reversed(pages)]
    while stack:
        page, current_parent_id = stack.pop()
        result.append(
            {
                "id": page["id"],
                "title": page.get("title", ""),
                "icon": page.get("icon") or "",
                "parent_id": current_parent_id,
            }
        )
        children = page.get("children", [])
        stack.extend((child, page["id"]) for child in reversed(children))
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
    from docmost_cli.api.pages import build_page_tree
    from docmost_cli.api.spaces import resolve_space_id
    from docmost_cli.convert.prosemirror_to_md import convert_to_markdown
    from docmost_cli.output.formatter import print_error
    from docmost_cli.sync.assets import (
        asset_markdown_path,
        asset_relative_path,
        build_asset_entry,
        collect_attachment_ids,
    )
    from docmost_cli.sync.conflicts import (
        fetch_server_page,
        get_server_revision_token,
        server_page_can_use_canonical_revision,
        server_page_revision_is_verified,
    )
    from docmost_cli.sync.frontmatter import write_sync_file
    from docmost_cli.sync.manifest import (
        MANIFEST_FILENAME,
        build_manifest,
        build_page_entry,
        build_server_revision,
        compute_content_hash,
        load_manifest,
        sanitize_filename,
        save_manifest,
    )
    from docmost_cli.sync.rich_content import (
        PageRevisionChangedError,
        build_pulled_rich_content_state,
        fetch_canonical_markdown,
        rewrite_attachment_urls,
    )

    target_path = Path(os.path.abspath(dir_path))
    if target_path == target_path.parent:
        print_error("The sync target must not be a filesystem root.")
    if target_path.is_symlink():
        print_error(f"Target directory '{dir_path}' must not be a symbolic link.")
    current_directory = Path.cwd().resolve()
    resolved_target = target_path.resolve()
    if current_directory == resolved_target or resolved_target in current_directory.parents:
        print_error(
            f"Leave target directory '{dir_path}' before pulling so it can be replaced safely."
        )
    _recover_interrupted_publish(target_path)
    if target_path.exists() and not target_path.is_dir():
        print_error(f"Target path '{dir_path}' is not a directory.")

    existing_manifest = load_manifest(target_path)
    if existing_manifest is not None and not force:
        print_error(f"Directory '{dir_path}' already has synced data. Use --force to overwrite.")

    # 1. Resolve space
    space_id = resolve_space_id(client, space_slug)

    # 2. Build page tree
    _err.print(f"Fetching page tree for '{space_slug}'...")
    tree = build_page_tree(client, space_id, max_depth=None)

    # 3. Flatten
    flat_pages = flatten_tree(tree)
    total = len(flat_pages)

    # 4. Prepare a same-filesystem staging directory so publication uses renames.
    target_snapshot = _snapshot_target(target_path)
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
            _assert_target_unchanged(target_path, target_snapshot)
        else:
            _apply_default_directory_mode(staging_path)
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
        protected_pages = 0
        for i, page_info in enumerate(flat_pages, 1):
            page_id = page_info["id"]
            title = page_info["title"]
            _err.print(f"Pulling {i}/{total}: {title}")

            # Keep the raw recovery snapshot, remote-conflict baseline, and
            # canonical Markdown on the same verified page revision.
            server_page: dict[str, Any] = {}
            for revision_attempt in range(3):
                fetched_page = fetch_server_page(
                    client,
                    page_id,
                    failure_suffix="The pull was not completed.",
                )
                if fetched_page is None:
                    print_error(
                        f"Page {page_id} disappeared while it was being pulled. "
                        "The pull was not completed."
                    )
                assert fetched_page is not None
                server_page = fetched_page
                revision_verified = server_page_revision_is_verified(server_page)
                pm_content = server_page.get("content")
                expected_updated_at = get_server_revision_token(server_page.get("updatedAt"))
                if server_page_can_use_canonical_revision(server_page):
                    try:
                        canonical_markdown = fetch_canonical_markdown(
                            client,
                            page_id,
                            expected_updated_at=expected_updated_at,
                        )
                    except PageRevisionChangedError:
                        if revision_attempt == 2:
                            print_error(
                                f"Page '{title}' changed repeatedly during pull. "
                                "Wait for edits to finish and retry."
                            )
                        _err.print(
                            f"  [yellow]Page '{title}' changed during pull; retrying.[/yellow]"
                        )
                        continue
                else:
                    canonical_markdown = None
                break

            title = str(server_page.get("title") or title)
            parent_id = server_page.get("parentPageId", page_info["parent_id"])
            icon = str(server_page.get("icon") or "")
            _ensure_managed_directory(
                staging_path,
                Path(".docmost") / "raw-pages",
            )
            rich_content = build_pulled_rich_content_state(
                staging_path,
                page_id,
                pm_content,
            )
            if expected_updated_at is None or not revision_verified:
                rich_content["unsafe_features"].append("conversion:unverified-revision")
                rich_content["unsafe_features"].sort()

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

            if canonical_markdown is None:
                unsafe_features = rich_content["unsafe_features"]
                if "conversion:local-fallback" not in unsafe_features:
                    unsafe_features.append("conversion:local-fallback")
                    unsafe_features.sort()
                _err.print(
                    f"  [yellow]Server Markdown conversion unavailable for '{title}'; "
                    "using the local converter.[/yellow]"
                )
                markdown = (
                    convert_to_markdown(pm_content, attachment_paths=attachment_paths)
                    if pm_content
                    else ""
                )
            else:
                markdown = rewrite_attachment_urls(
                    canonical_markdown,
                    attachment_paths,
                    docmost_origin=client.api_url("/"),
                )

            unsafe_features = rich_content["unsafe_features"]
            if unsafe_features:
                protected_pages += 1
                _err.print(
                    f"  [yellow]Protected rich content:[/yellow] "
                    f"{title} ({', '.join(unsafe_features)})"
                )

            filename = sanitize_filename(title, page_id)
            destination = _prepare_destination(
                staging_path,
                _relative_managed_path(filename, top_level=True),
            )
            metadata = {
                "id": page_id,
                "title": title,
                "parent_id": parent_id or "",
                "icon": icon,
            }
            write_sync_file(destination, metadata, markdown)

            content_hash = compute_content_hash(markdown)
            entry = build_page_entry(
                title=title,
                filename=filename,
                parent_id=parent_id,
                icon=icon,
                content_hash=content_hash,
                attachment_ids=attachment_ids,
                rich_content=rich_content,
                server_revision=(build_server_revision(server_page) if revision_verified else None),
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
        _sync_tree(staging_path)

        # 8. Publish as one directory snapshot, with rollback on rename failure.
        _assert_target_unchanged(target_path, target_snapshot)
        _publish_staged_pull(
            staging_path,
            target_path,
            expected_snapshot=target_snapshot,
        )
        published = True
    finally:
        _release_current_publish_ownership(target_path)
        if not published and (staging_path.exists() or staging_path.is_symlink()):
            if _staging_is_recovery_data(target_path, staging_path):
                _err.print(
                    f"[yellow]Warning:[/yellow] preserving previous snapshot "
                    f"'{staging_path}' for pull recovery"
                )
            else:
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
    if protected_pages:
        _err.print(
            f"[yellow]{protected_pages} page(s) contain rich content that Markdown cannot "
            "round-trip. Their Markdown may be read locally, but content pushes are blocked; "
            "title, icon, and parent changes remain safe.[/yellow]"
        )
    return PullResult(
        pages_pulled=total,
        dir_path=dir_path,
        attachments_pulled=len(assets),
    )
