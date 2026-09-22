#!/usr/bin/env bash
# Existing installation: acquire from all configured resources, then atomically import editions.
# Uses the project's existing PostgreSQL schema; no generic SQL dumps are executed.
set -Eeuo pipefail
umask 077
cd "$(dirname "$(readlink -f "$0")")"
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  cat <<'HELP'
Usage: sudo bash fill_database.sh [import|discover|download|status|config] [options]
Default: download all approved editions from biblenlp,getbible,helloao, then import.
  --sources biblenlp,getbible,helloao    Select resources and their order
  --languages ru,en,es                 Optional language filter
  --editions biblenlp:russyn,helloao:BSB Exact edition filter
  --profile all-open|core|extended|none
  --refresh                           Refresh cached text files too
Read docs/MULTISOURCE_IMPORT_RU.md. Existing installations must retain their .env.
HELP
  exit 0
fi
[[ -f .env ]] || die 'Run install.sh for a new installation, or copy the EXISTING .env from v1.2.0. Never invent a password for an existing database.'
command -v docker >/dev/null || die 'Docker is missing. Run install.sh first.'
docker compose version >/dev/null
docker compose config --quiet
ACTION=import
if [[ ${1:-} =~ ^(discover|download|import|status|config)$ ]]; then ACTION=$1; shift; fi
# Explicit default covers all approved editions, even when an old .env says "extended".
ARGS=(--profile all-open "$@")
mkdir -p runtime-evidence
RUN="runtime-evidence/corpus-$(date -u +%Y%m%dT%H%M%SZ)-$$"
run_cli() { docker compose run --rm -T --no-deps bootstrap python -m app.catalog.multisource.cli "$@"; }
if [[ "$ACTION" != status ]]; then docker compose build bootstrap; fi
# Validate arguments BEFORE touching running services. This command has no external requests.
run_cli config "${ARGS[@]}" >"${RUN}-config.json"
if [[ "$ACTION" != import ]]; then
  set +e
  run_cli "$ACTION" "${ARGS[@]}" 2> >(tee "${RUN}-${ACTION}.log" >&2) | tee "${RUN}-${ACTION}.json"
  status=${PIPESTATUS[0]}
  set -e
  exit "$status"
fi
printf '\nDownloading and validating first. Running bot services are not stopped in this phase.\n'
set +e
run_cli download "${ARGS[@]}" 2> >(tee "${RUN}-download.log" >&2) | tee "${RUN}-download.json"
download_status=${PIPESTATUS[0]}
set -e
case "$download_status" in
  0) ;;
  2) printf '\nSome sources/editions failed. Importing the validated available data; the report will keep all errors.\n' >&2 ;;
  *) die 'Acquisition failed. Running bot services were not stopped. Inspect download report and retry the same command.' ;;
esac
# The cache has the fresh catalog and completed byte files. Do not refresh those again in maintenance.
IMPORT_ARGS=()
for arg in "${ARGS[@]}"; do [[ "$arg" == --refresh ]] || IMPORT_ARGS+=("$arg"); done
IMPORT_ARGS+=(--cached-catalog)
docker compose up -d postgres
ready=false
for _ in $(seq 1 90); do
  if docker compose exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then ready=true; break; fi
  sleep 2
done
[[ "$ready" == true ]] || die 'PostgreSQL is not ready; no schema changes attempted.'
bash backup.sh
PREVIOUS=()
while IFS= read -r service; do
  case "$service" in bot|worker|admin) PREVIOUS+=("$service");; esac
done < <(docker compose ps --services --status running)
RESUME=false
finish() {
  local status=$?
  trap - EXIT
  if [[ "$RESUME" == true ]]; then
    # Even after an interrupted/partial import, resume only if actual DB rows remain consistent.
    if docker compose run --rm -T --no-deps bootstrap python -m app.cli audit --profile none >"${RUN}-database-audit.json"; then
      if (( ${#PREVIOUS[@]} )); then
        docker compose up -d --no-deps "${PREVIOUS[@]}" || status=1
      fi
    else
      printf '\nDatabase audit failed. Runtime services remain stopped. Inspect %s-database-audit.json and the saved backup.\n' "$RUN" >&2
      status=1
    fi
  fi
  printf '\nReports: %s-*; full itemized receipts: SOURCE_CACHE_DIR/reports inside the source_cache volume.\n' "$RUN"
  exit "$status"
}
trap finish EXIT
RESUME=true
if (( ${#PREVIOUS[@]} )); then docker compose stop "${PREVIOUS[@]}"; fi
printf '\nMaintenance: importing each validated edition in its own PostgreSQL transaction.\n'
set +e
run_cli import "${IMPORT_ARGS[@]}" 2> >(tee "${RUN}-import.log" >&2) | tee "${RUN}-import.json"
status=${PIPESTATUS[0]}
set -e
# Copy the complete receipt outside the container for the operator, without another download.
run_cli status --full-report >"${RUN}-full-report.json" || true
case "$status" in
  0) printf '\nImport run completed. The receipt gives actual counts, not worldwide completeness.\n';;
  2) printf '\nPartial import. Available editions were saved; failed or held editions remain in the report.\n' >&2;;
  *) printf '\nImport failed/interrupted. Completed edition transactions remain; no success is claimed.\n' >&2;;
esac
exit "$status"
