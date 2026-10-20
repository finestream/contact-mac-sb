#!/usr/bin/env python3
"""
export_contacts.py — Export macOS/iCloud Apple Contacts into SilverBullet-ready
Markdown files (one file per person, one file per company), with wikilinks
between people and their company, and between people who are related to
each other (Apple Contacts "Related Names" field).

Requires nothing beyond the Python 3 standard library. Talks to Contacts.app
via AppleScript (osascript), so it must run *on the Mac* where the contacts
live, and the terminal/Python running it needs Automation permission for
Contacts (macOS will prompt for this the first time; it can also be granted
under System Settings > Privacy & Security > Automation).

Usage
-----
    python3 export_contacts.py --localspace ~/notes/MySpace \\
        [--person Contacts/People] [--company Contacts/Companies] \\
        [--dry-run] [--prune]

Files are written to:
    <localspace>/<person>/<Name>.md
    <localspace>/<company>/<Name>.md

Internal links use the literal form requested by the project spec:
    [[<person>/<Name>.md]]
    [[<company>/<Name>.md]]

See README.md in this repo for the full field reference and design notes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Separators the AppleScript puts between vCards, and between the vCard
# section and the id section, of the single string it returns.
VCARD_SEP = "\n===VCARD-SEP===\n"
IDS_MARKER = "\n===IDS-SECTION===\n"

# --------------------------------------------------------------------------
# 1. Extraction: pull raw vCards (+ local Contacts.app ids) out of Contacts.app
#
# Everything comes back as the script's return value on stdout. Earlier
# versions had AppleScript write temp files with `open for access`, which
# fails with "Access not allowed (-1723)" for the process driving osascript,
# before Contacts' own Automation permission even comes into play.
# --------------------------------------------------------------------------

# `log` writes one line to stderr immediately, so logging a PROGRESS line
# every ~5% lets Python report progress while osascript is still running.
APPLESCRIPT_TEMPLATE = '''
tell application "Contacts"
    set thePeople to every person
end tell

set totalCount to count of thePeople
set stepSize to totalCount div 20
if stepSize < 1 then set stepSize to 1

set vcardList to {}
set idList to {}
set counter to 0
repeat with p in thePeople
    set counter to counter + 1
    set vcardText to ""
    try
        tell application "Contacts" to set vcardText to (vcard of p)
    end try
    set idText to ""
    try
        tell application "Contacts" to set idText to (id of p)
    end try
    set end of vcardList to vcardText
    set end of idList to idText
    if (counter mod stepSize = 0) or (counter = totalCount) then
        log ("PROGRESS " & counter & "/" & totalCount)
    end if
end repeat

set AppleScript's text item delimiters to "%(sep)s"
set vcardBlob to vcardList as text
set AppleScript's text item delimiters to "\\n"
set idBlob to idList as text
set AppleScript's text item delimiters to ""

return vcardBlob & "%(ids_marker)s" & idBlob
'''

PROGRESS_RE = re.compile(r"^PROGRESS (\d+)/(\d+)$")


def extract_from_contacts_app(verbose: bool = False) -> Tuple[str, List[str]]:
    """Run the AppleScript export and return (raw_vcard_blob, ids_in_order)."""
    script = APPLESCRIPT_TEMPLATE % {
        "sep": VCARD_SEP.replace("\n", "\\n"),
        "ids_marker": IDS_MARKER.replace("\n", "\\n"),
    }
    # stdout goes to a temp file, not a pipe: the result (often megabytes) is
    # written only at the very end, while we're still reading stderr for
    # progress. With a pipe, osascript blocks once ~64 KB are unread and never
    # exits, so stderr never closes: a deadlock. The file is opened by Python;
    # AppleScript itself writes nothing (see the -1723 note above).
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as out:
        try:
            proc = subprocess.Popen(
                ["osascript", "-e", script],
                stdout=out,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            sys.exit("osascript not found — this script must run on macOS.")

        # Stream stderr line by line while osascript runs (do not switch to
        # subprocess.run(capture_output=True): progress would only show at the end).
        stderr_lines: List[str] = []
        try:
            for raw_line in proc.stderr:
                line = raw_line.rstrip("\n")
                stderr_lines.append(line)
                m = PROGRESS_RE.match(line)
                if m:
                    done, total = int(m.group(1)), int(m.group(2))
                    pct = int(done * 100 / total) if total else 100
                    print(f"  ... {done}/{total} contacts read ({pct}%)", flush=True)
                elif verbose and line.strip():
                    print(f"[osascript] {line}", flush=True)
        except KeyboardInterrupt:
            proc.kill()
            raise

        returncode = proc.wait()
        out.seek(0)
        stdout_data = out.read()

    if returncode != 0:
        sys.exit(
            "AppleScript export failed. This is usually a missing "
            "Automation permission.\n"
            "Grant it under System Settings > Privacy & Security > "
            "Automation, allowing your terminal (or Python) to control "
            "Contacts, then re-run.\n\n"
            "osascript said: " + "\n".join(stderr_lines).strip()
        )
    if IDS_MARKER not in stdout_data:
        sys.exit(
            "AppleScript export returned unexpected output (no ids marker found). "
            "First 500 chars:\n" + stdout_data[:500]
        )
    vcard_blob, _, id_blob = stdout_data.partition(IDS_MARKER)
    return vcard_blob, id_blob.splitlines()


# --------------------------------------------------------------------------
# 2. vCard parsing (minimal, dependency-free; tuned for Apple's 3.0 export)
# --------------------------------------------------------------------------

def split_vcards(blob: str) -> List[str]:
    """Split the export into single vCards. A plain .vcf file (--vcf) has no
    VCARD_SEP, so fall back to splitting before each BEGIN:VCARD."""
    if VCARD_SEP in blob:
        chunks = blob.split(VCARD_SEP)
    else:
        chunks = re.split(r"(?=BEGIN:VCARD)", blob)
    return [c.strip() for c in chunks if "BEGIN:VCARD" in c]


def unfold_lines(text: str) -> List[str]:
    """RFC 6350 line unfolding: a line starting with space/tab continues the
    previous line."""
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def normalize_ws(s: str) -> str:
    """Collapse runs of whitespace (incl. newlines) to one space, and trim."""
    return re.sub(r"\s+", " ", s).strip()


def unescape_value(value: str) -> str:
    """Undo vCard escaping: \\n becomes a newline, \\X becomes X."""
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def split_components(value: str) -> List[str]:
    """Split a structured value (N, ORG, ADR) on unescaped ';' and unescape
    each part. Escape pairs are matched left to right, so 'a\\;b' is one
    component ('a;b') and 'a\\\\;b' is two ('a\\' and 'b')."""
    tokens = re.findall(r"\\.|;|[^\\;]+|\\", value)   # escape pair, separator, text, lone '\'
    parts = [""]
    for tok in tokens:
        if tok == ";":
            parts.append("")
        else:
            parts[-1] += tok
    return [unescape_value(p) for p in parts]


@dataclass
class VLine:
    """One content line, e.g. `item1.EMAIL;type=INTERNET:john@acme.ch`."""
    group: Optional[str]          # "item1": Apple ties a field to its X-ABLabel this way
    name: str                     # "EMAIL", upper-cased
    params: Dict[str, List[str]]  # {"TYPE": ["INTERNET"]}, keys upper-cased
    value: str                    # raw value, still escaped


def parse_line(line: str) -> Optional[VLine]:
    if ":" not in line:
        return None
    key_part, _, value = line.partition(":")
    group = None
    maybe_group, dot, rest = key_part.partition(".")
    # Only a bare token like "item1" is a group; a dot inside a parameter
    # value is not.
    if dot and re.fullmatch(r"[A-Za-z0-9_-]+", maybe_group):
        group, key_part = maybe_group, rest
    name, *raw_params = key_part.split(";")
    params: Dict[str, List[str]] = {}
    for p in raw_params:
        if "=" in p:
            pk, _, pv = p.partition("=")
            params.setdefault(pk.strip().upper(), []).append(pv.strip().strip('"'))
        else:
            # vCard 2.1-style bare TYPE, e.g. ";HOME"
            params.setdefault("TYPE", []).append(p.strip())
    return VLine(group=group, name=name.strip().upper(), params=params, value=value)


AB_LABEL_RE = re.compile(r"_\$!<(.+?)>!\$_")

# TYPE tokens that say nothing useful about a field (nearly every EMAIL/TEL
# has one), so a more specific TYPE or an X-ABLabel wins over them.
GENERIC_TYPE_TOKENS = {"PREF", "INTERNET", "VOICE"}


def clean_label(raw: str) -> str:
    """Apple's built-in labels look like `_$!<Work>!$_`; return `Work`.
    Custom labels are stored verbatim."""
    m = AB_LABEL_RE.match(raw.strip())
    return m.group(1) if m else raw.strip().strip("_")


def label_for(by_group: Dict[str, List[VLine]], vline: VLine) -> Optional[str]:
    """Label of a field: the X-ABLabel in its item group if there is one,
    else the first specific TYPE, else the first TYPE, else None."""
    for sib in by_group.get(vline.group or "", []):
        if sib.name == "X-ABLABEL":
            return clean_label(sib.value)
    types = vline.params.get("TYPE", [])
    specific = [t for t in types if t.upper() not in GENERIC_TYPE_TOKENS]
    if specific or types:
        return clean_label((specific or types)[0])
    return None


@dataclass
class Contact:
    uid: str = ""
    apple_id: str = ""
    full_name: str = ""
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    name_prefix: str = ""
    name_suffix: str = ""
    nickname: str = ""
    organization: str = ""
    department: str = ""
    job_title: str = ""
    birthday: str = ""
    # Multi-valued fields are (label, value) pairs; relations are (label, name).
    emails: List[Tuple[str, str]] = field(default_factory=list)
    phones: List[Tuple[str, str]] = field(default_factory=list)
    addresses: List[Tuple[str, str]] = field(default_factory=list)
    urls: List[Tuple[str, str]] = field(default_factory=list)
    social: List[Tuple[str, str]] = field(default_factory=list)
    im: List[Tuple[str, str]] = field(default_factory=list)
    relations: List[Tuple[str, str]] = field(default_factory=list)
    notes: str = ""

    @property
    def key(self) -> str:
        """Stable identity: the vCard UID, else the Contacts.app id."""
        return self.uid or self.apple_id


def parse_contact(raw: str, apple_id: str = "") -> Contact:
    lines = [parse_line(l) for l in unfold_lines(raw)]
    lines = [l for l in lines if l is not None and l.name not in ("BEGIN", "END", "VERSION")]

    by_group: Dict[str, List[VLine]] = {}
    for l in lines:
        if l.group:
            by_group.setdefault(l.group, []).append(l)

    c = Contact(apple_id=apple_id)
    for l in lines:
        name = l.name
        val = unescape_value(l.value)
        label = label_for(by_group, l)

        if name == "UID":
            c.uid = val
        elif name == "FN":
            c.full_name = val
        elif name == "N":
            # N: Family; Given; Additional; Prefix; Suffix
            parts = split_components(l.value) + [""] * 5
            c.last_name, c.first_name, c.middle_name, c.name_prefix, c.name_suffix = parts[:5]
        elif name == "NICKNAME":
            c.nickname = val
        elif name == "ORG":
            # ORG: Organization; Unit
            parts = split_components(l.value) + [""] * 2
            c.organization, c.department = normalize_ws(parts[0]), normalize_ws(parts[1])
        elif name == "TITLE":
            c.job_title = val
        elif name == "BDAY":
            c.birthday = val
        elif name == "NOTE":
            c.notes = val
        elif name == "EMAIL":
            c.emails.append((label or "other", val))
        elif name == "TEL":
            c.phones.append((label or "other", val))
        elif name == "ADR":
            # ADR: PO Box; Extended; Street; City; Region; PostalCode; Country
            _, _, street, city, region, postal, country = (split_components(l.value) + [""] * 7)[:7]
            addr_bits = [b for b in (street, f"{postal} {city}".strip(), region, country) if b.strip()]
            c.addresses.append((label or "other", ", ".join(addr_bits)))
        elif name == "URL":
            c.urls.append((label or "other", val))
        elif name == "X-SOCIALPROFILE":
            # The service ("twitter", "linkedin") is the TYPE, even if an
            # X-ABLabel exists.
            types = l.params.get("TYPE")
            c.social.append(((clean_label(types[0]) if types else label) or "other", val))
        elif name == "IMPP":
            # "xmpp:jane@example.com" -> ("xmpp", "jane@example.com")
            proto, sep, handle = val.partition(":")
            c.im.append((proto, handle) if sep else ("im", val))
        elif name in ("X-ABRELATEDNAMES", "RELATED"):  # RELATED: vCard 4.0 form
            c.relations.append((label or "related", val))

    if not c.full_name:
        c.full_name = normalize_ws(f"{c.first_name} {c.last_name}") or c.nickname or "Unnamed Contact"
    return c


# --------------------------------------------------------------------------
# 3. Filenames, slugs, wikilinks
# --------------------------------------------------------------------------

# Replaced by '-' in filenames. Two sources:
# - file system: / \ : * ? " < > | and control characters
# - SilverBullet page names (plug-api/lib/ref.ts, `refRegex`): @ # $ start a
#   position, header or anchor in a link; [[ and ]] would break the wikilink
INVALID_FILENAME_CHARS = re.compile(r'[\/\\:\*\?"<>\|@#\$\x00-\x1f]|\[\[|\]\]')


def sanitize_filename(name: str) -> str:
    """Display name -> filename stem that is also a valid SilverBullet page
    name, e.g. 'Newco S.A' -> 'Newco S-A'."""
    name = INVALID_FILENAME_CHARS.sub("-", normalize_ws(name))
    # A leading '.' would be a hidden file, a leading '^' a SilverBullet
    # meta link; trailing dots and spaces are trouble on most file systems.
    name = re.sub(r"^[\s.^]+|[\s.]+$", "", name)
    # A name ending in '.<letters/digits>' ("example.com") would be taken by
    # SilverBullet as a document with that extension, not a page.
    name = re.sub(r"\.(?=[A-Za-z0-9]+$)", "-", name)
    return name or "Unnamed"


def slugify(name: str) -> str:
    """'Müller & Co. AG' -> 'muller-co-ag'."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = re.sub(r"[^a-zA-Z0-9]+", "-", n).strip("-").lower()
    return n or "x"


