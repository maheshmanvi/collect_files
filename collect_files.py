#!/usr/bin/env python3
from __future__ import annotations

"""
collect_files.py

CLI utility that reads all (text) files from a file or directory tree and writes their contents
into a single outputs text file. Designed to be fast, robust, and user friendly.

Features:
- Accepts a file or directory as input (multiple inputs allowed).
- If outputs path is a directory, creates a default file "collected_files_<timestamp>.txt".
- Traverses subfolders recursively by default. Accepts --scale / --depth to limit traversal depth.
  (scale=1 -> only root + one level deep; scale=0 -> root files only).
- Attempts to read many common text encodings; falls back to binary-safe 'utf-8' with replacement.
- Detects binary files (skips them, logs skip reason). Uses null-byte heuristic.
- Writes a human-friendly header before each file:
      (two new lines) ----
      /path/to/file
  then the file contents.
- Shows progress bar using 'rich' (preferred), falls back to 'tqdm', then simple console progress.
- Supports options: --max-size to skip huge files, --include-hidden, --follow-symlinks, --append/--overwrite.
- Handles exceptions robustly and outputs a final summary.
- Fast streaming write (doesn't load entire directory into memory unnecessarily).
- Safe against symlink loops via seen inode tracking.

------------------------------
collect_files.py (patched)

Improvements:
- Robust inode/seen-key logic for Windows: when st_ino/st_dev look invalid (0/None),
  falls back to using the file's resolved path as a unique key. This prevents skipping
  everything on filesystems where st_ino/st_dev are not populated.
- Added --debug-discovery flag to print discovered files and why they may be skipped.
- Preserves the rest of the behaviour from the previous version (progress UI, encoding handling, etc).
"""
import argparse
import os
import sys
import time
import traceback
from pathlib2 import Path
from typing import Iterable, Iterator, List, Optional, Tuple
import threading

# Optional nice UI libraries
try:
    from rich.console import Console
    from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
    console = Console()
    RICH_AVAILABLE = True
except Exception:
    RICH_AVAILABLE = False
    console = None

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except Exception:
    TQDM_AVAILABLE = False

# Helpers
def is_hidden(path: Path) -> bool:
    if path.name.startswith('.'):
        return True
    try:
        if os.name == 'nt':
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs == -1:
                return False
            return bool(attrs & 2)  # FILE_ATTRIBUTE_HIDDEN == 2
    except Exception:
        pass
    return False

def human_size(n: int) -> str:
    for unit in ['B','KB','MB','GB','TB']:
        if n < 1024.0:
            return f"{n:3.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}PB"

def looks_binary_bytes(data: bytes) -> bool:
    if not data:
        return False
    sample = data[:1024]
    if b'\x00' in sample:
        return True
    text_chars = bytearray({7,8,9,10,12,13,27} | set(range(0x20, 0x100)))
    nontext = sum(1 for b in sample if b not in text_chars)
    ratio = nontext / max(1, len(sample))
    return ratio > 0.30

COMMON_ENCODINGS = ['utf-8', 'utf-8-sig', 'utf-16', 'utf-16-le', 'utf-16-be', 'latin-1', 'cp1252']

def try_decode(data: bytes) -> Tuple[str, str]:
    for enc in COMMON_ENCODINGS:
        try:
            return data.decode(enc), enc
        except Exception:
            continue
    try:
        return data.decode('utf-8', errors='replace'), 'utf-8-replace'
    except Exception:
        return data.decode('latin-1', errors='replace'), 'latin1-replace'

