# paperless-export

A thin scheduled wrapper around [Paperless-ngx](https://docs.paperless-ngx.com)'s
built-in `document_exporter`, plus the one thing it doesn't do: a materialized
**`_Steuer/YYYY/` tax view** built from your `Steuer-YYYY` tags.

Paperless's exporter already produces the full no-lock-in export — every
document laid out by your storage-path template, originals *and* PDF/A archive
versions, and a complete `manifest.json` (tags, correspondents, types, custom
fields). This tool deliberately does **not** rebuild any of that. It:

1. runs `document_exporter <target> --use-filename-format --compare-checksums --delete`
   (each flag toggleable), streams progress with bounded diagnostics, and
   **falls back to a flat export with a clear warning when a path exceeds the
   OS limit**,
2. optionally embeds metadata into each distinct exported original/archive PDF
   before any derived copies are created,
3. validates every manifest path inside the export root, then builds
   `_Steuer/<YYYY>/` — one original-based symlink (or copy) per document tagged
   `Steuer-YYYY` — plus a greppable `_Steuer/INDEX.csv`,
4. reports incomplete requested projections explicitly rather than silently
   omitting them.

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

Version `0.1.0` is published on
[PyPI](https://pypi.org/project/paperless-export/0.1.0/), as a
[GitHub Release](https://github.com/fileworks/paperless-export/releases/tag/v0.1.0),
and through `fileworks/tap`. Development after that tag remains unreleased
until the normal release workflow runs.

## Usage

```sh
# the nightly job (run from the directory containing your compose file):
paperless-export run --export-dir /volume1/paperless/export

# protected secret-file path only; the passphrase value never enters argv/env:
paperless-export run \
  --export-dir /volume1/paperless/export \
  --passphrase-file /run/secrets/paperless-export-passphrase

# rebuild only the tax view from an existing export:
paperless-export tax-view --export-dir /volume1/paperless/export

# FAT/exFAT or cloud targets that don't preserve symlinks:
paperless-export run --export-dir ./export --copy

# embed originals and archive PDFs without creating _Steuer:
paperless-export run --export-dir ./export --no-tax-view --embed-tags
```

Notes:

- `--exporter-target` (default `../export`) is the path **as the exporter
  process sees it** inside the container; `--export-dir` is the same directory
  **on this host**. With the standard compose setup they're the same bind mount.
- `PAPERLESS_URL` + `PAPERLESS_TOKEN` (env or flags) enable a preflight check
  so a bad token fails fast with a clear message — they're optional because the
  exporter itself runs inside the container and needs no API access.
- `PAPERLESS_EXPORT_PASSPHRASE_FILE` is a path-only alias for
  `--passphrase-file`. `-` reads the passphrase once from standard input.
  Protected files must be regular, not symlinks, and mode `0600` on POSIX.
- Passphrase transport is supported for the default
  `docker compose exec -T webserver document_exporter` adapter. A custom
  exporter command is rejected when a passphrase is configured unless this
  project gains a separately reviewed stdin adapter for it.
- `--embed-tags` rewrites each distinct original PDF and Paperless PDF/A
  archive. Non-PDF originals are skipped. Embedding happens before `_Steuer`
  copies, so copy mode receives the updated bytes. Rewrites change checksums,
  so Paperless can re-export those files on the next
  `--compare-checksums` run.

## Passphrase and export security

Without a passphrase, the command warns before launching Paperless because
Paperless-ngx 2.20.x may store supported account secrets in plaintext.
Paperless's native passphrase protects these fields:

- mail-account `password` and `refresh_token`;
- social-token `token` and `token_secret`.

The passphrase does **not** encrypt the entire `manifest.json`, exported
documents, or other metadata. Paperless records the cryptographic parameters
in `metadata.json`, and a later import needs the same passphrase. Keep the
passphrase in a secret-manager-mounted file (or provide it over stdin), make
the export directory accessible only to the backup account, and protect backup
copies with storage-level encryption. Do not put the secret value in a shell
argument or environment variable.

Every original and archive path read from `manifest.json` is treated as
untrusted. Absolute paths, Windows drive/UNC forms, empty/malformed components,
parent traversal, and symlink escapes are rejected before `_Steuer` is cleared
or any PDF is opened. The same confinement is repeated immediately before
file operations. Do not modify the export tree concurrently with
post-processing.

## Behavior guarantees

- **Read-only against Paperless** — writes only into the export directory.
- **Idempotent** — the `_Steuer/` view is rebuilt from scratch each run; safe nightly.
- **Verifiable** — after a run, `_Steuer/2025/` contains exactly the documents
  tagged `Steuer-2025`; `INDEX.csv` matches a manifest query.
- **Honest failures** — exporter, unavailable infrastructure, unsafe output,
  and incomplete projections have separate stable exit categories. Missing
  sources and PDF failures are aggregated while remaining safe work continues.
- **Never silent** — `document_exporter`'s output is relayed live rather than
  buffered until the end. Final errors repeat only the last 64 KiB diagnostic
  tail; path-too-long detection covers the entire stream independently.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | complete requested output, including successful advisory fallbacks |
| 1 | unexpected wrapper failure |
| 2 | bad configuration / authentication failure |
| 3 | Paperless API or Docker/Compose/service/container unavailable |
| 4 | malformed, unsafe, missing, or fatally unwritable export output |
| 5 | exporter succeeded but requested post-processing is incomplete |
| 6 | `document_exporter` ran but failed (its child code remains in diagnostics) |

## Scheduling on a Synology (DSM Task Scheduler)

```sh
cd /volume1/docker/paperless && \
/usr/local/bin/paperless-export run --export-dir /volume1/paperless/export
```

Nightly, after the Paperless backup window; the export target should live on a
share covered by your backup chain.

## Development

```sh
uv lock --check
uv sync --locked --all-extras --dev
uv run ruff check . && uv run ruff format --check .   # lint
uv run mypy                                           # strict types
uv run pytest                                         # tests
uv build
```

Conventional Commits drive releases (`python-semantic-release`): merge to
`main` → version bump + changelog + GitHub Release + PyPI publish (OIDC) +
Homebrew formula bump.

For per-clone paths, commands, or preferences, create an ignored
`CLAUDE.local.md` at the repository root. Do not put credentials or other
secrets in it.

## License

MIT