def wikilink(dir_param: str, stem: str) -> str:
    # dir_param is the --person/--company value as given, not the absolute
    # path, because links are relative to the space root.
    return f"[[{dir_param}/{stem}.md]]"


# --------------------------------------------------------------------------
# 4. YAML frontmatter (hand-rolled, no PyYAML dependency)
# --------------------------------------------------------------------------

# A plain YAML scalar can't start with an indicator character, contain
# ": " or " #", or have surrounding spaces; such values get double quotes.
YAML_NEEDS_QUOTES = re.compile(r'^[\s\-?:,\[\]{}#&*!|>\'"%@`]|:\s|\s#|\s$')


def yaml_scalar(value: str) -> str:
    if value == "":
        return '""'
    if "\n" in value:
        # Multi-line text (notes): literal block scalar, kept verbatim.
        return "|\n" + "\n".join("  " + line for line in value.split("\n"))
    if YAML_NEEDS_QUOTES.search(value) or value.lower() in ("true", "false", "null", "~"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def build_frontmatter(fields: List[Tuple[str, object]]) -> str:
    """Render (key, value) pairs; a value is a string or a list of strings."""
    lines = ["---"]
    for key, value in fields:
        if isinstance(value, list):
            if value:
                lines.append(f"{key}:")
                lines.extend(f"  - {yaml_scalar(item)}" for item in value)
            else:
                lines.append(f"{key}: []")
        else:
            lines.append(f"{key}: {yaml_scalar(str(value))}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 5. Page rendering
# --------------------------------------------------------------------------

def render_company_page(name: str, people_links: List[str], now_iso: str) -> str:
    fm = build_frontmatter([
        ("name", name),
        ("type", "company"),
        ("slug", slugify(name)),
        ("source", "apple-contacts"),
        ("exportedAt", now_iso),
    ])
    body_lines = [f"# {name}", ""]
    if people_links:
        body_lines += ["## People", ""]
        body_lines += [f"- {link}" for link in sorted(people_links)]
        body_lines.append("")
    return fm + "\n" + "\n".join(body_lines)


def render_person_page(c: Contact, company_link: str, relation_links: List[str], now_iso: str) -> str:
    def labelled(pairs: List[Tuple[str, str]]) -> List[str]:
        return [f"{lbl}:{val}" for lbl, val in pairs]

    fm = build_frontmatter([
        ("uid", c.key),
        ("appleId", c.apple_id),
        ("type", "person"),
        ("fullName", c.full_name),
        ("firstName", c.first_name),
        ("middleName", c.middle_name),
        ("lastName", c.last_name),
        ("namePrefix", c.name_prefix),
        ("nameSuffix", c.name_suffix),
        ("nickname", c.nickname),
        ("organization", c.organization),
        ("department", c.department),
        ("jobTitle", c.job_title),
        ("company", company_link),
        ("emails", labelled(c.emails)),
        ("phones", labelled(c.phones)),
        ("addresses", labelled(c.addresses)),
        ("urls", labelled(c.urls)),
        ("social", labelled(c.social)),
        ("im", labelled(c.im)),
        ("birthday", c.birthday),
        ("relations", relation_links),
        ("notes", c.notes),
        ("source", "apple-contacts"),
        ("exportedAt", now_iso),
    ])
    body_lines = [f"# {c.full_name}", ""]
    # "Head of Sales at [[Contacts/Companies/Acme AG.md]]", or just one half.
    tagline = " at ".join(part for part in (c.job_title, company_link) if part)
    if tagline:
        body_lines += [tagline, ""]
    return fm + "\n" + "\n".join(body_lines)


# --------------------------------------------------------------------------
# 6. Manifest: stable filenames, renames and stale files across runs
#
# Each output dir holds a manifest mapping a stable key (person: uid,
# company: slug) to the filename it was written under. On the next run a
# key keeps its old filename as long as its name hasn't changed, so clash
# numbering ("Anna Meier 2") no longer depends on Contacts.app's return
# order. Files whose key has disappeared are "stale": reported by default
# and remembered in the manifest (so they keep being reported), or deleted
# with --prune. Only files named in a manifest are ever renamed or deleted.
# --------------------------------------------------------------------------

MANIFEST_NAME = ".contacts-manifest.json"


def is_plain_md_filename(name: object) -> bool:
    """Guard against a hand-edited manifest pointing outside its directory."""
    return (isinstance(name, str) and name.endswith(".md") and name == Path(name).name
            and not name.startswith("."))


def load_manifest(directory: Path) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return (entries, stale), both key -> filename; empty if no manifest."""
    path = directory / MANIFEST_NAME
    if not path.exists():
        return {}, {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = dict(data.get("entries", {}))
        stale = dict(data.get("stale", {}))
    except (ValueError, AttributeError, TypeError) as e:
        sys.exit(f"Cannot read {path}: {e}\nFix or remove it, then re-run.")
    for k, f in list(entries.items()) + list(stale.items()):
        if not is_plain_md_filename(f):
            sys.exit(f"{path}: entry {k!r} has an invalid filename {f!r}. Fix or remove the manifest.")
    return entries, stale


def write_manifest(plan: DirPlan, prune: bool) -> None:
    data = {
        "version": 1,
        "kind": plan.kind,
        "entries": plan.entries,
        # Kept stale files stay listed, so the next run reports them again.
        "stale": {} if prune else plan.stale,
    }
    # Write a temp file and rename it over the old one, so an interrupted
    # run never leaves a half-written manifest.
    tmp = plan.directory / (MANIFEST_NAME + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, plan.directory / MANIFEST_NAME)


def assign_stems(items: List[Tuple[Optional[str], str]], previous: Dict[str, str], reserved: Set[str]) -> List[str]:
    """Pick a unique filename stem for each (key, base_name) item.

    Pass 1: an item keeps its previous stem if that is still its base name,
    or its base name plus a clash number. Pass 2: every other item gets the
    base name, or the base name with ' 2', ' 3', ... on collision.
    `reserved` stems (stale files being kept) are never handed out.
    Comparison is case-insensitive, like the macOS file system."""
    taken = {r.lower() for r in reserved}
    stems: List[Optional[str]] = [None] * len(items)
    for i, (key, base) in enumerate(items):
        old_file = previous.get(key) if key else None
        if not old_file:
            continue
        old_stem = old_file[:-len(".md")]
        still_fits = old_stem == base or re.fullmatch(re.escape(base) + r" \d+", old_stem)
        if still_fits and old_stem.lower() not in taken:
            stems[i] = old_stem
            taken.add(old_stem.lower())
    for i, (_, base) in enumerate(items):
        if stems[i] is not None:
            continue
        stem, n = base, 1
        while stem.lower() in taken:
            n += 1
            stem = f"{base} {n}"
        stems[i] = stem
        taken.add(stem.lower())
    return stems  # type: ignore[return-value]


@dataclass
class DirPlan:
    """Everything this run will do in one output directory."""
    directory: Path
    kind: str                        # "person" or "company"
    stems: List[str]                 # filename stem per item, in input order
    renames: List[Tuple[str, str]]   # (old filename, new filename)
    stale: Dict[str, str]            # key -> filename of existing files whose key is gone
    entries: Dict[str, str]          # key -> filename, the new manifest entries


def plan_dir(directory: Path, kind: str, items: List[Tuple[Optional[str], str]], prune: bool) -> DirPlan:
    """Plan filenames, renames and stale files for (key, base_name) items.
    Items with key None get a filename but aren't tracked in the manifest."""
    entries, old_stale = load_manifest(directory)
    previous = {**old_stale, **entries}   # a key that's back in Contacts gets its file back
    current_keys = {k for k, _ in items if k}
    # Stale entries whose file is already gone (deleted by hand) are dropped.
    stale = {k: f for k, f in previous.items() if k not in current_keys and (directory / f).exists()}
    # Without --prune, stale files stay on disk, so their names are taken.
    reserved = set() if prune else {f[:-len(".md")] for f in stale.values()}
    stems = assign_stems(items, previous, reserved)

    renames: List[Tuple[str, str]] = []
    new_entries: Dict[str, str] = {}
    for (key, _), stem in zip(items, stems):
        if not key:
            continue
        new_entries[key] = f"{stem}.md"
        old = previous.get(key)
        if old and old != f"{stem}.md" and (directory / old).exists():
            renames.append((old, f"{stem}.md"))
    return DirPlan(directory, kind, stems, renames, stale, new_entries)


def apply_renames_and_prune(plan: DirPlan, prune: bool) -> None:
    # Prune first: a deleted stale file's name may be reused this run.
    if prune:
        for f in plan.stale.values():
            (plan.directory / f).unlink()
    # Rename in two passes via temp names, so swapped names ("A" <-> "B")
    # and case-only renames work.
    temps = []
    for i, (old, new) in enumerate(plan.renames):
        tmp = plan.directory / f".contacts-rename-{i}.tmp"
        os.rename(plan.directory / old, tmp)
        temps.append((tmp, new))
    for tmp, new in temps:
        os.rename(tmp, plan.directory / new)


def report_plan(label: str, plan: DirPlan, prune: bool, dry_run: bool) -> None:
    for old, new in plan.renames:
        print(f"  {label}: {'would rename' if dry_run else 'renamed'} {old} -> {new}")
    if prune:
        action = "would delete" if dry_run else "deleted"
    else:
        action = "stale (kept; --prune deletes)"
    for key, f in sorted(plan.stale.items(), key=lambda kv: kv[1]):
        print(f"  {label}: {action}: {f}  [{key}]")


# --------------------------------------------------------------------------
# 7. Main pipeline
# --------------------------------------------------------------------------

def link_relations(contacts: List[Contact], person_stems: List[str], person_param: str
                   ) -> Tuple[List[List[str]], List[str]]:
    """Return (relation entries per contact, unmatched-relation messages).

    A related name becomes a wikilink only if exactly one *other* contact
    has that full name (case- and whitespace-insensitive); otherwise it's
    kept as plain text, so nothing is guessed."""
    by_name: Dict[str, List[int]] = {}
    for i, c in enumerate(contacts):
        by_name.setdefault(normalize_ws(c.full_name).lower(), []).append(i)

    all_links: List[List[str]] = []
    unmatched: List[str] = []
    for i, c in enumerate(contacts):
        links = []
        for label, rel_name in c.relations:
            matches = [j for j in by_name.get(normalize_ws(rel_name).lower(), []) if j != i]
            if len(matches) == 1:
                links.append(f"{label}:{wikilink(person_param, person_stems[matches[0]])}")
            else:
                reason = f"{len(matches)} contacts share that name" if matches else "no contact with that name"
                unmatched.append(f'{c.full_name} -- "{label}: {rel_name}" ({reason})')
                links.append(f"{label}:{rel_name}")
        all_links.append(links)
    return all_links, unmatched


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--localspace", required=True, help="Root directory of the SilverBullet space")
    ap.add_argument("--person", default="Contacts/People", help="Path (under localspace) for person pages")
    ap.add_argument("--company", default="Contacts/Companies", help="Path (under localspace) for company pages")
    ap.add_argument("--dry-run", action="store_true", help="Parse and report, write nothing")
    ap.add_argument("--prune", action="store_true",
                    help="Delete files whose contact/company no longer exists (default: only report them)")
    ap.add_argument("--vcf", type=Path, default=None, help="Read vCards from this file instead of Contacts.app (testing)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Print extra detail: every raw osascript log line, untracked contacts, and the "
                         "full list of unmatched/ambiguous relations with reasons")
    args = ap.parse_args()

    localspace = Path(args.localspace).expanduser().resolve()
    person_dir = localspace / args.person
    company_dir = localspace / args.company
    if person_dir.resolve() == company_dir.resolve():
        sys.exit("--person and --company must be different directories (each keeps its own manifest).")

    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    # -- read and parse --
    if args.vcf:
        blob, ids = args.vcf.read_text(encoding="utf-8", errors="replace"), []
    else:
        print("Reading contacts from Contacts.app (this may take a while for a large address book)...")
        blob, ids = extract_from_contacts_app(verbose=args.verbose)
    contacts = [parse_contact(raw, apple_id=ids[i] if i < len(ids) else "")
                for i, raw in enumerate(split_vcards(blob))]
    print(f"Parsed {len(contacts)} contacts.")

    # Companies are the unique ORG strings, whitespace-normalised, otherwise
    # verbatim ("Acme AG" and "Acme" are two companies).
    company_names = sorted({c.organization for c in contacts if c.organization})
    print(f"Found {len(company_names)} unique companies.")

    # -- plan filenames --
    # Companies are keyed by slug. Distinct names with the same slug
    # ("Acme AG" / "Acme-AG") get "#2", "#3", ... in sorted-name order.
    company_items: List[Tuple[Optional[str], str]] = []
    slug_counts: Dict[str, int] = {}
    for name in company_names:
        slug = slugify(name)
        slug_counts[slug] = slug_counts.get(slug, 0) + 1
        key = slug if slug_counts[slug] == 1 else f"{slug}#{slug_counts[slug]}"
        company_items.append((key, sanitize_filename(name)))
    company_plan = plan_dir(company_dir, "company", company_items, args.prune)
    company_stem = dict(zip(company_names, company_plan.stems))

    # People are keyed by uid. Cards without one, or repeating an earlier
    # card's uid, still get a file but aren't tracked in the manifest.
    person_items: List[Tuple[Optional[str], str]] = []
    seen_keys: Set[str] = set()
    for c in contacts:
        key = c.key if c.key and c.key not in seen_keys else None
        if key:
            seen_keys.add(key)
        elif args.verbose:
            print(f"  Not tracked in manifest (no unique uid): {c.full_name}")
        person_items.append((key, sanitize_filename(c.full_name)))
    person_plan = plan_dir(person_dir, "person", person_items, args.prune)
    person_stems = person_plan.stems

    # -- render pages --
    relation_links, unmatched = link_relations(contacts, person_stems, args.person)
    company_people: Dict[str, List[str]] = {name: [] for name in company_names}
    person_pages: Dict[str, str] = {}   # filename -> content
    for c, stem, rels in zip(contacts, person_stems, relation_links):
        company_link = ""
        if c.organization:
            company_link = wikilink(args.company, company_stem[c.organization])
            company_people[c.organization].append(wikilink(args.person, stem))
        person_pages[f"{stem}.md"] = render_person_page(c, company_link, rels, now_iso)
    company_pages = {f"{company_stem[name]}.md": render_company_page(name, company_people[name], now_iso)
                     for name in company_names}

    # -- write: renames/prune, then pages, then the manifest last, so an
    # interrupted run is simply redone on the next one --
    if not args.dry_run:
        for plan, pages in ((person_plan, person_pages), (company_plan, company_pages)):
            plan.directory.mkdir(parents=True, exist_ok=True)
            apply_renames_and_prune(plan, args.prune)
            for filename, page in pages.items():
                (plan.directory / filename).write_text(page, encoding="utf-8")
            write_manifest(plan, args.prune)

    # -- report --
    where = "would be written to" if args.dry_run else "written to"
    print(f"People {where}:    {person_dir}")
    print(f"Companies {where}: {company_dir}")
    report_plan("people", person_plan, args.prune, args.dry_run)
    report_plan("companies", company_plan, args.prune, args.dry_run)
    if unmatched:
        print(f"Note: {len(unmatched)} relation(s) could not be matched to exactly one contact "
              f"and were kept as plain text instead of a link.")
        if args.verbose:
            for line in unmatched:
                print(f"  - {line}")
        else:
            print("  Re-run with -v/--verbose to see each one and why.")
    if args.dry_run:
        print("(dry run — nothing was written)")


if __name__ == "__main__":
    main()
