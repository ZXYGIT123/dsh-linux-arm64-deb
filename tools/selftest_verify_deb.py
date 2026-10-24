#!/usr/bin/env python3
"""selftest_verify_deb.py - prove that verify_deb.py's checks actually fail.

The Debian packaging toolchain cannot run on a Windows workstation that has no
dpkg-deb, but the *verifier* can and must be testable there.  This script builds
synthetic .deb archives from first principles (Python tarfile + gzip/lzma + a
hand-written ar writer, plus optional ctypes libzstd) and asserts that:

  * a structurally correct package passes all checks (xz, gzip, plain and, when
    a zstd backend is available, zstd members; arm64 and amd64);
  * every intentionally broken package is rejected with a non-zero exit code
    and a failure message that names the broken property;
  * png_resize.py scales icons to exact dimensions.

Run it directly, no arguments required:

    python3 linux-deb/tools/selftest_verify_deb.py
    python3 linux-deb/tools/selftest_verify_deb.py --keep   # keep fixtures

Exit status is 0 only when every expectation holds.
"""

from __future__ import annotations

import argparse
import ctypes
import gzip
import hashlib
import lzma
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
VERIFY = os.path.join(HERE, "verify_deb.py")
PNG_RESIZE = os.path.join(HERE, "png_resize.py")
sys.path.insert(0, HERE)

import png_resize  # noqa: E402  (sibling module, stdlib-only)

PACKAGE = "deepseek-harness"
VERSION = "0.2.1~rc.3"
ARCH = "arm64"
EPOCH = 1700000000
MAINTAINER = "DeepSeek Harness Linux community build <dsh-linux@users.noreply.github.com>"
HOMEPAGE = "https://github.com/deepseek-ai/deepseek-harness"
DEPENDS = (
    "libasound2t64 | libasound2, libatk-bridge2.0-0t64 | libatk-bridge2.0-0, "
    "libatk1.0-0t64 | libatk1.0-0, libatspi2.0-0t64 | libatspi2.0-0, libcairo2, "
    "libcups2t64 | libcups2, libdbus-1-3, libdrm2, libexpat1, libfontconfig1, "
    "libfreetype6, libgbm1, libglib2.0-0t64 | libglib2.0-0, "
    "libgtk-3-0t64 | libgtk-3-0, libnspr4, libnss3, libpango-1.0-0, "
    "libsecret-1-0, libuuid1, libx11-6, libx11-xcb1, libxcb1, libxcb-dri3-0, "
    "libxcomposite1, libxdamage1, libxext6, libxfixes3, libxkbcommon0, "
    "libxrandr2, libxshmfence1, libxss1, libxtst6, libnotify4, xdg-utils"
)
RECOMMENDS = "fonts-noto-color-emoji, fonts-liberation, libvulkan1"

CLI_RELPATH = "resources/runtime/cli/bin/dsh"
CLI_TARGET = "/opt/%s/%s" % (PACKAGE, CLI_RELPATH)
SYMLINKS = {"usr/bin/dsh": CLI_TARGET}

ELF_MACHINE = {"arm64": 0xB7, "amd64": 0x3E}


# ---------------------------------------------------------------------------
# tiny helpers
# ---------------------------------------------------------------------------


class TestFailure(Exception):
    pass


def create_temp_dir(prefix):
    """Create a writable scratch directory (see the same helper in verify_deb.py)."""
    if os.name == "nt":
        base = tempfile.gettempdir()
        for _ in range(100):
            candidate = os.path.join(base, "%s%s" % (prefix, uuid.uuid4().hex[:12]))
            try:
                os.makedirs(candidate)
            except FileExistsError:
                continue
            return candidate
        raise TestFailure("cannot create a temporary directory under %s" % base)
    return tempfile.mkdtemp(prefix=prefix)


