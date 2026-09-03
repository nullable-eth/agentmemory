#!/usr/bin/env python3
r"""
reindex.py -- rebuild everything derived in the vault, then verify.

Self-contained: this is the only file the graph's generated material depends on.

Run it after filing imports into the graph, or any time the tree has been moved
around. It touches ONLY generated material:

  - cross-link footers at the bottom of each transcript
  - <Node> - Index.md for every node
  - the root archive table
  - structural tags, which are stripped wherever they've crept in

Transcript bodies above the footer are never modified.

  py -3 reindex.py [--vault <path>]

Nodes are top-level folders. Anything prefixed '_' or '.' is skipped, so
.imports, .staging and .tools stay out of the graph. A node with no chats, no
project files and no memory is omitted from the per-node index rather than
lingering as a zero row.
"""
import argparse
import os
import re
from pathlib import Path

STRUCTURAL_TAGS = {
    "chat", "chat-transcript", "summary", "index", "moc", "project",
    "project-file", "project-index", "project-memory", "chat-summary",
    "memory", "claude", "transcript", "node-scope",
}
DOC_FILES = {"claude.md", "readme.md", "agents.md"}


# ----------------------------------------------------------------- frontmatter
def split_frontmatter(text):
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---\n", 3)
    if end == -1:
        return None, text
    return text[4:end], text[end + 5:]


def parse_tags(fm_text):
    if not fm_text:
        return []
    m = re.search(r'(?m)^tags:\s*\[(.*?)\]', fm_text)
    if m:
        return [x.strip() for x in m.group(1).split(",") if x.strip()]
    block = re.search(r'(?ms)^tags:\s*\n((?:\s*-\s*.+\n)+)', fm_text)
    if block:
        return [re.sub(r'^\s*-\s*', '', l).strip() for l in block.group(1).split("\n") if l.strip()]
    return []


# ----------------------------------------------------------------- node selection
def skip_path(s: str) -> bool:
    """True for anything inside a dot- or underscore-prefixed directory."""
    return bool(re.search(r"\\[._]", s))


def is_node(d: Path) -> bool:
    """Real graph nodes. Excludes tooling and staging dirs."""
    return (d.is_dir()
            and d.name not in ("EXPORT", "Attachments")
            and not d.name.startswith("_")
            and not d.name.startswith("."))


def has_content(d: Path) -> bool:
    """A node with nothing in it shouldn't linger as a zero row in the table."""
    ft = d / "Chats" / "Full Transcripts"
    if ft.exists() and any(ft.glob("*.md")):
        return True
    pf = d / "Project Files"
    if pf.exists() and any(pf.glob("*.md")):
        return True
    return any(d.glob("Project Memory*.md"))


# ----------------------------------------------------------------- passes
def strip_structural_tags(vault: Path):
    """Tags in this vault are topical only; structural ones create graph
    hub-nodes and carry no information."""
    fixed = 0
    for f in vault.rglob("*.md"):
        s = str(f)
        if skip_path(s):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, body = split_frontmatter(text)
        if fm is None:
            continue
        tags = parse_tags(fm)
        keep = [t for t in tags if t not in STRUCTURAL_TAGS]
        if len(keep) == len(tags):
            continue
        new_fm = re.sub(r'(?m)^tags:.*$', f"tags: [{', '.join(keep)}]", fm, count=1)
        f.write_text("---\n" + new_fm + "\n---\n" + body, encoding="utf-8", newline="\n")
        fixed += 1
    print(f"     stripped structural tags from {fixed} note(s)")


def rebuild_footers(vault: Path):
    for d in [x for x in vault.iterdir() if is_node(x)]:
        ft = d / "Chats" / "Full Transcripts"
        if not ft.exists():
            continue
        lst = []
        for f in ft.glob("*.md"):
            head = f.read_text(encoding="utf-8", errors="replace")[:400]
            md = re.search(r"date: (\d{4}-\d{2}-\d{2})", head)
            mt = re.search(r'title: "?(.*?)"?\n', head)
            lst.append((md.group(1) if md else "0000-00-00", f.stem,
                        mt.group(1) if mt else f.stem))
        siblings = sorted(lst, key=lambda x: x[0], reverse=True)

        att_by_chat = {}
        adir = d / "Chats" / "Attachments"
        if adir.exists():
            for sub in adir.iterdir():
                if sub.is_dir():
                    att_by_chat[sub.name] = sorted(a.stem for a in sub.glob("*.md"))

        for date, stem, title in lst:
            f = ft / f"{stem}.md"
            t = f.read_text(encoding="utf-8", errors="replace")
            t = re.sub(r'\n+## Attachments\n.*?(?=\n---\n\n## Related in this project|\Z)',
                       '\n', t, flags=re.S)
            t = re.sub(r'\n+---\n\n## Related in this project.*$', '\n', t, flags=re.S)
            extra = []
            if att_by_chat.get(stem):
                extra += ["", "## Attachments", ""]
                extra += [f"- [[Attachments/{stem}/{a}|{a}]]" for a in att_by_chat[stem]]
                extra.append("")
            foot = ["", "---", "", "## Related in this project", ""]
            if (d / "Chats" / f"{stem}.md").exists():
                foot += [f"- \U0001f4c4 Summary: [[../{stem}|{title} (summary)]]", ""]
            others = [s for s in siblings if s[1] != stem][:8]
            if others:
                foot += ["Other conversations in this node:", ""]
                foot += [f"- **{dd}** \u2014 [[{ss}|{tt}]]" for dd, ss, tt in others]
            foot += ["", f"[[../../{d.name} \u2014 Index|\u2190 {d.name} index]]", ""]
            f.write_text(t.rstrip() + "\n" + "\n".join(extra) + "\n".join(foot),
                         encoding="utf-8", newline="\n")


