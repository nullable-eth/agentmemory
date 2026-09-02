#!/usr/bin/env python3
r"""
unpack_export.py -- complete, judgment-free unpacker for Anthropic data exports.

WHAT THIS IS
  Anthropic's export zip in, readable markdown out, under <vault>/.staging/.
  It decides nothing about where anything belongs.

THE COMPLETENESS CONTRACT
  The markdown is the archive. There is no JSON sidecar and nothing to fall back
  on, so the rendering has to carry everything worth having: what was asked,
  what was thought, which tools ran with what input, what came back, what was
  answered, and what files were attached.

  What is deliberately NOT carried: plumbing with no reader value -- approval
  keys, icon URLs, MCP server URLs, per-block start/stop timestamps, signature
  blobs. This is not a byte-for-byte copy of the export; it is a complete copy
  of the *content*.

  Nothing is ever truncated and nothing is ever summarized in place of the
  original. Summaries live in separate notes as navigation aids.

  --check re-extracts every text-bearing field from the export and asserts each
  one appears in the rendered markdown, exiting non-zero if not. Run it whenever
  the renderer changes. It is the only thing standing between this design and
  silent data loss, since there's no raw copy to recover from.

MESSAGE IDENTITY, AND WHY IT MATTERS
  Each message is rendered behind an HTML comment carrying its uuid:

      <!-- msg:6f1a... -->
      ## User - 2026-07-28T14:50:47Z

  Invisible in Obsidian, and it makes the archive self-describing: the importer
  can read exactly which messages a transcript already holds.

  This exists because message COUNT is not a safe guard. If a chat is archived
  at 200 messages, continues to 260, and Anthropic then exports only the newest
  250, a count comparison sees 250 > 200 and overwrites -- silently destroying
  the 10 oldest messages, which now exist nowhere. Comparing uuid SETS catches
  that, and the importer falls back to appending rather than replacing.

IMPORT MODES
  skip     the export adds nothing this transcript doesn't already have.
  rewrite  the export is a superset of what's archived -- safe full re-render.
  append   the export has new messages BUT is missing some the archive holds.
           The archived text is left untouched and only the new messages are
           appended. This is the partial-export case.
  new      not archived anywhere; written to .staging/Chats/Full Transcripts/.

  A legacy transcript with no uuid markers can't be verified either way, so it
  is never overwritten -- it's reported and left alone.

WHY IT DOESN'T CLASSIFY
  The export carries no conversation->project linkage, so filing is a reading
  task performed by an agent against each node's Scope.md. See CLAUDE.md.

USAGE
  py -3 unpack_export.py --export <dir> --vault <root> [--account Personal]
                         [--dry-run] [--check]
"""
import argparse
import json
import re
from pathlib import Path

IMPORTS_DIRNAME = ".staging"
WINDOWS_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
MSG_MARK_RE = re.compile(r"<!-- msg:([0-9a-fA-F-]{8,}) -->")

# Generated tail that reindex.py owns. Stripped before appending so appended
# messages don't land after it (and get eaten by the next footer rebuild).
FOOTER_RE = re.compile(r'\n+---\n\n## Related in this project.*$', re.S)
ATTACH_SECTION_RE = re.compile(
    r'\n+## Attachments\n.*?(?=\n---\n\n## Related in this project|\Z)', re.S)

# Fields carrying no reader value. Excluded from the unknown-field safety net.
NOISE_FIELDS = {
    "icon_name", "integration_icon_url", "mcp_server_url", "approval_key",
    "approval_key_legacy", "approval_options", "is_mcp_app", "hidden_in_chat",
    "start_timestamp", "stop_timestamp", "flags", "signature", "type",
    "citations_grouping_mode", "alternative_display_type", "tool_use_id", "id",
    "tool_identifier", "tool_origin", "index",
}
# Fields rendered explicitly, by block type.
RENDERED = {
    "text": {"text", "citations"},
    "thinking": {"thinking", "summaries", "cut_off", "truncated", "hidden", "thinking_hidden"},
    "tool_use": {"name", "input", "message", "context", "display_content", "integration_name"},
    "tool_result": {"name", "content", "is_error", "message", "display_content",
                    "structured_content", "meta", "integration_name"},
}