def fake_elf(machine, total_size=8192, e_type=3):
    endian = "<"
    ident = b"\x7fELF" + bytes((2, 1, 1, 0)) + b"\x00" * 8
    header = ident + struct.pack(
        endian + "HHIQQQIHHHHHH",
        e_type,  # e_type
        machine,  # e_machine
        1,  # e_version
        0,  # e_entry
        0,  # e_phoff
        0,  # e_shoff
        0,  # e_flags
        64,  # e_ehsize
        0,  # e_phentsize
        0,  # e_phnum
        0,  # e_shentsize
        0,  # e_shnum
        0,  # e_shstrndx
    )
    assert len(header) == 64
    return header + b"\x00" * max(0, total_size - len(header))


def solid_png(width, height, color=(32, 64, 128, 255)):
    pixel = bytes(color)
    return png_resize.encode_png_rgba(bytearray(pixel * (width * height)), width, height)


def gzip_bytes(data):
    return gzip.compress(data, compresslevel=9, mtime=0)


def xz_bytes(data):
    return lzma.compress(data, format=lzma.FORMAT_XZ, preset=6)


def _zstd_library():
    candidates = [
        os.environ.get("LIBZSTD"),
        "libzstd.dll",
        "libzstd.so.1",
        "libzstd.so",
        "libzstd.dylib",
    ]
    try:
        import glob as globmod

        home = os.path.expanduser("~")
        for pattern in (
            os.path.join(home, ".dsh", "dsh-ops", "bash-ipc", "*", "ucrt64", "bin", "libzstd.dll"),
            os.path.join(home, ".dsh", "dsh-ops", "bash-ipc", "*", "usr", "bin", "libzstd.dll"),
            os.path.join("C:\\", "msys64", "ucrt64", "bin", "libzstd.dll"),
        ):
            candidates.extend(sorted(globmod.glob(pattern)))
    except Exception:  # noqa: BLE001 - optional helper
        pass
    for candidate in candidates:
        if not candidate:
            continue
        try:
            library = ctypes.CDLL(candidate)
        except OSError:
            continue
        try:
            library.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
            library.ZSTD_compressBound.restype = ctypes.c_size_t
            library.ZSTD_compress.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_int,
            ]
            library.ZSTD_compress.restype = ctypes.c_size_t
            library.ZSTD_isError.argtypes = [ctypes.c_size_t]
            library.ZSTD_isError.restype = ctypes.c_uint
            library.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
            library.ZSTD_getErrorName.restype = ctypes.c_char_p
            return library
        except AttributeError:
            continue
    return None


def zstd_bytes(data):
    library = _zstd_library()
    if library is None:
        return None
    bound = library.ZSTD_compressBound(len(data))
    destination = ctypes.create_string_buffer(bound)
    source = ctypes.create_string_buffer(data, len(data))
    written = library.ZSTD_compress(
        ctypes.cast(destination, ctypes.c_void_p),
        bound,
        ctypes.cast(source, ctypes.c_void_p),
        len(data),
        9,
    )
    if library.ZSTD_isError(written):
        raise TestFailure("libzstd compression failed")
    return destination.raw[:written]


def compress_bytes(kind, data):
    if kind == "none":
        return data
    if kind == "gz":
        return gzip_bytes(data)
    if kind == "xz":
        return xz_bytes(data)
    if kind == "zst":
        compressed = zstd_bytes(data)
        if compressed is None:
            return None
        return compressed
    raise TestFailure("unknown compression %r" % kind)


def member_suffix(kind):
    return "" if kind == "none" else "." + kind


# ---------------------------------------------------------------------------
# synthetic package construction
# ---------------------------------------------------------------------------


