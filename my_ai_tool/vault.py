"""VAULT — the 'library system': AI-driven lazy-loading compressed storage.

The archive (data.zip / .tar.gz) stays compressed at rest. Only the exact
files the AI requested are extracted — into a RAM-backed scratch dir
(/dev/shm when available, else /tmp, mode 0700). After the task, files the
AI changed (detected by sha256 diff) are repacked into the archive
atomically, and the scratch dir is deleted immediately.

Security guards:
  - member name validation (no absolute paths, no .., no traversal)
  - zip-bomb caps (per-file MB + total MB, declared size checked, then
    enforced while reading)
  - tar: only regular files/dirs are extracted (no symlinks/devices)
  - archive lock (fcntl) so CLI and daemon never repack simultaneously
  - timestamped backup of the archive before every repack (rotated)
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import tarfile
import tempfile
import time
import zipfile
from datetime import datetime

from . import config, paths
from .logging_setup import get_logger

log = get_logger("vault")


class VaultError(RuntimeError):
    pass


class Busy(RuntimeError):
    pass


# ------------------------------------------------------------------ helpers

def _cfg() -> dict:
    return config.load_config().get("vault", {})


def archive_path() -> pathlib.Path:
    p = pathlib.Path(os.path.expanduser(
        _cfg().get("path") or str(paths.data_dir() / "vault.zip")))
    return p.resolve()


def _limits() -> tuple:
    v = _cfg()
    return int(v.get("max_file_mb", 256)) * 1024 * 1024, \
        int(v.get("max_total_mb", 1024)) * 1024 * 1024


def validate_member(name: str) -> str:
    """Normalise and reject dangerous member names (traversal etc.)."""
    if not name or "\x00" in name:
        raise VaultError(f"invalid member name: {name!r}")
    n = name.replace("\\", "/")
    while n.startswith("./"):     # strip plain './' prefixes only
        n = n[2:]
    if n.startswith("/") or pathlib.PurePosixPath(n).is_absolute():
        raise VaultError(f"absolute member path not allowed: {name!r}")
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise VaultError(f"path traversal not allowed: {name!r}")
    if not parts:
        raise VaultError(f"empty member name: {name!r}")
    if len(parts) > 32:
        raise VaultError(f"member path too deep: {name!r}")
    return "/".join(parts)


def sha256_file(p: os.PathLike) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 512), b""):
            h.update(chunk)
    return h.hexdigest()


class _CapReader:
    """Enforce the declared/uncompressed size while streaming a member out."""

    def __init__(self, fsrc, limit: int, name: str):
        self.fsrc, self.limit, self.name, self.read_len = fsrc, limit, name, 0

    def read(self, n=-1):
        data = self.fsrc.read(n)
        self.read_len += len(data)
        if self.read_len > self.limit:
            raise VaultError(
                f"member {self.name!r} exceeds size cap ({self.limit} bytes)"
                " — possible zip bomb; aborting")
        return data


# ------------------------------------------------------------------- locking

class ArchiveLock:
    """fcntl lock next to the archive so repacks never race."""

    def __init__(self, archive: pathlib.Path, timeout: float = 30.0):
        self.lock_path = archive.with_suffix(archive.suffix + ".lock")
        self.timeout = timeout
        self.fh = None

    def __enter__(self):
        import fcntl
        self.fh = open(self.lock_path, "w")
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() > deadline:
                    self.fh.close()
                    raise Busy("vault archive is busy (another repack running)")
                time.sleep(0.2)

    def __exit__(self, *a):
        try:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()
        return False


# ------------------------------------------------------------------- scratch

def pick_scratch_root() -> pathlib.Path:
    """RAM-first: /dev/shm (tmpfs) if usable, else /tmp, else tempdir."""
    v = _cfg().get("scratch", "auto")
    if v not in ("auto", "shm", "tmp", ""):
        p = pathlib.Path(os.path.expanduser(v))
        p.mkdir(parents=True, exist_ok=True)
        return p
    cands = ["/dev/shm", "/tmp"] if v in ("auto", "shm") else ["/tmp"]
    for c in cands:
        if os.path.isdir(c) and os.access(c, os.W_OK):
            return pathlib.Path(c)
    return pathlib.Path(tempfile.gettempdir())


def new_scratch_dir() -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp(prefix="mytool-vault-",
                                      dir=str(pick_scratch_root())))
    os.chmod(d, 0o700)  # private to the current user
    return d


def clean_stale_scratch(max_age_hours: float = 1.0) -> int:
    root = pick_scratch_root()
    cutoff = time.time() - max_age_hours * 3600
    n = 0
    for d in root.glob("mytool-vault-*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            continue
    return n


# -------------------------------------------------------------------- vault

def open_vault(path: pathlib.Path | None = None):
    p = path or archive_path()
    if not p.exists():
        raise VaultError(f"vault archive not found: {p} — create it with"
                         " `mytool vault-init <dir>` ya `config set vault.path`")
    name = p.name.lower()
    if name.endswith(".zip"):
        return ZipVault(p)
    if name.endswith((".tar.gz", ".tgz", ".tar")):
        return TarVault(p)
    raise VaultError(f"unsupported archive type: {p.name} (use .zip/.tar.gz/.tgz/.tar)")


class BaseVault:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.max_file, self.max_total = _limits()

    # -- introspection ------------------------------------------------------
    def list_members(self) -> list:
        raise NotImplementedError

    def index(self) -> list:
        """[(name, size_bytes)] of regular files only."""
        out = []
        for m in self.list_members():
            if m["is_file"]:
                out.append((m["name"], m["size"]))
        return out

    # -- selective (lazy) extraction -----------------------------------------
    def extract(self, names: list, dest: pathlib.Path) -> dict:
        """Extract ONLY these members into dest. Returns {name: sha256}."""
        if not names:
            return {}
        known = {m["name"]: m for m in self.list_members() if m["is_file"]}
        missing = [n for n in names if n not in known]
        if missing:
            raise VaultError(f"not in archive: {missing}")
        total = 0
        hashes = {}
        for n in names:
            m = known[n]
            if m["size"] > self.max_file:
                raise VaultError(f"{n} ({m['size']}B) exceeds per-file cap")
            total += m["size"]
            if total > self.max_total:
                raise VaultError("extraction would exceed max_total_mb cap")
        for n in names:
            target = dest / n
            target.parent.mkdir(parents=True, exist_ok=True)
            data = self._read_member(n, known[n])
            target.write_bytes(data)
            hashes[n] = hashlib.sha256(data).hexdigest()
        log.info("extracted %d file(s) from %s -> %s", len(names),
                 self.path.name, dest)
        return hashes

    def _read_member(self, name: str, meta: dict) -> bytes:
        raise NotImplementedError

    # -- repack ---------------------------------------------------------------
    def repack_changes(self, replace: dict, add: dict,
                       delete: set) -> dict:
        """replace/add: {archive_name: local_file_path}, delete: {names}.

        Atomic: writes a full new archive (old members streamed through,
        capped), timestamped backup first, then os.replace().
        """
        replace = replace or {}
        add = add or {}
        delete = set(delete or set())
        if not (replace or add or delete):
            return {"changed": False}

        keep_backups = int(_cfg().get("keep_backups", 3))
        bdir = paths.backup_dir() / "vault"
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = bdir / f"{self.path.name}.{stamp}.bak"
        shutil.copy2(self.path, backup)
        olds = sorted(bdir.glob(f"{self.path.name}.*.bak"))
        for old in olds[:-keep_backups] if keep_backups else olds:
            old.unlink(missing_ok=True)

        tmp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        try:
            counts = self._write_new(tmp, replace, add, delete)
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)
        log.info("vault repacked: +%d replaced +%d added -%d deleted"
                 " (backup %s)", len(replace), len(add), len(delete),
                 backup.name)
        return {"changed": True, "backup": str(backup), **counts}

    def _write_new(self, tmp, replace, add, delete) -> dict:
        raise NotImplementedError


class ZipVault(BaseVault):
    def list_members(self) -> list:
        out = []
        with zipfile.ZipFile(self.path) as zf:
            for zi in zf.infolist():
                if zi.filename.endswith("/"):
                    continue
                mode = (zi.external_attr >> 16) & 0o170000
                out.append({"name": validate_member(zi.filename),
                            "raw": zi.filename, "size": zi.file_size,
                            "is_file": mode not in (0o120000,),  # not symlink
                            })
        return out

    def _read_member(self, name: str, meta: dict) -> bytes:
        with zipfile.ZipFile(self.path) as zf:
            with zf.open(meta["raw"]) as f:
                capped = _CapReader(f, min(self.max_file, meta["size"] + 64),
                                    name)
                data = capped.read()
        if len(data) != meta["size"]:
            raise VaultError(f"{name}: declared {meta['size']}B but read"
                             f" {len(data)}B — archive corrupt?")
        return data

    def _write_new(self, tmp, replace, add, delete) -> dict:
        copied = 0
        with zipfile.ZipFile(self.path) as zin, \
                zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for zi in zin.infolist():
                name = validate_member(zi.filename)
                if zi.filename.endswith("/") or name in delete or name in replace:
                    continue
                with zin.open(zi) as fsrc, \
                        zout.open(zi, "w") as fdst:
                    shutil.copyfileobj(
                        _CapReader(fsrc, self.max_file, name), fdst, 1 << 20)
                copied += 1
            for name, local in {**replace, **add}.items():
                zout.write(local, arcname=name)
        return {"copied": copied,
                "replaced": len(replace), "added": len(add),
                "deleted": len(delete)}


class TarVault(BaseVault):
    def _mode(self):
        n = self.path.name.lower()
        if n.endswith(".tar.gz") or n.endswith(".tgz"):
            return "r:gz"
        return "r:"

    def _wmode(self):
        n = self.path.name.lower()
        if n.endswith(".tar.gz") or n.endswith(".tgz"):
            return "w:gz"
        return "w"

    def list_members(self) -> list:
        out = []
        with tarfile.open(self.path, self._mode()) as tf:
            for m in tf.getmembers():
                out.append({"name": validate_member(m.name), "raw": m.name,
                            "size": m.size, "is_file": m.isfile()})
        return out

    def _read_member(self, name: str, meta: dict) -> bytes:
        with tarfile.open(self.path, self._mode()) as tf:
            f = tf.extractfile(meta["raw"])
            if f is None:
                raise VaultError(f"{name} is not a regular file")
            return _CapReader(f, min(self.max_file, meta["size"] + 64),
                              name).read()

    def _write_new(self, tmp, replace, add, delete) -> dict:
        copied = skipped = 0
        with tarfile.open(self.path, self._mode()) as tin, \
                tarfile.open(tmp, self._wmode()) as tout:
            for m in tin:
                name = validate_member(m.name)
                if name in delete or name in replace:
                    continue
                if m.isfile():
                    f = tin.extractfile(m)
                    tout.addfile(m, _CapReader(f, self.max_file, name))
                    copied += 1
                elif m.isdir():
                    tout.addfile(m)
                else:
                    skipped += 1  # symlinks/devices are not carried over
            for name, local in {**replace, **add}.items():
                info = tout.gettarinfo(str(local), arcname=name)
                with open(local, "rb") as f:
                    tout.addfile(info, f)
        return {"copied": copied, "replaced": len(replace), "added": len(add),
                "deleted": len(delete), "skipped_meta": skipped}


# ------------------------------------------------------------ vault-init

def init_from_dir(src: str, save_config_path: bool = True) -> pathlib.Path:
    """Compress a directory into a fresh vault archive (zip default)."""
    srcp = pathlib.Path(os.path.expanduser(src)).resolve()
    if not srcp.is_dir():
        raise VaultError(f"not a directory: {srcp}")
    fmt = (_cfg().get("format") or "zip").lower()
    ext = ".zip" if fmt == "zip" else ".tar.gz"
    dst = archive_path().with_suffix(ext) if archive_path().suffix else \
        paths.data_dir() / f"vault{ext}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.tmp-{os.getpid()}")
    files = [p for p in sorted(srcp.rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts]
    if fmt == "zip":
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for p in files:
                zout.write(p, arcname=p.relative_to(srcp).as_posix())
    else:
        with tarfile.open(tmp, "w:gz") as tout:
            for p in files:
                tout.add(str(p), arcname=p.relative_to(srcp).as_posix())
    os.replace(tmp, dst)
    tmp.unlink(missing_ok=True)
    if save_config_path:
        cfg = config.load_config()
        cfg.setdefault("vault", {})["path"] = str(dst)
        config.save_config(cfg)
    log.info("vault created: %s (%d files from %s)", dst, len(files), srcp)
    return dst
