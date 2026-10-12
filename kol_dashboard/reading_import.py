"""Import an explicit POC content archive during the guarded release transaction.

Only original reading records and audio samples are accepted. Runtime state,
credentials, caches, diagnostic responses and model weights cannot be imported.
Existing remote content is never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import tarfile
import tempfile

MAX_TOTAL = 50 * 1024 * 1024
MAX_FILE = 12 * 1024 * 1024
ALLOWED = re.compile(r"^(articles/wx-[a-zA-Z0-9_-]{1,97}\.json|episodes/[a-zA-Z0-9_-]{1,100}\.json|media/[a-zA-Z0-9_-]{1,100}\.wav)$")


def _validate_record(name, data):
    if name.endswith(".wav"):
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise ValueError("Invalid audio sample")
        return
    record = json.loads(data)
    if not isinstance(record, dict) or record.get("id") != Path(name).stem:
        raise ValueError("Record identity mismatch")
    if any(key in record for key in ("api_key", "credentials", "csrf_token", "session_secret")):
        raise ValueError("Runtime secrets cannot be imported")
    if not isinstance(record.get("segments"), list):
        raise ValueError("Missing source segment list")
    if name.startswith("articles/") and record.get("content_kind") != "wechat_article":
        raise ValueError("Invalid article record")


def import_archive(archive: Path, data_dir: Path, *, owner: str | None = None):
    # Validate the complete bounded archive before creating any destination files.
    records, seen, total = [], set(), 0
    with tarfile.open(archive, "r:gz") as package:
        for member in package:
            if (not member.isfile() or not ALLOWED.fullmatch(member.name)
                    or member.name in seen or not 0 < member.size <= MAX_FILE):
                raise ValueError("Archive contains an unsupported member")
            total += member.size
            if total > MAX_TOTAL or len(seen) >= 500:
                raise ValueError("Reading import exceeds the bounded seed limit")
            stream = package.extractfile(member)
            if stream is None:
                raise ValueError("Missing archive member")
            with stream:
                content = stream.read(MAX_FILE + 1)
            if len(content) != member.size:
                raise ValueError("Incomplete archive member")
            _validate_record(member.name, content)
            seen.add(member.name)
            records.append((member.name, content))
    if not records:
        raise ValueError("Empty seed archive")
    identity = pwd.getpwnam(owner) if owner else None
    if data_dir.is_symlink():
        raise ValueError("Reading directory cannot be a symlink")
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.chmod(0o700)
    if identity:
        os.chown(data_dir, identity.pw_uid, identity.pw_gid)
    imported, skipped = [], []
    for name, content in records:
        destination = data_dir / name
        if destination.parent.is_symlink() or destination.is_symlink():
            raise ValueError("Reading destination cannot be a symlink")
        destination.parent.mkdir(mode=0o700, exist_ok=True)
        destination.parent.chmod(0o700)
        if identity:
            os.chown(destination.parent, identity.pw_uid, identity.pw_gid)
        if destination.exists():
            skipped.append(name)
            continue
        fd, temporary = tempfile.mkstemp(prefix=".reading-import-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if identity:
                os.chown(temporary, identity.pw_uid, identity.pw_gid)
            # A hard link atomically installs only if the destination is absent.
            try:
                os.link(temporary, destination)
                imported.append({"path": name, "sha256": hashlib.sha256(content).hexdigest()})
            except FileExistsError:
                skipped.append(name)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {"imported": len(imported), "skipped": len(skipped), "files": imported}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--owner")
    args = parser.parse_args()
    result = import_archive(args.archive, args.data_dir, owner=args.owner)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
