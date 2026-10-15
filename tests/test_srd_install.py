import io
import tarfile

import pytest

from la5016.srd import install

PD = '''
import sigrokdecode as srd
class Decoder(srd.Decoder):
    api_version = 3
    id = '%s'
    longname = 'Long %s'
    desc = 'Desc %s.'
'''


def archive(names, top="libsigrokdecode-abc"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(path, text):
            data = text.encode()
            info = tarfile.TarInfo("%s/%s" % (top, path))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        add("README", "x")
        add("decoders/common/__init__.py", "")
        add("decoders/common/srdhelper/__init__.py", "")
        for n in names:
            add("decoders/%s/__init__.py" % n, "from .pd import Decoder\n")
            add("decoders/%s/pd.py" % n, PD % (n, n, n))
    return buf.getvalue()


def test_full_install_replaces_tree(tmp_path):
    dest = tmp_path / "decoders"
    dest.mkdir()
    (dest / "stale").mkdir()
    assert install.extract(archive(["uart", "i2c"]), dest, "c1") == 2
    assert sorted(p.name for p in dest.iterdir()) == ["VERSION", "common", "i2c", "uart"]
    assert install.installed_version(dest) == "c1"
    assert [e.name for e in install.installed_entries(dest)] == ["i2c", "uart"]


def test_selective_install_adds_and_keeps_common(tmp_path):
    dest = tmp_path / "decoders"
    tar = archive(["uart", "i2c", "spi"])
    assert install.extract(tar, dest, "c1", ["uart"]) == 1
    assert sorted(p.name for p in dest.iterdir()) == ["VERSION", "common", "uart"]
    assert install.extract(tar, dest, "c1", ["spi"]) == 1
    assert [e.name for e in install.installed_entries(dest)] == ["spi", "uart"]


def test_selective_install_errors(tmp_path):
    dest = tmp_path / "decoders"
    install.extract(archive(["uart"]), dest, "c1")
    with pytest.raises(install.InstallError, match="not in libsigrokdecode"):
        install.extract(archive(["uart"]), dest, "c1", ["nosuch"])
    with pytest.raises(install.InstallError, match="reinstall all"):
        install.extract(archive(["uart"]), dest, "c2", ["uart"])


def test_unsafe_paths_rejected(tmp_path):
    with pytest.raises(install.InstallError, match="unsafe"):
        install.extract(archive(["uart"], top="x/decoders/.."), tmp_path / "d", "c1")


def test_archive_entries_and_list_text():
    entries = install.archive_entries(archive(["uart", "i2c"]))
    assert entries == [install.Entry("i2c", "Long i2c", "Desc i2c."), install.Entry("uart", "Long uart", "Desc uart.")]
    text = install.list_text(entries, "T:", {"uart"}, loglevel=3)
    assert text.splitlines() == [
        "T:",
        "  %-20s %-10s %s" % ("i2c", "-", "Long i2c"),
        "  %-20s %s%s" % ("", " " * 11, "Desc i2c."),
        "  %-20s %-10s %s" % ("uart", "installed", "Long uart"),
        "  %-20s %s%s" % ("", " " * 11, "Desc uart."),
    ]
    assert install.list_text([], "T:") == "T:\n  (none)\n"


def test_describe_never_executes():
    e = install.describe("import os\nos.system('false')\nclass Decoder:\n    longname = 'L'\n", "x")
    assert e == install.Entry("x", "L", "")
