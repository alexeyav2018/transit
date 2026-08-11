#!/usr/bin/env python3
"""bundle.py — pack произвольного каталога в один читаемый текстовый файл и обратно.

Пример:

    python3 bundle.py pack .opencode/skills/opentest-new-test
    # → создаётся opentest-new-test.bundle.txt в текущем каталоге

    python3 bundle.py unpack opentest-new-test.bundle.txt
    # → воссоздаёт путь из шапки относительно текущего каталога

    python3 bundle.py unpack opentest-new-test.bundle.txt --out /tmp
    # → воссоздаёт путь относительно /tmp
"""

import argparse
import base64
import os
import re
import shutil
import secrets
import stat
import sys
from datetime import datetime

BUNDLE_VERSION = "BUNDLE v1"

IGNORE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
IGNORE_FILES = {".DS_Store", "Thumbs.db"}
IGNORE_SUFFIXES = (".pyc", ".pyo")

OPEN_RE = re.compile(
    r"^===== FILE: (.+?) \((text|base64)(?:, mode=(\d+))?\) =====$"
)


def should_ignore(name, is_dir, ignore_enabled):
    if not ignore_enabled:
        return False
    if is_dir:
        return name in IGNORE_DIRS
    if name in IGNORE_FILES:
        return True
    return name.endswith(IGNORE_SUFFIXES)


def collect_files(src_dir, ignore_enabled):
    out = []
    for root, dirs, fnames in os.walk(src_dir):
        dirs[:] = sorted(
            d for d in dirs if not should_ignore(d, True, ignore_enabled)
        )
        for fn in sorted(fnames):
            if should_ignore(fn, False, ignore_enabled):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, src_dir)
            out.append((rel, full))
    out.sort(key=lambda x: x[0])
    return out


def read_file(path):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return ("text", raw.decode("utf-8"))
    except UnicodeDecodeError:
        return ("base64", base64.b64encode(raw).decode("ascii"))


def cmd_pack(args):
    src_dir = args.dir
    if not os.path.isdir(src_dir):
        print(f"error: каталог не найден: {src_dir}", file=sys.stderr)
        return 1

    abspath = os.path.abspath(src_dir)
    name = os.path.basename(abspath)
    rel_source = os.path.relpath(abspath, os.getcwd())
    nonce = secrets.token_hex(3)

    files = collect_files(abspath, ignore_enabled=not args.all)

    out_path = args.output or os.path.join(os.getcwd(), f"{name}.bundle.txt")
    out_path = os.path.abspath(out_path)

    lines = [
        f"# {BUNDLE_VERSION}",
        f"# name: {name}",
        f"# path: {rel_source}",
        f"# packed: {datetime.now().isoformat(timespec='seconds')}",
        f"# files: {len(files)}",
        f"# nonce: {nonce}",
        "",
    ]

    for rel, full in files:
        kind, content = read_file(full)
        mode = stat.S_IMODE(os.stat(full).st_mode)
        lines.append(f"===== FILE: {rel} ({kind}, mode={mode:o}) =====")
        lines.append(content)
        lines.append(f"===== END:{nonce} =====")
        lines.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Packed {len(files)} files → {out_path}")
    return 0


def write_extracted(target_root, rel, kind, mode, content):
    full = os.path.join(target_root, rel)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if kind == "text":
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    else:
        raw = base64.b64decode(content)
        with open(full, "wb") as f:
            f.write(raw)
    if mode is not None:
        try:
            os.chmod(full, mode)
        except OSError:
            pass


def cmd_unpack(args):
    bundle = args.bundle
    if not os.path.isfile(bundle):
        print(f"error: файл не найден: {bundle}", file=sys.stderr)
        return 1

    with open(bundle, "r", encoding="utf-8") as f:
        text = f.read()
    lines = text.split("\n")

    header = {}
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.startswith("# "):
            key, _, val = line[2:].partition(":")
            header[key.strip()] = val.strip()
            idx += 1
        elif line.strip() == "":
            idx += 1
            break
        else:
            break

    required = {"name", "path", "nonce", "files"}
    missing = required - set(header)
    if missing:
        print(
            f"error: в шапке бандла не хватает ключей: {sorted(missing)}",
            file=sys.stderr,
        )
        return 1

    nonce = header["nonce"]
    try:
        expected = int(header["files"])
    except ValueError:
        print(f"error: некорректное files в шапке: {header['files']!r}", file=sys.stderr)
        return 1
    bundle_path = header["path"]

    root = args.out if args.out else os.getcwd()
    target = os.path.normpath(os.path.join(root, bundle_path))

    if os.path.lexists(target):
        if not args.force:
            print(
                f"error: целевой путь существует: {target} (используйте --force)",
                file=sys.stderr,
            )
            return 1
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.unlink(target)

    os.makedirs(target, exist_ok=True)

    close_re = re.compile(rf"^===== END:{re.escape(nonce)} =====$")

    count = 0
    cur_rel = cur_kind = cur_mode = None
    buf = []
    in_block = False

    for line in lines[idx:]:
        if not in_block:
            m = OPEN_RE.match(line)
            if m:
                cur_rel = m.group(1)
                cur_kind = m.group(2)
                mode_str = m.group(3)
                cur_mode = int(mode_str, 8) if mode_str else None
                buf = []
                in_block = True
            continue
        if close_re.match(line):
            content = "\n".join(buf)
            write_extracted(target, cur_rel, cur_kind, cur_mode, content)
            count += 1
            in_block = False
            cur_rel = cur_kind = cur_mode = None
            buf = []
            continue
        buf.append(line)

    if in_block:
        print(f"error: незакрытый блок для {cur_rel}", file=sys.stderr)
        return 1

    if count != expected:
        print(
            f"error: несовпадение числа файлов: распаковано {count}, в шапке {expected}",
            file=sys.stderr,
        )
        return 1

    print(f"Unpacked {count} files → {target}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="bundle.py",
        description="Упаковка каталога в один текстовый файл и обратно.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pack = sub.add_parser("pack", help="упаковать каталог в .bundle.txt")
    p_pack.add_argument("dir", help="путь к каталогу-источнику")
    p_pack.add_argument(
        "-o", "--output", default=None,
        help="имя выходного файла (по умолчанию <name>.bundle.txt в CWD)",
    )
    p_pack.add_argument(
        "--all", action="store_true",
        help="не применять фильтр кэша/мусора (упаковать всё)",
    )
    p_pack.set_defaults(func=cmd_pack)

    p_unpack = sub.add_parser("unpack", help="распаковать .bundle.txt в каталог")
    p_unpack.add_argument("bundle", help="путь к .bundle.txt")
    p_unpack.add_argument(
        "--out", default=None,
        help="корень для распаковки (по умолчанию текущий каталог)",
    )
    p_unpack.add_argument(
        "--force", action="store_true",
        help="перезаписать, если целевой каталог существует",
    )
    p_unpack.set_defaults(func=cmd_unpack)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