# ---------- Fixed discovery function ----------
def discover_files(
    inputs: Iterable[Path],
    include_hidden: bool = False,
    follow_symlinks: bool = False,
    max_depth: Optional[int] = None,
    debug_print: bool = False,
) -> Iterator[Path]:
    """
    Walk inputs (files or dirs) and yield file paths respecting depth (None=unlimited).
    This function avoids using st_ino/st_dev when those values are not trustworthy (e.g. zeros on some Windows filesystems).
    """
    seen_keys = set()
    for root in inputs:
        if root.is_file():
            if debug_print:
                print("DISCOVER: root is file ->", root)
            yield root
            continue
        if not root.exists():
            if debug_print:
                print("DISCOVER: root does not exist ->", root)
            continue

        stack = [(root, 0)]
        while stack:
            current_dir, depth = stack.pop()
            try:
                if not include_hidden and is_hidden(current_dir):
                    if debug_print:
                        print("DISCOVER: skip hidden dir ->", current_dir)
                    continue
            except Exception:
                pass

            try:
                with os.scandir(current_dir) as it:
                    entries = list(it)
            except Exception as e:
                if debug_print:
                    print("DISCOVER: scandir failed on", current_dir, ":", e)
                continue

            for entry in entries:
                try:
                    p = Path(entry.path)
                except Exception:
                    continue

                try:
                    if not include_hidden and is_hidden(p):
                        if debug_print:
                            print("DISCOVER: skip hidden ->", p)
                        continue
                except Exception:
                    pass

                # Attempt to get stat info
                try:
                    st = entry.stat(follow_symlinks=follow_symlinks)
                    st_ino = getattr(st, "st_ino", None)
                    st_dev = getattr(st, "st_dev", None)
                except Exception:
                    st = None
                    st_ino = None
                    st_dev = None

                # Build a robust unique key for seen-checking:
                # Prefer (ino, dev) when both look valid and non-zero.
                # Otherwise fall back to resolved absolute path string.
                use_path_key_fallback = False
                if st_ino in (None,) or st_dev in (None,):
                    use_path_key_fallback = True
                else:
                    if (st_ino == 0 and st_dev == 0):
                        use_path_key_fallback = True

                if use_path_key_fallback:
                    try:
                        key = ("path", str(p.resolve()))
                    except Exception:
                        key = ("path", str(p.absolute()))
                else:
                    key = ("inode", int(st_ino), int(st_dev))

                if key in seen_keys:
                    if debug_print:
                        print("DISCOVER: skipping seen key ->", p, "key=", key)
                    continue
                seen_keys.add(key)

                # Now handle file/dir
                try:
                    is_f = entry.is_file(follow_symlinks=follow_symlinks)
                    is_d = entry.is_dir(follow_symlinks=follow_symlinks)
                except Exception:
                    # fallback checks
                    is_f = p.is_file()
                    is_d = p.is_dir()

                if is_f:
                    if debug_print:
                        print("DISCOVER: file ->", p)
                    yield p
                elif is_d:
                    if debug_print:
                        print("DISCOVER: dir ->", p, "depth=", depth)
                    if max_depth is None or (depth + 1) <= max_depth:
                        stack.append((p, depth + 1))
                    else:
                        if debug_print:
                            print("DISCOVER: depth limit reached, not descending ->", p)
    return

# ---------- Rest of the tool (kept similar to previous release) ----------
def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="collect_files.py", description="Collect textual files into a single outputs file.")
    p.add_argument("input", nargs="+", help="Input file(s) and/or directory(ies).")
    p.add_argument("-o", "--outputs", required=False, default=None, help="Output file path. If a directory is given, a default file will be created inside it.")
    p.add_argument("--scale", "--depth", dest="scale", type=int, default=None, help="How many directory levels to go into (scale). Default: unlimited.")
    p.add_argument("--include-hidden", action="store_true", default=False, help="Include hidden files and directories.")
    p.add_argument("--follow-symlinks", action="store_true", default=False, help="Follow symbolic links.")
    p.add_argument("--max-size", type=float, default=200.0, help="Maximum file size (in MB) to read. Default 200 MB.")
    p.add_argument("--append", action="store_true", default=False, help="Append to outputs file if it exists.")
    p.add_argument("--encoding-report", action="store_true", default=False, help="Print encoding used for each file (best-effort).")
    p.add_argument("--workers", type=int, default=1, help="Reserved for parallelism (1 = sequential).")
    p.add_argument("--verbose", "-v", action="store_true", default=False, help="Verbose logging.")
    p.add_argument("--debug-discovery", action="store_true", default=False, help="Print discovery debug outputs and exit (useful to inspect which files would be processed).")
    return p

class Summary:
    def __init__(self):
        self.total_files = 0
        self.processed = 0
        self.skipped_binary = 0
        self.skipped_large = 0
        self.errors = 0
        self.encoding_examples = {}

class ProgressUI:
    def __init__(self, total: int, description: str = "Processing"):
        self.total = total
        if RICH_AVAILABLE:
            self.progress = Progress(TextColumn("[progress.description]{task.description}"), BarColumn(), "[progress.percentage]{task.percentage:>3.0f}%", TimeElapsedColumn(), TimeRemainingColumn())
            self.progress.start()
            self.task_id = self.progress.add_task(description, total=total)
        elif TQDM_AVAILABLE:
            self.tqdm = tqdm(total=total, desc=description)
        else:
            self.lock = threading.Lock()
            self.current = 0
            print(f"{description}: 0/{total}", end="", flush=True)

    def advance(self, n=1):
        if RICH_AVAILABLE:
            self.progress.update(self.task_id, advance=n)
        elif TQDM_AVAILABLE:
            self.tqdm.update(n)
        else:
            with self.lock:
                self.current += n
                print(f"\rProcessing: {self.current}/{self.total}", end="", flush=True)

    def stop(self):
        if RICH_AVAILABLE:
            self.progress.stop()
        elif TQDM_AVAILABLE:
            self.tqdm.close()
        else:
            print()

def prepare_output_path(output: Optional[str]) -> Path:
    if output:
        outp = Path(output)
        if outp.exists() and outp.is_dir():
            ts = time.strftime("%Y%m%d_%H%M%S")
            return outp.joinpath(f"collected_files_{ts}.txt")
        else:
            parent = outp.parent
            if parent and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
            return outp
    else:
        ts = time.strftime("%Y%m%d_%H%M%S")
        return Path.cwd().joinpath(f"collected_files_{ts}.txt")

def safe_open_for_write(path: Path, append: bool):
    mode = "ab" if append else "wb"
    try:
        f = open(path, mode)
        return f
    except Exception as e:
        raise RuntimeError(f"Cannot open outputs file {path!s} for writing: {e}")

