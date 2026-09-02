#!/usr/bin/env python3
r"""
import_export.py -- importer for Anthropic data-export zips.

WHAT IT IS FOR
  Export zips land in <vault>/.imports/ -- put there by download_export.py
  (manifest-style exports), by hand, or by dragging zips onto the .cmd. This
  script stages them, merges multi-part/category exports into one coherent
  shape, and hands the result to unpack_export.py, which renders everything
  into <vault>/.staging/ awaiting filing approval.

  Downloading is NOT this script's job: the one-time manifest URLs sit behind
  a Cloudflare browser check, which only the desktop can pass -- see
  download_export.py. This half is desktop- and cluster-runnable.

WHAT IT DOES
  1. Collects zips: command-line args, or everything in .imports/, or a file
     picker as a last resort.
  2. Extracts into a scratch dir in %TEMP% -- never inside the vault.
  3. Merges parts. Old-style "batch-NNNN" zips each carry conversations.json;
     new-style category zips split by kind (conversations / projects /
     memories / light_metadata); split conversation sets
     (conversations/_index.json) are folded into one conversations.json.
     Duplicate conversations: newest updated_at wins.
  4. Runs unpack_export.py (renders into .staging/, idempotent).
  5. Archives the zip(s) + any manifest into .imports/archive/ so the inbox
     empties completely.
  6. Deletes the scratch dir.

USAGE
  Double-click "Import Claude Export.cmd", or drag zips onto it, or:
    py -3 import_export.py [zip ...] [--dry-run] [--account Team]
                           [--keep-scratch] [--no-file]
  Flags not listed pass through to unpack_export.py (e.g. --check).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

VAULT = Path(os.environ.get("VAULT_ROOT", "/vault"))
UNC_VAULT = VAULT   # single canonical path; set VAULT_ROOT to relocate
TOOL_DIR = Path(__file__).resolve().parent
UNC_TOOL_DIR = TOOL_DIR

INBOX = ".imports"                 # under <vault>/
ARCHIVE = "archive"                # under <vault>/.imports/
PY = sys.executable or "py"


def die(msg, code=1):
    print(f"\n!! {msg}")
    raise SystemExit(code)


def resolve_paths():
    global VAULT, TOOL_DIR
    if not VAULT.exists() and UNC_VAULT.exists():
        VAULT = UNC_VAULT
    if not TOOL_DIR.exists() and UNC_TOOL_DIR.exists():
        TOOL_DIR = UNC_TOOL_DIR
    problems = []
    if not VAULT.exists():
        problems.append(f"vault not found: {VAULT}")
    if not (TOOL_DIR / "unpack_export.py").exists():
        problems.append(f"unpack_export.py not found in: {TOOL_DIR}")
    if problems:
        die("\n".join(problems) +
            "\n\nIs the NAS reachable / is Z: mapped? Edit the paths at the top "
            "of this script if things have moved.")


def pick_zips():
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        die("tkinter isn't available, so I can't show a file picker.\n"
            "Pass the zip path(s) on the command line, or drop them in "
            f"{VAULT / INBOX} and re-run.")
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    picked = filedialog.askopenfilenames(
        title="Select the Claude export zip(s)",
        initialdir=str(VAULT / INBOX),
        filetypes=[("Claude export zip", "*.zip"), ("All files", "*.*")],
    )
    root.destroy()
    return [Path(p) for p in picked]


# --------------------------------------------------------------------------- staging
EXPORT_FILE_MARKERS = ("conversations.json", "memories.json", "users.json")


def export_roots(tree: Path):
    """Dirs that look like (part of) an export. New category zips carry only
    their own kind, so ANY marker qualifies a root."""
    roots = set()
    for name in EXPORT_FILE_MARKERS:
        for f in tree.rglob(name):
            roots.add(f.parent)
    for f in tree.rglob("_index.json"):
        if f.parent.name == "conversations":
            roots.add(f.parent.parent)
    for d in tree.rglob("projects"):
        if d.is_dir() and any(d.glob("*.json")):
            roots.add(d.parent)
    return sorted(roots)


def extract_all(zips, scratch: Path):
    export_dirs = []
    for i, z in enumerate(zips):
        if not z.exists():
            die(f"no such file: {z}")
        if not zipfile.is_zipfile(z):
            die(f"not a zip file: {z}")
        dest = scratch / f"part{i:02d}"
        dest.mkdir(parents=True, exist_ok=True)
        print(f"  extracting {z.name} ...")
        with zipfile.ZipFile(z) as zf:
            zf.extractall(dest)
        roots = export_roots(dest)
        if not roots:
            print(f"     [note] {z.name} carries no conversations/projects/"
                  "memories/users data -- skipping it.")
            continue
        export_dirs.extend(roots)
    if not export_dirs:
        die("None of the selected files contained recognizable export data.")
    return export_dirs


def iter_conversations(d: Path):
    """Yield conversations whether the export ships one monolithic
    conversations.json or a split conversations/ folder."""
    cdir = d / "conversations"
    idx = cdir / "_index.json"
    if idx.exists():
        try:
            entries = json.loads(idx.read_text(encoding="utf-8")).get("entries") or []
        except Exception:
            entries = []
        if entries:
            for e in entries:
                p = cdir / (e.get("file") or "")
                if p.exists():
                    try:
                        yield json.loads(p.read_text(encoding="utf-8"))
                    except Exception:
                        print(f"     [warn] unreadable conversation file: {p.name}")
            return
    if cdir.is_dir():
        split = sorted(f for f in cdir.glob("*.json") if f.name != "_index.json")
        if split:
            for p in split:
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    print(f"     [warn] unreadable conversation file: {p.name}")
                    continue
                if isinstance(data, list):
                    yield from data
                elif isinstance(data, dict):
                    yield data
            return
    mono = d / "conversations.json"
    if mono.exists():
        try:
            data = json.loads(mono.read_text(encoding="utf-8"))
        except Exception as e:
            die(f"couldn't parse {mono}: {e}\n"
                "The download may be truncated -- try re-downloading the export.")
        if not isinstance(data, list):
            die(f"{mono} isn't a JSON list; unexpected export format.")
        yield from data


def merge_exports(export_dirs, merged: Path):
    """Fold parts into one staging export dir: monolithic conversations.json +
    projects/ + memories.json, so the renderer sees exactly one shape."""
    merged.mkdir(parents=True, exist_ok=True)

    by_uuid = {}
    for d in export_dirs:
        for c in iter_conversations(d):
            u = c.get("uuid")
            if not u:
                continue
            prev = by_uuid.get(u)
            if prev is None or (c.get("updated_at") or "") > (prev.get("updated_at") or ""):
                by_uuid[u] = c
    (merged / "conversations.json").write_text(
        json.dumps(list(by_uuid.values()), ensure_ascii=False),
        encoding="utf-8", newline="\n")
    if not by_uuid:
        print("     [warn] no conversations found in any part -- continuing, "
              "in case this export is projects/memories only.")

    pdir = merged / "projects"
    pdir.mkdir(exist_ok=True)
    fresh = set()
    for d in export_dirs:
        src = d / "projects"
        if src.is_dir():
            for f in src.glob("*.json"):
                shutil.copy2(f, pdir / f.name)
                fresh.add(f.name)

    for name in ("memories.json", "users.json"):
        best, best_size = None, -1
        for d in export_dirs:
            f = d / name
            if f.exists() and f.stat().st_size > best_size:
                best, best_size = f, f.stat().st_size
        if best:
            shutil.copy2(best, merged / name)
        elif name == "memories.json":
            (merged / "memories.json").write_text(
                json.dumps([{"project_memories": {}}]), encoding="utf-8", newline="\n")

    print(f"  staged {len(export_dirs)} part(s): {len(by_uuid)} conversations, "
          f"{len(fresh)} project definition(s) in this export")
    return merged, fresh


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("zips", nargs="*",
                    help="Export zip(s). Omit to take everything in .imports/, "
                         "falling back to a file picker.")
    ap.add_argument("--keep-scratch", action="store_true")
    ap.add_argument("--no-file", action="store_true")
    args, passthrough = ap.parse_known_args()
    dry_run = "--dry-run" in passthrough

    print("=" * 74)
    print("  Claude export -> Obsidian vault import")
    print("=" * 74)
    resolve_paths()
    inbox = VAULT / INBOX
    inbox.mkdir(exist_ok=True)
    print(f"  vault: {VAULT}")
    print(f"  inbox: {inbox}")

    zips = [Path(z) for z in args.zips]
    manifests = []
    if not zips:
        zips = sorted(inbox.glob("*.zip"))
        manifests = sorted(inbox.glob("manifest-*.json"))
        if zips:
            print(f"  found {len(zips)} zip(s) in .imports")
    if not zips:
        zips = pick_zips()
    if not zips:
        print("\nNothing to import. Drop export zips in .imports\\ "
              "(download_export.py does this from a manifest) and re-run.")
        return 0

    print(f"\n[1/3] Staging {len(zips)} file(s) ...")
    scratch = Path(tempfile.mkdtemp(prefix="claude_export_"))
    try:
        export_dirs = extract_all(zips, scratch)
        export, _fresh = merge_exports(export_dirs, scratch / "merged")

        print("\n[2/3] Unpacking into .staging ...\n")
        cmd = [PY, str(TOOL_DIR / "unpack_export.py"),
               "--export", str(export), "--vault", str(VAULT)] + passthrough
        if "--account" not in passthrough:
            cmd += ["--account", "Personal"]
        rc = subprocess.run(cmd, cwd=str(TOOL_DIR)).returncode
        if rc != 0:
            die(f"unpack_export.py exited with code {rc}. "
                "Nothing was filed away; the zip(s) are untouched.", rc)

        print("\n[3/3] Tidying up ...")
        if dry_run or args.no_file:
            print("  leaving the zip(s)/manifest where they are "
                  f"({'--dry-run' if dry_run else '--no-file'})")
        else:
            dest = inbox / ARCHIVE
            dest.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d")
            for z in zips + manifests:
                target = dest / f"{stamp} {z.name}"
                n = 1
                while target.exists():
                    target = dest / f"{stamp} {z.stem} ({n}){z.suffix}"
                    n += 1
                try:
                    shutil.move(str(z), str(target))
                    print(f"  archived: {target.name}")
                except Exception as e:
                    print(f"  [warn] couldn't move {z.name}: {e}")
    finally:
        if args.keep_scratch:
            print(f"  scratch kept at: {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)

    print("\n" + "=" * 74)
    print("  Done. Everything landed in .staging\\ — verbatim, and unfiled.")
    print("  Filing is a reading job: see CLAUDE.md. Approved items MOVE into")
    print("  their node; .staging empties as approvals land.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