def base_payload(arch=ARCH):
    """Return payload files: path -> (bytes, mode, uid, gid)."""
    machine = ELF_MACHINE[arch]
    desktop = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=DeepSeek Harness\n"
        "Comment=DeepSeek Harness desktop application\n"
        "Exec=/usr/bin/%s %%U\n"
        "TryExec=/usr/bin/%s\n"
        "Icon=%s\n"
        "Terminal=false\n"
        "StartupNotify=true\n"
        "StartupWMClass=%s\n"
        "Categories=Development;Utility;\n"
        "MimeType=x-scheme-handler/dsh;\n"
        % (PACKAGE, PACKAGE, PACKAGE, PACKAGE)
    )
    launcher = (
        "#!/bin/sh\n"
        "set -eu\n"
        "APP_DIR=\"/opt/%s\"\n"
        "cd \"$APP_DIR\"\n"
        "exec \"$APP_DIR/%s\" \"$@\"\n"
        % (PACKAGE, PACKAGE)
    )
    cli_launcher = (
        "#!/bin/sh\n"
        "# Synthetic copy of the bundled Linux dsh launcher.\n"
        "set -e\n"
        "launcher=$0\n"
        "while [ -L \"$launcher\" ]; do launcher=$(readlink \"$launcher\"); done\n"
        "resources=$(CDPATH= cd -- \"$(dirname -- \"$launcher\")/../../..\" && pwd -P)\n"
        "ELECTRON_RUN_AS_NODE=1 exec \"$resources/../deepseek-harness\" --expose-internals "
        "\"$resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js\" \"$@\"\n"
    )
    changelog = (
        "%s (%s) unstable; urgency=medium\n\n"
        "  * Synthetic fixture used to test verify_deb.py.\n\n"
        " -- %s  Tue, 14 Nov 2023 22:13:20 +0000\n"
        % (PACKAGE, VERSION, MAINTAINER)
    )
    payload = {
        "opt/%s/%s" % (PACKAGE, PACKAGE): (fake_elf(machine), 0o755, 0, 0),
        "opt/%s/chrome-sandbox" % PACKAGE: (fake_elf(machine, 4096, 2), 0o4755, 0, 0),
        "opt/%s/resources/app.asar" % PACKAGE: (b"ASAR" + b"\x00" * 4092, 0o644, 0, 0),
        "opt/%s/resources/runtime/primary-runtime/dependencies/node/bin/node"
        % PACKAGE: (fake_elf(machine, 4096), 0o755, 0, 0),
        "opt/%s/resources/runtime/cli/bin/dsh" % PACKAGE: (
            cli_launcher.encode(),
            0o755,
            0,
            0,
        ),
        "usr/bin/%s" % PACKAGE: (launcher.encode(), 0o755, 0, 0),
        "usr/share/applications/%s.desktop" % PACKAGE: (desktop.encode(), 0o644, 0, 0),
        "usr/share/icons/hicolor/256x256/apps/%s.png" % PACKAGE: (
            solid_png(256, 256),
            0o644,
            0,
            0,
        ),
        "usr/share/icons/hicolor/512x512/apps/%s.png" % PACKAGE: (
            solid_png(512, 512),
            0o644,
            0,
            0,
        ),
        "usr/share/doc/%s/copyright" % PACKAGE: (
            (
                "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
                "Upstream-Name: DeepSeek Harness\n"
                "Source: %s\n\n"
                "Files: *\n"
                "Copyright: DeepSeek and contributors\n"
                "License: other\n"
                " Synthetic test fixture.\n" % HOMEPAGE
            ).encode(),
            0o644,
            0,
            0,
        ),
        "usr/share/doc/%s/changelog.Debian.gz" % PACKAGE: (
            gzip_bytes(changelog.encode()),
            0o644,
            0,
            0,
        ),
    }
    return payload


def payload_dirs(payload):
    dirs = set()
    for path in payload:
        parts = path.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:index]))
    return sorted(dirs)


def control_text(payload, arch=ARCH, installed_size=None, overrides=None, drop=()):
    overrides = dict(overrides or {})
    if installed_size is None:
        total = sum(len(body) for body, _mode, _uid, _gid in payload.values())
        installed_size = (total + 1023) // 1024 + 8
    fields = {
        "Package": PACKAGE,
        "Version": VERSION,
        "Architecture": arch,
        "Maintainer": MAINTAINER,
        "Installed-Size": str(installed_size),
        "Depends": DEPENDS,
        "Recommends": RECOMMENDS,
        "Section": "devel",
        "Priority": "optional",
        "Homepage": HOMEPAGE,
        "Description": (
            "DeepSeek Harness desktop application\n"
            " DeepSeek Harness is the desktop shell for the DeepSeek Harness\n"
            " agent harness. This synthetic package exists only to exercise the\n"
            " structural verifier used by the real deb build."
        ),
    }
    fields.update(overrides)
    for name in drop:
        fields.pop(name, None)
    lines = []
    for name, value in fields.items():
        lines.append("%s: %s" % (name, value))
    return "\n".join(lines) + "\n"


