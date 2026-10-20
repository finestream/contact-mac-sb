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
        [--dry-run] [--limit N]

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
import re
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VCARD_SEP = "\n===VCARD-SEP===\n"
IDS_MARKER = "\n===IDS-SECTION===\n"

# --------------------------------------------------------------------------
# 1. Extraction: pull raw vCards (+ local Contacts.app ids) out of Contacts.app
#
# Everything is returned on stdout via `return` — earlier versions of this
# script wrote to temp files with AppleScript's `open for access`, but that
# hits macOS's file-system sandboxing for the process driving osascript
# ("Can't get eof of vcardFile. Access not allowed. (-1723)") well before
# Contacts' own Automation permission even comes into play. Passing the
# whole export back as a string sidesteps that: Contacts is the only thing
# that needs a permission grant.
# --------------------------------------------------------------------------

# `log` statements write to stderr immediately (unbuffered, one line per
# call), so a progress line logged every ~5% of the way through the repeat
# loop lets Python report progress while osascript is still running, without
# needing anything fancier than reading stderr as it arrives.
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
    """Run the AppleScript export and return (raw_vcard_blob, ids_in_order).

    Streams stderr while osascript runs so progress log lines (see above)
    can be reported as they arrive, rather than only after the whole export
    finishes.
    """
    script = APPLESCRIPT_TEMPLATE % {
        "sep": VCARD_SEP.replace("\n", "\\n"),
        "ids_marker": IDS_MARKER.replace("\n", "\\n"),
    }
    try:
        proc = subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        sys.exit("osascript not found — this script must run on macOS.")

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

    stdout_data = proc.stdout.read()
    returncode = proc.wait(timeout=1800)

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
    ids = id_blob.splitlines()
    return vcard_blob, ids


# --------------------------------------------------------------------------
# 2. vCard parsing (minimal, dependency-free; tuned for Apple's 3.0 export)
# --------------------------------------------------------------------------

def unfold_lines(text: str) -> List[str]:
    """RFC 6350 line unfolding: a line starting with space/tab continues the
    previous line."""
    raw_lines = text.replace("\r\n", "\n").split("\n")
    lines: List[str] = []
    for raw in raw_lines:
        if raw.startswith(" ") or raw.startswith("\t"):
            if lines:
                lines[-1] += raw[1:]
            continue
        lines.append(raw)
    return lines


def unescape_value(value: str) -> str:
    out = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            elif nxt in (",", ";", "\\"):
                out.append(nxt)
            else:
                out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


@dataclass
class VLine:
    group: Optional[str]
    name: str
    params: Dict[str, List[str]]
    value: str


def parse_line(line: str) -> Optional[VLine]:
    if not line or ":" not in line:
        return None
    key_part, _, value = line.partition(":")
    group = None
    if "." in key_part:
        maybe_group, _, rest = key_part.partition(".")
        # A "group" is a bare token like "item1"; guard against ORG-style
        # names that happen to contain a dot (rare, but be defensive).
        if re.fullmatch(r"[A-Za-z0-9_-]+", maybe_group):
            group = maybe_group
            key_part = rest
    parts = key_part.split(";")
    name = parts[0].strip().upper()
    params: Dict[str, List[str]] = {}
    for p in parts[1:]:
        if "=" in p:
            pk, _, pv = p.partition("=")
            params.setdefault(pk.strip().upper(), []).append(pv.strip().strip('"'))
        else:
            # bare TYPE shorthand, e.g. ";HOME"
            params.setdefault("TYPE", []).append(p.strip())
    return VLine(group=group, name=name, params=params, value=value)


def split_vcards(blob: str) -> List[str]:
    if VCARD_SEP in blob:
        chunks = blob.split(VCARD_SEP)
    else:
        # Fallback for plain concatenated .vcf files (e.g. --vcf testing input)
        # that don't carry our extraction separator: split on each BEGIN:VCARD.
        chunks = re.split(r"(?=BEGIN:VCARD)", blob)
    return [c.strip() for c in chunks if "BEGIN:VCARD" in c]


AB_LABEL_RE = re.compile(r"_\$!<(.+?)>!\$_")


