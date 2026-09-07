"""ff.data.loader — json.gz fixture persistence.

Two functions, per the contract: ``save_json_gz(obj_dict, path)`` and
``load_json_gz(path)``. Callers serialize/deserialize dataclasses via the
``to_dict()/from_dict()`` helpers in ``ff.domain`` — this module deals only
in plain dicts.

Rules enforced here:

- **Atomic write.** Payload goes to a temp file in the destination
  directory, is fsynced, then ``os.replace``d into place — a crash or a
  concurrent reader can never observe a truncated fixture.
- **Deterministic bytes.** ``json.dumps(sort_keys=True)`` plus a gzip
  stream with ``mtime=0`` and an empty embedded filename: identical dicts
  produce byte-identical files (same seed ⇒ identical fixture, the
  generator's determinism law, extends all the way to disk).
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile


def save_json_gz(obj_dict: dict, path: str) -> None:
    """Write ``obj_dict`` to ``path`` as gzipped JSON, atomically.

    Enforces the atomic-write rule (tmp file + ``os.replace`` in the same
    directory, so the rename never crosses filesystems) and deterministic
    bytes (sorted keys, compact separators, gzip ``mtime=0``). Parent
    directories are created as needed. On any failure the temp file is
    removed and the original ``path`` is left untouched.
    """
    if not isinstance(obj_dict, dict):
        raise TypeError(f"save_json_gz expects a dict, got {type(obj_dict).__name__}")
    payload = json.dumps(obj_dict, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=".ff-tmp-", suffix=".json.gz"
    )
    try:
        with os.fdopen(fd, "wb") as raw:
            # mtime=0 + filename="" -> byte-identical output for equal dicts.
            with gzip.GzipFile(
                filename="", fileobj=raw, mode="wb", mtime=0
            ) as gz:
                gz.write(payload)
            raw.flush()
            os.fsync(raw.fileno())
        # mkstemp creates 0600 files; restore normal umask-derived perms so
        # committed fixtures aren't owner-only.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp_path, 0o666 & ~umask)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_json_gz(path: str) -> dict:
    """Read a gzipped-JSON fixture written by ``save_json_gz``.

    Enforces the dict contract: the top-level JSON value must be an object
    (Fleet/Schedule ``to_dict()`` output); anything else raises ValueError
    rather than leaking an unexpected type into the engine.
    """
    with gzip.open(path, "rt", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError(
            f"{path}: expected a top-level JSON object, got {type(obj).__name__}"
        )
    return obj
