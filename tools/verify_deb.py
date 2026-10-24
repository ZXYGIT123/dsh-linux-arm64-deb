#!/usr/bin/env python3
"""verify_deb.py - structural verifier for DeepSeek Harness Linux .deb packages.

Pure Python 3 standard library. It runs on Windows, macOS and Linux and needs
no third-party packages, no dpkg and no network access.

Everything is validated from the archive itself:

  * ar container: magic, 60-byte member headers, alignment, member order,
    ``debian-binary`` contents and the required control/data members;
  * ``control.tar.*``: RFC-822 control fields (Package/Version/Architecture/
    Depends/Recommends/Section/Priority/Maintainer/Homepage/Description/
    Installed-Size), Debian version syntax, dependency syntax, ``md5sums``
    coverage/digests and the maintainer scripts;
  * ``data.tar.*``: install layout, desktop entry, hicolor icons (real PNG
    dimensions), documentation files, ``md5sums`` coverage and digests,
    root ownership, setuid ``chrome-sandbox`` and ELF class/machine checks;
  * reproducibility hints (uniform member mtimes).

Compression support (``.deb`` members can be gzip, xz, zstd or uncompressed):

  * gzip / xz / none  - Python standard library (``gzip``, ``lzma``);
  * zstd              - Python >= 3.14 ``compression.zstd`` when available,
                        otherwise a ctypes binding to the system ``libzstd``
                        (``libzstd.dll`` / ``libzstd.so.1`` / ``libzstd.dylib``),
                        otherwise the ``zstd`` command line tool.

The zstd backends above are *optional accelerators*; nothing has to be pip
installed. If none of them is available the verifier exits with status 2 and
tells the caller how to proceed (rebuild with ``--compression xz``).

Exit codes:
    0  all checks passed
    1  the package violates the expected layout/metadata
    2  usage, I/O or missing decompression backend (environment error)

Usage:
    python3 verify_deb.py deepseek-harness_0.2.1~rc.3_arm64.deb
    python3 verify_deb.py --arch arm64 --json dist/*.deb
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import glob
import gzip
import hashlib
import json
import lzma
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import uuid

PROG = "verify_deb.py"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ENV = 2

ARP_MAGIC = b"!<arch>\n"
AR_HEADER_SIZE = 60
AR_HEADER_END = b"`\n"
DEBIAN_BINARY_CONTENT = b"2.0"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# e_machine values from the ELF spec.
ELF_MACHINES = {
    0x03: "i386",
    0x08: "mips",
    0x14: "ppc",
    0x28: "arm",
    0x2A: "sparc",
    0x3E: "amd64",
    0xB7: "arm64",
    0xF3: "riscv",
}

# Debian architecture -> (ELF class, ELF data encoding, e_machine, label).
EXPECTED_ELF = {
    "arm64": (2, 1, 0xB7, "AArch64"),
    "amd64": (2, 1, 0x3E, "x86-64"),
}

# Dependency clauses that must be present. Each tuple is a group of acceptable
# package names (24.04 t64 renames first, pre-24.04 names as the alternative).
REQUIRED_DEPENDENCY_GROUPS = (
    (("libgtk-3-0t64", "libgtk-3-0"), "GTK 3"),
    (("libnss3",), "NSS"),
    (("libasound2t64", "libasound2"), "ALSA"),
    (("libgbm1",), "GBM"),
    (("libxss1",), "XScreenSaver"),
    (("libxtst6",), "XTest"),
    (("libatk-bridge2.0-0t64", "libatk-bridge2.0-0"), "ATK bridge"),
    (("libdrm2",), "DRM"),
    (("libxkbcommon0",), "xkbcommon"),
    (("libsecret-1-0",), "libsecret"),
)

REQUIRED_CONTROL_FIELDS = (
    "Package",
    "Version",
    "Architecture",
    "Maintainer",
    "Depends",
    "Recommends",
    "Section",
    "Priority",
    "Description",
    "Homepage",
    "Installed-Size",
)

VALID_PRIORITIES = {"required", "important", "standard", "optional", "extra"}

COMPRESSION_MAGIC = {
    "gz": b"\x1f\x8b",
    "xz": b"\xfd7zXZ\x00",
    "zst": b"\x28\xb5\x2f\xfd",
}

CONTROL_MEMBER_RE = re.compile(r"^control\.tar(?:\.(gz|xz|zst))?$")
DATA_MEMBER_RE = re.compile(r"^data\.tar(?:\.(gz|xz|zst))?$")
CONTROL_MEMBER_NAMES = {
    "control",
    "md5sums",
    "preinst",
    "postinst",
    "prerm",
    "postrm",
    "conffiles",
    "triggers",
    "shlibs",
    "symbols",
    "templates",
    "config",
}
MAINTAINER_SCRIPT_NAMES = ("postinst", "prerm", "postrm")
DEBIAN_VERSION_RE = re.compile(
    r"^(?:[0-9]+:)?[0-9][A-Za-z0-9.+~]*(?:-[A-Za-z0-9+~.]+)?$"
)
PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
DEPENDENCY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9]*$")


# ---------------------------------------------------------------------------
# errors / reporting
# ---------------------------------------------------------------------------


class DebFormatError(Exception):
    """The .deb container or one of its members is structurally invalid."""


class EnvError(Exception):
    """The verifier cannot run (usage, I/O, missing decompressor)."""


class Report:
    """Collect check results and print them in a CI-friendly form."""

    def __init__(self, stream=None, quiet=False):
        self.stream = stream if stream is not None else sys.stdout
        self.quiet = quiet
        self.passed = 0
        self.warnings = 0
        self.failures = []

    def _print(self, text):
        print(text, file=self.stream)

    def section(self, title):
        self._print("")
        self._print("== %s ==" % title)

    def info(self, text):
        if not self.quiet:
            self._print("  info  %s" % text)

    def ok(self, text):
        self.passed += 1
        if not self.quiet:
            self._print("  PASS  %s" % text)

    def warn(self, text):
        self.warnings += 1
        self._print("  WARN  %s" % text)

    def fail(self, check, detail=""):
        self.failures.append((check, detail))
        suffix = ": %s" % detail if detail else ""
        self._print("  FAIL  %s%s" % (check, suffix))

    @property
    def ok_result(self):
        return not self.failures


# ---------------------------------------------------------------------------
# ar container
# ---------------------------------------------------------------------------


class ArMember:
    __slots__ = ("name", "offset", "size", "mode", "uid", "gid", "mtime")

    def __init__(self, name, offset, size, mode, uid, gid, mtime):
        self.name = name
        self.offset = offset
        self.size = size
        self.mode = mode
        self.uid = uid
        self.gid = gid
        self.mtime = mtime

    def __repr__(self):  # pragma: no cover - debugging aid
        return "ArMember(%r, size=%d)" % (self.name, self.size)


def _parse_ar_number(raw, field, default=0):
    value = raw.decode("ascii", "replace").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise DebFormatError("ar member field %r is not numeric: %r" % (field, value))


def read_ar_members(deb_path):
    """Parse the ar archive and return its member descriptors.

    Only headers are read here; payloads stay on disk and are streamed later so
    that multi-hundred-megabyte Electron payloads never have to fit in memory.
    """
    members = []
    total = os.path.getsize(deb_path)
    with open(deb_path, "rb") as handle:
        magic = handle.read(8)
        if magic != ARP_MAGIC:
            raise DebFormatError(
                "not an ar archive (bad magic %r, expected %r)" % (magic, ARP_MAGIC)
            )
        position = 8
        while position < total:
            handle.seek(position)
            header = handle.read(AR_HEADER_SIZE)
            if len(header) < AR_HEADER_SIZE:
                raise DebFormatError(
                    "truncated ar member header at offset %d" % position
                )
            if header[58:60] != AR_HEADER_END:
                raise DebFormatError(
                    "invalid ar member header terminator at offset %d (%r)"
                    % (position, header[58:60])
                )
            name = header[0:16].decode("utf-8", "replace").strip()
            mtime = _parse_ar_number(header[16:28], "mtime")
            uid = _parse_ar_number(header[28:34], "uid")
            gid = _parse_ar_number(header[34:40], "gid")
            mode = _parse_ar_number(header[40:48], "mode")
            size = _parse_ar_number(header[48:58], "size")
            data_offset = position + AR_HEADER_SIZE
            if size < 0:
                raise DebFormatError("negative ar member size for %r" % name)
            if name.startswith("#1/"):
                # GNU ar long-name extension: the name is stored in the data.
                try:
                    name_length = int(name[3:])
                except ValueError:
                    raise DebFormatError("invalid GNU ar long name %r" % name)
                handle.seek(data_offset)
                long_name = handle.read(name_length)
                if len(long_name) < name_length:
                    raise DebFormatError("truncated GNU ar long name %r" % name)
                name = long_name.rstrip(b"\x00").decode("utf-8", "replace")
                data_offset += name_length
                size -= name_length
            if name == "//" or name == "/":
                raise DebFormatError("GNU ar symbol tables are not valid in a .deb")
            if size < 0 or data_offset + size > total:
                raise DebFormatError(
                    "ar member %r claims %d bytes but the file ends at %d"
                    % (name, size, total)
                )
            members.append(ArMember(name, data_offset, size, mode, uid, gid, mtime))
            position = data_offset + size
            if size % 2 == 1:
                position += 1
            if position > total:
                raise DebFormatError("ar archive has a truncated padding byte")
    if total < 8 + AR_HEADER_SIZE:
        raise DebFormatError("file is too small to be a .deb archive")
    return members


def read_member_bytes(deb_path, member):
    with open(deb_path, "rb") as handle:
        handle.seek(member.offset)
        return handle.read(member.size)


def copy_member_bytes(deb_path, member, destination):
    remaining = member.size
    with open(deb_path, "rb") as source, open(destination, "wb") as sink:
        source.seek(member.offset)
        while remaining > 0:
            chunk = source.read(min(1 << 20, remaining))
            if not chunk:
                raise DebFormatError(
                    "unexpected end of file while copying ar member %r" % member.name
                )
            sink.write(chunk)
            remaining -= len(chunk)
    return destination


# ---------------------------------------------------------------------------
# zstd backends (stdlib -> ctypes libzstd -> zstd CLI)
# ---------------------------------------------------------------------------


class _ZstdInBuffer(ctypes.Structure):
    _fields_ = [
        ("src", ctypes.c_void_p),
        ("size", ctypes.c_size_t),
        ("pos", ctypes.c_size_t),
    ]


class _ZstdOutBuffer(ctypes.Structure):
    _fields_ = [
        ("dst", ctypes.c_void_p),
        ("size", ctypes.c_size_t),
        ("pos", ctypes.c_size_t),
    ]


class _StdlibZstd:
    name = "python compression.zstd"

    def __init__(self, module):
        self.module = module

    def decompress_file(self, source, destination):
        zstd_file = getattr(self.module, "ZstdFile", None)
        if zstd_file is not None:
            with zstd_file(source, "rb") as src, open(destination, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            return
        with open(source, "rb") as src:
            data = src.read()
        with open(destination, "wb") as out:
            out.write(self.module.decompress(data))


class _CtypesZstd:
    name = "ctypes libzstd"

    def __init__(self, library, library_path):
        self.library = library
        self.library_path = library_path
        self.name = "ctypes libzstd (%s)" % library_path
        lib = library
        lib.ZSTD_createDStream.restype = ctypes.c_void_p
        lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
        lib.ZSTD_freeDStream.restype = ctypes.c_size_t
        lib.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
        lib.ZSTD_initDStream.restype = ctypes.c_size_t
        lib.ZSTD_decompressStream.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ZstdOutBuffer),
            ctypes.POINTER(_ZstdInBuffer),
        ]
        lib.ZSTD_decompressStream.restype = ctypes.c_size_t
        lib.ZSTD_DStreamOutSize.restype = ctypes.c_size_t
        lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
        lib.ZSTD_getErrorName.restype = ctypes.c_char_p
        lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        lib.ZSTD_isError.restype = ctypes.c_uint

    def _check(self, code):
        if self.library.ZSTD_isError(code):
            message = self.library.ZSTD_getErrorName(code)
            raise EnvError(
                "libzstd decompression error: %s"
                % message.decode("utf-8", "replace")
            )

    def decompress_file(self, source, destination, chunk=1 << 20):
        stream = self.library.ZSTD_createDStream()
        if not stream:
            raise EnvError("libzstd: ZSTD_createDStream failed")
        self._check(self.library.ZSTD_initDStream(stream))
        out_size = int(self.library.ZSTD_DStreamOutSize())
        out_buffer = ctypes.create_string_buffer(out_size)
        written = 0
        try:
            with open(source, "rb") as src, open(destination, "wb") as out:
                data = src.read(chunk)
                while True:
                    in_buffer = _ZstdInBuffer(
                        ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p),
                        len(data),
                        0,
                    )
                    while in_buffer.pos < in_buffer.size:
                        out_struct = _ZstdOutBuffer(
                            ctypes.cast(out_buffer, ctypes.c_void_p), out_size, 0
                        )
                        code = self.library.ZSTD_decompressStream(
                            stream,
                            ctypes.byref(out_struct),
                            ctypes.byref(in_buffer),
                        )
                        self._check(code)
                        if out_struct.pos:
                            out.write(out_buffer.raw[: out_struct.pos])
                            written += out_struct.pos
                    data = src.read(chunk)
                    if not data:
                        break
        finally:
            self.library.ZSTD_freeDStream(stream)
        return written


class _CliZstd:
    name = "zstd command line tool"

    def __init__(self, executable):
        self.executable = executable
        self.name = "zstd command line tool (%s)" % executable

    def decompress_file(self, source, destination):
        with open(destination, "wb") as out:
            process = subprocess.run(
                [self.executable, "-d", "-q", "-c", source],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.PIPE,
            )
        if process.returncode != 0:
            message = process.stderr.decode("utf-8", "replace").strip()
            raise EnvError("zstd CLI failed (%d): %s" % (process.returncode, message))


def _zstd_library_candidates():
    candidates = []
    override = os.environ.get("LIBZSTD")
    if override:
        candidates.append(override)
    if sys.platform.startswith("win"):
        home = os.path.expanduser("~")
        patterns = (
            os.path.join(
                home, ".dsh", "dsh-ops", "bash-ipc", "*", "ucrt64", "bin", "libzstd.dll"
            ),
            os.path.join(
                home, ".dsh", "dsh-ops", "bash-ipc", "*", "usr", "bin", "libzstd.dll"
            ),
            os.path.join(home, ".dsh", "dsh-ops", "bash-ipc", "*", "bin", "libzstd.dll"),
            os.path.join("C:\\", "msys64", "ucrt64", "bin", "libzstd.dll"),
            os.path.join("C:\\", "Program Files", "Git", "usr", "bin", "libzstd.dll"),
        )
        for pattern in patterns:
            for path in sorted(glob.glob(pattern)):
                candidates.append(path)
        candidates.extend(["libzstd.dll", "zstd.dll"])
    else:
        found = ctypes.util.find_library("zstd")
        if found:
            candidates.append(found)
        if sys.platform == "darwin":
            candidates.extend(
                [
                    "libzstd.dylib",
                    "/opt/homebrew/lib/libzstd.dylib",
                    "/usr/local/lib/libzstd.dylib",
                ]
            )
        else:
            candidates.extend(["libzstd.so.1", "libzstd.so"])
    deduped = []
    for candidate in candidates:
        if candidate and candidate not in deduped:
            deduped.append(candidate)
    return deduped


_ZSTD_BACKEND = None
_ZSTD_BACKEND_TRIED = False
_ZSTD_BACKEND_ERRORS = []


def zstd_backend():
    """Return an object with ``decompress_file`` or raise EnvError."""
    global _ZSTD_BACKEND, _ZSTD_BACKEND_TRIED
    if _ZSTD_BACKEND is not None:
        return _ZSTD_BACKEND
    if _ZSTD_BACKEND_TRIED:
        raise EnvError(_zstd_error_message())
    _ZSTD_BACKEND_TRIED = True

    try:  # Python 3.14+
        from compression import zstd as stdlib_zstd  # type: ignore

        _ZSTD_BACKEND = _StdlibZstd(stdlib_zstd)
        return _ZSTD_BACKEND
    except Exception as exc:  # noqa: BLE001 - optional backend
        _ZSTD_BACKEND_ERRORS.append("stdlib compression.zstd: %s" % exc)

    for candidate in _zstd_library_candidates():
        try:
            library = ctypes.CDLL(candidate)
            _ZSTD_BACKEND = _CtypesZstd(library, candidate)
            return _ZSTD_BACKEND
        except OSError as exc:
            _ZSTD_BACKEND_ERRORS.append("%s: %s" % (candidate, exc))

    executable = shutil.which("zstd") or shutil.which("unzstd")
    if executable:
        _ZSTD_BACKEND = _CliZstd(executable)
        return _ZSTD_BACKEND
    _ZSTD_BACKEND_ERRORS.append("zstd CLI not found on PATH")
    raise EnvError(_zstd_error_message())


def _zstd_error_message():
    return (
        "cannot decompress zstd members: no zstd backend available "
        "(need Python >= 3.14, a system libzstd, or the zstd CLI). "
        "Details: %s. Rebuild the package with --compression xz to verify it "
        "without a zstd backend." % "; ".join(_ZSTD_BACKEND_ERRORS)
    )


# ---------------------------------------------------------------------------
# member extraction
# ---------------------------------------------------------------------------


def compression_extension(member_name):
    for extension in ("gz", "xz", "zst"):
        if member_name.endswith("." + extension):
            return extension
    return ""


def extract_member_to_file(deb_path, member, destination):
    """Stream one compressed ar member into an uncompressed file."""
    extension = compression_extension(member.name)
    raw_path = destination + ".raw"
    copy_member_bytes(deb_path, member, raw_path)
    try:
        with open(raw_path, "rb") as handle:
            prefix = handle.read(8)
        expected = COMPRESSION_MAGIC.get(extension)
        if expected is not None and not prefix.startswith(expected):
            raise DebFormatError(
                "%s: compression magic %r does not match extension .%s"
                % (member.name, prefix[:6], extension)
            )
        if extension == "gz":
            with gzip.open(raw_path, "rb") as source, open(destination, "wb") as sink:
                shutil.copyfileobj(source, sink, 1 << 20)
        elif extension == "xz":
            with lzma.open(raw_path, "rb") as source, open(destination, "wb") as sink:
                shutil.copyfileobj(source, sink, 1 << 20)
        elif extension == "zst":
            zstd_backend().decompress_file(raw_path, destination)
        elif extension == "":
            os.replace(raw_path, destination)
            raw_path = None
        else:  # pragma: no cover - guarded by the member regexes
            raise DebFormatError("unsupported compression .%s" % extension)
    finally:
        if raw_path is not None and os.path.exists(raw_path):
            os.unlink(raw_path)
    return destination


# ---------------------------------------------------------------------------
# control file / desktop file parsing
# ---------------------------------------------------------------------------


def parse_control_file(text):
    """Parse an RFC-822 style control file into an ordered mapping."""
    fields = {}
    order = []
    current = None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line[0] in " \t":
            if current is None:
                raise DebFormatError(
                    "control line %d starts with whitespace before any field" % number
                )
            fields[current] += "\n" + line
            continue
        if ":" not in line:
            raise DebFormatError("control line %d is not a field: %r" % (number, line))
        name, value = line.split(":", 1)
        name = name.strip()
        if not FIELD_NAME_RE.match(name):
            raise DebFormatError(
                "control line %d has an invalid field name: %r" % (number, name)
            )
        if name in fields:
            raise DebFormatError("control field %r appears more than once" % name)
        fields[name] = value.strip()
        order.append(name)
        current = name
    return fields


def parse_desktop_file(text):
    """Minimal Desktop Entry parser: returns {group: {key: value}}."""
    groups = {}
    current_group = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_group = line[1:-1]
            groups.setdefault(current_group, {})
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if current_group is not None:
            groups[current_group][key.strip()] = value.strip()
    return groups


def split_dependencies(value):
    """Return dependency groups as lists of alternative package names."""
    groups = []
    for clause in value.split(","):
        clause = clause.strip()
        if not clause:
            raise DebFormatError("Depends contains an empty clause")
        alternatives = []
        for alternative in clause.split("|"):
            alternative = alternative.strip()
            if not alternative:
                raise DebFormatError("Depends contains an empty alternative")
            token = alternative.split()[0]
            name = token.split(":", 1)[0]
            if not DEPENDENCY_NAME_RE.match(name):
                raise DebFormatError("invalid dependency name %r" % token)
            alternatives.append(name)
        groups.append(alternatives)
    return groups


def dependency_names(groups):
    names = set()
    for alternatives in groups:
        names.update(alternatives)
    return names


def png_dimensions(data):
    if len(data) < 33 or not data.startswith(PNG_SIGNATURE):
        return None
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack_from(">II", data, 16)
    bit_depth = data[24]
    color_type = data[25]
    interlace = data[28]
    return width, height, bit_depth, color_type, interlace


def parse_elf_header(header):
    if len(header) < 20 or header[:4] != b"\x7fELF":
        return None
    elf_class = header[4]
    elf_data = header[5]
    endian = "<" if elf_data == 1 else ">"
    e_type = struct.unpack_from(endian + "H", header, 16)[0]
    e_machine = struct.unpack_from(endian + "H", header, 18)[0]
    return {
        "class": elf_class,
        "data": elf_data,
        "type": e_type,
        "machine": e_machine,
        "machine_name": ELF_MACHINES.get(e_machine, hex(e_machine)),
    }


# ---------------------------------------------------------------------------
# data.tar scan
# ---------------------------------------------------------------------------


def create_temp_dir(prefix):
    """Create a writable scratch directory.

    ``tempfile.mkdtemp`` uses mode 0700. On Windows that translates to an
    owner-only DACL, which a sandboxed (restricted-token) Python process may not
    be able to add files to, so on Windows use the inherited-directory-ACL
    behaviour of ``os.makedirs`` instead. POSIX systems keep mkdtemp's secure
    0700 permissions.
    """
    if os.name == "nt":
        base = tempfile.gettempdir()
        for _ in range(100):
            candidate = os.path.join(base, "%s%s" % (prefix, uuid.uuid4().hex[:12]))
            try:
                os.makedirs(candidate)
            except FileExistsError:
                continue
            return candidate
        raise EnvError("cannot create a temporary directory under %s" % base)
    return tempfile.mkdtemp(prefix=prefix)


class PayloadEntry:
    __slots__ = ("name", "size", "mode", "uid", "gid", "mtime", "md5", "head", "content")

    def __init__(self, name, size, mode, uid, gid, mtime, md5, head, content):
        self.name = name
        self.size = size
        self.mode = mode
        self.uid = uid
        self.gid = gid
        self.mtime = mtime
        self.md5 = md5
        self.head = head
        self.content = content


def normalize_tar_name(name):
    normalized = name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


def scan_payload_tar(tar_path, wanted_paths, report):
    """Scan data.tar and return (files, dirs, symlinks)."""
    files = {}
    dirs = set()
    symlinks = {}
    wanted = set(wanted_paths)
    try:
        with tarfile.open(tar_path, "r:") as archive:
            for member in archive:
                name = normalize_tar_name(member.name)
                if not name or name == ".":
                    continue
                if name.startswith("/") or ".." in name.split("/"):
                    report.fail("data.tar: unsafe path", member.name)
                    continue
                if member.isdir():
                    dirs.add(name)
                    continue
                if member.issym():
                    symlinks[name] = member.linkname
                    continue
                if member.islnk():
                    report.fail(
                        "data.tar: hardlink members are not supported", member.name
                    )
                    continue
                if not member.isreg():
                    report.fail(
                        "data.tar: unsupported member type (type=%r)"
                        % (member.type,),
                        member.name,
                    )
                    continue
                handle = archive.extractfile(member)
                if handle is None:  # pragma: no cover - defensive
                    report.fail("data.tar: unable to read member", member.name)
                    continue
                digest = hashlib.md5()
                head = b""
                content = b"" if name in wanted else None
                while True:
                    chunk = handle.read(1 << 20)
                    if not chunk:
                        break
                    if len(head) < 64:
                        head += chunk[: 64 - len(head)]
                    digest.update(chunk)
                    if content is not None:
                        content += chunk
                files[name] = PayloadEntry(
                    name=name,
                    size=member.size,
                    mode=stat.S_IMODE(member.mode),
                    uid=member.uid,
                    gid=member.gid,
                    mtime=member.mtime,
                    md5=digest.hexdigest(),
                    head=head,
                    content=content,
                )
    except tarfile.TarError as exc:
        raise DebFormatError("data.tar is not a readable tar archive: %s" % exc)
    return files, dirs, symlinks


def scan_control_tar(tar_path, report):
    """Read every regular file of control.tar into memory."""
    entries = {}
    try:
        with tarfile.open(tar_path, "r:") as archive:
            for member in archive:
                name = normalize_tar_name(member.name)
                if not name or member.isdir():
                    continue
                if not member.isreg():
                    report.fail(
                        "control.tar: unsupported member type (type=%r)"
                        % (member.type,),
                        name,
                    )
                    continue
                handle = archive.extractfile(member)
                if handle is None:  # pragma: no cover - defensive
                    report.fail("control.tar: unable to read member", name)
                    continue
                entries[name] = {
                    "mode": stat.S_IMODE(member.mode),
                    "uid": member.uid,
                    "gid": member.gid,
                    "mtime": member.mtime,
                    "data": handle.read(),
                }
    except tarfile.TarError as exc:
        raise DebFormatError("control.tar is not a readable tar archive: %s" % exc)
    return entries


# ---------------------------------------------------------------------------
# verification steps
# ---------------------------------------------------------------------------


def check_ar_structure(deb_path, members, report):
    """Validate member order/content; return (control_member, data_member)."""
    report.section("ar container")
    names = [member.name for member in members]
    report.info("members: " + ", ".join(names))

    if not members:
        report.fail("ar: archive has no members")
        return None, None

    debian_binary = members[0]
    if debian_binary.name != "debian-binary":
        report.fail(
            "ar: debian-binary must be the first member",
            "first member is %r" % debian_binary.name,
        )
        return None, None
    if debian_binary.size > 32:
        report.fail(
            "ar: debian-binary is suspiciously large",
            "%d bytes" % debian_binary.size,
        )
    else:
        binary = read_member_bytes(deb_path, debian_binary).strip()
        if binary != DEBIAN_BINARY_CONTENT:
            report.fail(
                "ar: debian-binary must contain 2.0", "found %r" % binary
            )
        else:
            report.ok("ar: debian-binary contains 2.0")

    control_index = None
    data_index = None
    for index, member in enumerate(members):
        if index == 0:
            continue  # debian-binary, validated above
        if CONTROL_MEMBER_RE.match(member.name):
            if control_index is not None:
                report.fail("ar: more than one control member", member.name)
            else:
                control_index = index
            if not control_index == 1:
                report.fail(
                    "ar: control member must be the second member",
                    "found at index %d" % index,
                )
            continue
        if DATA_MEMBER_RE.match(member.name):
            if data_index is not None:
                report.fail("ar: more than one data member", member.name)
            else:
                data_index = index
            continue
        if member.name.startswith("_gpg"):
            report.info("ar: optional signature member %r" % member.name)
            continue
        report.fail("ar: unexpected member", member.name)

    if control_index is None or data_index is None:
        missing = []
        if control_index is None:
            missing.append("control.tar.*")
        if data_index is None:
            missing.append("data.tar.*")
        report.fail("ar: required member missing", ", ".join(missing))
        return None, None

    if not control_index == 1:
        report.fail(
            "ar: control.tar must come directly after debian-binary",
            "control index %d" % control_index,
        )
    if not data_index > control_index:
        report.fail(
            "ar: data.tar must come after control.tar",
            "data index %d, control index %d" % (data_index, control_index),
        )
    control_member = members[control_index]
    data_member = members[data_index]
    report.ok("ar: 3 standard members in debian order")
    for member in members:
        alignment = "even" if member.size % 2 == 0 else "padded"
        report.info(
            "%s: %d bytes, %s" % (member.name, member.size, alignment)
        )
        if member.uid not in (0, 1000) or member.gid not in (0, 1000):
            report.warn(
                "ar: member %r has odd ownership %d:%d"
                % (member.name, member.uid, member.gid)
            )
    return control_member, data_member


def check_control_fields(control_text, args, report):
    """Validate the control file; return parsed fields (or None)."""
    report.section("control")
    preview_fields = ("Package", "Version", "Architecture", "Maintainer", "Installed-Size")
    preview = []
    for name in preview_fields:
        if name in control_text:
            for line in control_text.splitlines():
                if line.startswith(name + ":"):
                    preview.append(line)
                    break
    for line in preview:
        report.info(line)
    report.info("control file: %d bytes" % len(control_text))
    try:
        fields = parse_control_file(control_text)
    except DebFormatError as exc:
        report.fail("control: cannot parse control file", str(exc))
        return None

    missing = [name for name in REQUIRED_CONTROL_FIELDS if name not in fields]
    if missing:
        report.fail("control: required fields missing", ", ".join(missing))
    else:
        report.ok("control: all required fields present")

    if fields.get("Package") == args.package:
        report.ok("control: Package is %s" % args.package)
    elif "Package" in fields:
        report.fail(
            "control: Package is %r, expected %r"
            % (fields["Package"], args.package)
        )

    if "Architecture" in fields:
        if fields["Architecture"] == args.arch:
            report.ok("control: Architecture is %s" % args.arch)
        else:
            report.fail(
                "control: Architecture is %r, expected %r"
                % (fields["Architecture"], args.arch)
            )
        if fields["Architecture"] in ("all", "any"):
            report.fail(
                "control: Architecture must be a concrete architecture",
                fields["Architecture"],
            )

    if "Version" in fields:
        version = fields["Version"]
        if DEBIAN_VERSION_RE.match(version):
            report.ok("control: Version %s is valid Debian syntax" % version)
        else:
            report.fail("control: Version is not valid Debian syntax", version)

    if "Maintainer" in fields:
        if re.match(r"^[^<>]+<[^<>@\s]+@[^<>\s]+>$", fields["Maintainer"].strip()):
            report.ok("control: Maintainer has an e-mail address")
        else:
            report.warn("control: Maintainer is not in 'Name <email>' form")

    if "Homepage" in fields:
        if fields["Homepage"].startswith(("http://", "https://")):
            report.ok("control: Homepage is an HTTP(S) URL")
        else:
            report.warn("control: Homepage is not an HTTP(S) URL")

    if "Section" in fields:
        if re.match(r"^[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)?$", fields["Section"]):
            report.ok("control: Section is %s" % fields["Section"])
        else:
            report.warn("control: unusual Section %r" % fields["Section"])

    if "Priority" in fields:
        if fields["Priority"] in VALID_PRIORITIES:
            report.ok("control: Priority is %s" % fields["Priority"])
        else:
            report.fail(
                "control: unknown Priority %r" % fields["Priority"]
            )

    if "Depends" in fields:
        depends = fields["Depends"].strip()
        if not depends:
            report.fail("control: Depends is empty")
        else:
            try:
                groups = split_dependencies(depends)
            except DebFormatError as exc:
                report.fail("control: Depends is malformed", str(exc))
                groups = []
            names = dependency_names(groups)
            report.info("control: %d dependency groups" % len(groups))
            if groups:
                report.ok("control: Depends is non-empty and syntactically valid")
            missing_groups = []
            for alternatives, label in REQUIRED_DEPENDENCY_GROUPS:
                if not names.intersection(alternatives):
                    missing_groups.append("%s (%s)" % (label, " | ".join(alternatives)))
            if missing_groups:
                report.fail(
                    "control: Depends misses Electron runtime libraries",
                    "; ".join(missing_groups),
                )
            else:
                report.ok(
                    "control: Depends covers the Electron runtime library set"
                )
            if "libgtk-3-0t64" in names and "libgtk-3-0" in names:
                report.info("control: both pre/post t64 GTK names are listed")
            if any(name.startswith("libc6") for name in names):
                report.info("control: explicit libc6 dependency present")
    else:
        report.fail("control: Depends field is missing")

    if "Recommends" in fields:
        try:
            split_dependencies(fields["Recommends"])
            report.ok("control: Recommends is syntactically valid")
        except DebFormatError as exc:
            report.fail("control: Recommends is malformed", str(exc))
    else:
        report.fail("control: Recommends field is missing")

    if "Installed-Size" in fields:
        raw_size = fields["Installed-Size"].strip()
        if not raw_size.isdigit() or int(raw_size) <= 0:
            report.fail("control: Installed-Size is not a positive integer", raw_size)
        else:
            report.ok("control: Installed-Size is %s KiB" % raw_size)

    if "Description" in fields:
        description = fields["Description"]
        synopsis = description.splitlines()[0].strip()
        if not synopsis:
            report.fail("control: Description has an empty synopsis")
        elif len(synopsis) > 82:
            report.warn(
                "control: Description synopsis is very long (%d chars)"
                % len(synopsis)
            )
        else:
            report.ok("control: Description synopsis is present")
        if "\n " not in description:
            report.warn("control: Description has no extended description")
        else:
            report.ok("control: Description has an extended description")
    return fields


def check_md5sums(md5_text, payload_files, report):
    report.section("md5sums")
    expected = {}
    parse_errors = 0
    for number, line in enumerate(md5_text.splitlines(), 1):
        if not line.strip():
            continue
        if len(line) < 35 or line[32:34] != "  ":
            report.fail("md5sums: malformed line", "line %d: %r" % (number, line))
            parse_errors += 1
            continue
        digest = line[:32]
        path = line[34:]
        if not re.match(r"^[0-9a-f]{32}$", digest):
            report.fail("md5sums: malformed digest", "line %d: %r" % (number, digest))
            parse_errors += 1
            continue
        if path.startswith("/") or path.startswith("./") or ".." in path.split("/"):
            report.fail("md5sums: non-canonical path", path)
            parse_errors += 1
            continue
        if path in expected:
            report.fail("md5sums: duplicate path", path)
            parse_errors += 1
            continue
        expected[path] = digest
    if parse_errors == 0:
        report.ok("md5sums: format is valid (%d entries)" % len(expected))

    wrong = []
    for path in sorted(expected):
        entry = payload_files.get(path)
        if entry is None:
            wrong.append("missing from data.tar: %s" % path)
            continue
        if entry.md5 != expected[path]:
            wrong.append(
                "digest mismatch: %s (%s != %s)"
                % (path, entry.md5, expected[path])
            )
    if wrong:
        for detail in wrong[:20]:
            report.fail("md5sums: %s" % detail)
        if len(wrong) > 20:
            report.fail("md5sums: %d further problems" % (len(wrong) - 20))
    elif expected:
        report.ok("md5sums: every listed digest matches data.tar")

    uncovered = sorted(set(payload_files) - set(expected))
    if uncovered:
        for path in uncovered[:20]:
            report.fail("md5sums: data.tar file not covered", path)
        if len(uncovered) > 20:
            report.fail("md5sums: %d further uncovered files" % (len(uncovered) - 20))
    else:
        report.ok("md5sums: covers all %d regular payload files" % len(payload_files))
    return expected


def check_maintainer_scripts(control_entries, report):
    report.section("maintainer scripts")
    for name in MAINTAINER_SCRIPT_NAMES:
        entry = control_entries.get(name)
        if entry is None:
            report.fail("maintainer scripts: %s is missing" % name)
            continue
        text = entry["data"].decode("utf-8", "replace")
        first_line = text.splitlines()[0] if text.splitlines() else ""
        if not first_line.startswith("#!"):
            report.fail("maintainer scripts: %s has no shebang" % name)
        elif not first_line.split()[0].endswith(("/sh", "bash")):
            report.warn("maintainer scripts: %s uses %r" % (name, first_line))
        if not re.search(r"(?m)^\s*set -e", text):
            report.fail(
                "maintainer scripts: %s does not enable 'set -e'" % name
            )
        if not entry["mode"] & 0o111:
            report.fail("maintainer scripts: %s is not executable" % name)
        if entry["uid"] != 0 or entry["gid"] != 0:
            report.fail(
                "maintainer scripts: %s is not root-owned" % name,
                "%d:%d" % (entry["uid"], entry["gid"]),
            )
        if text.count("@@") > 0:
            report.fail(
                "maintainer scripts: %s contains unsubstituted @@ placeholders" % name
            )
        if name == "postinst":
            if "chrome-sandbox" in text and "4755" in text:
                report.ok("maintainer scripts: postinst repairs chrome-sandbox mode")
            else:
                report.fail(
                    "maintainer scripts: postinst does not repair chrome-sandbox"
                )
            if "update-desktop-database" in text:
                report.ok("maintainer scripts: postinst updates desktop database")
            else:
                report.warn(
                    "maintainer scripts: postinst does not call update-desktop-database"
                )
            if "xdg-mime default" in text and "x-scheme-handler/dsh" in text:
                report.ok("maintainer scripts: postinst registers the dsh:// handler")
            else:
                report.fail(
                    "maintainer scripts: postinst does not register the dsh:// handler",
                    "expected 'xdg-mime default ... x-scheme-handler/dsh'",
                )
            if "command -v xdg-mime" in text:
                report.ok("maintainer scripts: postinst tolerates missing xdg-mime")
            else:
                report.warn(
                    "maintainer scripts: postinst does not guard the xdg-mime call"
                )
        if name == "postrm" and "purge" in text:
            report.ok("maintainer scripts: postrm implements purge cleanup")
        report.ok("maintainer scripts: %s looks valid" % name)


def check_payload_layout(args, payload_files, payload_dirs, symlinks, report):
    report.section("payload")
    package = args.package
    app_prefix = "opt/%s/" % package
    issues = 0

    if "opt/%s" % package in payload_dirs:
        report.ok("payload: /opt/%s directory is present" % package)
    else:
        report.fail("payload: /opt/%s directory is missing" % package)
        issues += 1

    exe_path = app_prefix + args.exe_name
    exe = payload_files.get(exe_path)
    if exe is None:
        report.fail("payload: application executable is missing", exe_path)
        issues += 1
    else:
        if exe.mode & 0o111:
            report.ok("payload: application executable is executable")
        else:
            report.fail(
                "payload: application executable is not executable",
                oct(exe.mode),
            )
        if exe.size < 4096:
            report.fail(
                "payload: application executable is suspiciously small",
                "%d bytes" % exe.size,
            )

    asar_path = app_prefix + "resources/app.asar"
    asar = payload_files.get(asar_path)
    if asar is None:
        report.fail("payload: resources/app.asar is missing", asar_path)
        issues += 1
    elif asar.size < 1024:
        report.fail(
            "payload: resources/app.asar is suspiciously small",
            "%d bytes" % asar.size,
        )
    else:
        report.ok("payload: resources/app.asar is present (%d bytes)" % asar.size)

    runtime_dirs = [
        name
        for name in payload_dirs
        if name == app_prefix + "resources/runtime"
        or name.startswith(app_prefix + "resources/runtime/")
    ]
    runtime_files = [
        name for name in payload_files if name.startswith(app_prefix + "resources/runtime/")
    ]
    if runtime_dirs and runtime_files:
        report.ok(
            "payload: resources/runtime is present (%d files)" % len(runtime_files)
        )
    else:
        report.fail("payload: resources/runtime is missing or empty")

    sandbox = payload_files.get(app_prefix + "chrome-sandbox")
    if sandbox is None:
        report.fail("payload: chrome-sandbox is missing", app_prefix + "chrome-sandbox")
        issues += 1
    else:
        if sandbox.mode == 0o4755:
            report.ok("payload: chrome-sandbox mode is 4755")
        else:
            report.fail(
                "payload: chrome-sandbox mode must be 4755",
                "found %o" % sandbox.mode,
            )
        if sandbox.uid == 0 and sandbox.gid == 0:
            report.ok("payload: chrome-sandbox is owned by root:root")
        else:
            report.fail(
                "payload: chrome-sandbox must be root:root",
                "%d:%d" % (sandbox.uid, sandbox.gid),
            )

    launcher = payload_files.get("usr/bin/%s" % package)
    if launcher is None:
        report.fail("payload: /usr/bin/%s launcher is missing" % package)
        issues += 1
    else:
        text = launcher.content.decode("utf-8", "replace") if launcher.content else ""
        if not text.startswith("#!"):
            report.fail("payload: launcher has no shebang")
        if text.count("@@") > 0:
            report.fail("payload: launcher contains unsubstituted @@ placeholders")
        if app_prefix not in text and ("/opt/" + package) not in text:
            report.fail(
                "payload: launcher does not reference /opt/%s" % package
            )
        if "exec " not in text:
            report.fail("payload: launcher does not exec the application binary")
        elif launcher.mode & 0o111:
            report.ok("payload: launcher execs the packaged application")
        if launcher.mode == 0o755:
            report.ok("payload: launcher mode is 0755")
        else:
            report.fail("payload: launcher mode must be 0755", "found %o" % launcher.mode)

    # The deb must expose the bundled dsh CLI as /usr/bin/dsh. The launcher in
    # the app payload is a shell script that re-executes the Electron binary in
    # Node mode, and the package-owned symlink keeps it on PATH after install.
    cli_relpath = "resources/runtime/cli/bin/dsh"
    cli_path = app_prefix + cli_relpath
    cli_entry = payload_files.get(cli_path)
    if cli_entry is None:
        report.fail("payload: bundled dsh CLI launcher is missing", cli_path)
    else:
        text = cli_entry.content.decode("utf-8", "replace") if cli_entry.content else ""
        if cli_entry.mode & 0o111:
            report.ok("payload: bundled dsh CLI launcher is executable")
        else:
            report.fail(
                "payload: bundled dsh CLI launcher is not executable",
                oct(cli_entry.mode),
            )
        if text.startswith("#!"):
            report.ok("payload: bundled dsh CLI launcher has a shebang")
        else:
            report.fail("payload: bundled dsh CLI launcher has no shebang")
        if "ELECTRON_RUN_AS_NODE=1" in text or "ELECTRON_RUN_AS_NODE" in text:
            report.ok("payload: bundled dsh CLI runs Electron in Node mode")
        else:
            report.fail(
                "payload: bundled dsh CLI does not set ELECTRON_RUN_AS_NODE"
            )
        if "app.asar" in text and "cli.js" in text:
            report.ok("payload: bundled dsh CLI points at the Host CLI entrypoint")
        else:
            report.fail("payload: bundled dsh CLI does not reference the CLI entrypoint")

    dsh_target = "/opt/%s/%s" % (package, cli_relpath)
    dsh_link = symlinks.get("usr/bin/dsh")
    if dsh_link is None:
        if "usr/bin/dsh" in payload_files:
            report.fail(
                "payload: /usr/bin/dsh must be a symlink",
                "found a regular file",
            )
        else:
            report.fail(
                "payload: /usr/bin/dsh symlink is missing",
                "expected -> %s" % dsh_target,
            )
    else:
        if dsh_link.startswith("/"):
            resolved = dsh_link.lstrip("/")
        else:
            resolved = os.path.normpath(
                os.path.join("usr/bin", dsh_link)
            ).replace(os.sep, "/")
        if resolved == dsh_target.lstrip("/"):
            report.ok("payload: /usr/bin/dsh -> %s" % dsh_target)
        else:
            report.fail(
                "payload: /usr/bin/dsh has the wrong target",
                "%r (resolves to %r) != %r" % (dsh_link, resolved, dsh_target),
            )
        if resolved in payload_files:
            report.ok("payload: /usr/bin/dsh target exists in the package")
        else:
            report.fail("payload: /usr/bin/dsh target is not in the package", dsh_link)

    doc_prefix = "usr/share/doc/%s/" % package
    for name, minimum in (("copyright", 1), ("changelog.Debian.gz", 1)):
        path = doc_prefix + name
        entry = payload_files.get(path)
        if entry is None:
            report.fail("payload: %s is missing" % path)
            issues += 1
        elif entry.size < minimum:
            report.fail("payload: %s is empty" % path)
        else:
            report.ok("payload: %s is present" % path)
    changelog = payload_files.get(doc_prefix + "changelog.Debian.gz")
    if changelog is not None and changelog.content:
        try:
            text = gzip.decompress(changelog.content).decode("utf-8", "replace")
        except OSError as exc:
            report.fail("payload: changelog.Debian.gz is not valid gzip", str(exc))
        else:
            if text.startswith(package + " ("):
                report.ok("payload: changelog.Debian.gz is valid")
            else:
                report.fail(
                    "payload: changelog.Debian.gz does not start with the package name"
                )
            fields = None
            if "%s (" % package in text:
                fields = re.search(r"\(([^)]+)\)", text)
            if fields is not None and args.version is not None:
                if fields.group(1) == args.version:
                    report.ok("payload: changelog version matches %s" % args.version)
                else:
                    report.warn(
                        "payload: changelog version %r differs from %r"
                        % (fields.group(1), args.version)
                    )
    copyright_entry = payload_files.get(doc_prefix + "copyright")
    if copyright_entry is not None and copyright_entry.content is not None:
        text = copyright_entry.content.decode("utf-8", "replace")
        if "License" in text or "license" in text:
            report.ok("payload: copyright mentions a license")
        else:
            report.warn("payload: copyright does not mention a license")

    for name in payload_files:
        if name.startswith(app_prefix):
            continue
        if name in (
            "usr/bin/%s" % package,
            "usr/share/applications/%s.desktop" % package,
            doc_prefix + "copyright",
            doc_prefix + "changelog.Debian.gz",
            "usr/share/icons/hicolor/256x256/apps/%s.png" % package,
            "usr/share/icons/hicolor/512x512/apps/%s.png" % package,
            app_prefix + "resources/runtime/cli/bin/dsh",
        ):
            continue
        report.warn("payload: unexpected file outside the standard layout", name)
    return issues


def check_desktop_entry(args, payload_files, report):
    report.section("desktop entry")
    package = args.package
    path = "usr/share/applications/%s.desktop" % package
    entry = payload_files.get(path)
    if entry is None:
        report.fail("desktop: %s is missing" % path)
        return
    text = entry.content.decode("utf-8", "replace") if entry.content else ""
    if "@@" in text:
        report.fail("desktop: unsubstituted @@ placeholders")
    groups = parse_desktop_file(text)
    desktop = groups.get("Desktop Entry")
    if desktop is None:
        report.fail("desktop: [Desktop Entry] group is missing")
        return
    if desktop.get("Type") == "Application":
        report.ok("desktop: Type=Application")
    else:
        report.fail("desktop: Type must be Application", repr(desktop.get("Type")))
    if desktop.get("Name"):
        report.ok("desktop: Name is %r" % desktop["Name"])
    else:
        report.fail("desktop: Name is missing")
    exec_value = desktop.get("Exec", "").strip()
    if not exec_value:
        report.fail("desktop: Exec is missing")
    else:
        executable = exec_value.split()[0]
        expected = "/usr/bin/%s" % package
        if executable == expected:
            report.ok("desktop: Exec points to %s" % expected)
        elif executable == package:
            report.fail(
                "desktop: Exec must use the absolute /usr/bin path", exec_value
            )
        else:
            report.fail(
                "desktop: Exec does not point to %s" % expected, exec_value
            )
        target = executable if executable.startswith("/") else "usr/bin/" + executable
        target_path = target.lstrip("/")
        if target_path in payload_files:
            report.ok("desktop: Exec target exists in the payload")
        else:
            report.fail("desktop: Exec target is not in the payload", target)
    icon = desktop.get("Icon", "")
    if icon == package:
        report.ok("desktop: Icon is %s" % icon)
    else:
        report.fail("desktop: Icon must be %r" % package, repr(icon))
    terminal = desktop.get("Terminal")
    if terminal is None:
        report.warn("desktop: Terminal key is absent")
    elif terminal.lower() == "false":
        report.ok("desktop: Terminal=false")
    else:
        report.fail("desktop: Terminal must be false", terminal)
    categories = desktop.get("Categories", "")
    if categories and categories.endswith(";"):
        report.ok("desktop: Categories is %r" % categories)
    else:
        report.fail("desktop: Categories must be a non-empty ';'-terminated list")
    try_exec = desktop.get("TryExec")
    if try_exec is not None and try_exec != "/usr/bin/%s" % package:
        report.warn("desktop: unusual TryExec %r" % try_exec)
    if entry.mode != 0o644:
        report.warn("desktop: mode is %o, expected 0644" % entry.mode)


def check_icons(args, payload_files, report):
    report.section("icons")
    package = args.package
    for size in (256, 512):
        path = "usr/share/icons/hicolor/%dx%d/apps/%s.png" % (size, size, package)
        entry = payload_files.get(path)
        if entry is None:
            report.fail("icons: %s is missing" % path)
            continue
        if entry.content is None:
            report.fail("icons: %s could not be read" % path)
            continue
        dimensions = png_dimensions(entry.content)
        if dimensions is None:
            report.fail("icons: %s is not a valid PNG" % path)
            continue
        width, height, bit_depth, color_type, interlace = dimensions
        if width == size and height == size:
            report.ok("icons: %s is %dx%d PNG" % (path, width, height))
        else:
            report.fail(
                "icons: %s has wrong dimensions" % path,
                "%dx%d" % (width, height),
            )
        if bit_depth not in (8, 16):
            report.fail("icons: %s has unsupported bit depth" % path, str(bit_depth))
        if color_type not in (0, 2, 3, 4, 6):
            report.fail("icons: %s has unsupported color type" % path, str(color_type))
        if interlace != 0:
            report.warn("icons: %s is interlaced" % path)
        if entry.mode != 0o644:
            report.warn("icons: %s mode is %o, expected 0644" % (path, entry.mode))


def check_elf(args, payload_files, report):
    report.section("ELF payload")
    expected_class, expected_data, expected_machine, label = EXPECTED_ELF[args.arch]
    package = args.package
    app_prefix = "opt/%s/" % package
    checked = 0
    wrong = []
    for path in sorted(payload_files):
        entry = payload_files[path]
        info = parse_elf_header(entry.head)
        if info is None:
            continue
        checked += 1
        if info["machine"] != expected_machine:
            wrong.append((path, info))
            continue
        if path.startswith(app_prefix) and (
            info["class"] != expected_class or info["data"] != expected_data
        ):
            wrong.append((path, info))
    if wrong:
        for path, info in wrong[:20]:
            report.fail(
                "ELF: wrong architecture for %s" % path,
                "class=%d data=%d machine=%s"
                % (info["class"], info["data"], info["machine_name"]),
            )
        if len(wrong) > 20:
            report.fail("ELF: %d further architecture problems" % (len(wrong) - 20))
    else:
        report.ok(
            "ELF: all %d ELF files are %s %s"
            % (checked, label, "64-bit" if expected_class == 2 else "32-bit")
        )

    for label_path in (
        app_prefix + args.exe_name,
        app_prefix + "chrome-sandbox",
    ):
        entry = payload_files.get(label_path)
        if entry is None:
            continue
        info = parse_elf_header(entry.head)
        if info is None:
            report.fail("ELF: %s is not an ELF binary" % label_path)
            continue
        if (
            info["class"] == expected_class
            and info["data"] == expected_data
            and info["machine"] == expected_machine
        ):
            report.ok(
                "ELF: %s is ELF%d %s %s"
                % (
                    label_path,
                    64 if expected_class == 2 else 32,
                    label,
                    "DYN" if info["type"] == 3 else "EXEC" if info["type"] == 2 else hex(info["type"]),
                )
            )
        else:
            report.fail(
                "ELF: %s must be ELF64 %s" % (label_path, label),
                "class=%d data=%d machine=%s"
                % (info["class"], info["data"], info["machine_name"]),
            )
        if label_path.endswith("chrome-sandbox"):
            if entry.mode == 0o4755 and entry.uid == 0 and entry.gid == 0:
                report.ok("ELF: chrome-sandbox is root:root 4755")
            else:
                report.fail(
                    "ELF: chrome-sandbox must be root:root with mode 4755",
                    "%d:%d %o" % (entry.uid, entry.gid, entry.mode),
                )
    return checked


def check_ownership_and_mtimes(args, payload_files, payload_dirs, symlinks, report):
    report.section("ownership / reproducibility")
    bad_owner = []
    for name, entry in payload_files.items():
        if entry.uid != 0 or entry.gid != 0:
            bad_owner.append("%s: %d:%d" % (name, entry.uid, entry.gid))
    if bad_owner:
        for detail in bad_owner[:10]:
            report.fail("ownership: payload file is not root:root", detail)
        if len(bad_owner) > 10:
            report.fail("ownership: %d further files are not root:root" % (len(bad_owner) - 10))
    else:
        report.ok("ownership: all payload files are root:root")
    if symlinks:
        unsafe = []
        package_root = "opt/%s/" % args.package
        for name, target in symlinks.items():
            if os.path.isabs(target):
                absolute = os.path.normpath(target).lstrip("/").replace(os.sep, "/")
                if absolute.startswith(package_root):
                    continue
                unsafe.append("%s -> %s (absolute outside the package)" % (name, target))
                continue
            joined = os.path.normpath(os.path.join(os.path.dirname(name), target))
            if joined.startswith(".."):
                unsafe.append("%s -> %s (escapes root)" % (name, target))
        if unsafe:
            for detail in unsafe:
                report.fail("payload: unsafe symlink", detail)
        else:
            report.ok("payload: %d symlinks stay inside the package root" % len(symlinks))

    mtimes = {entry.mtime for entry in payload_files.values()}
    if len(mtimes) == 1:
        report.ok("reproducibility: all payload files share one fixed mtime")
    elif len(mtimes) > 1:
        report.warn(
            "reproducibility: payload mtimes are not uniform (%d distinct values)"
            % len(mtimes)
        )


def check_installed_size(fields, payload_files, payload_dirs, symlinks, report):
    if not fields or "Installed-Size" not in fields:
        return
    raw = fields["Installed-Size"].strip()
    if not raw.isdigit():
        return
    declared = int(raw)
    total_bytes = sum(entry.size for entry in payload_files.values())
    entries = len(payload_files) + len(payload_dirs) + len(symlinks)
    lower = (total_bytes + 1023) // 1024
    upper = lower + 4 * entries + 64
    if lower <= declared <= upper:
        report.ok(
            "control: Installed-Size %d KiB is consistent with %d payload bytes"
            % (declared, total_bytes)
        )
    else:
        report.fail(
            "control: Installed-Size is inconsistent with the payload",
            "declared %d KiB, expected %d..%d KiB for %d bytes in %d entries"
            % (declared, lower, upper, total_bytes, entries),
        )


def check_control_tar_members(control_entries, report):
    report.section("control.tar contents")
    for name in control_entries:
        if name not in CONTROL_MEMBER_NAMES:
            report.warn("control.tar: unexpected member %r" % name)
    control = control_entries.get("control")
    if control is None:
        report.fail("control.tar: control file is missing")
    else:
        if control["mode"] != 0o644:
            report.warn("control.tar: control mode is %o, expected 0644" % control["mode"])
        if control["uid"] != 0 or control["gid"] != 0:
            report.warn("control.tar: control is not root-owned")
    if "md5sums" not in control_entries:
        report.fail("control.tar: md5sums file is missing")
    else:
        if control_entries["md5sums"]["mode"] != 0o644:
            report.warn(
                "control.tar: md5sums mode is %o, expected 0644"
                % control_entries["md5sums"]["mode"]
            )
    for name in MAINTAINER_SCRIPT_NAMES:
        if name in control_entries and control_entries[name]["mode"] & 0o022:
            report.fail(
                "control.tar: %s is group/world writable" % name,
                oct(control_entries[name]["mode"]),
            )


def check_filename(args, deb_path, fields, report):
    base = os.path.basename(deb_path)
    if fields is None:
        return
    version = fields.get("Version")
    package = fields.get("Package")
    arch = fields.get("Architecture")
    if not version or not package or not arch:
        return
    expected = "%s_%s_%s.deb" % (package, version, arch)
    if base == expected:
        report.ok("filename: %s follows the dpkg convention" % base)
    else:
        report.warn(
            "filename: %r does not follow %r (not fatal: CI may rename artifacts)"
            % (base, expected)
        )


def finalize(report, args, summary):
    print("", file=report.stream)
    print("== summary ==", file=report.stream)
    if report.ok_result:
        print(
            "  result: PASS (%d checks, %d warnings)"
            % (report.passed, report.warnings),
            file=report.stream,
        )
    else:
        print(
            "  result: FAIL (%d checks, %d warnings, %d failures)"
            % (report.passed, report.warnings, len(report.failures)),
            file=report.stream,
        )
        for check, detail in report.failures:
            print("    - %s%s" % (check, (": " + detail) if detail else ""), file=report.stream)
    exit_code = EXIT_OK if report.ok_result else EXIT_FAIL
    if args.json:
        summary["ok"] = report.ok_result
        summary["passed"] = report.passed
        summary["warnings"] = report.warnings
        summary["failures"] = [
            {"check": check, "detail": detail} for check, detail in report.failures
        ]
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return exit_code


def verify(args):
    deb_path = os.path.abspath(args.deb)
    if not os.path.isfile(deb_path):
        raise EnvError("no such file: %s" % args.deb)
    if os.path.getsize(deb_path) < 8 + AR_HEADER_SIZE:
        raise DebFormatError("file is too small to be a .deb archive")
    if sys.version_info < (3, 8):
        raise EnvError("Python 3.8 or newer is required")

    summary = {
        "deb": deb_path,
        "arch": args.arch,
        "package": args.package,
        "version": args.version,
    }
    stream = sys.stderr if args.json else sys.stdout
    report = Report(stream=stream, quiet=args.quiet)
    report.section("package")
    report.info("file: %s (%d bytes)" % (deb_path, os.path.getsize(deb_path)))
    report.info("expected: %s %s for %s" % (args.package, args.version or "(any)", args.arch))
    report.info("python: %s" % sys.version.split()[0])

    temp_root = create_temp_dir("dsh-verify-deb-")
    try:
        try:
            members = read_ar_members(deb_path)
            control_member, data_member = check_ar_structure(
                deb_path, members, report
            )
        except DebFormatError as exc:
            report.fail("ar: archive is malformed", str(exc))
            return finalize(report, args, summary)
        if control_member is None or data_member is None:
            return finalize(report, args, summary)

        control_tar_path = extract_member_to_file(
            deb_path, control_member, os.path.join(temp_root, "control.tar")
        )
        control_entries = scan_control_tar(control_tar_path, report)
        check_control_tar_members(control_entries, report)
        check_maintainer_scripts(control_entries, report)

        control_entry = control_entries.get("control")
        if control_entry is None:
            return finalize(report, args, summary)
        control_text = control_entry["data"].decode("utf-8", "replace")
        fields = check_control_fields(control_text, args, report)
        if fields is not None and args.version is None:
            summary["version"] = fields.get("Version")

        control_mtime = {entry["mtime"] for entry in control_entries.values()}
        if len(control_mtime) == 1:
            report.ok("reproducibility: control.tar members share one fixed mtime")
        elif len(control_mtime) > 1:
            report.warn("reproducibility: control.tar mtimes are not uniform")

        data_tar_path = extract_member_to_file(
            deb_path, data_member, os.path.join(temp_root, "data.tar")
        )
        package = args.package
        wanted = {
            "usr/bin/%s" % package,
            "usr/share/applications/%s.desktop" % package,
            "usr/share/doc/%s/copyright" % package,
            "usr/share/doc/%s/changelog.Debian.gz" % package,
            "usr/share/icons/hicolor/256x256/apps/%s.png" % package,
            "usr/share/icons/hicolor/512x512/apps/%s.png" % package,
            "opt/%s/resources/runtime/cli/bin/dsh" % package,
        }
        payload_files, payload_dirs, symlinks = scan_payload_tar(
            data_tar_path, wanted, report
        )
        report.section("payload overview")
        total_bytes = sum(entry.size for entry in payload_files.values())
        report.info(
            "%d files, %d directories, %d symlinks, %.1f MiB uncompressed"
            % (
                len(payload_files),
                len(payload_dirs),
                len(symlinks),
                total_bytes / (1024.0 * 1024.0),
            )
        )

        check_payload_layout(args, payload_files, payload_dirs, symlinks, report)
        check_desktop_entry(args, payload_files, report)
        check_icons(args, payload_files, report)
        check_elf(args, payload_files, report)
        check_ownership_and_mtimes(
            args, payload_files, payload_dirs, symlinks, report
        )
        check_installed_size(
            fields, payload_files, payload_dirs, symlinks, report
        )

        md5_entry = control_entries.get("md5sums")
        if md5_entry is None:
            report.fail("md5sums: control.tar has no md5sums file")
        else:
            check_md5sums(
                md5_entry["data"].decode("utf-8", "replace"), payload_files, report
            )

        check_filename(args, deb_path, fields, report)
        return finalize(report, args, summary)
    finally:
        if args.keep_temp:
            print("kept temporary directory: %s" % temp_root, file=stream)
        else:
            shutil.rmtree(temp_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Validate the structure and metadata of a DeepSeek Harness .deb.",
        epilog=(
            "exit codes: 0 = valid, 1 = invalid package, 2 = environment error "
            "(e.g. no zstd backend)"
        ),
    )
    parser.add_argument("deb", help="path to the .deb file to verify")
    parser.add_argument(
        "--arch",
        default="arm64",
        choices=sorted(EXPECTED_ELF),
        help="expected Debian architecture (default: arm64)",
    )
    parser.add_argument(
        "--package-name",
        dest="package",
        default="deepseek-harness",
        help="expected package name (default: deepseek-harness)",
    )
    parser.add_argument(
        "--exe-name",
        dest="exe_name",
        default="deepseek-harness",
        help="application executable inside /opt/<package> (default: deepseek-harness)",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="expected package version; also cross-checked against the changelog",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="only print warnings, failures and the summary"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print a JSON summary on stdout (human output goes to stderr)",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="keep the temporary decompression directory for debugging",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return verify(args)
    except EnvError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        print("%s: environment error: %s" % (PROG, exc), file=sys.stderr)
        return EXIT_ENV
    except DebFormatError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        print("%s: invalid package: %s" % (PROG, exc), file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:  # pragma: no cover
        print("%s: interrupted" % PROG, file=sys.stderr)
        return EXIT_ENV


if __name__ == "__main__":
    sys.exit(main())
