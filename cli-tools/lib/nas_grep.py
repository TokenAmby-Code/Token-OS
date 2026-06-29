"""NAS-safe grep wrapper for Token-OS agents.

The CLI is intentionally conservative: it prefers ripgrep when available, but adds
NAS-friendly excludes, bounded output, limited worker threads, and a small shared
lease so multiple agents do not hammer the same NAS mount at once.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EXCLUDE_DIRS = (
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "bower_components",
    ".venv",
    "venv",
    "env",
    ".env",
    ".tox",
    ".nox",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "cache",
    "caches",
    "Cache",
    "Caches",
    "dist",
    "build",
    "target",
    "coverage",
    ".coverage",
    ".next",
    ".nuxt",
    ".turbo",
    ".vite",
    ".parcel-cache",
    "Trash",
    ".Trash",
    ".Trashes",
    "Obsidian Trash",
    "$RECYCLE.BIN",
    "@Recycle",
    "@Recently-Snapshot",
    "#recycle",
    "recycle",
    "Recycling Bin",
)

DEFAULT_EXCLUDE_GLOBS = (
    "*.bak",
    "*.backup",
    "*.tmp",
    "*.temp",
    "*.swp",
    "*.swo",
    "*~",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    "*.pyc",
    "*.pyo",
    "*.class",
    "*.o",
    "*.so",
    "*.dylib",
    "*.dll",
    "*.exe",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
    "*.7z",
    "*.rar",
)

NAS_PREFIXES = (
    "/Volumes/Imperium",
    "/mnt/imperium",
)


@dataclass(frozen=True)
class SearchConfig:
    pattern: str
    paths: tuple[str, ...]
    tool: str = "auto"
    fixed_strings: bool = False
    ignore_case: bool = False
    case_sensitive: bool = False
    hidden: bool = False
    max_results: int = 200
    max_count_per_file: int = 20
    max_filesize: str = "5M"
    threads: int = 2
    timeout: float = 120.0
    lease: bool = False
    dry_run: bool = False
    extra_exclude_dirs: tuple[str, ...] = ()
    extra_exclude_globs: tuple[str, ...] = ()

    @property
    def exclude_dirs(self) -> tuple[str, ...]:
        return (*DEFAULT_EXCLUDE_DIRS, *self.extra_exclude_dirs)

    @property
    def exclude_globs(self) -> tuple[str, ...]:
        return (*DEFAULT_EXCLUDE_GLOBS, *self.extra_exclude_globs)


def parse_size(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)([KkMmGg])?[Bb]?\s*", value)
    if not match:
        raise ValueError(f"invalid size: {value!r}")
    number = int(match.group(1))
    suffix = (match.group(2) or "").lower()
    multiplier = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[suffix]
    return number * multiplier


def clamp_threads(value: int) -> int:
    return max(1, min(value, 4))


def is_nas_path(path: str | Path) -> bool:
    raw = os.fspath(path)
    if raw in ("", "."):
        raw = os.getcwd()
    expanded = os.path.abspath(os.path.expanduser(raw))
    for prefix in NAS_PREFIXES:
        if expanded == prefix or expanded.startswith(prefix + os.sep):
            return True
    return False


def needs_nas_lease(paths: Sequence[str]) -> bool:
    if os.environ.get("NAS_GREP_ALWAYS_LEASE") == "1":
        return True
    return any(is_nas_path(path) for path in paths)


def build_rg_command(config: SearchConfig, rg_path: str = "rg") -> list[str]:
    args = [
        rg_path,
        "--color=never",
        "--line-number",
        "--with-filename",
        "--no-heading",
        "--no-messages",
        "--max-count",
        str(config.max_count_per_file),
        "--max-filesize",
        config.max_filesize,
        "--threads",
        str(clamp_threads(config.threads)),
    ]
    if config.hidden:
        args.append("--hidden")
    if config.fixed_strings:
        args.append("--fixed-strings")
    if config.ignore_case:
        args.append("--ignore-case")
    elif not config.case_sensitive:
        args.append("--smart-case")

    for dirname in config.exclude_dirs:
        args.extend(["--glob", f"!**/{dirname}/**"])
    for glob in config.exclude_globs:
        args.extend(["--glob", f"!{glob}"])

    args.extend(["--", config.pattern])
    args.extend(config.paths)
    return args


class NasGrepLease:
    """Small mkdir-based cross-process lease for NAS grep starts."""

    def __init__(
        self,
        root: Path | None = None,
        max_concurrency: int | None = None,
        ttl_seconds: float | None = None,
        wait_seconds: float | None = None,
        jitter_ms: int | None = None,
    ) -> None:
        self.root = root or Path(os.environ.get("NAS_GREP_LEASE_DIR", "/tmp/token-os-nas-grep"))
        self.max_concurrency = max(
            1, int(max_concurrency or os.environ.get("NAS_GREP_MAX_CONCURRENCY", "2"))
        )
        self.ttl_seconds = float(ttl_seconds or os.environ.get("NAS_GREP_LEASE_TTL", "600"))
        self.wait_seconds = float(wait_seconds or os.environ.get("NAS_GREP_LEASE_WAIT", "30"))
        self.jitter_ms = int(
            jitter_ms if jitter_ms is not None else os.environ.get("NAS_GREP_JITTER_MS", "750")
        )
        self.slot: Path | None = None

    def __enter__(self) -> NasGrepLease:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def acquire(self) -> None:
        if self.jitter_ms > 0:
            time.sleep(random.uniform(0, self.jitter_ms / 1000.0))
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.wait_seconds
        while True:
            self._reap_stale_slots()
            for index in range(self.max_concurrency):
                candidate = self.root / f"slot-{index}"
                try:
                    candidate.mkdir()
                except FileExistsError:
                    continue
                (candidate / "pid").write_text(str(os.getpid()))
                (candidate / "started_at").write_text(str(time.time()))
                self.slot = candidate
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"nas-grep: no NAS search lease available after {self.wait_seconds:.0f}s "
                    f"({self.max_concurrency} concurrent slot(s)); retry later"
                )
            time.sleep(random.uniform(0.25, 0.75))

    def release(self) -> None:
        if self.slot is None:
            return
        shutil.rmtree(self.slot, ignore_errors=True)
        self.slot = None

    def _reap_stale_slots(self) -> None:
        now = time.time()
        for slot in self.root.glob("slot-*"):
            try:
                age = now - slot.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > self.ttl_seconds:
                shutil.rmtree(slot, ignore_errors=True)
                continue
            pid_file = slot / "pid"
            try:
                pid = int(pid_file.read_text().strip())
            except (FileNotFoundError, ValueError):
                continue
            if not _pid_exists(pid):
                shutil.rmtree(slot, ignore_errors=True)


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lower_priority() -> None:
    try:
        os.nice(10)
    except OSError:
        pass


def select_tool(requested: str) -> str:
    if requested == "auto":
        return "rg" if shutil.which("rg") else "python"
    if requested == "rg" and not shutil.which("rg"):
        raise RuntimeError("nas-grep: --tool rg requested but rg is not on PATH")
    return requested


def run_rg(config: SearchConfig, stdout: object = sys.stdout, stderr: object = sys.stderr) -> int:
    rg = shutil.which("rg")
    if rg is None:
        raise RuntimeError("nas-grep: rg is not on PATH")
    command = build_rg_command(config, rg)
    if config.dry_run:
        print(" ".join(_shell_quote(part) for part in command), file=stdout)
        return 0

    proc = subprocess.Popen(  # noqa: S603 - command is assembled without shell=True.
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=lower_priority if hasattr(os, "nice") else None,
    )
    assert proc.stdout is not None
    count = 0
    truncated = False
    deadline = time.monotonic() + config.timeout
    try:
        for line in proc.stdout:
            if time.monotonic() > deadline:
                proc.kill()
                print(f"nas-grep: timed out after {config.timeout:.0f}s", file=stderr)
                return 124
            if count < config.max_results:
                print(line, end="", file=stdout)
                count += 1
                if count % 100 == 0:
                    time.sleep(0.025)
            if count >= config.max_results:
                truncated = True
                proc.terminate()
                break
        try:
            proc.wait(timeout=max(1.0, min(5.0, config.timeout)))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    err = ""
    if proc.stderr is not None:
        err = proc.stderr.read()
    if err:
        print(err, end="", file=stderr)
    if truncated:
        print(f"nas-grep: stopped after --max-results={config.max_results}", file=stderr)
        return 0
    return proc.returncode if proc.returncode is not None else 2


def run_python_search(
    config: SearchConfig, stdout: object = sys.stdout, stderr: object = sys.stderr
) -> int:
    if config.dry_run:
        print("nas-grep: would run Python fallback search", file=stdout)
        return 0
    lower_priority()
    try:
        max_bytes = parse_size(config.max_filesize)
    except ValueError as exc:
        print(f"nas-grep: {exc}", file=stderr)
        return 2

    flags = 0
    if config.ignore_case or (
        not config.case_sensitive and config.pattern.lower() == config.pattern
    ):
        flags |= re.IGNORECASE
    regex = None
    if not config.fixed_strings:
        try:
            regex = re.compile(config.pattern, flags)
        except re.error as exc:
            print(f"nas-grep: invalid regex: {exc}", file=stderr)
            return 2

    needle = config.pattern
    if flags & re.IGNORECASE:
        needle = needle.lower()

    start = time.monotonic()
    matches = 0
    for root in config.paths:
        for file_path in iter_files(Path(root), config):
            if time.monotonic() - start > config.timeout:
                print(f"nas-grep: timed out after {config.timeout:.0f}s", file=stderr)
                return 124
            try:
                if file_path.stat().st_size > max_bytes:
                    continue
            except OSError:
                continue
            file_matches = 0
            try:
                with file_path.open("rb") as handle:
                    sample = handle.read(4096)
                    if b"\0" in sample:
                        continue
                    handle.seek(0)
                    for line_number, raw_line in enumerate(handle, 1):
                        if file_matches >= config.max_count_per_file:
                            break
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                        haystack = line.lower() if flags & re.IGNORECASE else line
                        hit = (
                            needle in haystack
                            if config.fixed_strings
                            else bool(regex and regex.search(line))
                        )
                        if not hit:
                            continue
                        print(f"{file_path}:{line_number}:{line}", file=stdout)
                        matches += 1
                        file_matches += 1
                        if matches % 100 == 0:
                            time.sleep(0.025)
                        if matches >= config.max_results:
                            print(
                                f"nas-grep: stopped after --max-results={config.max_results}",
                                file=stderr,
                            )
                            return 0
            except OSError as exc:
                print(f"nas-grep: skipped {file_path}: {exc}", file=stderr)
                continue
    return 0 if matches else 1


def iter_files(root: Path, config: SearchConfig) -> Iterable[Path]:
    if root.is_file():
        if not is_excluded_file(root, config):
            yield root
        return
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        kept_dirs = []
        for dirname in dirnames:
            if is_excluded_dir(dirname, config):
                continue
            if not config.hidden and dirname.startswith("."):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in filenames:
            if not config.hidden and filename.startswith("."):
                continue
            file_path = current / filename
            if is_excluded_file(file_path, config):
                continue
            yield file_path


def is_excluded_dir(dirname: str, config: SearchConfig) -> bool:
    return dirname in set(config.exclude_dirs)


def is_excluded_file(path: Path, config: SearchConfig) -> bool:
    name = path.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in config.exclude_globs)


def _shell_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=@%+,-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nas-grep",
        description="NAS-safe grep wrapper with conservative excludes, bounded output, and concurrency lease.",
    )
    parser.add_argument("pattern", help="regex pattern, or literal text with --fixed-strings")
    parser.add_argument(
        "paths", nargs="*", default=(".",), help="paths to search; defaults to current directory"
    )
    parser.add_argument(
        "-F", "--fixed-strings", action="store_true", help="treat pattern as literal text"
    )
    parser.add_argument("-i", "--ignore-case", action="store_true", help="case-insensitive search")
    parser.add_argument("--case-sensitive", action="store_true", help="disable smart-case default")
    parser.add_argument(
        "--hidden", action="store_true", help="include hidden files/dirs except explicit excludes"
    )
    parser.add_argument(
        "--max-results", type=int, default=int(os.environ.get("NAS_GREP_MAX_RESULTS", "200"))
    )
    parser.add_argument(
        "--max-count-per-file",
        type=int,
        default=int(os.environ.get("NAS_GREP_MAX_COUNT_PER_FILE", "20")),
    )
    parser.add_argument("--max-filesize", default=os.environ.get("NAS_GREP_MAX_FILESIZE", "5M"))
    parser.add_argument(
        "--threads",
        type=int,
        default=int(os.environ.get("NAS_GREP_THREADS", "2")),
        help="rg worker threads; clamped to 1..4",
    )
    parser.add_argument(
        "--timeout", type=float, default=float(os.environ.get("NAS_GREP_TIMEOUT", "120"))
    )
    parser.add_argument(
        "--tool", choices=("auto", "rg", "python"), default=os.environ.get("NAS_GREP_TOOL", "auto")
    )
    parser.add_argument(
        "--lease", action="store_true", help="force shared NAS lease even for non-NAS paths"
    )
    parser.add_argument("--no-lease", action="store_true", help="skip shared NAS lease")
    parser.add_argument(
        "--exclude-dir", action="append", default=(), help="additional directory name to exclude"
    )
    parser.add_argument(
        "--exclude-glob", action="append", default=(), help="additional file glob to exclude"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print selected backend command/config and exit"
    )
    return parser


def config_from_args(argv: Sequence[str] | None = None) -> SearchConfig:
    args = make_parser().parse_args(argv)
    paths = tuple(args.paths or (".",))
    lease = args.lease or (not args.no_lease and needs_nas_lease(paths))
    return SearchConfig(
        pattern=args.pattern,
        paths=paths,
        tool=args.tool,
        fixed_strings=args.fixed_strings,
        ignore_case=args.ignore_case,
        case_sensitive=args.case_sensitive,
        hidden=args.hidden,
        max_results=max(1, args.max_results),
        max_count_per_file=max(1, args.max_count_per_file),
        max_filesize=args.max_filesize,
        threads=clamp_threads(args.threads),
        timeout=max(1.0, args.timeout),
        lease=lease,
        dry_run=args.dry_run,
        extra_exclude_dirs=tuple(args.exclude_dir),
        extra_exclude_globs=tuple(args.exclude_glob),
    )


def run(config: SearchConfig, stdout: object = sys.stdout, stderr: object = sys.stderr) -> int:
    try:
        tool = select_tool(config.tool)
    except RuntimeError as exc:
        print(str(exc), file=stderr)
        return 2
    if config.lease:
        try:
            with NasGrepLease():
                return _run_selected(tool, config, stdout=stdout, stderr=stderr)
        except TimeoutError as exc:
            print(str(exc), file=stderr)
            return 75
    return _run_selected(tool, config, stdout=stdout, stderr=stderr)


def _run_selected(
    tool: str, config: SearchConfig, stdout: object = sys.stdout, stderr: object = sys.stderr
) -> int:
    if tool == "rg":
        return run_rg(config, stdout=stdout, stderr=stderr)
    return run_python_search(config, stdout=stdout, stderr=stderr)


def main(argv: Sequence[str] | None = None) -> int:
    config = config_from_args(argv)
    return run(config)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