def clean_label(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    m = AB_LABEL_RE.match(raw.strip())
    if m:
        return m.group(1)
    return raw.strip().strip("_")


# Generic vCard TYPE tokens that don't carry meaningful label information —
# every EMAIL/TEL in the world has one of these, so an accompanying
# X-ABLabel (or a more specific TYPE) is preferred when available.
GENERIC_TYPE_TOKENS = {"PREF", "INTERNET", "VOICE"}


def label_for(vlines_by_group: Dict[str, List[VLine]], vline: VLine, params_key="TYPE") -> Optional[str]:
    """Prefer a sibling X-ABLabel in the same item group (Apple's
    grouped-label convention, used for Home/Work/custom labels); fall back to
    a specific TYPE param; then any TYPE param; then None."""
    if vline.group:
        for sib in vlines_by_group.get(vline.group, []):
            if sib.name == "X-ABLABEL":
                return clean_label(sib.value)
    types = vline.params.get(params_key, [])
    specific = [t for t in types if t.upper() not in GENERIC_TYPE_TOKENS]
    if specific:
        return clean_label(specific[0])
    if types:
        return clean_label(types[0])
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
    emails: List[Tuple[str, str]] = field(default_factory=list)     # (label, value)
    phones: List[Tuple[str, str]] = field(default_factory=list)
    addresses: List[Tuple[str, str]] = field(default_factory=list)
    urls: List[Tuple[str, str]] = field(default_factory=list)
    social: List[Tuple[str, str]] = field(default_factory=list)
    im: List[Tuple[str, str]] = field(default_factory=list)
    relations: List[Tuple[str, str]] = field(default_factory=list)  # (label, name)
    notes: str = ""


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

        if name == "UID":
            c.uid = val
        elif name == "FN":
            c.full_name = val
        elif name == "N":
            parts = [unescape_value(p) for p in l.value.split(";")]
            parts += [""] * (5 - len(parts))
            c.last_name, c.first_name, c.middle_name, c.name_prefix, c.name_suffix = parts[:5]
        elif name == "NICKNAME":
            c.nickname = val
        elif name == "ORG":
            parts = [unescape_value(p) for p in l.value.split(";") if p != ""]
            if parts:
                c.organization = normalize_ws(parts[0])
            if len(parts) > 1:
                c.department = normalize_ws(parts[1])
        elif name == "TITLE":
            c.job_title = val
        elif name == "BDAY":
            c.birthday = val
        elif name == "NOTE":
            c.notes = val
        elif name == "EMAIL":
            c.emails.append((label_for(by_group, l) or "other", val))
        elif name == "TEL":
            c.phones.append((label_for(by_group, l) or "other", val))
        elif name == "ADR":
            comps = [unescape_value(p) for p in l.value.split(";")]
            comps += [""] * (7 - len(comps))
            # ADR: PO Box; Extended; Street; City; Region; PostalCode; Country
            _, _, street, city, region, postal, country = comps[:7]
            addr_bits = [b for b in (street, f"{postal} {city}".strip(), region, country) if b.strip()]
            c.addresses.append((label_for(by_group, l) or "other", ", ".join(addr_bits)))
        elif name == "URL":
            c.urls.append((label_for(by_group, l) or "other", val))
        elif name in ("X-SOCIALPROFILE",):
            service = None
            if "TYPE" in l.params:
                service = clean_label(l.params["TYPE"][0])
            c.social.append((service or label_for(by_group, l) or "other", val))
        elif name in ("IMPP",):
            proto = val.split(":", 1)[0] if ":" in val else "im"
            c.im.append((proto, val.split(":", 1)[-1]))
        elif name == "X-ABRELATEDNAMES":
            lbl = None
            if l.group:
                for sib in by_group.get(l.group, []):
                    if sib.name == "X-ABLABEL":
                        lbl = clean_label(sib.value)
            c.relations.append((lbl or "related", val))
        elif name == "RELATED":  # vCard 4.0 form, just in case
            lbl = clean_label(l.params.get("TYPE", [None])[0]) if l.params.get("TYPE") else None
            c.relations.append((lbl or "related", val))

    if not c.full_name:
        c.full_name = normalize_ws(f"{c.first_name} {c.last_name}") or c.nickname or "Unnamed Contact"

    return c


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# --------------------------------------------------------------------------
# 3. Filenames, slugs, wikilinks
# --------------------------------------------------------------------------

INVALID_FILENAME_CHARS = re.compile(r'[\/\\:\*\?"<>\|\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    name = normalize_ws(name)
    name = INVALID_FILENAME_CHARS.sub("-", name)
    name = name.strip(" .")
    return name or "Unnamed"


def slugify(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = re.sub(r"[^a-zA-Z0-9]+", "-", n).strip("-").lower()
    return n or "x"


def wikilink(dir_param: str, stem: str) -> str:
    return f"[[{dir_param}/{stem}.md]]"


# --------------------------------------------------------------------------
# 4. YAML frontmatter (hand-rolled, no PyYAML dependency)
# --------------------------------------------------------------------------

def yaml_scalar(value: str) -> str:
    if value == "":
        return '""'
    needs_quote = bool(re.search(r'^[\s\-?:,\[\]{}#&*!|>\'"%@`]|:\s|\s#|\n', value)) or value.strip() != value
    if "\n" in value:
        # literal block scalar
        indented = "\n".join("  " + line for line in value.split("\n"))
        return "|\n" + indented
    if needs_quote or value.lower() in ("true", "false", "null", "~"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def yaml_list(items: List[str]) -> str:
    if not items:
        return "[]"
    return "\n" + "\n".join(f"  - {yaml_scalar(i)}" for i in items)


def build_frontmatter(fields: List[Tuple[str, object]]) -> str:
    lines = ["---"]
    for key, value in fields:
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:{yaml_list(value)}")
        else:
            sval = str(value)
            rendered = yaml_scalar(sval)
            if rendered.startswith("|"):
                lines.append(f"{key}: {rendered}")
            else:
                lines.append(f"{key}: {rendered}")
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
        body_lines.append("## People")
        body_lines.append("")
        for link in sorted(people_links):
            body_lines.append(f"- {link}")
        body_lines.append("")
    return fm + "\n" + "\n".join(body_lines)


def render_person_page(c: Contact, company_link: Optional[str], relation_links: List[str], now_iso: str) -> str:
    fields: List[Tuple[str, object]] = [
        ("uid", c.uid or c.apple_id or ""),
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
        ("company", company_link or ""),
        ("emails", [f"{lbl}:{val}" for lbl, val in c.emails]),
        ("phones", [f"{lbl}:{val}" for lbl, val in c.phones]),
        ("addresses", [f"{lbl}:{val}" for lbl, val in c.addresses]),
        ("urls", [f"{lbl}:{val}" for lbl, val in c.urls]),
        ("social", [f"{lbl}:{val}" for lbl, val in c.social]),
        ("im", [f"{lbl}:{val}" for lbl, val in c.im]),
        ("birthday", c.birthday),
        ("relations", relation_links),
        ("notes", c.notes),
        ("source", "apple-contacts"),
        ("exportedAt", now_iso),
    ]
    fm = build_frontmatter(fields)
    body_lines = [f"# {c.full_name}", ""]
    if c.job_title or c.organization:
        line = c.job_title
        if c.organization:
            line = f"{line} at {company_link}" if line else f"{company_link}"
        body_lines.append(line)
        body_lines.append("")
    return fm + "\n" + "\n".join(body_lines)


# --------------------------------------------------------------------------
# 6. Main pipeline
# --------------------------------------------------------------------------

def unique_path_for(base_dir: Path, stem: str, taken: Dict[str, int]) -> str:
    """Return a filesystem-unique stem, appending ' N' on collision."""
    key = stem.lower()
    if key not in taken:
        taken[key] = 1
        return stem
    taken[key] += 1
    new_stem = f"{stem} {taken[key]}"
    return unique_path_for(base_dir, new_stem, taken) if new_stem.lower() in taken else new_stem


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--localspace", required=True, help="Root directory of the SilverBullet space")
    ap.add_argument("--person", default="Contacts/People", help="Path (under localspace) for person pages")
    ap.add_argument("--company", default="Contacts/Companies", help="Path (under localspace) for company pages")
    ap.add_argument("--dry-run", action="store_true", help="Parse and report, write nothing")
    ap.add_argument("--limit", type=int, default=None, help="Only process the first N contacts (testing)")
    ap.add_argument("--vcf", type=Path, default=None, help="Read vCards from this file instead of Contacts.app (testing)")
    ap.add_argument("-v", "--verbose", action="store_true",
                     help="Print extra detail: every raw osascript log line, and the full list of "
                          "unmatched/ambiguous relations with reasons")
    args = ap.parse_args()

    localspace = Path(args.localspace).expanduser().resolve()
    person_dir = localspace / args.person
    company_dir = localspace / args.company

    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    if args.vcf:
        blob = args.vcf.read_text(encoding="utf-8", errors="replace")
        ids = [""] * blob.count("BEGIN:VCARD")
    else:
        print("Reading contacts from Contacts.app (this may take a while for a large address book)...")
        blob, ids = extract_from_contacts_app(verbose=args.verbose)

    raw_cards = split_vcards(blob)
    if args.limit:
        raw_cards = raw_cards[: args.limit]
        ids = ids[: args.limit]

    contacts = [parse_contact(raw, apple_id=(ids[i] if i < len(ids) else "")) for i, raw in enumerate(raw_cards)]
    print(f"Parsed {len(contacts)} contacts.")

    # -- companies: unique, verbatim (whitespace-normalised) ORG strings --
    company_names = sorted({c.organization for c in contacts if c.organization})
    print(f"Found {len(company_names)} unique companies.")

    company_taken: Dict[str, int] = {}
    company_stem_by_name: Dict[str, str] = {}
    for name in company_names:
        stem = unique_path_for(company_dir, sanitize_filename(name), company_taken)
        company_stem_by_name[name] = stem

    # -- people: unique filenames, disambiguated on full-name collision --
    person_taken: Dict[str, int] = {}
    person_stem_by_contact: Dict[int, str] = {}
    for i, c in enumerate(contacts):
        base = sanitize_filename(c.full_name)
        stem = unique_path_for(person_dir, base, person_taken)
        person_stem_by_contact[i] = stem

    # lookup for relation matching: normalised full name -> list of indices
    name_index: Dict[str, List[int]] = {}
    for i, c in enumerate(contacts):
        name_index.setdefault(normalize_ws(c.full_name).lower(), []).append(i)

    # -- render + write people --
    company_people_links: Dict[str, List[str]] = {name: [] for name in company_names}

    if not args.dry_run:
        person_dir.mkdir(parents=True, exist_ok=True)
        company_dir.mkdir(parents=True, exist_ok=True)

    unmatched_details: List[str] = []
    for i, c in enumerate(contacts):
        stem = person_stem_by_contact[i]
        company_link = None
        if c.organization:
            company_link = wikilink(args.company, company_stem_by_name[c.organization])
            company_people_links[c.organization].append(wikilink(args.person, stem))

        relation_links: List[str] = []
        for label, rel_name in c.relations:
            candidates = name_index.get(normalize_ws(rel_name).lower(), [])
            candidates = [idx for idx in candidates if idx != i]
            if len(candidates) == 1:
                target_stem = person_stem_by_contact[candidates[0]]
                relation_links.append(f"{label}:{wikilink(args.person, target_stem)}")
            else:
                reason = "no contact with that name" if not candidates else f"{len(candidates)} contacts share that name"
                unmatched_details.append(f'{c.full_name} -- "{label}: {rel_name}" ({reason})')
                relation_links.append(f"{label}:{rel_name}")

        page = render_person_page(c, company_link, relation_links, now_iso)
        if args.dry_run:
            continue
        (person_dir / f"{stem}.md").write_text(page, encoding="utf-8")

    for name in company_names:
        page = render_company_page(name, company_people_links[name], now_iso)
        if args.dry_run:
            continue
        (company_dir / f"{company_stem_by_name[name]}.md").write_text(page, encoding="utf-8")

    print(f"People written to:    {person_dir}")
    print(f"Companies written to: {company_dir}")
    if unmatched_details:
        print(f"Note: {len(unmatched_details)} relation(s) could not be matched to exactly one contact "
              f"and were kept as plain text instead of a link.")
        if args.verbose:
            for line in unmatched_details:
                print(f"  - {line}")
        else:
            print("  Re-run with -v/--verbose to see each one and why.")
    if args.dry_run:
        print("(dry run — nothing was written)")


if __name__ == "__main__":
    main()
