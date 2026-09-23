"""The canonical walk, archive and content digest of a bundled tree (#2268).

A bundled host folder travels as a tar built from :func:`walk_tree` and is
verified on import by re-walking the restored directory; a podman volume
travels as ``podman volume export``'s tar and is verified by re-exporting the
restored volume. Both checks compare the same **content digest** — a hash
over each entry's relative path, kind, and content (file bytes, or a
symlink's target) — so a directory and a tar of it digest identically by
construction. Modes, owners and mtimes are deliberately not part of it: a tar
round trip through a safe extraction filter does not preserve them, and they
are not what "the same installation" means.

The walk carries three kinds — directory, regular file, symlink — and
refuses anything else (a socket, FIFO or device) by path: an entry the bundle
cannot reproduce must stop the move, never vanish from it. A bundled tree
also refuses, by path, any secret-shaped file and any symlink the import's
safe extraction would refuse (absolute, or pointing outside the tree) —
checked at export preflight and again inside :func:`build_folder_tar`, so no
caller can write a tar that carries a secret or cannot be restored.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat
import tarfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from app.installation_migration.contents import is_secret_shaped
from app.installation_migration.errors import MigrationRefused

_CHUNK_BYTES = 1 << 20

EntryKind = Literal["d", "f", "l"]


def sha256_file(path: Path) -> str:
    """The hex SHA-256 of a file's bytes, read in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(handle: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One entry of a walked tree: its POSIX relative path and kind."""

    path: str
    kind: EntryKind
    absolute: Path
    link_target: str | None = None