def md5sums_text(payload):
    lines = []
    for path in sorted(payload):
        body = payload[path][0]
        lines.append("%s  %s" % (hashlib.md5(body).hexdigest(), path))
    return "\n".join(lines) + "\n"


POSTINST = (
    "#!/bin/sh\n"
    "set -e\n"
    "APP_DIR=\"/opt/%s\"\n"
    "if [ -f \"$APP_DIR/chrome-sandbox\" ]; then\n"
    "  chown root:root \"$APP_DIR/chrome-sandbox\" 2>/dev/null || true\n"
    "  chmod 4755 \"$APP_DIR/chrome-sandbox\" 2>/dev/null || true\n"
    "fi\n"
    "if command -v update-desktop-database >/dev/null 2>&1; then\n"
    "  update-desktop-database -q /usr/share/applications >/dev/null 2>&1 || true\n"
    "fi\n"
    "if command -v xdg-mime >/dev/null 2>&1; then\n"
    "  xdg-mime default %s.desktop x-scheme-handler/dsh >/dev/null 2>&1 || true\n"
    "fi\n"
    "exit 0\n" % (PACKAGE, PACKAGE)
)
PRERM = "#!/bin/sh\nset -e\nexit 0\n"
POSTRM = (
    "#!/bin/sh\n"
    "set -e\n"
    "if [ \"${1:-}\" = \"purge\" ]; then\n"
    "  rm -rf \"/opt/%s\" 2>/dev/null || true\n"
    "fi\n"
    "exit 0\n" % PACKAGE
)


def tar_bytes(entries):
    """Build a tar archive from [(name, type, body, mode, uid, gid, mtime)]."""
    import io

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, kind, body, mode, uid, gid, mtime in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.uid = uid
            info.gid = gid
            info.mtime = mtime
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = body.decode() if isinstance(body, bytes) else body
                archive.addfile(info)
            else:
                info.type = tarfile.REGTYPE
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def data_tar_bytes(payload, symlinks=None):
    symlinks = dict(symlinks or {})
    entries = []
    for directory in payload_dirs(payload):
        entries.append((directory, "dir", b"", 0o755, 0, 0, EPOCH))
    for path in sorted(payload):
        body, mode, uid, gid = payload[path]
        entries.append((path, "file", body, mode, uid, gid, EPOCH))
    for path in sorted(symlinks):
        entries.append((path, "sym", symlinks[path].encode(), 0o777, 0, 0, EPOCH))
    return tar_bytes(entries)


def control_tar_bytes(control, md5sums, maintainer_scripts=None, extra=None):
    maintainer_scripts = (
        {"postinst": POSTINST, "prerm": PRERM, "postrm": POSTRM}
        if maintainer_scripts is None
        else maintainer_scripts
    )
    entries = []
    files = {"control": control, "md5sums": md5sums}
    modes = {"control": 0o644, "md5sums": 0o644}
    files.update(maintainer_scripts)
    for name in maintainer_scripts:
        modes[name] = 0o755
    if extra:
        for name, body in extra.items():
            files[name] = body
            modes.setdefault(name, 0o644)
    for path in sorted(files):
        entries.append(
            (path, "file", files[path].encode() if isinstance(files[path], str) else files[path],
             modes[path], 0, 0, EPOCH)
        )
    return tar_bytes(entries)


def ar_bytes(members, magic=b"!<arch>\n"):
    output = bytearray(magic)
    for name, body in members:
        name_field = name.ljust(16).encode("ascii")
        header = (
            name_field
            + ("%d" % EPOCH).ljust(12).encode("ascii")
            + "0".ljust(6).encode("ascii")
            + "0".ljust(6).encode("ascii")
            + "100644".ljust(8).encode("ascii")
            + ("%d" % len(body)).ljust(10).encode("ascii")
            + b"`\n"
        )
        assert len(header) == 60, len(header)
        output.extend(header)
        output.extend(body)
        if len(body) % 2 == 1:
            output.extend(b"\n")
    return bytes(output)