def sanitize(name, maxlen=120):
    name = WINDOWS_BAD.sub("", name or "").strip().rstrip(".")
    return (name or "untitled")[:maxlen]


def norm_key(title, date):
    return f"{date}:{re.sub(r'[^a-z0-9]+', '', (title or '').lower())}"


def parse_fm(text):
    m = FM_RE.match(text)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


def yaml_q(s):
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def fence(text):
    longest = max((len(m) for m in re.findall(r"`+", text or "")), default=0)
    return "`" * max(3, longest + 1)


def jdump(o):
    try:
        return json.dumps(o, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(o)


def conv_date(conv):
    return (conv.get("updated_at") or "")[:10] or (conv.get("created_at") or "")[:10] or "0000-00-00"


def details(summary, body, lang=""):
    f = fence(body)
    return f"<details><summary>{summary}</summary>\n\n{f}{lang}\n{body}\n{f}\n\n</details>"


def as_text(v):
    """Pull readable text out of a value that might be a string, a list of
    content blocks, or a nested structure."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(as_text(x) for x in v)
    if isinstance(v, dict):
        if isinstance(v.get("text"), str):
            return v["text"]
        return jdump(v)
    return "" if v is None else str(v)


# ----------------------------------------------------------------- reading export
def load_conversations(export: Path):
    idx = export / "conversations" / "_index.json"
    if idx.exists():
        try:
            entries = json.loads(idx.read_text(encoding="utf-8")).get("entries") or []
        except Exception:
            entries = []
        if entries:
            for e in entries:
                p = export / "conversations" / (e.get("file") or "")
                if p.exists():
                    try:
                        yield json.loads(p.read_text(encoding="utf-8"))
                    except Exception:
                        continue
            return
    mono = export / "conversations.json"
    if mono.exists():
        for c in json.loads(mono.read_text(encoding="utf-8")):
            yield c


def scan_vault(vault: Path):
    """Index what's already archived.

    The title+date fallback is registered ONLY for files under 'Full
    Transcripts'. A summary note sits at Chats/<stem>.md and a transcript at
    Chats/Full Transcripts/<stem>.md with an identical stem; indexing both let
    the fallback resolve to the summary note, and a continued chat would be
    written over it. Transcripts are the only valid rewrite target.
    """
    uuids, keys, by_uuid = set(), {}, {}
    for f in vault.rglob("*.md"):
        s = str(f)
        if "\\.obsidian\\" in s:
            continue
        is_transcript = "Full Transcripts" in s
        try:
            head = f.read_text(encoding="utf-8-sig", errors="replace")[:2000]
        except OSError:
            continue
        fm = parse_fm(head)
        u = fm.get("source_uuid")
        if u:
            uuids.add(u)
            if is_transcript:
                by_uuid[u] = f
        if not is_transcript:
            continue
        if fm.get("title") and fm.get("date"):
            keys[norm_key(fm["title"], fm["date"])] = f
        m = re.match(r"(\d{4}-\d{2}-\d{2}) - (.*)", f.stem)
        if m:
            keys.setdefault(norm_key(m.group(2), m.group(1)), f)
    return uuids, keys, by_uuid


def archived_msg_ids(text):
    return set(MSG_MARK_RE.findall(text or ""))


# ----------------------------------------------------------------- rendering
def render_extras(b):
    """Safety net: any block field neither rendered nor known-noise gets dumped,
    so a new field Anthropic adds shows up rather than vanishing."""
    known = RENDERED.get(b.get("type"), set()) | NOISE_FIELDS
    extra = {k: v for k, v in b.items()
             if k not in known and v not in (None, "", [], {}, False)}
    if not extra:
        return ""
    return details(f"Other fields ({', '.join(sorted(extra))})", jdump(extra), "json")


def render_citations(cits):
    lines = []
    for c in cits:
        if not isinstance(c, dict):
            lines.append(f"- {c}")
            continue
        title = c.get("title") or c.get("source") or "source"
        url = c.get("url") or ""
        cited = (c.get("cited_text") or "").strip().replace("\n", " ")
        line = f"- [{title}]({url})" if url else f"- {title}"
        if cited:
            line += f" \u2014 \u201c{cited[:200]}\u201d"
        lines.append(line)
    return "**Citations**\n\n" + "\n".join(lines)


def render_block(b):
    t = b.get("type")
    out = []

    if t == "text":
        out.append((b.get("text") or "").rstrip())
        if b.get("citations"):
            out.append(render_citations(b["citations"]))

    elif t == "thinking":
        # Fences rather than blockquote callouts: prefixing every line with "> "
        # mutates the text and turns blank lines into "> ".
        think = b.get("thinking") or ""
        if think.strip():
            out.append(details("\U0001f9e0 Extended thinking", think.rstrip()))
        for i, s in enumerate(b.get("summaries") or []):
            txt = (s.get("summary") if isinstance(s, dict) else s) or ""
            if isinstance(txt, str) and txt.strip():
                out.append(details(f"Thinking summary {i + 1}", txt.rstrip()))
        for flag in ("cut_off", "truncated", "thinking_hidden", "hidden"):
            if b.get(flag):
                out.append(f"*[thinking flagged `{flag}` by the export]*")

    elif t == "tool_use":
        name = b.get("name") or "tool"
        head = f"**\U0001f527 Tool call \u00b7 `{name}`**"
        if b.get("integration_name"):
            head += f" \u00b7 {b['integration_name']}"
        out.append(head)
        if (b.get("message") or "").strip():
            out.append(b["message"].strip())
        inp = jdump(b.get("input"))
        if inp not in ("null", "{}", '""'):
            f = fence(inp)
            out.append(f"{f}json\n{inp}\n{f}")
        for k in ("context", "display_content"):
            txt = as_text(b.get(k)).strip()
            if txt and txt not in inp:
                out.append(details(f"tool_use.{k}", txt))

    elif t == "tool_result":
        name = b.get("name") or "tool"
        err = " \u2014 error" if b.get("is_error") else ""
        text = as_text(b.get("content"))
        out.append(details(
            f"Tool result{err} \u00b7 <code>{name}</code> ({len(text):,} chars)", text))
        if (b.get("message") or "").strip():
            out.append(b["message"].strip())
        # Only surface these when they carry something the content didn't.
        for k in ("display_content", "structured_content", "meta"):
            txt = as_text(b.get(k)).strip()
            if txt and txt not in text:
                out.append(details(f"tool_result.{k}", txt))

    else:
        out.append(details(f"Unrendered block type <code>{t}</code>", jdump(b), "json"))
        return "\n\n".join(x for x in out if x)

    extra = render_extras(b)
    if extra:
        out.append(extra)
    return "\n\n".join(x for x in out if x)


def render_attachments(m):
    out = []
    for label, key in (("Attachments", "attachments"), ("Files", "files")):
        items = m.get(key) or []
        if not items:
            continue
        lines = [f"**{label} ({len(items)})**", ""]
        bodies = []
        for it in items:
            if not isinstance(it, dict):
                lines.append(f"- `{it}`")
                continue
            nm = it.get("file_name") or it.get("filename") or it.get("name") or "(unnamed)"
            size = it.get("file_size")
            kind = it.get("file_type") or ""
            bits = [b for b in (kind, f"{size:,} bytes" if isinstance(size, int) else "") if b]
            lines.append(f"- `{nm}`" + (f" \u00b7 {' \u00b7 '.join(bits)}" if bits else ""))
            # Anthropic sometimes includes text it extracted from the upload.
            # That's real content and has to be kept.
            body = as_text(it.get("extracted_content") or it.get("content")).strip()
            if body:
                bodies.append(details(f"Extracted text \u00b7 <code>{nm}</code>", body))
        out.append("\n".join(lines))
        out.extend(bodies)
    return "\n\n".join(x for x in out if x)


def render_message(m):
    """One message, behind its identity marker."""
    L = []
    mid = m.get("uuid") or ""
    if mid:
        L.append(f"<!-- msg:{mid} -->")
    sender = "User" if m.get("sender") == "human" else "Claude"
    L += [f"## {sender} \u00b7 {m.get('created_at', '')}", ""]

    blocks = m.get("content") or []
    rendered_text = []
    for b in blocks:
        r = render_block(b)
        if r:
            L += [r, ""]
        if b.get("type") == "text":
            rendered_text.append(b.get("text") or "")

    mt = (m.get("text") or "").strip()
    if mt and mt not in "\n".join(rendered_text):
        L += [mt, ""] if not blocks else [details("message.text", mt), ""]

    att = render_attachments(m)
    if att:
        L += [att, ""]
    return "\n".join(L)


def sort_msgs(msgs):
    return sorted(msgs, key=lambda m: (m.get("created_at") or "", m.get("uuid") or ""))


def render_transcript(conv, msgs=None):
    name = conv.get("name") or "(untitled conversation)"
    msgs = sort_msgs(conv.get("chat_messages") or [] if msgs is None else msgs)
    L = ["---",
         f"title: {yaml_q(name)}",
         f"date: {conv_date(conv)}",
         "type: chat-transcript",
         "status: unfiled",
         f"created: {conv.get('created_at', '')}",
         f"updated: {conv.get('updated_at', '')}",
         f"message_count: {len(msgs)}",
         f"source_uuid: {conv['uuid']}",
         "tags: []",
         "---", "",
         f"# {name}", "",
         "> Complete transcript \u2014 every message, thought, tool call and reply.",
         "> Unfiled \u2014 see [[CLAUDE]] for how this gets placed into the graph.", ""]

    if (conv.get("summary") or "").strip():
        L += [details("Summary recorded by Claude in the export",
                      conv["summary"].rstrip()), ""]

    for m in msgs:
        L += [render_message(m), ""]
    return "\n".join(L).rstrip() + "\n"


# --- in-place rewrite: keep the fields that describe filing, not content -----
PRESERVE_ON_REWRITE = ("date", "status", "project", "tags")


def raw_fm_lines(text):
    m = FM_RE.match(text)
    if not m:
        return {}
    return {line.split(":", 1)[0].strip(): line
            for line in m.group(1).split("\n") if ":" in line}


def preserve_filed_frontmatter(new_md, old_text):
    """Re-apply filing fields from the existing transcript, keeping the original
    lines verbatim so quoting and wikilink syntax survive.

    `date` is preserved deliberately: it matches the filename, and a stable
    filename means every link pointing at this transcript keeps working when the
    chat is continued. The real last-touched time is in `updated`.
    """
    old = raw_fm_lines(old_text)
    m = FM_RE.match(new_md)
    if not m:
        return new_md
    out, seen = [], set()
    for line in m.group(1).split("\n"):
        k = line.split(":", 1)[0].strip() if ":" in line else ""
        if k in PRESERVE_ON_REWRITE and k in old:
            out.append(old[k])
            seen.add(k)
        else:
            out.append(line)
    for k in PRESERVE_ON_REWRITE:
        if k in old and k not in seen:
            out.append(old[k])
    return "---\n" + "\n".join(out) + "\n---\n" + new_md[m.end():]


def append_messages(old_text, conv, new_msgs, missing_count):
    """Partial export: keep every archived character, append only what's new.

    The generated footer is stripped first so appended messages don't land after
    it and get swallowed by the next footer rebuild; reindex.py puts it back.
    """
    body = FOOTER_RE.sub("", old_text)
    body = ATTACH_SECTION_RE.sub("\n", body)
    fm = parse_fm(old_text)
    old_count = len(archived_msg_ids(old_text))

    parts = [body.rstrip(), "",
             f"> [!warning] Partial export merged on import",
             f"> This export was missing {missing_count} message(s) that were already",
             f"> archived here, so it was appended to rather than replacing the file.",
             f"> The {len(new_msgs)} message(s) below are the new ones.", ""]
    for m in sort_msgs(new_msgs):
        parts += [render_message(m), ""]
    merged = "\n".join(parts).rstrip() + "\n"

    total = old_count + len(new_msgs)
    merged = re.sub(r'(?m)^message_count:.*$', f"message_count: {total}", merged, count=1)
    if conv.get("updated_at"):
        merged = re.sub(r'(?m)^updated:.*$', f"updated: {conv['updated_at']}", merged, count=1)
    if "source_uuid" not in fm:
        merged = re.sub(r'(?m)^(message_count:.*)$',
                        rf"\1\nsource_uuid: {conv['uuid']}", merged, count=1)
    return merged


def render_doc(doc, pname, puuid):
    fn = doc.get("filename") or "untitled"
    content = doc.get("content") or ""
    ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
    lang = {"yml": "yaml", "yaml": "yaml", "py": "python", "sh": "bash", "ps1": "powershell",
            "js": "javascript", "ts": "typescript", "json": "json"}.get(ext, "")
    f = fence(content)
    body = content if ext in ("md", "txt", "") else f"{f}{lang}\n{content}\n{f}"
    return "\n".join([
        "---", f"title: {yaml_q(fn)}", "type: project-file", "status: unfiled",
        f"claude_project: {yaml_q(pname or '(unnamed project)')}",
        f"claude_project_uuid: {puuid}", f"source_uuid: {doc.get('uuid', '')}",
        f"created: {doc.get('created_at', '')}", "tags: []", "---", "",
        f"# {fn}", "",
        f"> Project document from the Claude project **{pname or '(unnamed)'}**. Complete.",
        "", body, ""])


def render_memory(title, text, kind, ref):
    return "\n".join([
        "---", f"title: {yaml_q(title)}", "type: memory", "status: unfiled",
        f"memory_scope: {kind}", f"source_ref: {ref}", "tags: []", "---", "",
        f"# {title}", "", "> Memory dossier from the export. Complete.", "",
        (text or "").rstrip(), ""])


# ----------------------------------------------------------------- check
def text_bearing(msgs):
    out = []
    for m in msgs:
        for b in m.get("content") or []:
            t = b.get("type")
            if t == "text" and (b.get("text") or "").strip():
                out.append(("text", b["text"]))
            elif t == "thinking":
                if (b.get("thinking") or "").strip():
                    out.append(("thinking", b["thinking"]))
                for s in b.get("summaries") or []:
                    v = (s.get("summary") if isinstance(s, dict) else s) or ""
                    if isinstance(v, str) and v.strip():
                        out.append(("summary", v))
            elif t == "tool_result":
                v = as_text(b.get("content"))
                if v.strip():
                    out.append(("tool_result", v))
            elif t == "tool_use":
                v = jdump(b.get("input"))
                if v not in ("null", "{}", '""'):
                    out.append(("tool_input", v))
    return out


def check_complete(msgs, md):
    missing = []
    for kind, s in text_bearing(msgs):
        probe = s.strip()
        if not probe or probe in md:
            continue
        if "\n".join(l.rstrip() for l in probe.split("\n")) in md:
            continue
        missing.append((kind, len(probe), probe[:70].replace("\n", " ")))
    return missing


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--account", default="Personal")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="Assert every text-bearing field survives into the markdown.")
    ap.add_argument("--rerender-unmarked", action="store_true",
                    help="Replace transcripts that have no message identity markers "
                         "with a fresh render from this export. Only safe when you "
                         "know the export holds the whole conversation; used once to "
                         "migrate transcripts written before markers existed.")
    args = ap.parse_args()

    export, vault = Path(args.export), Path(args.vault)
    if not vault.is_dir():
        raise SystemExit(f"!! vault not found: {vault}")

    inbox = vault / IMPORTS_DIRNAME
    chats_dir = inbox / "Chats" / "Full Transcripts"

    print("=" * 74)
    print(f"  unpack: {export}")
    print(f"  vault:  {vault}")
    print("=" * 74)

    print("\n[1/4] Scanning what's already archived ...")
    known_uuids, known_keys, path_by_uuid = scan_vault(vault)
    print(f"  {len(known_uuids)} uuid(s), {len(known_keys)} title+date key(s) known")

    print("\n[2/4] Rendering conversations ...")
    added = rewritten = appended = skipped = empty = unverifiable = 0
    problems = []

    for conv in load_conversations(export):
        uuid, name = conv.get("uuid"), conv.get("name") or ""
        msgs = conv.get("chat_messages") or []
        if not uuid:
            continue
        if not msgs or not any((m.get("content") or m.get("text")) for m in msgs):
            empty += 1
            continue

        date = conv_date(conv)
        stem = f"{date} - {sanitize(name)}"
        existing = path_by_uuid.get(uuid) or known_keys.get(norm_key(name, date))
        export_ids = {m.get("uuid") for m in msgs if m.get("uuid")}

        mode, old_text, target, render_msgs = "new", None, chats_dir / f"{stem}.md", msgs

        if existing:
            target = existing
            try:
                old_text = existing.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                old_text = ""
            archived = archived_msg_ids(old_text)

            if not archived:
                # No identity markers: this predates them. We cannot tell whether
                # the export is a superset, so we must not overwrite.
                old_n = int(parse_fm(old_text[:2000]).get("message_count") or 0)
                if len(msgs) < old_n:
                    skipped += 1
                    continue
                if args.rerender_unmarked:
                    mode = "rewrite"
                    print(f"  ~~ re-rendering unmarked transcript ({old_n} archived, "
                          f"{len(msgs)} in export): {existing.name}")
                    rewritten += 1
                elif len(msgs) > old_n:
                    print(f"  !! {existing.name}: export has {len(msgs)} msgs vs "
                          f"{old_n} archived, but the archived copy has no message "
                          f"identity markers, so a merge can't be verified. Left alone. "
                          f"(--rerender-unmarked to replace it from the export.)")
                    unverifiable += 1
                    continue
                else:
                    skipped += 1
                    continue
            else:
                new_ids = export_ids - archived
                missing = archived - export_ids

                if not new_ids:
                    skipped += 1
                    continue
                if missing:
                    mode = "append"
                    render_msgs = [m for m in msgs if m.get("uuid") in new_ids]
                    print(f"  >> partial export ({len(missing)} archived msg(s) absent, "
                          f"{len(new_ids)} new), appending: {existing.name}")
                    appended += 1
                else:
                    mode = "rewrite"
                    print(f"  ~~ continued ({len(archived)} -> {len(export_ids)} msgs), "
                          f"rewriting in place: {existing.name}")
                    rewritten += 1
        else:
            print(f"  ++ {stem}.md")
            added += 1

        if mode == "append":
            md = append_messages(old_text, conv, render_msgs, len(archived - export_ids))
        else:
            md = render_transcript(conv)
            if old_text:
                md = preserve_filed_frontmatter(md, old_text)
        filed = IMPORTS_DIRNAME not in target.parts
        md = re.sub(r'(?m)^status:.*$',
                    f"status: {'filed' if filed else 'unfiled'}", md, count=1)

        if args.check:
            miss = check_complete(render_msgs, md)
            if miss:
                problems.append((stem, miss))
                print(f"     !! {len(miss)} text field(s) missing from render")

        if not args.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(md, encoding="utf-8", newline="\n")

    print(f"  new={added} rewritten={rewritten} appended={appended} "
          f"unchanged={skipped} empty={empty} unverifiable={unverifiable}")

    print("\n[3/4] Rendering project documents ...")
    ndocs = 0
    pdir = export / "projects"
    if pdir.is_dir():
        for pf in sorted(pdir.glob("*.json")):
            try:
                p = json.loads(pf.read_text(encoding="utf-8"))
            except Exception:
                continue
            pname = (p.get("name") or "").strip()
            for d in p.get("docs") or []:
                fn = (d.get("filename") or "").strip()
                if not fn and not (d.get("content") or "").strip():
                    continue
                out = (inbox / "Project Files" / sanitize(pname or "(unnamed project)")
                       / f"{sanitize(fn or 'untitled')}.md")
                if out.exists():
                    continue
                if not args.dry_run:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(render_doc(d, pname, p.get("uuid", "")),
                                   encoding="utf-8", newline="\n")
                print(f"  ++ {pname or '(unnamed)'} / {fn}")
                ndocs += 1
    print(f"  {ndocs} document(s) written")

    print("\n[4/4] Rendering memory ...")
    nmem = 0
    mfile = export / "memories.json"
    if mfile.exists():
        try:
            mem = json.loads(mfile.read_text(encoding="utf-8"))
            mem = mem[0] if isinstance(mem, list) and mem else mem
        except Exception:
            mem = {}
        acct = (mem.get("conversations_memory") or "").strip()
        if acct:
            out = inbox / "Memory" / f"Account Memory - {args.account}.md"
            if not args.dry_run:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(render_memory(f"Account Memory - {args.account}", acct,
                                             "account", mem.get("account_uuid", "")),
                               encoding="utf-8", newline="\n")
            print(f"  ++ {out.name}")
            nmem += 1
        names = {}
        if pdir.is_dir():
            for pf in pdir.glob("*.json"):
                try:
                    p = json.loads(pf.read_text(encoding="utf-8"))
                    names[p.get("uuid")] = (p.get("name") or "").strip()
                except Exception:
                    continue
        for puuid, text in (mem.get("project_memories") or {}).items():
            if not (text or "").strip():
                continue
            label = names.get(puuid) or puuid
            out = inbox / "Memory" / f"Project Memory - {sanitize(label)}.md"
            if not args.dry_run:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(render_memory(f"Project Memory - {label}", text,
                                             "project", puuid),
                               encoding="utf-8", newline="\n")
            print(f"  ++ {out.name}")
            nmem += 1
    print(f"  {nmem} memory file(s) written")

    if not args.dry_run and inbox.exists():
        readme = inbox / "README.md"
        if not readme.exists():
            readme.write_text(INBOX_README, encoding="utf-8", newline="\n")

    print("\n" + "=" * 74)
    if problems:
        print(f"  !! COMPLETENESS CHECK FAILED for {len(problems)} conversation(s).")
        for stem, miss in problems[:5]:
            print(f"     {stem}: {len(miss)} field(s), e.g. [{miss[0][0]}] {miss[0][2]}")
        print("  Fix the renderer before trusting this import.")
    elif args.check:
        print("  Completeness check passed: every text-bearing field is in the markdown.")
    if unverifiable:
        print(f"  {unverifiable} transcript(s) left alone: no message identity markers,")
        print("  so a merge couldn't be proven safe. Nothing was overwritten.")
    if args.dry_run:
        print("  (dry run \u2014 nothing written)")
    elif added or rewritten or appended or ndocs or nmem:
        print(f"  Unpacked into {IMPORTS_DIRNAME}/. Nothing has been filed into the graph.")
    else:
        print("  Nothing new. Everything in this export is already archived.")
    print("=" * 74)
    return 1 if problems else 0


INBOX_README = """---
title: Staging
type: inbox-readme
tags: []
---

# .staging

Approval queue. `unpack_export.py` renders every conversation, project document
and memory dossier from an Anthropic data export into here, complete and
unabridged, and makes no decision about where any of it belongs.

Everything here has `status: unfiled`. Filing (by an agent reading `CLAUDE.md`
and each node's `Scope.md`, with human approval) sets `status: filed`, adds a
`project:` wikilink, and MOVES the file into its node — nothing lingers here
after approval. The dot-prefix keeps Obsidian from indexing any of it, so the
visible vault is only the organized graph.

The markdown is the archive -- there is no JSON sidecar. Every message is
rendered behind an invisible `<!-- msg:uuid -->` marker, which is what lets a
later export be merged safely instead of overwriting.

`superseded/` holds retired versions of account/project memory dossiers —
kept, per the nothing-is-deleted rule, just out of sight.
"""


if __name__ == "__main__":
    raise SystemExit(main())
