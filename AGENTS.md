# Repository instructions

- The tool wraps `document_exporter`; never reimplement what it already does.
- Sanitize every string that reaches a log, a report, or the terminal — the
  passphrase and token must not appear in any of them.
- Publish output only via a same-directory temporary, fsync, and atomic
  replace inside the export boundary.
- Keep exit codes on the shared `ExitCode` vocabulary (see README).
- Keep parser boundaries typed and keep strict mypy enabled globally.
- Use Conventional Commits; do not add automated co-author trailers.
