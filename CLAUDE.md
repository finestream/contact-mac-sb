# contact-mac-sb

Exports Apple Contacts (macOS/iCloud) to Markdown files for a SilverBullet
space: one file per person, one per company, linked with wikilinks.
Single script, Python 3 stdlib only: `export_contacts.py`. See `README.md`
for the user-facing field reference.

## Run and test

```sh
# Safe test, no Contacts.app access, no real data:
python3 export_contacts.py --localspace /tmp/sb --vcf test_fixture.vcf -v

# Real run (macOS only). Use --dry-run first, then without it:
python3 export_contacts.py --localspace ~/Syncthing/notes/testcontacts --dry-run -v
```

Flags: `--localspace` (required), `--person` (default `Contacts/People`),
`--company` (default `Contacts/Companies`), `--dry-run`, `--prune`,
`--vcf FILE`, `-v/--verbose`. Both `--person` and `--company` are paths under
localspace and may contain `/`.

Output: `<localspace>/<person>/<Name>.md` and `<localspace>/<company>/<Name>.md`.
Internal links are written literally as `[[<person>/<Name>.md]]` and
`[[<company>/<Name>.md]]` (the `.md` suffix is intentional, per the spec).

## Spec (what the owner asked for)

1. Extract contacts from Apple Contacts.
2. Extract unique companies.
3. Company files: minimal frontmatter (`name`, `type`, `slug`, `source`, `exportedAt`).
4. Person files: all fields in frontmatter, plus a link to their company.
5. Relations between people (Apple "Related Names") become md links.
6. People and companies go to separate, configurable directories.

## Design decisions

- Extraction is AppleScript (`osascript`): `vcard of every person`, parsed by a
  small hand-written vCard parser. No third-party packages, on purpose.
- Apple grouped labels (`item1.EMAIL` + `item1.X-ABLabel:_$!<Work>!$_`) are
  handled; an X-ABLabel wins over generic TYPE tokens (`INTERNET`, `PREF`, `VOICE`).
- Companies = unique ORG strings, whitespace-normalised, otherwise verbatim
  (`Acme AG` and `Acme` are different companies).
- Relations link only when the related name matches exactly one other contact's
  full name. Otherwise they stay plain text; `-v` lists each with the reason.
- Filename clashes get ` 2`, ` 3`, ... appended.
- Filenames must also be valid SilverBullet page names (rules from
  `plug-api/lib/ref.ts` `refRegex` / `isValidName` in silverbulletmd/silverbullet):
  no `@ # $ | < >`, no `[[`/`]]`, no leading `.`/`^`, no trailing `.ext`
  (`example.com` would be a document, not a page). See `sanitize_filename`.
- Structured vCard values (N, ORG, ADR) split on unescaped `;` only
  (`split_components`); `\\;` is an escaped backslash followed by a separator.

## Traps (do not relearn these)

- **Never use AppleScript `open for access` / `write` to temp files.** It fails
  with `-1723 Access not allowed` for the process driving osascript, before
  Contacts permission even matters. Everything is returned on stdout instead.
- Progress: the AppleScript `log`s `PROGRESS n/total` roughly every 5% to
  stderr; Python streams stderr while osascript runs. Do not switch to
  `subprocess.run(capture_output=True)`, it would hide progress until the end.
- **osascript stdout must go to a temp file (opened by Python), not a pipe.**
  The result is written only at exit while we're still draining stderr; with
  a pipe, osascript blocks once ~64 KB are unread and both sides hang.
  Verified: a 1 MB return hangs with a pipe, takes 0.1 s with a temp file.
- Automation permission: if no prompt appears, macOS holds a stored denial.
  Fix: `tccutil reset AppleEvents com.googlecode.iterm2` (use
  `com.apple.Terminal` for Terminal.app), then re-run. Check with
  `osascript -e 'tell application "Contacts" to count every person'`.
- `uid` (vCard UID) is the only stable identity. Never key anything on the
  filename or display name; duplicate names are normal.

## Known gaps / next work

- Stale-file handling is done: `.contacts-manifest.json` per output dir
  (people by `uid`, companies by `slug`; slug clashes get `#2`). Keys keep
  their old filename while the name is unchanged; changed names are renamed;
  missing keys are stale (reported and kept in the manifest's `stale` map, or
  deleted with `--prune`).
  Files from pre-manifest runs are untracked; nothing outside a manifest is
  ever renamed or deleted.
- **Re-runs overwrite whole files**, so hand-written notes are lost. Planned:
  a marker line (e.g. `<!-- contacts:generated:end -->`); regenerate above it,
  keep everything below byte for byte.
- Relation matching is exact full-name only (no fuzzy matching).
- No photos.
- Extraction takes minutes for ~800 contacts; this is AppleScript, not a bug.

## Conventions

- Never run the real export against the user's address book without
  `--dry-run` first, and never write outside the given `--localspace`.
- Test changes with `--vcf test_fixture.vcf`; add cases to the fixture for new
  parsing behaviour.
- Commit only when asked.

## Background

Background lives in the claude.ai project "Import contacts in silverbullet.md":
`contacts-overview.md` (status, known gaps, lessons such as clash naming and
SilverBullet page-name rules) and `contact-identifiers-and-links.md`
(`addressbook://` deep links). An earlier, heavier design (Python sidecar +
Space Lua library, CardDAV via vdirsyncer) was abandoned in October 2026 in
favour of this script; don't reintroduce it.