def build_deb(
    directory,
    filename,
    compression="xz",
    arch=ARCH,
    drop_payload=(),
    payload_overrides=None,
    drop_symlinks=(),
    symlink_overrides=None,
    control_overrides=None,
    drop_control_fields=(),
    maintainer_scripts=None,
    extra_control_files=None,
    md5_mutator=None,
    ar_member_filter=None,
    ar_magic=b"!<arch>\n",
    truncate=0,
):
    payload = base_payload(arch)
    for path in drop_payload:
        payload.pop(path, None)
    for path, value in (payload_overrides or {}).items():
        payload[path] = value
    symlinks = dict(SYMLINKS)
    for path in drop_symlinks:
        symlinks.pop(path, None)
    symlinks.update(symlink_overrides or {})
    control = control_text(
        payload,
        arch=arch,
        overrides=control_overrides,
        drop=drop_control_fields,
    )
    md5sums = md5sums_text(payload)
    if md5_mutator is not None:
        md5sums = md5_mutator(md5sums)
    control_tar = control_tar_bytes(
        control, md5sums, maintainer_scripts=maintainer_scripts,
        extra=extra_control_files,
    )
    data_tar = data_tar_bytes(payload, symlinks)
    compressed_control = compress_bytes(compression, control_tar)
    compressed_data = compress_bytes(compression, data_tar)
    if compressed_control is None or compressed_data is None:
        return None
    suffix = member_suffix(compression)
    members = [
        ("debian-binary", b"2.0\n"),
        ("control.tar" + suffix, compressed_control),
        ("data.tar" + suffix, compressed_data),
    ]
    if ar_member_filter is not None:
        members = [member for member in members if ar_member_filter(member[0])]
    raw = ar_bytes(members, magic=ar_magic)
    if truncate:
        raw = raw[:truncate]
    path = os.path.join(directory, filename)
    with open(path, "wb") as handle:
        handle.write(raw)
    return path


# ---------------------------------------------------------------------------
# verification invocation
# ---------------------------------------------------------------------------


def run_verify(deb_path, extra_args=()):
    command = [sys.executable, VERIFY, deb_path] + list(extra_args)
    process = subprocess.run(command, capture_output=True, text=True)
    return process.returncode, process.stdout + process.stderr


class Results:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.details = []

    def ok(self, label):
        self.passed += 1
        print("PASS  %s" % label)

    def fail(self, label, detail):
        self.failed += 1
        print("FAIL  %s\n      %s" % (label, detail.replace("\n", "\n      ")))

    def skip(self, label, reason):
        self.skipped += 1
        print("SKIP  %s (%s)" % (label, reason))


def expect_pass(results, label, deb_path, extra_args=()):
    if deb_path is None:
        results.skip(label, "compression backend unavailable")
        return
    code, output = run_verify(deb_path, extra_args)
    if code == 0:
        results.ok(label)
    elif code == 2:
        results.skip(label, "verifier environment error: %s" % output.strip().splitlines()[-1])
    else:
        results.fail(label, "expected exit 0, got %d\n%s" % (code, output))


def expect_fail(results, label, deb_path, keyword):
    if deb_path is None:
        results.skip(label, "fixture could not be built")
        return
    code, output = run_verify(deb_path)
    if code == 1 and keyword.lower() in output.lower():
        results.ok("%s (rejected: %r)" % (label, keyword))
    elif code == 1:
        results.fail(label, "rejected but output does not mention %r\n%s" % (keyword, output))
    else:
        results.fail(label, "expected exit 1, got %d\n%s" % (code, output))


# ---------------------------------------------------------------------------
# test matrix
# ---------------------------------------------------------------------------


