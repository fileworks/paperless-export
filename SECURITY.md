# Security policy

Report vulnerabilities privately through GitHub Security Advisories for
`fileworks/paperless-export`. Do not include exported documents, media, or API tokens in
a report.

Security fixes target the latest release on PyPI.

## Threat model

`paperless-export` is a command-line client that talks to **your own** Paperless-ngx server and
writes to your own filesystem. It holds one secret — the API token — which is
read from the environment and is never written to disk, to a log line, or to an
export manifest.

Worth knowing when assessing a report:

- **Server responses are untrusted input.** This tool wraps Paperless's own `document_exporter` and post-processes its output. Titles, correspondents, and tags become path components and are sanitised before use; an escape from that sanitisation — or a path-length failure that truncates into the wrong directory — is a real vulnerability.
- **Export directories are trusted.** The tool writes where you tell it to, and
  a path you passed is a path you meant.

## Out of scope

Anything that requires already holding your API token, and anything that depends
on the upstream server being trusted to behave — that is the server's own
security boundary, not this client's.
