"""Confined access to the optional read-only host media library."""

from pathlib import Path

from .db.models import Source

LIBRARY_ROOT = Path("/media")


class LibraryError(ValueError):
    pass


def resolve_library_path(relative: str, *, must_exist: bool = True) -> Path:
    if (
        not relative
        or Path(relative).is_absolute()
        or "\\" in relative
        or ".." in Path(relative).parts
    ):
        raise LibraryError("Choose a relative path inside the media library.")
    try:
        root = LIBRARY_ROOT.resolve(strict=True)
        lexical = root / relative
        current = root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise LibraryError("Symlinks are not supported in the media library.")
        candidate = lexical.resolve(strict=must_exist)
        candidate.relative_to(root)
        return candidate
    except (OSError, RuntimeError, ValueError) as exc:
        raise LibraryError("Path is unavailable or outside the media library.") from exc


def media_path(source: Source) -> Path:
    """Resolve a source for consumption, rechecking library confinement each time."""
    if source.library_path is None:
        return Path(source.path)
    try:
        path = resolve_library_path(source.library_path)
        if not path.is_file():
            raise LibraryError("Source media is not a regular file.")
        with path.open("rb"):
            pass
        return path
    except (LibraryError, OSError) as exc:
        message = (
            "Library media is unavailable or outside the media library."
            if source.library_path is not None
            else "Source media is unavailable."
        )
        raise LibraryError(message) from exc
