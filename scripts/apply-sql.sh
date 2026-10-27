#!/bin/sh
# Apply a service's SQL in order: 001_schema, 002_seed, then migrations/*.sql. 003_queries is never applied.
# Used by the *-db-init containers and by `make db`. Reads PGHOST, PGUSER, PGPASSWORD, PGDATABASE and APP_PASSWORD.
set -eu
dir="${1:?usage: apply-sql.sh <sql-dir>}"
run() {
  echo "applying $1"
  psql --no-psqlrc -v ON_ERROR_STOP=1 -v app_password="${APP_PASSWORD:?}" -q -f "$1"
}
for file in "$dir"/00[0-9]_*.sql; do
  case "$file" in *_queries.sql) continue ;; esac
  run "$file"
done
for file in "$dir"/migrations/*.sql; do
  [ -e "$file" ] || continue
  run "$file"
done