def walk_tree(root: Path) -> list[TreeEntry]:
    """Every entry under ``root`` in sorted order, never following a symlink."""
    entries: list[TreeEntry] = []

    def scan(directory: Path, prefix: str) -> None:
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda child: child.name)
        for child in children:
            relative = f"{prefix}{child.name}"
            absolute = Path(child.path)
            mode = child.stat(follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                entries.append(
                    TreeEntry(relative, "l", absolute, link_target=os.readlink(absolute))
                )
            elif stat.S_ISDIR(mode):
                entries.append(TreeEntry(relative, "d", absolute))
                scan(absolute, f"{relative}/")
            elif stat.S_ISREG(mode):
                entries.append(TreeEntry(relative, "f", absolute))
            else:
                raise MigrationRefused(
                    "unsupported_file_type",
                    f"{absolute} is neither a directory, a regular file nor a "
                    "symlink, so the bundle cannot carry it; remove it (a stale "
                    "socket or pipe) and retry.",
                    details={"path": str(absolute)},
                )

    scan(root, "")
    return entries


def _link_escapes(entry: TreeEntry) -> bool:
    """Whether a symlink points outside its tree (or anywhere absolute)."""
    target = entry.link_target or ""
    if target.startswith(("/", "\\")) or os.path.isabs(target):
        return True
    landing = posixpath.normpath(posixpath.join(posixpath.dirname(entry.path), target))
    return landing == ".." or landing.startswith("../")


def _refuse_unbundleable(walks: Mapping[Path, list[TreeEntry]]) -> None:
    secrets = sorted(
        str(entry.absolute)
        for entries in walks.values()
        for entry in entries
        if entry.kind != "d" and is_secret_shaped(posixpath.basename(entry.path))
    )
    if secrets:
        raise MigrationRefused(
            "secret_in_bundle_source",
            f"Secret-shaped file(s) {', '.join(secrets)} sit inside a bundled folder; "
            "the bundle never carries a secret. Move them out of the folder (the new "
            "host mints its own tokens), then retry.",
            details={"paths": secrets},
        )
    unsafe = sorted(
        str(entry.absolute)
        for entries in walks.values()
        for entry in entries
        if entry.kind == "l" and _link_escapes(entry)
    )
    if unsafe:
        raise MigrationRefused(
            "unsafe_symlink_in_bundle_source",
            f"Symlink(s) {', '.join(unsafe)} point outside their bundled folder, which "
            "the import's safe extraction refuses; replace them with the file or a "
            "link inside the folder, then retry.",
            details={"paths": unsafe},
        )


def require_bundleable(roots: Iterable[Path]) -> None:
    """Refuse, naming every path, a tree holding a secret or an escaping symlink."""
    _refuse_unbundleable({root: walk_tree(root) for root in roots})


def _digest(entries: Iterable[tuple[str, str, str]]) -> str:
    digest = hashlib.sha256()
    for path, kind, payload in sorted(entries):
        digest.update(json.dumps([path, kind, payload]).encode("utf-8") + b"\n")
    return digest.hexdigest()


def tree_digest_from_dir(root: Path) -> str:
    """The content digest of a directory tree."""
    return _digest(
        (
            entry.path,
            entry.kind,
            sha256_file(entry.absolute)
            if entry.kind == "f"
            else (entry.link_target or ""),
        )
        for entry in walk_tree(root)
    )


@contextmanager
def _reading(archive: Path) -> Iterator[None]:
    """Map a corrupt or truncated tar to a refusal naming it.

    ``FilterError`` is re-raised untouched so the caller names the unsafe
    member; every other ``TarError`` (and a short read) is the archive itself
    being unreadable.
    """
    try:
        yield
    except tarfile.FilterError:
        raise
    except (tarfile.TarError, EOFError) as exc:
        raise MigrationRefused(
            "bundle_member_unreadable",
            f"{archive} is not a readable tar: {exc}",
            details={"archive": str(archive)},
        ) from exc


def _member_path(name: str) -> str:
    """A tar member name in the walk's relative form (``./a/`` → ``a``)."""
    while name.startswith("./"):
        name = name[2:]
    return name.strip("/")


def tree_digest_from_tar(archive: Path) -> str:
    """The content digest of a tar, comparable to :func:`tree_digest_from_dir`.

    A hardlink member digests as the regular file it is once extracted.
    """
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    with _reading(archive), tarfile.open(archive, "r:*") as bundle:
        for member in bundle:
            path = _member_path(member.name)
            if path in ("", "."):
                continue
            if path in seen:
                raise MigrationRefused(
                    "bundle_member_duplicate",
                    f"{archive} lists {path!r} more than once; its content is ambiguous.",
                    details={"archive": str(archive), "path": path},
                )
            seen.add(path)
            if member.isdir():
                entries.append((path, "d", ""))
            elif member.issym():
                entries.append((path, "l", member.linkname))
            elif member.isreg() or member.islnk():
                handle = bundle.extractfile(member)
                if handle is None:
                    raise MigrationRefused(
                        "bundle_member_unreadable",
                        f"{archive} member {path!r} has no readable content.",
                        details={"archive": str(archive), "path": path},
                    )
                with handle:
                    entries.append((path, "f", _sha256_stream(handle)))
            else:
                raise MigrationRefused(
                    "unsupported_file_type",
                    f"{archive} member {path!r} is neither a directory, a regular "
                    "file nor a symlink.",
                    details={"archive": str(archive), "path": path},
                )
    return _digest(entries)


def build_folder_tar(root: Path, archive: Path) -> None:
    """Write ``root``'s walk as a new tar at ``archive`` (never overwriting).

    Entries are added from the walk itself rather than ``TarFile.add``, so the
    tar holds exactly what the directory digest hashed — no hardlink members,
    no silently skipped socket.
    """
    entries = walk_tree(root)
    _refuse_unbundleable({root: entries})
    with tarfile.open(archive, "x", format=tarfile.PAX_FORMAT) as bundle:
        for entry in entries:
            metadata = entry.absolute.lstat()
            info = tarfile.TarInfo(entry.path)
            info.mode = stat.S_IMODE(metadata.st_mode)
            info.mtime = int(metadata.st_mtime)
            if entry.kind == "d":
                info.type = tarfile.DIRTYPE
                bundle.addfile(info)
            elif entry.kind == "l":
                info.type = tarfile.SYMTYPE
                info.linkname = entry.link_target or ""
                bundle.addfile(info)
            else:
                info.type = tarfile.REGTYPE
                info.size = metadata.st_size
                with entry.absolute.open("rb") as handle:
                    bundle.addfile(info, handle)


def extract_tar(archive: Path, destination: Path) -> None:
    """Extract ``archive`` under ``destination`` through the ``data`` filter.

    The filter refuses absolute paths, ``..`` escapes and links pointing
    outside the destination; any such member is a refusal, never a partial
    extraction reported as success.
    """
    try:
        with _reading(archive), tarfile.open(archive, "r:*") as bundle:
            bundle.extractall(destination, filter="data")
    except tarfile.FilterError as exc:
        raise MigrationRefused(
            "bundle_member_unsafe",
            f"{archive} holds a member that would land outside {destination}: {exc}",
            details={"archive": str(archive), "destination": str(destination)},
        ) from exc


__all__ = [
    "TreeEntry",
    "build_folder_tar",
    "extract_tar",
    "require_bundleable",
    "sha256_file",
    "tree_digest_from_dir",
    "tree_digest_from_tar",
    "walk_tree",
]