def gather_file_list(inputs: List[Path], discover_opts, debug_discovery: bool = False) -> List[Path]:
    files = []
    for p in discover_files(inputs, debug_print=debug_discovery, **discover_opts):
        if p.exists() and p.is_file():
            files.append(p)
    return files

def process_files_sequential(file_list: List[Path], out_fh, summary: Summary, args, ui: ProgressUI):
    max_size_bytes = int(args.max_size * 1024 * 1024) if args.max_size else None
    for path in file_list:
        summary.total_files += 1
    for path in file_list:
        try:
            try:
                st = path.stat()
                fsize = st.st_size
            except Exception:
                fsize = -1
            if max_size_bytes is not None and fsize >= 0 and fsize > max_size_bytes:
                summary.skipped_large += 1
                if args.verbose:
                    _log(f"Skipped (too large {human_size(fsize)}): {path}")
                ui.advance(1)
                continue

            try:
                with open(path, "rb") as r:
                    sample = r.read(8192)
            except Exception as e:
                summary.errors += 1
                if args.verbose:
                    _log(f"Error reading (sample) {path}: {e}")
                ui.advance(1)
                continue

            if looks_binary_bytes(sample):
                summary.skipped_binary += 1
                if args.verbose:
                    _log(f"Skipped (binary-like): {path}")
                ui.advance(1)
                continue

            header = b"\n\n----\n" + str(path).encode("utf-8", errors="replace") + b"\n"
            out_fh.write(header)

            encoding_used = "unknown"
            with open(path, "rb") as r:
                if sample:
                    txt, enc = try_decode(sample)
                    encoding_used = enc
                    out_fh.write(txt.encode("utf-8"))
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    txt, enc = try_decode(chunk)
                    encoding_used = enc
                    out_fh.write(txt.encode("utf-8"))
            if args.encoding_report:
                summary.encoding_examples[path.as_posix()] = encoding_used

            summary.processed += 1
        except Exception as e:
            summary.errors += 1
            if args.verbose:
                _log(f"Error processing {path}: {e}\n{traceback.format_exc()}")
        finally:
            ui.advance(1)

def _log(msg: str):
    if RICH_AVAILABLE:
        console.log(msg)
    else:
        print(msg)

def main():
    parser = build_argparser()
    args = parser.parse_args()

    inputs = [Path(p).expanduser().resolve() for p in args.input]
    for p in inputs:
        if not p.exists():
            print(f"Warning: input {p} does not exist and will be skipped.", file=sys.stderr)

    out_path = prepare_output_path(args.output)
    append_mode = args.append and out_path.exists()

    discover_opts = {
        "include_hidden": args.include_hidden,
        "follow_symlinks": args.follow_symlinks,
        "max_depth": (None if args.scale is None else args.scale)
    }

    # If user requested debug discovery only, print discovered entries and exit
    if args.debug_discovery:
        print("Running discovery in debug mode...")
        for p in discover_files(inputs, debug_print=True, **discover_opts):
            print(" WOULD-PROCESS:", p)
        print("Debug discovery finished.")
        return

    try:
        file_list = gather_file_list(inputs, discover_opts, debug_discovery=False)
    except Exception as e:
        print(f"Failed during discovery: {e}", file=sys.stderr)
        sys.exit(2)

    # Filter out outputs file if inside inputs
    try:
        absolute_out = out_path.resolve()
        file_list = [p for p in file_list if p.resolve() != absolute_out]
    except Exception:
        pass

    if not file_list:
        print("No files found to process. Exiting.")
        sys.exit(0)

    total_files = len(file_list)
    ui = ProgressUI(total_files, description="Collecting files")
    try:
        out_fh = safe_open_for_write(out_path, append=append_mode)
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(3)

    summary = Summary()
    try:
        if not append_mode:
            top_banner = f"# Collected files outputs generated on {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode("utf-8")
            out_fh.write(top_banner)
        process_files_sequential(file_list, out_fh, summary, args, ui)
    finally:
        try:
            out_fh.close()
        except Exception:
            pass
        ui.stop()

    print("\n\nSummary:")
    print(f"  Files discovered: {total_files}")
    print(f"  Files processed:  {summary.processed}")
    if summary.skipped_binary:
        print(f"  Skipped (binary-like): {summary.skipped_binary}")
    if summary.skipped_large:
        print(f"  Skipped (too large): {summary.skipped_large}")
    if summary.errors:
        print(f"  Errors: {summary.errors}")
    try:
        print(f"  Output file: {out_path}  (size: {human_size(out_path.stat().st_size)})")
    except Exception:
        print(f"  Output file: {out_path}")
    if args.encoding_report and summary.encoding_examples:
        print("\nEncodings detected (sample):")
        shown = 0
        for p, enc in list(summary.encoding_examples.items())[:10]:
            print(f"  {p} -> {enc}")
            shown += 1
        if len(summary.encoding_examples) > shown:
            print(f"  ... and {len(summary.encoding_examples)-shown} more")
    print("\nDone.")

if __name__ == "__main__":
    main()
