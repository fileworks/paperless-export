# paperless-export

A thin scheduled wrapper around [Paperless-ngx](https://docs.paperless-ngx.com)'s
built-in `document_exporter`, plus the one thing it doesn't do: a materialized
**`_Steuer/YYYY/` tax view** built from your `Steuer-YYYY` tags.

Paperless's exporter already produces the full no-lock-in export — every
document laid out by your storage-path template, originals *and* PDF/A archive
versions, and a complete `manifest.json` (tags, correspondents, types, custom
fields). This tool deliberately does **not** rebuild any of that. It:

1. runs `document_exporter <target> --use-filename-format --compare-checksums --delete`
   (each flag toggleable), surfacing failures honestly and **falling back to a
   flat export with a clear warning when a path exceeds the OS limit**,
2. reads `manifest.json` and builds `_Steuer/<YYYY>/` — one symlink (or copy)
   per document tagged `Steuer-YYYY` — plus a greppable `_Steuer/INDEX.csv`,
3. optionally embeds tags/correspondent/type into the exported PDFs' XMP
   (`--embed-tags`, needs the `[pdf]` extra).

```
export/
  Bescheid/Finanzamt/2024-05-01 Steuerbescheid.pdf   # ← document_exporter
  manifest.json                                       # ← document_exporter
  _Steuer/
    2024/2024-05-01 Steuerbescheid.pdf → ../../Bescheid/Finanzamt/…
    INDEX.csv                                         # year,title,correspondent,created,original_path
```

## Install

```sh
pipx install paperless-export          # + 'paperless-export[pdf]' for --embed-tags
# or
brew install fileworks/tap/paperless-export
```

*(Not yet published — first release pending; until then: `uv run paperless-export` from a checkout.)*

## Usage

```sh
# the nightly job (run from the directory containing your compose file):
paperless-export run --export-dir /volume1/paperless/export

# rebuild only the tax view from an existing export:
paperless-export tax-view --export-dir /volume1/paperless/export

# FAT/exFAT or cloud targets that don't preserve symlinks:
paperless-export run --export-dir ./export --copy
```

Notes:

- `--exporter-target` (default `../export`) is the path **as the exporter
  process sees it** inside the container; `--export-dir` is the same directory
  **on this host**. With the standard compose setup they're the same bind mount.
- `PAPERLESS_URL` + `PAPERLESS_TOKEN` (env or flags) enable a preflight check
  so a bad token fails fast with a clear message — they're optional because the
  exporter itself runs inside the container and needs no API access.
- `--embed-tags` rewrites the exported PDFs, which changes their checksums, so
  those files are re-exported on the next `--compare-checksums` run. The
  manifest already preserves all metadata — only embed if you want tags
  *inside* the files.

## Behavior guarantees

- **Read-only against Paperless** — writes only into the export directory.
- **Idempotent** — the `_Steuer/` view is rebuilt from scratch each run; safe nightly.
- **Verifiable** — after a run, `_Steuer/2025/` contains exactly the documents
  tagged `Steuer-2025`; `INDEX.csv` matches a manifest query.
- **Honest failures** — a non-zero `document_exporter` exit surfaces its stderr
  and exit code; symlink-unsupported filesystems auto-switch to copies with a notice.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 2 | bad configuration / authentication failure |
| 3 | Paperless (or Docker) not reachable |
| 4 | export dir missing / manifest unreadable |
| *n* | `document_exporter` failed with exit code *n* |

## Scheduling on a Synology (DSM Task Scheduler)

```sh
cd /volume1/docker/paperless && \
/usr/local/bin/paperless-export run --export-dir /volume1/paperless/export
```

Nightly, after the Paperless backup window; the export target should live on a
share covered by your backup chain.

## Development

```sh
uv sync --all-extras --dev
uv run ruff check . && uv run ruff format --check .   # lint
uv run mypy                                           # strict types
uv run pytest                                         # tests
uv build
```

Conventional Commits drive releases (`python-semantic-release`): merge to
`main` → version bump + changelog + GitHub Release + PyPI publish (OIDC) +
Homebrew formula bump.

## License

MIT
