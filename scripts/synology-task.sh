#!/bin/sh
# POSIX/BusyBox-compatible entry point for Synology DSM Task Scheduler.
set -eu
umask 077

: "${PAPERLESS_COMPOSE_DIR:=/volume1/docker/paperless}"
: "${PAPERLESS_EXPORT_DIR:=/volume1/paperless/export}"
: "${PAPERLESS_EXPORT_BIN:=/usr/local/bin/paperless-export}"

case "$PAPERLESS_COMPOSE_DIR" in
  /*) ;;
  *) echo "PAPERLESS_COMPOSE_DIR must be an absolute path" >&2; exit 2 ;;
esac
case "$PAPERLESS_EXPORT_DIR" in
  /*) ;;
  *) echo "PAPERLESS_EXPORT_DIR must be an absolute path" >&2; exit 2 ;;
esac

if [ ! -d "$PAPERLESS_COMPOSE_DIR" ]; then
  echo "Paperless Compose directory does not exist: $PAPERLESS_COMPOSE_DIR" >&2
  exit 3
fi
if [ ! -d "$PAPERLESS_EXPORT_DIR" ] || [ ! -w "$PAPERLESS_EXPORT_DIR" ]; then
  echo "Export directory must exist and be writable: $PAPERLESS_EXPORT_DIR" >&2
  exit 4
fi
if [ ! -x "$PAPERLESS_EXPORT_BIN" ]; then
  echo "paperless-export is not executable: $PAPERLESS_EXPORT_BIN" >&2
  exit 3
fi

cd "$PAPERLESS_COMPOSE_DIR"
exec "$PAPERLESS_EXPORT_BIN" run --export-dir "$PAPERLESS_EXPORT_DIR"