def rebuild_indexes(vault: Path):
    nodes = sorted([d for d in vault.iterdir() if is_node(d) and has_content(d)])

    def meta(f):
        t = f.read_text(encoding="utf-8", errors="replace")[:1500]
        dd = re.search(r"date: (\d{4}-\d{2}-\d{2})", t)
        mc = re.search(r"message_count: (\d+)", t)
        return (dd.group(1) if dd else "0000-00-00", mc.group(1) if mc else None)

    tt = ts = tf = 0
    for d in nodes:
        ft = d / "Chats" / "Full Transcripts"
        if not ft.exists():
            continue
        transcripts = sorted(ft.glob("*.md"), key=lambda f: f.name, reverse=True)
        pf = d / "Project Files"
        pfiles = sorted(pf.glob("*.md")) if pf.exists() else []
        mems = sorted(d.glob("Project Memory*.md"))
        summaries = {s.stem for s in (d / "Chats").glob("*.md")}
        L = ["---", f"title: {d.name}", "type: project-index", "---", "",
             f"# {d.name}", "",
             f"Complete archive for **{d.name}** \u2014 every conversation, project "
             f"file, and memory dossier.", ""]
        if (d / "Scope.md").exists():
            L += ["> [[Scope|What belongs in this node]] \u2014 read this before "
                  "filing anything here.", ""]
        if mems:
            L += ["## Memory", ""] + [f"- [[{m.stem}]]" for m in mems] + [""]
        if pfiles:
            L += [f"## Project Files ({len(pfiles)})", ""]
            L += [f"- [[Project Files/{p.stem}|{p.stem}]]" for p in pfiles] + [""]
        L += [f"## Chat Transcripts ({len(transcripts)})", "",
              "Most recent first; \U0001f4c4 marks a summary note.", ""]
        for f in transcripts:
            date, mc = meta(f)
            icon = " \U0001f4c4" if f.stem in summaries else ""
            extra = f" \u00b7 {mc} msgs" if mc else ""
            L.append(f"- **{date}** \u2014 [[Chats/Full Transcripts/{f.stem}|{f.stem}]]{icon}{extra}")
        L.append("")
        (d / f"{d.name} \u2014 Index.md").write_text("\n".join(L), encoding="utf-8", newline="\n")
        tt += len(transcripts); ts += len(summaries); tf += len(pfiles)

    # The root "Claude Archive" table was retired 2026-09-02 as redundant with
    # the per-node indexes; CLAUDE.md carries the node table.
    print(f"     indexes rebuilt: {tt} transcripts, {ts} summaries, {tf} project files")


def verify(vault: Path):
    broken = floating = struct = nouuid = 0
    for f in vault.rglob("*.md"):
        s = str(f)
        if skip_path(s) or f.name.lower() in DOC_FILES:
            continue
        t = f.read_text(encoding="utf-8", errors="replace")
        if re.search(r'\[\[\.{0,2}/?\.{0,2}/?README[\|\]]', t):
            broken += 1
        if not (t.startswith("---") and "\n---\n" in t[:1500]):
            floating += 1
        fm, _ = split_frontmatter(t)
        if any(tg in STRUCTURAL_TAGS for tg in parse_tags(fm or "")):
            struct += 1
        if "Full Transcripts" in s and not re.search(r'(?m)^source_uuid:\s*\S', t):
            nouuid += 1
    flag = "OK" if not (broken or floating or struct or nouuid) else "ISSUES"
    print(f"     verify [{flag}]: broken_links={broken} floating={floating} "
          f"structural_tags={struct} transcripts_without_uuid={nouuid}")
    if flag != "OK":
        print("     ^ run a manual check; something needs attention.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.environ.get("VAULT_ROOT", "/vault"))
    args = ap.parse_args()
    vault = Path(args.vault)
    if not vault.is_dir():
        raise SystemExit(f"!! vault not found: {vault}")

    print(f"Reindexing {vault}\n")
    print("[1/4] Stripping structural tags ...")
    strip_structural_tags(vault)
    print("[2/4] Rebuilding cross-link footers ...")
    rebuild_footers(vault)
    print("[3/4] Rebuilding indexes ...")
    rebuild_indexes(vault)
    print("[4/4] Verifying ...")
    verify(vault)
    print("\nDone.")


if __name__ == "__main__":
    main()
