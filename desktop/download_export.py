#!/usr/bin/env python3
r"""
download_export.py -- desktop helper: manifest json in, export zips landed in
<vault>/.imports/ ready for import.

Anthropic's export (mid-2026 on) delivers a manifest-....json pointing at
several category zips behind ONE-TIME download URLs, gated by a Cloudflare
browser check. No plain HTTP client passes that check, so this script drives
YOUR DEFAULT BROWSER: it opens each URL, watches your real Downloads folder
(resolved from the registry -- redirected/OneDrive Downloads folders are
handled) until the zip lands, then moves it into the vault's .imports inbox
alongside the manifest.

Single-use URL safety: a URL is opened at most once. Zips already present (in
Downloads since the manifest was created, or already in .imports) are reused,
never re-fetched. If a run dies partway, just re-run it.

This runs on the desktop only. The cluster's AgentMemory service picks up
whatever lands in .imports; it cannot click through a browser check itself.

USAGE
  py -3 download_export.py [manifest.json]
  Omit the argument to use the newest manifest-*.json found in .imports, the
  vault root, or Downloads (in that order).
"""
import json
import os
import re
import sys
import time
import webbrowser
from pathlib import Path

VAULT = Path(os.environ.get("VAULT_ROOT", r"V:\Vault"))  # set VAULT_ROOT to your vault
UNC_VAULT = VAULT
INBOX = ".imports"
DOWNLOAD_TIMEOUT = 900
MANIFEST_GLOB = "manifest-*.json"


def die(msg, code=1):
    print(f"\n!! {msg}")
    raise SystemExit(code)


def vault():
    v = VAULT if VAULT.exists() else UNC_VAULT
    if not v.exists():
        die(f"vault not found at {VAULT} or {UNC_VAULT}")
    return v


def downloads_dir():
    """The browser's real Downloads folder. %USERPROFILE%\\Downloads is only a
    default -- redirected folders (OneDrive et al) live in the registry."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        raw, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
        winreg.CloseKey(key)
        import os
        p = Path(os.path.expandvars(raw))
        if p.exists():
            return p
    except Exception:
        pass
    return Path.home() / "Downloads"


def find_manifest(v: Path):
    cands = []
    for d in (v / INBOX, v, downloads_dir()):
        if d.exists():
            cands += list(d.glob(MANIFEST_GLOB))
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def parse_manifest(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        die(f"couldn't parse manifest {path.name}: {e}")
    entries = data.get("data_files")
    if not isinstance(entries, list) or not entries:
        die(f"{path.name} has no data_files list -- not an export manifest?")
    for e in entries:
        if not isinstance(e, dict) or not e.get("filename"):
            die(f"{path.name} has a malformed data_files entry: {e!r}")
    return sorted(entries, key=lambda e: e.get("batch_index", 0))


def _candidates(dl: Path, filename: str, min_mtime: float):
    stem, suffix = Path(filename).stem, Path(filename).suffix
    out = []
    for p in dl.glob(f"{stem}*{suffix}"):
        try:
            if p.is_file() and p.stat().st_mtime >= min_mtime:
                out.append(p)
        except OSError:
            continue
    return out


def _in_progress(dl: Path, filename: str):
    stem = Path(filename).stem
    return (list(dl.glob(f"{stem}*.crdownload")) + list(dl.glob(f"{stem}*.part")))


def wait_for_download(dl: Path, filename: str, started: float,
                      timeout=DOWNLOAD_TIMEOUT):
    last_sizes = {}
    deadline = time.time() + timeout
    reminded = False
    while time.time() < deadline:
        busy = _in_progress(dl, filename)
        done = []
        for p in _candidates(dl, filename, started - 5):
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            if sz > 0 and last_sizes.get(p) == sz and not busy:
                done.append(p)
            last_sizes[p] = sz
        if done:
            return max(done, key=lambda p: p.stat().st_mtime)
        if not reminded and time.time() - started > 20 and not busy and not last_sizes:
            print("     ... nothing landing yet. If the browser shows a "
                  "Cloudflare check or login page, click through it; also check "
                  "for a 'download multiple files' permission bar and Allow it.")
            reminded = True
        time.sleep(2)
    return None


def main():
    v = vault()
    inbox = v / INBOX
    inbox.mkdir(exist_ok=True)
    dl = downloads_dir()

    manifest = Path(sys.argv[1]) if len(sys.argv) > 1 else find_manifest(v)
    if not manifest or not manifest.exists():
        die("no manifest found. Pass its path, or drop it in .imports\\ first.")

    entries = parse_manifest(manifest)
    m_mtime = manifest.stat().st_mtime
    print(f"manifest: {manifest}")
    print(f"downloads folder (registry-resolved): {dl}")
    print(f"{len(entries)} zip(s); URLs are SINGLE-USE -- existing files are "
          "reused, never re-fetched.\n")

    for e in entries:
        fname = e["filename"]
        if (inbox / fname).exists():
            print(f"  =  {fname}: already in .imports")
            continue
        have = _candidates(dl, fname, m_mtime - 60)
        if not have:
            url = e.get("export_url")
            if not url:
                die(f"manifest entry {fname} has no export_url")
            print(f"  v  {fname}: opening one-time URL in your browser ...")
            started = time.time()
            webbrowser.open(url)
            got = wait_for_download(dl, fname, started)
            if got is None:
                die(f"timed out waiting for {fname} in {dl}.\n"
                    "Finish any browser prompt, let the download complete, and "
                    "re-run -- completed zips are picked up, burned URLs never "
                    "reopened.")
            have = [got]
        z = max(have, key=lambda p: p.stat().st_mtime)
        dest = inbox / fname
        print(f"  -> .imports\\{fname} ({z.stat().st_size:,} bytes)")
        z.replace(dest) if z.drive == dest.drive else (dest.write_bytes(z.read_bytes()), z.unlink())

    if manifest.parent != inbox:
        target = inbox / manifest.name
        if not target.exists():
            manifest.replace(target) if manifest.drive == target.drive else \
                (target.write_bytes(manifest.read_bytes()), manifest.unlink())
        print(f"  -> .imports\\{manifest.name}")

    print("\nAll parts staged in .imports\\. Run 'Import Claude Export.cmd' "
          "(or let the cluster service pick them up).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
