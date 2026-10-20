# contacts-mac-sb

Export Apple Contacts (macOS/iCloud) into Markdown files for a SilverBullet
space — one file per person, one file per company, linked to each other with
wikilinks, plus best-effort links between related people (spouse, friend,
etc. — Apple Contacts' "Related Names" field).

## Requirements

- macOS, with the contacts you want in Contacts.app (iCloud account synced
  locally is enough — nothing is read from the network directly).
- Python 3 (the one that ships with macOS is fine — no third-party packages
  are used).
- Automation permission for Contacts. The first run will trigger the macOS
  prompt; if you don't see it, grant it manually under
  **System Settings → Privacy & Security → Automation** — allow your
  terminal app (or `python3`) to control **Contacts**.

## Usage

```sh
python3 export_contacts.py --localspace ~/notes/MySilverBulletSpace
```

Optional flags:

| Flag | Default | Meaning |
|---|---|---|
| `--person` | `Contacts/People` | Path under `localspace` for person pages. May contain `/`. |
| `--company` | `Contacts/Companies` | Path under `localspace` for company pages. May contain `/`. |
| `--dry-run` | off | Parse everything, write nothing — use to sanity-check counts first |
| `--limit N` | none | Only process the first N contacts (quick smoke test) |
| `--vcf FILE` | none | Read vCards from a `.vcf` file instead of Contacts.app (for testing without touching the real address book) |

Example with custom folders (both containing a `/`):

```sh
python3 export_contacts.py \
  --localspace ~/notes/MySpace \
  --person "Contacts/People" \
  --company "Contacts/Companies"
```

Files land at:

```
<localspace>/<person>/<Name>.md
<localspace>/<company>/<Name>.md
```

Re-running the script overwrites files with a matching name. It does not
delete anything, and it does not currently preserve hand-written edits below
the frontmatter (see **Known limitations**) — so if you've started annotating
a contact page, check `git diff` before re-running and re-importing.

## What gets extracted

Extraction runs an AppleScript pass that asks Contacts.app for the vCard of
every person, so it picks up everything Contacts.app can export: names,
company/title, all emails/phones/addresses/URLs with their labels (including
Apple's custom "Home"/"Work"/other labels), IM handles, social profiles,
birthday, notes, and the "Related Names" field.

### Company pages (minimal frontmatter)

One page per **unique, verbatim `ORG` string** (whitespace-normalised only —
`Acme AG` and `Acme` are treated as two different companies, matching your
address book's actual spelling).

```yaml
---
name: Acme AG
type: company
slug: acme-ag
source: apple-contacts
exportedAt: 2026-09-19T15:32:49+02:00
---
```

Body: a heading and a generated `## People` list linking every person at
that company (recomputed on every run).

### Person pages (all fields in frontmatter)

```yaml
---
uid: <vCard UID, or the Contacts.app local id if the card has none>
appleId: <Contacts.app local record id — machine-specific, informational only>
type: person
fullName: ...
firstName: / middleName: / lastName: / namePrefix: / nameSuffix: / nickname: ...
organization: ... / department: ... / jobTitle: ...
company: "[[Contacts/Companies/Acme AG.md]]"     # wikilink, empty if no org
emails: ["Work:...", "Home:..."]
phones: ["Mobile:..."]
addresses: ["Work:Street, Postal City, Region, Country"]
urls: [...]
social: [...]
im: [...]
birthday: ...
relations: ["Spouse:[[Contacts/People/Jane Doe.md]]", "Friend:Some Unmatched Name"]
notes: |
  multi-line note text, verbatim
source: apple-contacts
exportedAt: ...
---
```

### Relations between people

Apple Contacts' "Related Names" field (spouse, child, friend, manager, etc.)
is the source for this — it's the one place Apple Contacts records a
person-to-person relationship. For each related name, the script looks for
**exactly one** other contact with that exact full name:

- exactly one match → a wikilink (`[[Contacts/People/Jane Doe.md]]`)
- zero or more than one match (ambiguous, or the related person isn't in
  your address book at all) → left as plain text, so nothing is silently
  guessed wrong

### Filenames and name clashes

Both people and companies use the sanitized display name as the filename
(illegal filesystem characters replaced with `-`). Two contacts with the same
full name get " 2", " 3", ... appended to the second and later ones — simple
and deterministic, but note it is **not** stable across re-runs in whatever
order Contacts.app happens to return people, so a clash pair could in
principle swap numbers between runs. If that matters to you, rename manually
after import; the script never renames or deletes an existing file on its
own.

## Known limitations / things a v2 could add

- **No merge with hand-edited pages.** Re-running overwrites the whole file.
  If you want to add your own notes safely, put them in a separate linked
  page rather than editing the generated file directly, or ask for a
  "generated section + preserved body" version (the same split used in the
  broader `Contacts` SilverBullet library design in this project) to be
  added here.
- **Relation matching is exact-name, not fuzzy.** A related name spelled
  slightly differently than the actual contact ("Jane" vs "Jane Doe") won't
  match.
- **No photos.** Text fields only.
- Extraction can take a couple of minutes on a large (500–1000 contact)
  address book, since AppleScript talks to Contacts.app one property at a
  time per contact.
