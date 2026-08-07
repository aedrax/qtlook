#!/usr/bin/env python3
"""qtlook downloads and unpacks a Qt desktop release.

The script talks to the Qt online repository directly.
It supports desktop builds only, on four platforms:

    linux x64, linux arm64, macos arm64, windows mingw

Use --list to see the modules of a release. Use --install to install one.
Leave out VERSION, OS or ARCH to see the values that the repository holds.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

try:
    import requests
except ImportError:  # pragma: no cover - depends on the environment
    sys.exit("qtlook needs the 'requests' package. Run: pip install requests")

TRUSTED_HOST = "https://download.qt.io"
DEFAULT_MIRROR = TRUSTED_HOST
REPO_ROOT = "online/qtsdkrepository"

# The script shows the releases of this major version when the user gives no version.
DEFAULT_MAJOR = 6

# Qt publishes some packages that hold empty directories only. They are under
# 41 bytes after decompression. Hide them from the module list.
MIN_MODULE_SIZE = 41

# From Qt 6.8 these two modules live in their own repository.
EXTENSION_MODULES = ("qtwebengine", "qtpdf")

# The absolute paths of the Qt build machines. The patch step replaces them.
BUILD_PREFIXES = {
    "linux": "/home/qt/work/install",
    "macos": "/Users/qt/work/install",
    "windows": "c:/Users/qt/work/install",
}

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0
TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)
RETRIES = 3

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class QtlookError(Exception):
    """The script stops with this error and prints the message."""


class PartialVersion(QtlookError):
    """The user gave '6.8' or '6' instead of a full version.

    The handler turns this into a message that lists the real releases.
    """

    def __init__(self, text: str) -> None:
        super().__init__(f"'{text}' is not a full version. Give three parts, e.g. 6.8.1.")
        self.text = text


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def dotless(self) -> str:
        """Return the version as the repository writes it, e.g. 681 for 6.8.1."""
        return f"{self.major}{self.minor}{self.patch}"

    @staticmethod
    def parse(text: str) -> "Version":
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", text.strip())
        if not match:
            raise PartialVersion(text.strip())
        return Version(int(match[1]), int(match[2]), int(match[3]))

    @staticmethod
    def from_folder(folder: str) -> Optional["Version"]:
        """Turn a repository folder name such as qt6_6110 into 6.11.0.

        The folder name has no dots. The patch number has two digits only when
        four digits follow the major version, such as qt5_51212 for 5.12.12.
        """
        match = re.fullmatch(r"qt(\d+)_(\d+)", folder)
        if not match or not match[2].startswith(match[1]):
            return None
        rest = match[2][len(match[1]) :]
        if not 2 <= len(rest) <= 4:
            return None
        split = 2 if len(rest) == 4 else len(rest) - 1
        return Version(int(match[1]), int(rest[:split]), int(rest[split:]))


def same_release(package_version: str, version: Version) -> bool:
    """Say whether a package version such as 6.8.1-0-202411221531 is of a release.

    The folder names have no dots, so 6.1.10 and 6.11.0 share the folder qt6_6110.
    The package version shows which release the folder really holds.
    """
    return package_version.split("-", 1)[0] == str(version)


# ---------------------------------------------------------------------------
# Platform table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Platform:
    os_name: str  # linux, macos or windows
    host: str  # repository host folder, e.g. linux_x64
    qt_arch: str  # architecture in the package names, e.g. linux_gcc_64
    install_dir: str  # directory the archives extract into, e.g. gcc_64
    ext_arch: str  # architecture segment of the extension repository URL


# host folder, Qt architecture, extension URL segment
_TABLE = {
    ("linux", "x64"): ("linux_x64", "linux_gcc_64", "x86_64"),
    ("linux", "arm64"): ("linux_arm64", "linux_gcc_arm64", "arm64"),
    ("macos", "arm64"): ("mac_x64", "clang_64", "clang_64"),
    ("windows", "mingw"): ("windows_x86", "win64_mingw", "mingw"),
}

# The repository host folders of each OS.
_HOSTS = {
    "linux": ("linux_x64", "linux_arm64"),
    "macos": ("mac_x64",),
    "windows": ("windows_x86",),
}

_OS_ALIASES = {
    "linux": "linux",
    "mac": "macos",
    "macos": "macos",
    "osx": "macos",
    "darwin": "macos",
    "win": "windows",
    "windows": "windows",
}

_ARCH_ALIASES = {
    "x64": "x64",
    "x86_64": "x64",
    "amd64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "mingw": "mingw",
    "win64_mingw": "mingw",
}


def arch_dir_name(qt_arch: str, version: Version) -> str:
    """Return the directory name that the archives of this architecture extract into."""
    if qt_arch.startswith("win64_mingw"):
        return qt_arch[len("win64_") :] + "_64"
    if qt_arch.startswith("win64_llvm"):
        return "llvm-" + qt_arch[len("win64_llvm_") :] + "_64"
    if qt_arch.startswith("win32_mingw"):
        return qt_arch[len("win32_") :] + "_32"
    if qt_arch.startswith("win"):
        if qt_arch.endswith("_cross_compiled"):
            return qt_arch[len("win64_") : -len("_cross_compiled")]
        return qt_arch[len("win64_") :]
    if qt_arch == "clang_64":
        # Qt renamed the macOS directory in 6.1.2. The architecture name did not change.
        return "macos" if version >= Version(6, 1, 2) else "clang_64"
    if qt_arch in ("gcc_64", "linux_gcc_64"):
        return "gcc_64"
    if qt_arch == "linux_gcc_arm64":
        return "gcc_arm64"
    return qt_arch


def resolve_os(os_text: str) -> str:
    """Turn the friendly OS word into linux, macos or windows."""
    os_name = _OS_ALIASES.get(os_text.lower())
    if os_name is None:
        raise QtlookError(
            f"Unknown OS '{os_text}'. Use one of: linux, macos, windows."
        )
    return os_name


def resolve_platform(os_text: str, arch_text: str, version: Version) -> Platform:
    """Turn the friendly OS and architecture words into repository names."""
    os_name = resolve_os(os_text)

    arch_key = _ARCH_ALIASES.get(arch_text.lower())
    if arch_key is not None and (os_name, arch_key) in _TABLE:
        host, qt_arch, ext_arch = _TABLE[(os_name, arch_key)]
        # Qt renamed the linux x64 architecture in 6.7.
        if qt_arch == "linux_gcc_64" and version < Version(6, 7, 0):
            qt_arch = "gcc_64"
        return Platform(os_name, host, qt_arch, arch_dir_name(qt_arch, version), ext_arch)

    # The user gave a raw Qt architecture. Pass it through.
    qt_arch = arch_text
    if os_name == "linux":
        host = "linux_arm64" if "arm64" in qt_arch else "linux_x64"
        ext_arch = "arm64" if "arm64" in qt_arch else "x86_64"
    elif os_name == "macos":
        host = "mac_x64"
        ext_arch = qt_arch
    else:
        host = "windows_x86"
        ext_arch = qt_arch.replace("win64_", "", 1).replace("_cross_compiled", "", 1)
    return Platform(os_name, host, qt_arch, arch_dir_name(qt_arch, version), ext_arch)


def supported_combinations() -> str:
    return ", ".join(f"{o} {a}" for (o, a) in _TABLE)


def select_hosts(
    os_text: Optional[str], arch_text: Optional[str], version: Version
) -> list[str]:
    """Return the host folders that the OS and the architecture select.

    A missing architecture selects every host of the OS. A missing OS selects
    every host.
    """
    if os_text is None:
        return [host for hosts in _HOSTS.values() for host in hosts]
    if arch_text is None:
        return list(_HOSTS[resolve_os(os_text)])
    return [resolve_platform(os_text, arch_text, version).host]


def short_arch_name(os_name: str, qt_arch: str, version: Version) -> Optional[str]:
    """Return the short word of the platform table for a Qt architecture."""
    for table_os, key in _TABLE:
        if table_os != os_name:
            continue
        if resolve_platform(table_os, key, version).qt_arch == qt_arch:
            return key
    return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Http:
    """A small wrapper over requests. It adds retries and a shared session."""

    def __init__(self, mirror: str) -> None:
        self.mirror = mirror.rstrip("/")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "qtlook/1.0"

    def _get(self, url: str, **kwargs):
        """Send a GET request. Try again after a connection error or a server error."""
        last: Optional[Exception] = None
        response = None
        for attempt in range(RETRIES):
            if attempt:
                time.sleep(attempt)
            try:
                response = self.session.get(url, timeout=TIMEOUT, **kwargs)
            except requests.RequestException as error:
                last = error
                response = None
                continue
            if response.status_code < 500:
                return response
            response.close()
        if response is None:
            raise QtlookError(f"Cannot reach {url}: {last}")
        return response

    def _file(self, rel_path: str, host: Optional[str]):
        """Return the response for a file, or None when the server answers 404."""
        base = (host or self.mirror).rstrip("/")
        response = self._get(f"{base}/{rel_path}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise QtlookError(f"{base}/{rel_path} answered {response.status_code}")
        return response

    def text(self, rel_path: str, host: Optional[str] = None) -> Optional[str]:
        """Return the body of a file as text, or None when the server answers 404."""
        response = self._file(rel_path, host)
        return None if response is None else response.text

    def content(self, rel_path: str, host: Optional[str] = None) -> Optional[bytes]:
        """Return the body of a file as bytes, or None when the server answers 404."""
        response = self._file(rel_path, host)
        return None if response is None else response.content

    def stream(self, rel_path: str):
        return self._get(f"{self.mirror}/{rel_path}", stream=True, allow_redirects=True)


# ---------------------------------------------------------------------------
# Repository discovery
# ---------------------------------------------------------------------------


def main_repo_candidates(platform: Platform, version: Version) -> list[str]:
    """List the paths that may hold Updates.xml, best first.

    Qt changed this layout twice. Probe instead of guessing:
      Qt <= 6.7        qt6_673
      Qt 6.8 to 6.10   qt6_681/qt6_681
      Qt >= 6.11 win   qt6_6110/qt6_6110_mingw
    """
    base = f"{REPO_ROOT}/{platform.host}/desktop"
    folder = f"qt{version.major}_{version.dotless}"
    candidates = []
    if platform.os_name == "windows":
        # The folder of a cross-compiled build keeps the '_cross_compiled' tail.
        suffix = platform.qt_arch.replace("win64_", "", 1)
        candidates.append(f"{base}/{folder}/{folder}_{suffix}")
        if suffix.endswith("_cross_compiled"):
            short = suffix[: -len("_cross_compiled")]
            candidates.append(f"{base}/{folder}/{folder}_{short}")
    candidates.append(f"{base}/{folder}/{folder}")
    candidates.append(f"{base}/{folder}")
    return candidates


def extension_repo(platform: Platform, version: Version, module: str) -> str:
    return (
        f"{REPO_ROOT}/{platform.host}/extensions/{module}"
        f"/{version.dotless}/{platform.ext_arch}"
    )


def available_versions(http: Http, hosts: Iterable[str], major: int) -> list[Version]:
    """Read the host folders and return every release they hold."""
    found = set()
    for host in hosts:
        body = http.text(f"{REPO_ROOT}/{host}/desktop/")
        if body is None:
            continue
        for folder in re.findall(rf'href="(qt{major}_\d+)/"', body):
            parsed = Version.from_folder(folder)
            if parsed is not None:
                found.add(parsed)
    return sorted(found)


def release_repos(http: Http, host: str, version: Version) -> list[str]:
    """Return every repository path of a release on one host.

    Up to Qt 6.7 the release folder is the repository. From Qt 6.8 the release
    folder holds one repository or more. Read the folder to find them.
    """
    base = f"{REPO_ROOT}/{host}/desktop"
    folder = f"qt{version.major}_{version.dotless}"
    body = http.text(f"{base}/{folder}/")
    if body is None:
        return []
    inner = sorted(set(re.findall(rf'href="({folder}(?:_\w+)?)/"', body)))
    if inner:
        return [f"{base}/{folder}/{name}" for name in inner]
    return [f"{base}/{folder}"]


# ---------------------------------------------------------------------------
# Updates.xml
# ---------------------------------------------------------------------------


@dataclass
class Package:
    name: str
    full_version: str
    archives: list[str]
    repo: str
    uncompressed: int = 0
    module: str = ""
    is_base: bool = False

    def archive_paths(self) -> list[str]:
        """Return the repository path of every archive of this package."""
        return [f"{self.repo}/{self.name}/{self.full_version}{a}" for a in self.archives]


def _split_list(element: Optional[ElementTree.Element]) -> list[str]:
    if element is None or not element.text:
        return []
    return [part.strip() for part in element.text.split(",") if part.strip()]


def module_of(name: str, version: Version, qt_arch: str) -> Optional[str]:
    """Return the module name of a package, or None when the package is not wanted.

    The base package returns an empty string.
    """
    if "debug_information" in name or "debug_info" in name:
        return None

    # Extension repository: extensions.qtwebengine.681.linux_gcc_64
    prefix = "extensions."
    if name.startswith(prefix):
        rest = name[len(prefix) :]
        tail = f".{version.dotless}.{qt_arch}"
        if rest.endswith(tail):
            return rest[: -len(tail)]
        return None

    suffix = f".{qt_arch}"
    if not name.endswith(suffix):
        return None
    stem = name[: -len(suffix)]

    for head in (f"qt.qt{version.major}.{version.dotless}", f"qt.{version.dotless}"):
        if stem == head:
            return ""  # the base package
        if stem.startswith(head + "."):
            rest = stem[len(head) + 1 :]
            if rest.startswith("addons."):
                rest = rest[len("addons.") :]
            # Skip doc.qt3d, examples.qt3d and the like.
            if "." in rest:
                return None
            return rest
    return None


def read_metadata(http: Http, repo: str, verify: bool) -> Optional[bytes]:
    """Return the Updates.xml of a repository, or None when the repository has none.

    The metadata selects the archives, and a mirror can change it. Before an
    install, compare it with the checksum of the trusted host.
    """
    rel_path = f"{repo}/Updates.xml"
    body = http.content(rel_path)
    if body is None or not verify:
        return body
    algorithm, expected = fetch_hash(http, rel_path)
    if expected is None or algorithm is None:
        raise QtlookError(
            f"No checksum for the metadata at {repo}. "
            f"Use --no-verify to install without one."
        )
    if hashlib.new(algorithm, body).hexdigest() != expected:
        raise QtlookError(
            f"The metadata at {http.mirror}/{rel_path} does not agree with the "
            f"checksum of {TRUSTED_HOST}.\nThe mirror is out of date or is not safe. "
            f"Use a different mirror."
        )
    return body


def parse_packages(
    xml_data: bytes, repo: str, version: Version, qt_arch: str
) -> list[Package]:
    try:
        root = ElementTree.fromstring(xml_data)
    except ElementTree.ParseError as error:
        raise QtlookError(f"The metadata at {repo} is damaged: {error}") from error

    packages = []
    for node in root.iter("PackageUpdate"):
        name = (node.findtext("Name") or "").strip()
        module = module_of(name, version, qt_arch)
        if module is None:
            continue
        archives = _split_list(node.find("DownloadableArchives"))
        if not archives:
            continue
        full_version = (node.findtext("Version") or "").strip()
        if not same_release(full_version, version):
            continue
        update_file = node.find("UpdateFile")
        size = 0
        if update_file is not None:
            size = int(update_file.attrib.get("UncompressedSize", "0") or 0)
        packages.append(
            Package(
                name=name,
                full_version=full_version,
                archives=archives,
                repo=repo,
                uncompressed=size,
                module=module,
                is_base=(module == ""),
            )
        )
    return packages


def base_architectures(xml_data: bytes, repo: str, version: Version) -> set[str]:
    """Return the architectures that have a base package in the metadata."""
    try:
        root = ElementTree.fromstring(xml_data)
    except ElementTree.ParseError as error:
        raise QtlookError(f"The metadata at {repo} is damaged: {error}") from error

    heads = (f"qt.qt{version.major}.{version.dotless}.", f"qt.{version.dotless}.")
    found = set()
    for node in root.iter("PackageUpdate"):
        name = (node.findtext("Name") or "").strip()
        if not _split_list(node.find("DownloadableArchives")):
            continue
        if not same_release((node.findtext("Version") or "").strip(), version):
            continue
        for head in heads:
            rest = name[len(head) :]
            if name.startswith(head) and rest and "." not in rest:
                found.add(rest)
    return found


def available_architectures(http: Http, os_name: str, version: Version) -> list[str]:
    """Read the metadata of a release and return every architecture of one OS."""
    found: set[str] = set()
    for host in _HOSTS[os_name]:
        for repo in release_repos(http, host, version):
            xml_data = http.content(f"{repo}/Updates.xml")
            if xml_data is not None:
                found.update(base_architectures(xml_data, repo, version))
    return sorted(found)


def architecture_lines(os_name: str, found: list[str], version: Version) -> list[str]:
    """Show each architecture with the short word of the platform table."""
    width = max(len(qt_arch) for qt_arch in found)
    lines = []
    for qt_arch in found:
        short = short_arch_name(os_name, qt_arch, version)
        note = f"  (short name: {short})" if short else ""
        lines.append(f"  {qt_arch:<{width}}{note}".rstrip())
    return lines


def near_releases(http: Http, hosts: Iterable[str], version: Version) -> str:
    """Return a line that lists the real releases, for a 'not found' error."""
    found = available_versions(http, hosts, version.major)
    same_minor = [v for v in found if v.minor == version.minor]
    shown = same_minor or found
    if not shown:
        return ""
    return "\nAvailable: " + " ".join(str(v) for v in shown)


def no_packages_error(http: Http, platform: Platform, version: Version) -> QtlookError:
    """Explain why a release has no packages for a platform.

    The release does not exist for the OS, or the OS does not have the architecture.
    """
    os_name = platform.os_name
    found = available_architectures(http, os_name, version)
    if not found:
        return QtlookError(
            f"Qt {version} is not in the repository for {os_name}."
            + near_releases(http, _HOSTS[os_name], version)
        )
    lines = [
        f"Qt {version} for {os_name} has no architecture '{platform.qt_arch}'. "
        f"Use one of these:"
    ]
    lines.extend(architecture_lines(os_name, found, version))
    return QtlookError("\n".join(lines))


def collect_packages(
    http: Http, platform: Platform, version: Version, verify: bool = False
) -> list[Package]:
    """Read the main repository and the two extension repositories."""
    packages: list[Package] = []
    for candidate in main_repo_candidates(platform, version):
        xml_data = read_metadata(http, candidate, verify)
        if xml_data is not None:
            packages = parse_packages(xml_data, candidate, version, platform.qt_arch)
            break

    if not packages:
        raise no_packages_error(http, platform, version)

    for module in EXTENSION_MODULES:
        ext_repo = extension_repo(platform, version, module)
        ext_xml = read_metadata(http, ext_repo, verify)
        if ext_xml is None:
            continue  # Qt does not build this module for this platform.
        packages.extend(parse_packages(ext_xml, ext_repo, version, platform.qt_arch))

    return packages


def module_names(packages: Iterable[Package]) -> list[str]:
    names = {
        p.module
        for p in packages
        if p.module and p.uncompressed >= MIN_MODULE_SIZE
    }
    return sorted(names)


def select_packages(packages: list[Package], wanted: list[str]) -> list[Package]:
    """Return the base package plus the packages of the wanted modules."""
    chosen = [p for p in packages if p.is_base]
    if not chosen:
        raise QtlookError("The repository has no base package for this architecture.")

    available = module_names(packages)
    if "all" in wanted:
        wanted = available
    wanted = list(dict.fromkeys(wanted))  # Remove a module that the user gave twice.

    unknown = [name for name in wanted if name not in available]
    if unknown:
        lines = []
        for name in unknown:
            close = difflib.get_close_matches(name, available, n=3)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            lines.append(f"  {name}{hint}")
        raise QtlookError(
            "These modules do not exist for this release:\n"
            + "\n".join(lines)
            + "\nRun --list to see every module."
        )

    for name in wanted:
        chosen.extend(p for p in packages if p.module == name)
    return chosen


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def fetch_hash(http: Http, rel_path: str) -> tuple[Optional[str], Optional[str]]:
    """Return the algorithm and the hash of an archive.

    The hash always comes from the trusted host, even when the bytes come from
    another mirror.
    """
    for algorithm in ("sha256", "sha1"):
        body = http.text(f"{rel_path}.{algorithm}", host=TRUSTED_HOST)
        if body:
            digest = body.split()[0].strip()
            if re.fullmatch(r"[0-9a-fA-F]+", digest):
                return algorithm, digest.lower()
    return None, None


def kept_file_is_good(http: Http, rel_path: str, local: Path, verify: bool) -> bool:
    """Say whether a kept archive is complete and can be used again.

    An interrupted run leaves a short file. Check the hash before you trust it.
    """
    if not local.is_file() or not verify:
        return False
    algorithm, expected = fetch_hash(http, rel_path)
    if expected is None or algorithm is None:
        return False
    digest = hashlib.new(algorithm)
    with open(local, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def _stream_to_file(http: Http, rel_path: str, dest: Path, algorithm: Optional[str]) -> str:
    """Write one archive to disk. Return its hash, or an empty string for no algorithm."""
    digest = hashlib.new(algorithm) if algorithm else None
    name = rel_path.split("/")[-1]
    with http.stream(rel_path) as response:
        if response.status_code != 200:
            raise QtlookError(f"Download of {name} failed with {response.status_code}")

        total = int(response.headers.get("Content-Length", "0") or 0)
        done = 0
        with open(dest, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 16):
                handle.write(chunk)
                if digest is not None:
                    digest.update(chunk)
                done += len(chunk)
                if sys.stderr.isatty():
                    bar = f"{human(done)}" + (f" / {human(total)}" if total else "")
                    print(f"\r  {name[:60]}  {bar}   ", end="", file=sys.stderr)
    if sys.stderr.isatty():
        print("\r" + " " * 100 + "\r", end="", file=sys.stderr)
    return digest.hexdigest() if digest is not None else ""


def download(http: Http, rel_path: str, dest: Path, verify: bool) -> None:
    """Stream one archive to disk and check its hash while it arrives."""
    name = rel_path.split("/")[-1]
    algorithm, expected = (None, None)
    if verify:
        algorithm, expected = fetch_hash(http, rel_path)
        if expected is None:
            raise QtlookError(
                f"No checksum for {name}. Use --no-verify to install without one."
            )

    # The connection can drop while the archive arrives. Start the archive again.
    last: Optional[Exception] = None
    for attempt in range(RETRIES):
        if attempt:
            time.sleep(attempt)
        try:
            actual = _stream_to_file(http, rel_path, dest, algorithm)
            break
        except requests.RequestException as error:
            last = error
    else:
        dest.unlink(missing_ok=True)
        raise QtlookError(f"Download of {name} failed: {last}")

    if expected is not None and actual != expected:
        dest.unlink(missing_ok=True)
        raise QtlookError(
            f"{name} is corrupt.\n  expected {expected}\n  actual   {actual}"
        )


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------


def find_7z() -> Optional[str]:
    for command in ("7z", "7zz", "7za"):
        path = shutil.which(command)
        if path:
            return path
    return None


def archive_names(archive: Path) -> list[str]:
    """Return the paths that an archive holds. This reads the header only."""
    command = find_7z()
    if command is not None:
        result = subprocess.run(
            [command, "l", "-ba", "-slt", str(archive)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise QtlookError(f"7z cannot read {archive.name}:\n{result.stderr.strip()}")
        return [
            line[len("Path = ") :].strip()
            for line in result.stdout.splitlines()
            if line.startswith("Path = ")
        ]

    try:
        import py7zr
    except ImportError:
        raise QtlookError(
            "qtlook cannot read 7z files.\n"
            "Install the p7zip package, or run: pip install py7zr"
        ) from None

    with py7zr.SevenZipFile(archive, "r") as handle:
        return list(handle.getnames())


def destination(
    names: Iterable[str], version: Version, outdir: Path, prefix: Path, os_name: str
) -> Path:
    """Say where an archive must go.

    Qt writes archives in three shapes. Read the archive. Do not guess from the
    version number.

    1. Up to Qt 6.7 every path starts with '<version>/<arch>/'. Unpack into the
       output directory.
    2. From Qt 6.8 the archive is flat and starts at 'bin/' or 'lib/'. Unpack into
       the prefix.
    3. A helper archive holds bare files, such as the ICU libraries or a Windows
       DLL. Qt keeps these with the other runtime libraries.
    """
    paths = [name.replace("\\", "/") for name in names if name.strip()]
    if paths and all(p.split("/", 1)[0] == str(version) for p in paths):
        return outdir
    if not any("/" in p for p in paths):
        return prefix / ("bin" if os_name == "windows" else "lib")
    return prefix


def extract(archive: Path, dest: Path) -> None:
    """Unpack one 7z archive into dest."""
    dest.mkdir(parents=True, exist_ok=True)
    command = find_7z()
    if command is not None:
        result = subprocess.run(
            [command, "x", "-aoa", "-bd", "-bso0", "-y", f"-o{dest}", str(archive)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise QtlookError(f"7z failed on {archive.name}:\n{result.stderr.strip()}")
        return

    try:
        import py7zr
    except ImportError:
        raise QtlookError(
            "qtlook cannot unpack 7z files.\n"
            "Install the p7zip package, or run: pip install py7zr"
        ) from None

    with py7zr.SevenZipFile(archive, "r") as handle:
        handle.extractall(path=str(dest))


# ---------------------------------------------------------------------------
# Patch
# ---------------------------------------------------------------------------


def _replace_in_file(path: Path, old: str, new: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return False
    if old not in text:
        return False
    path.write_text(text.replace(old, new), encoding="utf-8", errors="surrogateescape")
    return True


def _patch_binary_prefix(path: Path, prefix: str) -> bool:
    """Rewrite the prefix that older Qt builds store inside qmake.

    Qt 6 desktop builds do not hold this marker. The step then does nothing.
    """
    if not path.is_file():
        return False
    data = path.read_bytes()
    changed = False
    new = prefix.encode()
    if len(new) > 255:
        return False
    for key in (b"qt_prfxpath=", b"qt_epfxpath=", b"qt_hpfxpath="):
        start = data.find(key)
        if start < 0:
            continue
        value_start = start + len(key)
        end = data.find(b"\0", value_start)
        if end < 0:
            continue
        # The value sits in a buffer of 256 bytes. NUL bytes fill the buffer after
        # the value, so a new value can be longer than the old value.
        limit = min(value_start + 256, len(data))
        while end < limit and data[end] == 0:
            end += 1
        if len(new) >= end - value_start:
            continue  # No space for the value and its NUL byte.
        data = (
            data[:value_start] + new + b"\0" * (end - value_start - len(new)) + data[end:]
        )
        changed = True
    if changed:
        mode = path.stat().st_mode
        path.write_bytes(data)
        os.chmod(path, mode)
    return changed


def _patch_license(qconfig: Path) -> bool:
    """Set the open source edition in the qconfig.pri of a Qt 5 build.

    The Qt 5 archives name the commercial edition, and qmake then stops with
    'License check failed'. The Qt installer changes these two lines for an
    open source install. Do the same. Qt 6 does not have these lines.
    """
    try:
        text = qconfig.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return False
    new = re.sub(r"(?m)^QT_EDITION = Enterprise$", "QT_EDITION = OpenSource", text)
    new = re.sub(r"(?m)^QT_LICHECK = .+$", "QT_LICHECK =", new)
    if new == text:
        return False
    qconfig.write_text(new, encoding="utf-8", errors="surrogateescape")
    return True


def patch_install(prefix: Path, os_name: str, extracted: list[str]) -> None:
    """Fix the build machine paths so that qmake and pkg-config work."""
    if not (prefix / "bin").is_dir():
        raise QtlookError(
            f"The archives did not extract to the expected place.\n"
            f"  expected: {prefix}\n"
            f"  found at the top level: {', '.join(sorted(set(extracted))) or '(nothing)'}\n"
            f"The install is incomplete."
        )

    # 1. qt.conf. This is the step that makes a moved Qt find itself.
    (prefix / "bin" / "qt.conf").write_text("[Paths]\nPrefix=..\n", encoding="utf-8")

    build_prefix = BUILD_PREFIXES[os_name]

    # 2. pkg-config files hold an absolute prefix.
    for pc_file in sorted((prefix / "lib" / "pkgconfig").glob("*.pc")):
        _replace_in_file(pc_file, f"prefix={build_prefix}", f"prefix={prefix}")
        if os_name == "macos":
            _replace_in_file(pc_file, f"-F{build_prefix}/lib", f"-F{prefix}/lib")

    # 3. prl files get the qmake variable, so the install stays movable.
    for prl_file in sorted((prefix / "lib").glob("*.prl")):
        _replace_in_file(prl_file, f"{build_prefix}/lib", "$$[QT_INSTALL_LIBS]")

    # 4. Old Qt builds hold the prefix inside the binaries.
    suffix = ".exe" if os_name == "windows" else ""
    for tool in ("qmake", "qmake6", "qtpaths", "qtpaths6"):
        _patch_binary_prefix(prefix / "bin" / (tool + suffix), str(prefix))

    # 5. Qt 5 builds name the commercial edition. qmake then asks for a license.
    _patch_license(prefix / "mkspecs" / "qconfig.pri")

    # target_qt.conf belongs to cross builds. Patch it only when it is present.
    target_conf = prefix / "bin" / "target_qt.conf"
    if target_conf.is_file():
        _replace_in_file(target_conf, f"Prefix={build_prefix}/target", f"Prefix={prefix}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def command_list(args: argparse.Namespace) -> int:
    version = Version.parse(args.version)
    platform = resolve_platform(args.os_name, args.arch, version)
    http = Http(args.mirror)
    packages = collect_packages(http, platform, version)

    names = module_names(packages)
    print(f"Qt {version}  {platform.host}  {platform.qt_arch}")
    print(f"installs into  {version}/{platform.install_dir}")
    print(f"{len(names)} modules:")
    for name in names:
        print(f"  {name}")
    return 0


def versions_text(http: Http, major: int) -> str:
    """List the releases of one major version, one minor version on each line."""
    found = available_versions(http, select_hosts(None, None, Version(major, 0, 0)), major)
    if not found:
        raise QtlookError(f"The repository shows no Qt {major} releases.")
    lines = [f"Qt {major} releases:"]
    for minor in sorted({v.minor for v in found}):
        lines.append("  " + " ".join(str(v) for v in found if v.minor == minor))
    lines.append("Add a version to see the OSes that have it.")
    return "\n".join(lines)


def systems_text(http: Http, version: Version) -> str:
    """List the OSes that have a release."""
    names = [
        os_name
        for os_name, hosts in _HOSTS.items()
        if version in available_versions(http, hosts, version.major)
    ]
    if not names:
        raise QtlookError(
            f"Qt {version} is not in the repository."
            + near_releases(http, select_hosts(None, None, version), version)
        )
    lines = [f"Qt {version} is available for these OSes:"]
    lines.extend(f"  {name}" for name in names)
    lines.append("Add an OS to see its architectures.")
    return "\n".join(lines)


def architectures_text(http: Http, version: Version, os_name: str) -> str:
    """List the architectures of a release for one OS."""
    found = available_architectures(http, os_name, version)
    if not found:
        raise QtlookError(
            f"Qt {version} is not in the repository for {os_name}."
            + near_releases(http, _HOSTS[os_name], version)
        )
    lines = [f"Qt {version} for {os_name} has these architectures:"]
    lines.extend(architecture_lines(os_name, found, version))
    return "\n".join(lines)


def command_choices(args: argparse.Namespace) -> int:
    """Show the values of the first position that the user did not give."""
    http = Http(args.mirror)
    if args.version is None:
        text = versions_text(http, DEFAULT_MAJOR)
    else:
        version = Version.parse(args.version)
        if args.os_name is None:
            text = systems_text(http, version)
        else:
            text = architectures_text(http, version, resolve_os(args.os_name))

    if args.install is not None:
        print(f"error: --install needs VERSION OS ARCH.\n{text}", file=sys.stderr)
        return 1
    print(text)
    return 0


def command_install(args: argparse.Namespace) -> int:
    version = Version.parse(args.version)
    platform = resolve_platform(args.os_name, args.arch, version)
    http = Http(args.mirror)
    outdir = Path(args.outdir or ".").expanduser().resolve()

    if args.no_verify:
        print("WARNING: qtlook does not check the downloads.", file=sys.stderr)
    packages = collect_packages(http, platform, version, verify=not args.no_verify)

    wanted = list(args.modules or [])
    _check_unavailable(wanted, packages, platform)
    chosen = select_packages(packages, wanted)

    paths: list[str] = []
    for package in chosen:
        paths.extend(package.archive_paths())

    prefix = outdir / str(version) / platform.install_dir
    print(f"Qt {version}  {platform.qt_arch}")
    print(f"prefix   {prefix}")
    print(f"mirror   {args.mirror}")
    print(f"archives {len(paths)}")

    if args.dry_run:
        for path in paths:
            print(f"  {path}")
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    if args.keep:
        archive_dir = outdir / ".qtlook-archives"
        archive_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = None
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="qtlook-")
        archive_dir = Path(temp_dir.name)

    top_level: list[str] = []
    try:
        for index, rel_path in enumerate(paths, start=1):
            name = rel_path.split("/")[-1]
            print(f"[{index}/{len(paths)}] {name}")
            local = archive_dir / name
            if not kept_file_is_good(http, rel_path, local, not args.no_verify):
                download(http, rel_path, local, verify=not args.no_verify)
            target = destination(
                archive_names(local), version, outdir, prefix, platform.os_name
            )
            before = set(os.listdir(target)) if target.is_dir() else set()
            extract(local, target)
            top_level.extend(set(os.listdir(target)) - before)
            if not args.keep:
                local.unlink(missing_ok=True)
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    patch_install(prefix, platform.os_name, top_level)
    print(f"\nDone. Qt {version} is in {prefix}")
    print(f"Build with: cmake -DCMAKE_PREFIX_PATH={prefix}")
    return 0


def _check_unavailable(wanted: list[str], packages: list[Package], platform: Platform) -> None:
    """Explain the modules that Qt does not build for this platform."""
    have = set(module_names(packages))
    for module in EXTENSION_MODULES:
        if module in wanted and module not in have:
            raise QtlookError(
                f"Qt does not build {module} for {platform.host} {platform.qt_arch}.\n"
                f"Remove it from --modules."
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def explain_partial_version(error: PartialVersion, args: argparse.Namespace) -> str:
    """Add the list of real releases to the 'not a full version' message."""
    parts = error.text.split(".")
    if not parts[0].isdigit():
        return str(error)
    major = int(parts[0])
    try:
        hosts = select_hosts(args.os_name, args.arch, Version(major, 0, 0))
        found = available_versions(Http(args.mirror), hosts, major)
    except QtlookError:
        return str(error)
    if len(parts) > 1 and parts[1].isdigit():
        found = [v for v in found if v.minor == int(parts[1])]
    if not found:
        return str(error)
    return f"{error}\nAvailable: " + " ".join(str(v) for v in found)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qtlook.py",
        usage=(
            "qtlook.py --list [VERSION [OS [ARCH]]] [--mirror URL]\n"
            "       qtlook.py --install VERSION OS ARCH [-m MODULE ...] [-o DIR]\n"
            "                 [--mirror URL] [--dry-run] [--keep] [--no-verify]"
        ),
        description=(
            "Download and unpack a Qt desktop release.\n"
            "\n"
            "commands:\n"
            "  --list [VERSION [OS [ARCH]]]\n"
            "                        list the modules of a release. Leave out ARCH, OS\n"
            "                        or VERSION to list the architectures, the OSes or\n"
            "                        the releases\n"
            "  --install VERSION OS ARCH\n"
            "                        install a release, e.g. --install 6.8.1 linux x64"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  qtlook.py --list                  show the Qt releases\n"
            "  qtlook.py --list 6.8.1            show the OSes of a release\n"
            "  qtlook.py --list 6.8.1 linux      show the architectures of an OS\n"
            "  qtlook.py --list 6.8.1 linux x64  show the modules\n"
            "  qtlook.py --install 6.8.1 linux x64 --modules qtimageformats qtwebview\n"
            "  qtlook.py --install 6.8.1 windows mingw -o /opt/qt\n"
            f"\nsupported: {supported_combinations()}\n"
        ),
    )
    # The two commands take zero to three values. A missing value makes the
    # script show the values that the repository holds. The usage and the
    # description above show the commands, because argparse cannot show
    # optional positions such as [VERSION [OS [ARCH]]].
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--install", nargs="*", help=argparse.SUPPRESS)
    group.add_argument("--list", nargs="*", help=argparse.SUPPRESS)
    parser.add_argument(
        "-m", "--modules", nargs="+", default=[], metavar="MODULE",
        help="modules to install, or 'all'",
    )
    parser.add_argument(
        "-o", "--outdir", default=None, metavar="DIR",
        help="directory to install into (default: the current directory)",
    )
    parser.add_argument(
        "--mirror", default=DEFAULT_MIRROR, metavar="URL",
        help=f"base URL of the Qt repository (default: {DEFAULT_MIRROR})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show the archives, download nothing"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the downloaded archives"
    )
    parser.add_argument(
        "--no-verify", action="store_true", help="skip the checksum check"
    )
    return parser


def install_only_options(args: argparse.Namespace) -> list[str]:
    """Return the options that the user gave and that only --install uses."""
    given = {
        "--modules": bool(args.modules),
        "--outdir": args.outdir is not None,
        "--dry-run": args.dry_run,
        "--keep": args.keep,
        "--no-verify": args.no_verify,
    }
    return [name for name, used in given.items() if used]


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.install is not None:
        option, values = "--install", args.install
    elif args.list is not None:
        option, values = "--list", args.list
    else:
        parser.error("give --list or --install")
    if len(values) > 3:
        parser.error(f"{option} takes VERSION OS ARCH and no more values")
    args.version, args.os_name, args.arch = (values + [None] * 3)[:3]

    if args.install is None:
        unused = install_only_options(args)
        if unused:
            print(f"WARNING: --list does not use {', '.join(unused)}.", file=sys.stderr)

    try:
        if args.arch is None:
            return command_choices(args)
        if args.install is not None:
            return command_install(args)
        return command_list(args)
    except PartialVersion as error:
        print(f"error: {explain_partial_version(error, args)}", file=sys.stderr)
        return 1
    except QtlookError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        # A directory that the script cannot write, a full disk, and the like.
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
