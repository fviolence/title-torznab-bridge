"""SQLite connection helper with actionable diagnostics for Docker mounts."""

from pathlib import Path
import os
import sqlite3
import stat


def connect_sqlite(path: str) -> sqlite3.Connection:
    try:
        return sqlite3.connect(path, timeout=15)
    except sqlite3.OperationalError as exc:
        parent = Path(path).parent
        try:
            info = parent.stat()
            details = (f"directory={parent!s}, owner={info.st_uid}:{info.st_gid}, "
                       f"mode={stat.S_IMODE(info.st_mode):04o}")
        except OSError as stat_exc:
            details = f"directory={parent!s}, stat_error={type(stat_exc).__name__}"
        raise RuntimeError(
            f"Unable to open SQLite database {path!r} as "
            f"UID:GID {os.geteuid()}:{os.getegid()} ({details}). "
            "Check that the mounted directory is writable. "
            "The default Docker Compose named volume avoids host UID ownership issues."
        ) from exc
