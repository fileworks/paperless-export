<img src=".github/icon.svg" alt="" width="72" height="72" align="left">

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
4. publishes the derived view only after every required source and staged output
   validates; a failed replacement leaves the prior complete `_Steuer` current.

```
export/
  Bescheid/Finanzamt/2024-05-01 Steuerbescheid.pdf   # ← document_exporter
  manifest.json                                       # ← document_exporter
  _Steuer/
    2024/2024-05-01 Steuerbescheid.pdf → ../../Bescheid/Finanzamt/…
    INDEX.csv                                         # year,title,correspondent,created,original_path
```

## Status

Released **1.2.0** — verified on PyPI, as a GitHub Release, and through
`fileworks/tap` on 2026-08-01. Development after that tag is unreleased
until the release workflow runs.

## Overview

`paperless-export` wraps Paperless-ngx's own `document_exporter` and adds what
it leaves out: a scheduled, verified run, streamed progress instead of silence,
and an optional tax view (`_Steuer`) built from your existing tags.

It is an escape hatch, not a backup — it produces a readable copy of what
Paperless holds, so that Paperless never becomes the only place your documents
exist.

## Install

```sh
pipx install paperless-export          # + 'paperless-export[pdf]' for --embed-tags
# or
brew install fileworks/tap/paperless-export
```

Version `1.2.0` is published on
[PyPI](https://pypi.org/project/paperless-export/1.2.0/), as a
[GitHub Release](https://github.com/fileworks/paperless-export/releases/tag/v1.2.0),
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
- `PAPERLESS_EXPORT_LOG_FILE` is the environment alias for `--log-file`.
  Without it, a bounded rotating `paperless-export.log` is written beside the
  export directory for unattended-job diagnostics.
- `PAPERLESS_EXPORT_TIMEOUT_SECONDS` is the environment alias for
  `--exporter-timeout`. The reviewed default is six hours; expiry terminates the
  child cleanly, then force-kills only if it will not exit.
- Passphrase transport is supported for the default
  `docker compose exec -T webserver document_exporter` adapter. A custom
  exporter command is rejected when a passphrase is configured unless this
  project gains a separately reviewed stdin adapter for it.
- `--embed-tags` rewrites each distinct original PDF and Paperless PDF/A
  archive. Non-PDF originals are skipped. Embedding happens before `_Steuer`
  copies, so copy mode receives the updated bytes. Rewrites change checksums,
  so Paperless can re-export those files on the next
  `--compare-checksums` run.

## Quick start

```sh
# from the directory holding your Paperless compose file
paperless-export run --export-dir ~/paperless-export
```

The first run performs a full export. Later runs reuse what is already there and
rebuild only what changed.

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
- **Atomic derived view** — `_Steuer/` is built in a confined same-filesystem
  sibling stage and journalled through publication. Missing sources, copy/link
  failures, and interruptions preserve the last complete view.
- **Verifiable** — after a run, `_Steuer/2025/` contains exactly the documents
  tagged `Steuer-2025`; `INDEX.csv` matches a manifest query.
- **Honest failures** — exporter, unavailable infrastructure, unsafe output,
  and incomplete PDF metadata have separate stable exit categories. A missing
  tax-view source is a fatal output error because publishing a partial view is
  forbidden.
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

## Configuration

Configuration is by flag and environment variable; nothing is stored between
runs.

| Setting | Purpose |
|---|---|
| `--exporter-cmd` | Exporter invocation; defaults to Docker Compose in the working directory |
| `--exporter-target` | Export path as seen inside the exporter container |
| `--passphrase-file` | File holding the export passphrase. The value never enters argv or the environment |
| `--copy` | Use verified copies in `_Steuer` instead of symlinks |
| `--no-tax-view` | Skip building `_Steuer` |
| `--log-file` | Bounded rotating logfile path |
| `--exporter-timeout` | Stop a stuck child after 21600 seconds by default |

`--help` is authoritative.

## Troubleshooting

**The export appears to hang.** Child output is streamed and a five-second
heartbeat reaches both the terminal and rotating logfile during silent work.
The configured exporter timeout stops a genuinely stuck child.

**Exit code 4, 5, or 6.** Four means exported output is missing, unsafe, or
cannot be published; five means non-fatal requested post-processing (currently
PDF metadata embedding) is incomplete; six means `document_exporter` itself
failed. All three leave Paperless originals untouched, and tax-view publication
failures retain the previous complete `_Steuer`.

**Symlinks fail on the target.** Use `--copy`; the tax view is then built
from copies.

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

Each successful publication is also recorded as a GitHub Deployment in its
matching protected environment: `github-release`, then `pypi`, then `homebrew`.
The release itself remains the user-facing version; deployments provide channel
history and policy enforcement.

For per-clone paths, commands, or preferences, create an ignored
`CLAUDE.local.md` at the repository root. Do not put credentials or other
secrets in it.

## License

MIT
