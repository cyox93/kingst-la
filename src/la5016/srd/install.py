"""--install-decoders: fetch libsigrokdecode's decoders/ at a pinned commit.

The decoders are GPL (mostly 2+) Python files from the sigrok project; they
are downloaded on demand instead of being vendored into this repository.
"""

from __future__ import annotations

import ast
import io
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import List, NamedTuple, Optional

from la5016.srd.loader import decoders_dir

DEFAULT_COMMIT = "71f451443029322d57376214c330b518efd84f88"
URL = "https://codeload.github.com/sigrokproject/libsigrokdecode/tar.gz/%s"


class InstallError(Exception):
    pass


COMMON = "common"  # helper package every decoder may import


def extract(tar_bytes: bytes, dest: Path, commit: str, names: Optional[List[str]] = None) -> int:
    """Install the tarball's decoders/ tree into `dest`. Returns the number of decoders installed.

    Without `names` the whole tree replaces `dest`. With `names` only those
    decoders (plus common/) are added or replaced, keeping the others; that
    requires `dest` to hold the same commit, so versions never mix.
    """
    if names is not None:
        have = installed_version(dest)
        if dest.exists() and have not in (None, commit):
            raise InstallError("installed decoders are from %s, not %s; reinstall all first"
                               % (have[:8], commit[:8]))
        available = {e.name for e in archive_entries(tar_bytes)}
        unknown = sorted(set(names) - available)
        if unknown:
            raise InstallError("not in libsigrokdecode %s: %s" % (commit[:8], ", ".join(unknown)))
        wanted = set(names) | {COMMON}
    tmp = Path(tempfile.mkdtemp(prefix="decoders-", dir=str(dest.parent)))
    try:
        count = 0
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
            for m in tar.getmembers():
                parts = PurePosixPath(m.name).parts
                if len(parts) < 3 or parts[1] != "decoders":
                    continue
                if names is not None and (len(parts) < 4 or parts[2] not in wanted):
                    continue
                rel = PurePosixPath(*parts[2:])
                if rel.is_absolute() or ".." in rel.parts:
                    raise InstallError("unsafe path in archive: %s" % m.name)
                target = tmp.joinpath(*rel.parts)
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif m.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(m) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    if rel.name == "pd.py" and len(rel.parts) == 2:
                        count += 1
                # links and devices are skipped on purpose
        if not count:
            raise InstallError("archive has no decoders/ directory")
        if names is None:
            (tmp / "VERSION").write_text("libsigrokdecode %s\n" % commit)
            if dest.exists():
                shutil.rmtree(dest)
            tmp.rename(dest)
            return count
        dest.mkdir(parents=True, exist_ok=True)
        for d in tmp.iterdir():
            target = dest / d.name
            if target.exists():
                shutil.rmtree(target)
            d.rename(target)
        (dest / "VERSION").write_text("libsigrokdecode %s\n" % commit)
        return count
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def fetch(commit: str) -> bytes:
    try:
        with urllib.request.urlopen(URL % commit, timeout=60) as resp:
            return resp.read()
    except OSError as e:
        raise InstallError("cannot download libsigrokdecode %s: %s" % (commit, e)) from None


def install(commit: Optional[str] = None, dest: Optional[Path] = None,
            names: Optional[List[str]] = None) -> tuple:
    commit = commit or DEFAULT_COMMIT
    dest = dest or decoders_dir()
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest, extract(fetch(commit), dest, commit, names)


class Entry(NamedTuple):
    name: str       # directory name, what -P takes
    longname: str
    desc: str


def describe(source: str, name: str) -> Entry:
    """Read longname/desc from a pd.py without executing it."""
    found = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return Entry(name, "", "")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Decoder":
            for stmt in node.body:
                if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                        and isinstance(stmt.targets[0], ast.Name)
                        and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)):
                    found[stmt.targets[0].id] = stmt.value.value
    return Entry(name, found.get("longname", ""), found.get("desc", ""))


def archive_entries(tar_bytes: bytes) -> List[Entry]:
    out = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for m in tar.getmembers():
            parts = PurePosixPath(m.name).parts
            if m.isfile() and len(parts) == 4 and parts[1] == "decoders" and parts[3] == "pd.py":
                with tar.extractfile(m) as f:
                    out.append(describe(f.read().decode("utf-8", "replace"), parts[2]))
    return sorted(out)


def installed_entries(path: Optional[Path] = None) -> List[Entry]:
    path = path or decoders_dir()
    if not path.is_dir():
        return []
    out = []
    for d in sorted(path.iterdir()):
        pd = d / "pd.py"
        if (d / "__init__.py").is_file() and pd.is_file():
            out.append(describe(pd.read_text("utf-8", "replace"), d.name))
    return out


def installed_version(path: Optional[Path] = None) -> Optional[str]:
    f = (path or decoders_dir()) / "VERSION"
    try:
        return f.read_text().split()[-1]
    except (OSError, IndexError):
        return None


def list_text(entries: List[Entry], title: str, installed: Optional[set] = None, loglevel: int = 2) -> str:
    lines = [title]
    for e in entries:
        mark = ""
        if installed is not None:
            mark = "%-10s " % ("installed" if e.name in installed else "-")
        lines.append("  %-20s %s%s" % (e.name, mark, e.longname))
        if loglevel >= 3 and e.desc:
            lines.append("  %-20s %s%s" % ("", " " * len(mark), e.desc))
    if len(lines) == 1:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"
