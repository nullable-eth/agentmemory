#!/usr/bin/env python3
r"""
backfill_uuids.py -- give every transcript a source_uuid.

WHY
  `source_uuid` is the identity key: it's what makes re-importing an export a
  no-op instead of a duplicate. Transcripts archived by the retired pipeline had
  it stripped during post-processing, leaving the importer to fall back on
  normalized title+date matching for them. That fallback works, but it means two
  classes of record with different guarantees, and a caveat that has to be
  carried in the documentation forever.

  These legacy transcripts came from an account that no longer exists, so their
  original Anthropic uuids are unrecoverable and those conversations will never
  appear in a future export. Assigning a stable synthetic uuid costs nothing and
  makes every record in the vault look and behave the same.

HOW
  UUIDv5 over a fixed namespace, derived from the transcript's normalized title
  and date. Deterministic: running this twice produces the same uuid, and a
  transcript that gets moved between nodes keeps its identity. Version 5 rather
  than random so the value is reproducible if the frontmatter is ever lost.

  Only transcripts with no `source_uuid` are touched. Real Anthropic uuids are
  never overwritten.

  py -3 backfill_uuids.py [--vault <path>] [--dry-run]
"""
import argparse
import os
import re
import uuid
from pathlib import Path

# Stable namespace for this vault. Do not change it -- doing so would reassign
# every synthetic uuid and break the identity guarantee.
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://agentmemory.local/legacy-transcript")

FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def synth_uuid(title: str, date: str) -> str:
    key = f"{date}:{re.sub(r'[^a-z0-9]+', '', (title or '').lower())}"
    return str(uuid.uuid5(NAMESPACE, key))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.environ.get("VAULT_ROOT", "/vault"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    vault = Path(args.vault)
    if not vault.is_dir():
        raise SystemExit(f"!! vault not found: {vault}")

    stamped = had = skipped = 0
    for f in sorted(vault.rglob("*.md")):
        s = str(f)
        if "Full Transcripts" not in s or "\\.obsidian\\" in s:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        m = FM_RE.match(text)
        if not m:
            print(f"  -- no frontmatter, skipping: {f.name}")
            skipped += 1
            continue
        fm = m.group(1)
        if re.search(r'(?m)^source_uuid:\s*\S', fm):
            had += 1
            continue

        title = ""
        mt = re.search(r'(?m)^title:\s*"?(.*?)"?\s*$', fm)
        if mt:
            title = mt.group(1)
        date = ""
        md = re.search(r'(?m)^date:\s*(\S+)', fm)
        if md:
            date = md.group(1)
        if not title or not date:
            fn = re.match(r"(\d{4}-\d{2}-\d{2}) - (.*)", f.stem)
            if fn:
                date = date or fn.group(1)
                title = title or fn.group(2)
        if not title or not date:
            print(f"  -- can't derive a key, skipping: {f.name}")
            skipped += 1
            continue

        u = synth_uuid(title, date)
        # Insert after message_count if present, else before tags, else at the end
        # of the frontmatter -- keeping field order consistent with new records.
        if re.search(r'(?m)^message_count:.*$', fm):
            new_fm = re.sub(r'(?m)^(message_count:.*)$', rf'\1\nsource_uuid: {u}', fm, count=1)
        elif re.search(r'(?m)^tags:.*$', fm):
            new_fm = re.sub(r'(?m)^(tags:.*)$', rf'source_uuid: {u}\n\1', fm, count=1)
        else:
            new_fm = fm.rstrip() + f"\nsource_uuid: {u}"

        print(f"  ++ {u}  {f.name[:60]}")
        if not args.dry_run:
            f.write_text("---\n" + new_fm + "\n---\n" + text[m.end():],
                         encoding="utf-8", newline="\n")
        stamped += 1

    print(f"\n{'(dry run) ' if args.dry_run else ''}"
          f"stamped {stamped}, already had one {had}, skipped {skipped}")


if __name__ == "__main__":
    main()
