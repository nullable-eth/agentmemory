"""Import watcher: poll <VAULT_ROOT>/.imports/ for export zips and run the
vendored pipeline on a stable batch. CIFS copies are slow, so a batch only
counts once every zip has identical (mtime, size) on two consecutive polls —
a zip seen only once is never imported. The pipeline archives the inbox
itself on success; this loop only triggers and observes."""
import asyncio
import logging
import re
import sys
import time
from pathlib import Path

import scanner
from config import IMPORT_INTERVAL_S, VAULT_ROOT
from metrics import CHECK_FAILS, IMPORT_RUNS, LAST_IMPORT

log = logging.getLogger("agentmemory")

INBOX = Path(VAULT_ROOT) / ".imports"
PIPELINE = Path(__file__).parent / "importer" / "import_export.py"
_lock = asyncio.Lock()


def _zip_stats() -> dict:
    """Map each inbox zip to (mtime, size); files that vanish mid-scan are skipped."""
    stats = {}
    for p in INBOX.glob("*.zip"):
        try:
            st = p.stat()
        except OSError:
            continue
        stats[p] = (st.st_mtime, st.st_size)
    return stats


async def _run_import(zips: list[Path]) -> bool:
    # Invoke with NO zip argv: the pipeline then scans the inbox itself, which
    # is the only mode that also archives manifest-*.json alongside the zips
    # (explicit argv leaves manifests orphaned in the inbox). The tkinter
    # picker fallback is unreachable because we only run when zips exist.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(PIPELINE), "--check",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    if proc.returncode == 0:
        log.info("import: pipeline succeeded (%d zip(s)):\n%s", len(zips), text)
        IMPORT_RUNS.labels(result="success").inc()
        LAST_IMPORT.set(time.time())
        await scanner.scan_once()
        await scanner.embed_drain()
    else:
        log.error("import: pipeline failed (exit %d):\n%s", proc.returncode, text)
        IMPORT_RUNS.labels(result="failure").inc()
    if re.search(r"check", text, re.IGNORECASE) and re.search(r"fail", text, re.IGNORECASE):
        CHECK_FAILS.inc()
    return proc.returncode == 0


async def import_loop() -> None:
    last: dict = {}
    failures = 0
    while True:
        try:
            current = await asyncio.to_thread(_zip_stats)
            stable = bool(current) and all(
                last.get(p) == ms for p, ms in current.items())
            last = current
            if stable:
                async with _lock:
                    last = {}          # next batch starts seen-once again
                    ok = await _run_import(sorted(current))
                    failures = 0 if ok else failures + 1
                    if failures >= 3:
                        # Poison batch: quarantine so it stops retrying but is
                        # never deleted. Rename moves it outside the *.zip glob.
                        for p in sorted(current):
                            try:
                                p.rename(p.with_suffix(p.suffix + ".failed"))
                                log.error("import: quarantined %s after %d failures",
                                          p.name, failures)
                            except OSError:
                                log.exception("import: couldn't quarantine %s", p.name)
                        failures = 0
        except Exception:
            log.exception("import loop error")
        await asyncio.sleep(IMPORT_INTERVAL_S)
