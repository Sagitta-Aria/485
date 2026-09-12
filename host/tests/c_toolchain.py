"""Locate the host C compiler used to compile the firmware C tests.

The firmware C harnesses build the real production `.c` files against stub
headers, so every one of them needs a working native C compiler.  A missing
toolchain is a configuration error, not a reason to report success: this module
raises `RuntimeError` instead of `unittest.SkipTest`, because a skipped C test
looks exactly like a passing one in the summary line while silently dropping
dozens of the most valuable assertions in the suite.

Set `CABLE_HOST_CC` to choose a specific compiler, for example::

    $env:CABLE_HOST_CC = 'D:\\python28\\music\\Dev-Cpp\\MinGW64\\bin\\gcc.exe'
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

# Tried in this order when CABLE_HOST_CC is not set.
PATH_NAMES = ("cc", "gcc", "clang")
ENVIRONMENT_OVERRIDES = ("CABLE_HOST_CC", "CABLE_HOST_CLANG")

# A vendored Zig could be dropped in here; nothing else is assumed about it.
ZIG_GLOBS = ("zig.exe", "*/zig.exe", "*/*/zig.exe", "*/zig")

# How to install a compiler, quoted verbatim in the failure message.
INSTALL_HINT = r"$env:CABLE_HOST_CC = 'D:\python28\music\Dev-Cpp\MinGW64\bin\gcc.exe'"

# Only the linker needs a plausible output name; the probe never runs it.
_OUTPUT_SUFFIX = ".exe" if os.name == "nt" else ".dylib" if sys.platform == "darwin" else ".so"

# A cross-compiler can link a trivial main against its own target libc, so the
# probe additionally has to prove the target is this host: every platform that
# can run these tests is 64-bit.
_PROBE_PROGRAM = (
    "#if !defined(__x86_64__) && !defined(__aarch64__) && !defined(_M_X64) && !defined(_M_ARM64)\n"
    '#error "probe target is not this 64-bit host"\n'
    "#endif\n"
    "int main(void) { return 0; }\n"
)


@dataclasses.dataclass(frozen=True)
class HostCompiler:
    """One compiler invocation that already compiled and linked for the host."""

    command: tuple[str, ...]
    source: str
    is_zig: bool = False

    @property
    def executable(self) -> str:
        return self.command[0]


def _probe(command: tuple[str, ...], source: str, is_zig: bool) -> HostCompiler | None:
    """Accept a candidate only once it compiled and linked for this host.

    Answering `-v` is not enough, and neither is linking a trivial `main`: the
    ESP-IDF xtensa toolchain does both against its own target libc.  The probe
    therefore also requires a 64-bit host target, which every platform able to
    run this suite is.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="c_probe_") as directory:
            object_file = Path(directory) / "probe.o"
            result = subprocess.run(
                [*command, "-x", "c", "-c", "-", "-o", str(object_file)],
                input=_PROBE_PROGRAM, capture_output=True, text=True, timeout=60, errors="replace",
            )
            if result.returncode != 0:
                return None
            executable = Path(directory) / f"probe{_OUTPUT_SUFFIX}"
            result = subprocess.run(
                [*command, "-x", "c", "-", "-o", str(executable)],
                input=_PROBE_PROGRAM, capture_output=True, text=True, timeout=60, errors="replace",
            )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return HostCompiler(command=command, source=source, is_zig=is_zig)


def _zig_compiler(root: Path) -> HostCompiler | None:
    """Use a project-local Zig only when it is genuinely present and runnable."""
    tools = root / "host/tests/.tools/ziglang"
    for pattern in ZIG_GLOBS:
        for executable in sorted(tools.glob(pattern)):
            if executable.is_file():
                compiler = _probe((str(executable), "cc"), f"vendored Zig {executable}", True)
                if compiler is not None:
                    return compiler
    return None


def _database_compiler(root: Path) -> HostCompiler | None:
    """Fall back to the compiler recorded by a completed slave IDF configure."""
    database = root / "slave/build/compile_commands.json"
    if not database.is_file():
        return None
    try:
        import json
        import shlex

        entry = next(
            item
            for item in json.loads(database.read_text(encoding="utf-8"))
            if Path(item["file"]).name == "app_main.c"
        )
        arguments = entry.get("arguments") or shlex.split(entry["command"], posix=os.name != "nt")
    except (OSError, ValueError, KeyError, StopIteration):
        print(
            f"[c_toolchain] {database} is unreadable or has no app_main.c entry; ignoring it",
            flush=True,
        )
        return None
    # The IDF entry passes target flags this probe must not reuse, so take the
    # compiler executable only and probe it on its own.
    candidate = arguments[0].strip('"')
    if not Path(candidate).is_file():
        print(
            f"[c_toolchain] {database} names a compiler that is gone: {candidate}",
            flush=True,
        )
        return None
    return _probe((candidate,), f"IDF build database {database}", False)


def _candidates(root: Path) -> list[HostCompiler]:
    found: list[HostCompiler] = []
    seen: set[str] = set()

    def add(compiler: HostCompiler | None) -> None:
        if compiler is None:
            return
        key = os.path.normcase(os.path.abspath(compiler.executable))
        if key not in seen:
            seen.add(key)
            found.append(compiler)

    for name in ENVIRONMENT_OVERRIDES:
        value = os.environ.get(name)
        if value:
            add(_probe((value,), f"{name} environment variable", False))
    for name in PATH_NAMES:
        located = shutil.which(name)
        if located:
            add(_probe((located,), f"{name} on PATH", False))
    add(_zig_compiler(root))
    add(_database_compiler(root))
    return found


def resolve_c_compiler(root: Path | None = None) -> HostCompiler:
    """Return a working compiler, or fail loudly with the exact fix to apply.

    Raises:
        RuntimeError: no candidate compiler ran successfully.
    """
    root = root or Path(__file__).resolve().parents[2]
    candidates = _candidates(root)
    if not candidates:
        raise RuntimeError(
            "No native C compiler was found, so the firmware C tests cannot run. "
            "These tests are not optional: they compile the real firmware sources and are "
            "the only coverage for the journal, lease and topology state machines. "
            f"Install a compiler or set CABLE_HOST_CC, for example: {INSTALL_HINT}\n"
            f"Probed: {', '.join(ENVIRONMENT_OVERRIDES)}; {', '.join(PATH_NAMES)} on PATH; "
            f"a vendored Zig under {root / 'host/tests/.tools/ziglang'}; "
            f"and {root / 'slave/build/compile_commands.json'}"
        )
    return candidates[0]


def build_environment(compiler: HostCompiler, root: Path | None = None) -> dict[str, str]:
    """Return the child environment for one compiler invocation.

    A compiler that was located by name still needs its own directory on PATH,
    and a vendored Zig needs private caches so the repository stays clean.
    """
    root = root or Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    executable = compiler.executable
    if os.sep not in executable and not (os.altsep and os.altsep in executable):
        environment["PATH"] = str(Path(executable).parent) + os.pathsep + environment.get("PATH", "")
    if compiler.is_zig:
        cache = root / "host/tests/.tools/zig-cache"
        environment["ZIG_LOCAL_CACHE_DIR"] = str(cache / "local")
        environment["ZIG_GLOBAL_CACHE_DIR"] = str(cache / "global")
    return environment
