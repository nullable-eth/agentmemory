"""Import local-client chat threads (Jan's on-disk format) into the vault.

The export pipeline next door handles assistant-export zips. A local
client keeps its own threads on disk instead — one directory per thread,
holding `thread.json` (title, id, model) and `messages.jsonl` (one message
per line) — so those conversations never reached the vault at all, and the
only two options in the spec were to standardise them at the edge or to
read the format here. This is the second: the desktop drops the directory
in the inbox and the service renders it.

Output is deliberately the same shape the export unpacker writes, because
everything downstream (chunking, search, filing) keys off that shape:

  .staging/Chats/Full Transcripts/<date> - <title>.md
  frontmatter: title/date/type/status/created/updated/message_count/
               source_uuid/tags
  body: one `<!-- msg:<uuid> -->` marker per message, then `## Sender · ts`

Message ids in this format are not uuids, and the marker is what chunk
identity is keyed on, so each one becomes a UUIDv5 of
`jan:<thread id>:<message id>`: stable across re-imports, unique per
thread, and matching the marker pattern the chunker looks for.
"""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

WINDOWS_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL


def sanitize(name: str, maxlen: int = 120) -> str:
    name = WINDOWS_BAD.sub("", name or "").strip().rstrip(".")
    return (name or "untitled")[:maxlen]


def yaml_q(s: str) -> str:
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _ts(ms) -> str:
    """Epoch milliseconds (or seconds) -> ISO 8601 Z, '' when unusable."""
    try:
        v = float(ms)
    except (TypeError, ValueError):
        return ""
    if v > 1e11:            # milliseconds
        v /= 1000.0
    try:
        return datetime.fromtimestamp(v, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def _text(msg: dict) -> str:
    """The message's text, across the shapes the format uses."""
    out = []
    for block in msg.get("content") or []:
        if not isinstance(block, dict):
            continue
        t = block.get("text")
        if isinstance(t, dict):
            t = t.get("value")
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
    if not out and isinstance(msg.get("text"), str):
        out.append(msg["text"].strip())
    return "\n\n".join(out).strip()


def _messages(path: Path) -> list:
    msgs = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue            # a truncated tail must not cost us the thread
        if isinstance(m, dict):
            msgs.append(m)
    msgs.sort(key=lambda m: (m.get("created_at") or 0, str(m.get("id") or "")))
    return msgs


def render_thread(tdir: Path) -> tuple[str, str] | None:
    """(filename, markdown) for one thread directory, or None if unusable."""
    try:
        meta = json.loads((tdir / "thread.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    msgs = _messages(tdir / "messages.jsonl") if (tdir / "messages.jsonl").is_file() else []
    if not msgs:
        return None

    tid = str(meta.get("id") or tdir.name)
    title = (meta.get("title") or "untitled conversation").strip().replace("\n", " ")
    model = ((meta.get("model") or {}).get("id")
             or (meta.get("model") or {}).get("provider") or "Assistant")

    # Only messages with text become chunks, so they are what the count must
    # report — an empty or tool-only line would make message_count a lie.
    rendered = [(m, _text(m)) for m in msgs]
    rendered = [(m, t) for m, t in rendered if t]
    if not rendered:
        return None

    created = _ts(msgs[0].get("created_at"))
    updated = _ts(msgs[-1].get("completed_at") or msgs[-1].get("created_at")
                  or meta.get("updated"))
    date = (created or updated or "")[:10] or datetime.now(timezone.utc).date().isoformat()

    L = ["---",
         f"title: {yaml_q(title)}",
         f"date: {date}",
         "type: chat-transcript",
         "status: unfiled",
         f"created: {created}",
         f"updated: {updated}",
         f"message_count: {len(rendered)}",
         f"source_uuid: {tid}",
         "tags: []",
         "---", "",
         f"# {title}", "",
         "> Complete transcript — every message, thought, tool call and reply.",
         "> Unfiled — see [[CLAUDE]] for how this gets placed into the graph.", ""]

    for m, body in rendered:
        mid = uuid.uuid5(NS, f"jan:{tid}:{m.get('id') or len(L)}")
        sender = "User" if m.get("role") == "user" else str(model)
        L += [f"<!-- msg:{mid} -->",
              f"## {sender} · {_ts(m.get('created_at'))}", "",
              body, ""]

    # Title first, so a thread keeps one filename as it grows; the title in
    # this format is the first prompt and can be a paragraph long.
    fname = f"{date} - {sanitize(title, 90)}.md"
    return fname, "\n".join(L).rstrip() + "\n"


def import_threads(threads_dir: Path, vault: Path, archive: Path) -> list[str]:
    """Render every thread directory, then archive the source. Returns the
    vault-relative paths written. A thread that renders to what is already
    on disk is left alone, so re-importing changes nothing."""
    written = []
    dest_dir = vault / ".staging" / "Chats" / "Full Transcripts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for tdir in sorted(p for p in threads_dir.iterdir() if p.is_dir()):
        rendered = render_thread(tdir)
        if rendered is None:
            continue
        fname, text = rendered
        dest = dest_dir / fname
        if not dest.exists() or dest.read_text(encoding="utf-8", errors="replace") != text:
            dest.write_text(text, encoding="utf-8")
            written.append(str(dest.relative_to(vault)).replace("\\", "/"))
        stamp = datetime.now(timezone.utc).date().isoformat()
        keep = archive / f"{stamp} threads" / tdir.name
        keep.parent.mkdir(parents=True, exist_ok=True)
        if keep.exists():                      # never overwrite an archive copy
            keep = keep.with_name(f"{tdir.name}-{int(datetime.now().timestamp())}")
        tdir.rename(keep)
    return written