def run_tests(directory, keep=False):
    results = Results()
    counter = {"n": 0}

    def fixture(label, **kwargs):
        counter["n"] += 1
        filename = "case-%02d-%s.deb" % (counter["n"], label)
        return build_deb(directory, filename, **kwargs)

    # 1. structurally valid packages in every compression the runner supports.
    for compression in ("xz", "gz", "none", "zst"):
        expect_pass(
            results,
            "valid package with %s members" % compression,
            fixture("valid-%s" % compression, compression=compression),
        )

    # 2. cross-architecture check (amd64 fixture verified with --arch amd64).
    amd64_deb = fixture("valid-amd64", compression="xz", arch="amd64")
    expect_pass(results, "valid amd64 package (--arch amd64)", amd64_deb, ("--arch", "amd64"))
    expect_fail(results, "amd64 package rejected for --arch arm64", amd64_deb, "Architecture")

    # 3. metadata defects.
    expect_fail(
        results,
        "wrong Architecture field",
        fixture(
            "wrong-arch-field",
            control_overrides={"Architecture": "amd64"},
        ),
        "Architecture",
    )
    expect_fail(
        results,
        "missing Depends field",
        fixture("missing-depends", drop_control_fields=("Depends",)),
        "Depends",
    )
    expect_fail(
        results,
        "Depends without GTK runtime",
        fixture(
            "depends-without-gtk",
            control_overrides={"Depends": "libnss3, libgbm1"},
        ),
        "GTK",
    )
    expect_fail(
        results,
        "bogus Installed-Size",
        fixture("bad-installed-size", control_overrides={"Installed-Size": "1"}),
        "Installed-Size",
    )
    expect_fail(
        results,
        "missing Recommends field",
        fixture("missing-recommends", drop_control_fields=("Recommends",)),
        "Recommends",
    )

    # 4. payload defects.
    expect_fail(
        results,
        "missing 512px icon",
        fixture(
            "missing-512-icon",
            drop_payload=("usr/share/icons/hicolor/512x512/apps/%s.png" % PACKAGE,),
        ),
        "512x512",
    )
    expect_fail(
        results,
        "icon with wrong dimensions",
        fixture(
            "wrong-icon-size",
            payload_overrides={
                "usr/share/icons/hicolor/512x512/apps/%s.png" % PACKAGE: (
                    solid_png(256, 256), 0o644, 0, 0
                )
            },
        ),
        "512x512",
    )
    expect_fail(
        results,
        "main executable is x86-64",
        fixture(
            "x86-main",
            payload_overrides={
                "opt/%s/%s" % (PACKAGE, PACKAGE): (
                    fake_elf(0x3E), 0o755, 0, 0
                )
            },
        ),
        "ELF",
    )
    expect_fail(
        results,
        "chrome-sandbox is not setuid",
        fixture(
            "sandbox-not-setuid",
            payload_overrides={
                "opt/%s/chrome-sandbox" % PACKAGE: (
                    fake_elf(0xB7, 4096, 2), 0o755, 0, 0
                )
            },
        ),
        "4755",
    )
    expect_fail(
        results,
        "desktop Exec points at another binary",
        fixture(
            "desktop-exec-wrong",
            payload_overrides={
                "usr/share/applications/%s.desktop" % PACKAGE: (
                    base_payload()["usr/share/applications/%s.desktop" % PACKAGE][0]
                    .replace(b"Exec=/usr/bin/deepseek-harness", b"Exec=/usr/bin/other-app"),
                    0o644,
                    0,
                    0,
                )
            },
        ),
        "Exec",
    )
    expect_fail(
        results,
        "launcher is missing",
        fixture(
            "missing-launcher",
            drop_payload=("usr/bin/%s" % PACKAGE,),
        ),
        "launcher",
    )
    expect_fail(
        results,
        "dsh CLI launcher is missing from the payload",
        fixture(
            "missing-cli-launcher",
            drop_payload=("opt/%s/%s" % (PACKAGE, CLI_RELPATH),),
        ),
        "dsh",
    )
    expect_fail(
        results,
        "/usr/bin/dsh symlink is missing",
        fixture("missing-dsh-symlink", drop_symlinks=("usr/bin/dsh",)),
        "dsh",
    )
    expect_fail(
        results,
        "/usr/bin/dsh points at the wrong target",
        fixture(
            "wrong-dsh-symlink",
            symlink_overrides={
                "usr/bin/dsh": "/opt/%s/resources/runtime/other" % PACKAGE
            },
        ),
        "target",
    )

    # 5. maintainer script / md5sums defects.
    expect_fail(
        results,
        "postinst missing",
        fixture(
            "missing-postinst",
            maintainer_scripts={"prerm": PRERM, "postrm": POSTRM},
        ),
        "postinst",
    )
    expect_fail(
        results,
        "postinst without set -e",
        fixture(
            "postinst-no-set-e",
            maintainer_scripts={
                "postinst": "#!/bin/sh\nexit 0\n",
                "prerm": PRERM,
                "postrm": POSTRM,
            },
        ),
        "set -e",
    )
    expect_fail(
        results,
        "postinst without dsh:// handler registration",
        fixture(
            "postinst-no-xdg-mime",
            maintainer_scripts={
                "postinst": (
                    "#!/bin/sh\nset -e\n"
                    "chmod 4755 /opt/%s/chrome-sandbox 2>/dev/null || true\n"
                    "exit 0\n" % PACKAGE
                ),
                "prerm": PRERM,
                "postrm": POSTRM,
            },
        ),
        "xdg-mime",
    )

    def flip_digest(text):
        lines = text.splitlines()
        first = lines[0]
        digest, path = first[:32], first[34:]
        flipped = ("0" if digest[0] != "0" else "1") + digest[1:]
        lines[0] = "%s  %s" % (flipped, path)
        return "\n".join(lines) + "\n"

    expect_fail(
        results,
        "wrong md5sum digest",
        fixture("bad-md5", md5_mutator=flip_digest),
        "md5",
    )
    expect_fail(
        results,
        "md5sums entry missing",
        fixture(
            "md5-missing-entry",
            md5_mutator=lambda text: "\n".join(text.splitlines()[:-1]) + "\n",
        ),
        "md5",
    )

    # 6. container defects.
    expect_fail(
        results,
        "bad ar magic",
        fixture("bad-ar-magic", ar_magic=b"!<arch>X"),
        "magic",
    )
    expect_fail(
        results,
        "missing debian-binary member",
        fixture(
            "missing-debian-binary",
            ar_member_filter=lambda name: name != "debian-binary",
        ),
        "debian-binary",
    )
    expect_fail(
        results,
        "truncated ar member",
        fixture("truncated-ar", truncate=200),
        "truncated",
    )

    # 7. CLI behaviour.
    missing_path = os.path.join(directory, "does-not-exist.deb")
    code, _output = run_verify(missing_path)
    if code == 2:
        results.ok("missing input file exits with status 2")
    else:
        results.fail("missing input file exits with status 2", "got exit %d" % code)

    # 8. png_resize.py produces exact dimensions.
    source_png = os.path.join(directory, "source-512.png")
    with open(source_png, "wb") as handle:
        handle.write(solid_png(512, 512))
    resized_png = os.path.join(directory, "resized-256.png")
    process = subprocess.run(
        [sys.executable, PNG_RESIZE, source_png, resized_png, "256", "256"],
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        results.fail("png_resize.py resize", process.stdout + process.stderr)
    else:
        width, height, bit_depth, color_type, _interlace = png_resize.read_png_info(
            resized_png
        )
        if (width, height, bit_depth, color_type) == (256, 256, 8, 6):
            results.ok("png_resize.py produces a 256x256 RGBA PNG")
        else:
            results.fail(
                "png_resize.py produces a 256x256 RGBA PNG",
                "got %dx%d bit_depth=%d color_type=%d"
                % (width, height, bit_depth, color_type),
            )

    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep generated fixtures")
    args = parser.parse_args(argv)

    directory = create_temp_dir("dsh-selftest-deb-")
    print("selftest directory: %s" % directory)
    try:
        results = run_tests(directory, keep=args.keep)
    finally:
        if args.keep:
            print("kept fixtures in %s" % directory)
        else:
            shutil.rmtree(directory, ignore_errors=True)

    print("")
    print(
        "summary: %d passed, %d failed, %d skipped"
        % (results.passed, results.failed, results.skipped)
    )
    if results.failed:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
