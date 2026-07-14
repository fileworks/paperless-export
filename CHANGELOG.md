# CHANGELOG


## v0.1.0 (2026-07-14)

### Features

- **exporter**: Stream document_exporter's output instead of buffering it
  ([#5](https://github.com/fileworks/paperless-export/pull/5),
  [`a788025`](https://github.com/fileworks/paperless-export/commit/a7880256cc9cfee21024a91de614d8a62e054793))

Exporting thousands of documents takes minutes, and the output was captured and withheld until the
  process exited — so a long, healthy export was indistinguish-able from a hung one. Its lines are
  now relayed as they arrive.

stderr is folded into stdout while doing it, because Paperless reports a failure on either depending
  on the version: the path-too-long fallback was only ever scanning stderr, and would have missed a
  failure announced on stdout. The error message now repeats the useful tail of the log rather than
  the whole thing.


## v0.0.3 (2026-07-13)

### Bug Fixes

- **release**: Attach the sdist and wheel to the GitHub Release
  ([#4](https://github.com/fileworks/paperless-export/pull/4),
  [`5f963ad`](https://github.com/fileworks/paperless-export/commit/5f963adeeb93dfd1133a1e2add8f022a1ab7aa4b))

The semantic-release action only runs 'version' — it bumps, tags, writes the changelog and creates
  the GitHub Release, but it never runs 'publish'. So every Release was created with no files
  attached: a bare tag pointing at PyPI.

Upload the sdist and wheel that semantic-release already built, so the Release stands on its own.
  That matters for a tool whose whole purpose is to be an escape hatch — you should be able to grab
  it from GitHub without going through a package index.

Uses the preinstalled gh CLI rather than another action, so the org's Actions allow-list does not
  need a new entry.


## v0.0.2 (2026-07-13)

### Bug Fixes

- **release**: Drop the duplicate build that broke publishing
  ([#3](https://github.com/fileworks/paperless-export/pull/3),
  [`a51f396`](https://github.com/fileworks/paperless-export/commit/a51f3963604e0777966a317f01d27cc9f1eec1bc))

The release job died at 'Build sdist + wheel' with

PermissionError: [Errno 13] Permission denied: dist/<pkg>-0.0.1.tar.gz

python-semantic-release already builds the sdist and wheel into dist/ via the build_command in
  pyproject.toml. It is a Docker action running as root, so those artefacts are root-owned. The
  workflow then ran a second, redundant 'uv build' as the unprivileged runner user, which cannot
  overwrite them.

Publish to PyPI and the Homebrew bump are both gated on that step, so a permission error on a build
  that never needed to happen silently took out the entire release.

Remove the duplicate build and the setup-uv step that only served it, and let the publish action
  consume the dist/ that semantic-release produced.

- **release**: Give checkout the release token so the version push is allowed
  ([#2](https://github.com/fileworks/paperless-export/pull/2),
  [`e201478`](https://github.com/fileworks/paperless-export/commit/e201478af0f822dbe19163edcfb1431f4427926d))

The release job fails with GH013 — the chore(release) version commit cannot be pushed to protected
  main.

Handing SEMANTIC_RELEASE_TOKEN to the python-semantic-release action is not enough: that input only
  authenticates GitHub API calls. The actual git push uses the credentials actions/checkout persists
  into the remote's extraheader, which were still the default GITHUB_TOKEN — an identity with no
  ruleset bypass.

Set the token on checkout as well, mirroring media-sorter's semantic-release workflow, so the push
  authenticates as an org owner and the always-bypass applies.

### Continuous Integration

- Allow CI to be triggered manually ([#1](https://github.com/fileworks/paperless-export/pull/1),
  [`ff5c192`](https://github.com/fileworks/paperless-export/commit/ff5c192cfcca77171f324f6f200c136a0a9f9879))

* ci: allow CI to be triggered manually

workflow_dispatch makes it possible to re-run checks without pushing a commit — needed while
  verifying the org's Actions allow-list, and useful any time a run fails for reasons outside the
  code.

* fix(release): push the version commit with a token that can bypass the ruleset

semantic-release commits the version bump to main. The branch ruleset requires a PR, and the default
  GITHUB_TOKEN has no bypass — GitHub rejects the Actions app as a bypass actor ("must be part of
  the ruleset source or owner organization"), so the release job could never land its commit.

SEMANTIC_RELEASE_TOKEN is a fine-grained PAT that acts as an org owner, who already holds an
  always-bypass on the ruleset. Falls back to GITHUB_TOKEN so nothing breaks where the secret is
  absent.
