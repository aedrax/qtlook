# qtlook

qtlook downloads a Qt desktop release and unpacks it. It talks to the Qt online
repository directly.

## Scope

qtlook supports desktop builds only. It supports four platforms:

| OS | ARCH | Qt architecture | Install directory |
|---|---|---|---|
| `linux` | `x64` | `linux_gcc_64` | `gcc_64` |
| `linux` | `arm64` | `linux_gcc_arm64` | `gcc_arm64` |
| `macos` | `arm64` | `clang_64` | `macos` |
| `windows` | `mingw` | `win64_mingw` | `mingw_64` |

The macOS build is a universal binary. Apple silicon uses the `mac_x64` host folder
and the `clang_64` architecture. This looks wrong and is correct.

The `ARCH` position also accepts a raw Qt architecture name, such as `clang_64` or
`win64_msvc2022_64`. Use this for a platform that the table does not hold. Run
`./qtlook.py --list VERSION OS` to see the names that a release has.

## Install

qtlook needs Python 3.10 or later and the `requests` package:

```
pip install requests
```

qtlook also needs a 7z unpacker. Install the `p7zip` package of your system, or run
`pip install py7zr`. qtlook prefers the `7z` command when it is on the PATH.

## Use

qtlook has two commands. Each command takes the same three values:

```
./qtlook.py --list [VERSION [OS [ARCH]]]
./qtlook.py --install VERSION OS ARCH
```

### Find a release

Leave out a value to see what the repository holds. qtlook shows the choices for
the first value that you do not give:

```
./qtlook.py --list                # the Qt 6 releases
./qtlook.py --list 6.8.1          # the OSes that have Qt 6.8.1
./qtlook.py --list 6.8.1 linux    # the architectures of Qt 6.8.1 for linux
```

Each list comes from the repository, so it shows only the values that exist. For
example:

```
$ ./qtlook.py --list 6.8.1 windows
Qt 6.8.1 for windows has these architectures:
  win64_llvm_mingw
  win64_mingw                          (short name: mingw)
  win64_msvc2022_64
  win64_msvc2022_arm64_cross_compiled
```

The list of architectures shows the Qt architecture names. It also shows the short
name from the table above, when one exists. `ARCH` accepts the two forms.

`--install` shows the same lists when a value is missing. It then stops with exit
code 1, because it installs nothing.

### List and install

List the modules of a release:

```
./qtlook.py --list 6.8.1 linux x64
```

Install a release with modules:

```
./qtlook.py --install 6.8.1 linux x64 --modules qtimageformats qtwebview qtmultimedia
```

Install into a directory:

```
./qtlook.py --install 6.8.1 windows mingw -o /opt/qt
```

Qt lands in `<outdir>/<version>/<install-directory>`. The example above puts qmake at
`/opt/qt/6.8.1/mingw_64/bin/qmake`.

Build against the result:

```
cmake -DCMAKE_PREFIX_PATH=/opt/qt/6.8.1/mingw_64 ..
```

## Options

| Option | Default | Meaning |
|---|---|---|
| `-m`, `--modules` | none | Modules to install. Use `all` for every module. |
| `-o`, `--outdir` | `.` | Directory to install into. |
| `--mirror` | `https://download.qt.io` | Base URL of the Qt repository. |
| `--dry-run` | off | Show the archives. Download nothing. |
| `--keep` | off | Keep the downloaded archives. |
| `--no-verify` | off | Skip the checksum check. |

`--mirror` applies to the two commands. The other options change an install only,
and `--list` shows a warning when you give one of them.

The version must have three parts. `6.8` is an error, and the error lists the real
`6.8.x` releases. This also works without `OS` and `ARCH`:

```
$ ./qtlook.py --list 6.8
error: '6.8' is not a full version. Give three parts, e.g. 6.8.1.
Available: 6.8.0 6.8.1 6.8.2 6.8.3
```

## Checksums

qtlook checks the SHA-256 of every archive while it downloads. It reads the checksum
from `https://download.qt.io`, even when `--mirror` points somewhere else. The bytes
may come from any mirror. The checksum comes from the trusted host.

Before an install, qtlook checks each `Updates.xml` in the same way. This file
selects the archives, so a mirror must not be able to change it. `--list` does not
do this check, because it installs nothing.

CAUTION: `--no-verify` turns these checks off. Do not use it in production.

qtlook tries a request again after a connection error or a server error. It tries
three times. It also starts an archive again when the connection drops in the
middle of the download.

## Modules

Module names are the plain names, such as `qtimageformats` or `qtmultimedia`. Run
`--list` to see the names of a release.

From Qt 6.8 the modules `qtwebengine` and `qtpdf` live in their own repositories.
qtlook reads them and shows them with the other modules. Qt does not build
`qtwebengine` for windows mingw. qtlook says so when you ask for it.

## What qtlook patches

The archives hold the absolute paths of the Qt build machines. qtlook repairs them
after it unpacks:

1. It writes `bin/qt.conf`. This makes qmake and the other tools find their own
   prefix. This is the step that matters most.
2. It rewrites the prefix in `lib/pkgconfig/*.pc`.
3. It rewrites the library path in `lib/*.prl` to `$$[QT_INSTALL_LIBS]`, so the
   install stays movable.
4. It rewrites the prefix inside the `qmake` and `qtpaths` binaries, if the binaries
   hold one. Qt 6 desktop builds do not, so this step usually does nothing.
5. It sets the open source edition in `mkspecs/qconfig.pri`. Only Qt 5 needs this.
   Without it, the qmake of Qt 5 stops with "License check failed".

CMake needs none of this. The files in `lib/cmake/Qt6*` find their paths without help.

## Repository layout

Qt changed the layout of the repository twice. qtlook does not compute the layout
from the version. It asks for `Updates.xml` at each candidate path and keeps the
first path that answers. This survives the next change.

| Qt version | Path |
|---|---|
| 6.7 and older | `qt6_673/` |
| 6.8 to 6.10 | `qt6_681/qt6_681/` |
| 6.11 and later, windows | `qt6_6110/qt6_6110_mingw/` |
| 6.11 and later, linux and macOS | `qt6_6110/qt6_6110/` |

From Qt 6.11 each windows architecture has its own folder. The name of the folder
is the architecture name without `win64_`, such as `qt6_6110_llvm_mingw` or
`qt6_6110_msvc2022_arm64_cross_compiled`.

The lists of `--list` use the same repository:

- The releases and the OSes come from the folder page of each host, such as
  `linux_x64/desktop/`.
- The architectures come from the base packages in the `Updates.xml` of the release.

qtlook sends these requests to the `--mirror` host. The mirror must serve folder
pages. If it does not, qtlook finds no values and stops with an error.

The archives changed too. Up to Qt 6.7 an archive holds its own `6.7.3/gcc_64/`
directories. From Qt 6.8 an archive is flat and starts at `bin/` and `lib/`. qtlook
reads the header of each archive and unpacks it in the correct place. The result is
the same tree for every version.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Error. qtlook prints the reason. This includes `--install` with a missing value. |
| 2 | The command line is wrong. qtlook prints the usage. |
| 130 | The user stopped the script. |
