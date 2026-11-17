#!/usr/bin/env bash
# Deploy an attested main commit to the native systemd installation.
set -Eeuo pipefail
umask 077
base=/opt/bible-messenger-bot
config=/etc/bible-messenger-bot
runtime=/usr/local/libexec/bible-systemd-ops
units=(admin max-web bot maxbot worker composer illustrator speaker)
[[ $EUID == 0 && $# -ge 2 && "$2" =~ ^[0-9a-f]{40}$ ]] || exit 64
target=$2

if [[ $1 == submit && $# == 2 ]]; then
    bundle=$(mktemp "$base/.panel.XXXXXXXX")
    if ! timeout 90 head -c 33554433 > "$bundle" || [[ $(stat -c %s "$bundle") -gt 33554432 || ! -s $bundle ]]; then
        rm -f -- "$bundle"
        echo 'Expected a panel archive of at most 32 MiB on stdin.' >&2
        exit 65
    fi
    # The deployment continues independently when SSH is disconnected.
    unit="bible-deploy-${target:0:12}-$$"
    result=0
    systemd-run --quiet --wait --collect --unit="$unit" \
        -p RuntimeMaxSec=4500 -p MemoryHigh=384M -p MemoryMax=640M -p CPUQuota=100% \
        /usr/local/sbin/bible-auto-deploy run "$target" "$bundle" || result=$?
    journalctl -u "$unit" --no-pager -o cat | sed -nE '/^(BIBLE_DEPLOY_(NOTICE|REPORT)=|DEPLOYED:|SKIPPED:)/p'
    exit "$result"
fi

[[ $1 == run && $# == 3 && "$3" =~ ^/opt/bible-messenger-bot/\.panel\.[A-Za-z0-9]{8}$ && -f $3 && ! -L $3 ]] || exit 64
bundle=$3
trap 'rm -f -- "$bundle"' EXIT
exec 9>/run/lock/bible-auto-deploy.lock
flock -w 3600 9
old=$(readlink -e "$base/current")
[[ $old =~ ^/opt/bible-messenger-bot/releases/[0-9a-f]{40}$ ]]
old_environment=$(readlink -e "$old/.venv")
previous=$(git -C "$old" rev-parse HEAD)
release="$base/releases/$target"
state="$base/runtime-evidence/auto-deploy/$(date -u +%Y%m%dT%H%M%SZ)-${target:0:12}-$$"
public="$base/runtime-evidence/deploy-status"
mkdir -p "$state" "$public"
exec > >(tee -a "$state/deploy.log") 2>&1

stage=checking
reason=preflight_failed
service=''
outcome=''
activated=false
schema_before=''
panel=''
venv_tmp=''

status() {
    python3 /usr/local/libexec/bible-deploy-status "$public" "$state" "$target" "$previous" "$@"
}

switch_release() {
    ln -s "$1" "$base/.current.$$"
    mv -Tf "$base/.current.$$" "$base/current"
}

run_app() {
    local directory=$1
    shift
    systemd-run --quiet --wait --pipe --collect --uid=biblemessenger \
        -p "WorkingDirectory=$directory" -p "EnvironmentFile=$config/runtime.env" \
        -p MemoryHigh=256M -p MemoryMax=384M -p RuntimeMaxSec=1500 "$@"
}

fingerprint() {
    run_app "$old" /usr/bin/python3 "$runtime" fingerprint
}

check_unit() {
    local unit=bible-messenger-$1 pid
    for _ in {1..10}; do
        if systemctl is-active --quiet "$unit" &&
            pid=$(systemctl show "$unit" -p MainPID --value) &&
            [[ $pid -gt 0 && $(readlink -e "/proc/$pid/cwd") == "$2" ]]; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

web_ready() {
    local url=$1
    for _ in {1..20}; do
        if curl -fsS --max-time 2 -o /dev/null "$url"; then return 0; fi
        sleep 1
    done
    return 1
}

finish() {
    local code=$? failed_stage=$stage failed_service=$service restored=true current_schema
    trap - EXIT TERM INT
    rm -f -- "$bundle" "$base/.current.$$"
    if [[ -n $panel && -d $panel ]]; then rm -rf -- "$panel"; fi
    if [[ -n $venv_tmp && -d $venv_tmp ]]; then rm -rf -- "$venv_tmp"; fi
    if (( code != 0 )); then
        outcome=failed
        if [[ $release == "$old" ]]; then
            ln -sfn "$old_environment" "$old/.venv" || restored=false
        fi
        if [[ $activated == true ]]; then
            if [[ -n $schema_before ]] && current_schema=$(fingerprint) && [[ $current_schema == "$schema_before" ]]; then
                stage=rollback
                status running "$stage" || true
                switch_release "$old" || restored=false
                for item in "${units[@]}"; do
                    systemctl restart "bible-messenger-$item" && check_unit "$item" "$old" || restored=false
                done
                if [[ $restored == true ]]; then outcome=rolled_back; fi
            else
                stage=stopped
                outcome=manual_intervention
                systemctl stop "${units[@]/#/bible-messenger-}" || true
                echo 'Database schema changed or cannot be verified. Application services stopped; backup retained.' >&2
            fi
        fi
        status "$outcome" "$stage" "$reason" "$failed_stage" "$code" "$failed_service" || true
    elif [[ $outcome == skipped ]]; then
        status skipped skipped outdated_commit checking 0 '' || code=1
    else
        status succeeded completed || code=1
        if (( code == 0 )); then
            printf 'DEPLOYED: %s; all systemd services verified.\n' "$target"
        fi
    fi
    printf '%s\n' "$code" > "$state/exit-code"
    python3 /usr/local/libexec/bible-deploy-notify "$old" "$state" --systemd \
        || echo 'BIBLE_DEPLOY_NOTICE=unavailable'
    exit "$code"
}
trap finish EXIT
trap 'reason=interrupted; exit 143' TERM
trap 'reason=interrupted; exit 130' INT
status running checking
echo "Deploy $previous -> $target"
reason=configuration_missing
test -r "$config/runtime.env"
test -r "$config/messengers.env"
reason=disk_space
[[ $(df -Pk "$base" | awk 'NR==2 {print $4}') -ge 4194304 ]]
reason=fetch_failed
repo="$base/source.git"
if [[ ! -d $repo ]]; then
    git init --quiet --bare "$repo"
fi
git -C "$repo" fetch --quiet --no-tags https://github.com/Xsenus/BibleMessengerBot.git +refs/heads/main:refs/heads/main
if [[ $(git -C "$repo" rev-parse refs/heads/main) != "$target" ]]; then
    echo 'SKIPPED: checked commit is no longer the head of main.'
    outcome=skipped
    exit 0
fi
reason=ancestry_failed
git -C "$repo" merge-base --is-ancestor "$previous" "$target"
reason=activation_failed
if [[ ! -d $release ]]; then
    (umask 022; git -C "$repo" worktree add --quiet --detach "$release" "$target")
fi
[[ $(git -C "$release" rev-parse HEAD) == "$target" ]]
[[ -z $(git -C "$release" status --porcelain -- . ':(exclude).venv') ]]

panel=$(mktemp -d "$base/.static.XXXXXXXX")
python3 "$runtime" panel "$bundle" "$panel"
if [[ ! -s $release/app/web/static/panel/index.html ]]; then
    mv -T "$panel" "$release/app/web/static/panel"
    panel=''
fi

# Use immutable environments per dependency set. Existing units resolve the physical release at startup.
dependency_hash=$(cd "$release" && sha256sum requirements.txt constraints-linux.txt | sha256sum | cut -d ' ' -f1)
environment="$base/environments/$dependency_hash"
install -d -m 755 "$base/environments"
if [[ ! -f $environment/.installed ]]; then
    if [[ -d $environment ]]; then rm -rf -- "$environment"; fi
    venv_tmp=$environment
    install -d -m 755 -o biblemessenger "$environment"
    (
        umask 022
        runuser -u biblemessenger -- /usr/bin/python3.12 -m venv "$environment"
        # docopt is pure Python and publishes only a source distribution.
        runuser -u biblemessenger -- "$environment/bin/python" -m pip --isolated --disable-pip-version-check \
            install --no-cache-dir --only-binary=:all: --no-binary=docopt \
            -c "$release/constraints-linux.txt" -r "$release/requirements.txt"
        runuser -u biblemessenger -- "$environment/bin/python" -m pip --isolated --disable-pip-version-check check
    )
    chown -hR root:root "$environment"
    touch "$environment/.installed"
    venv_tmp=''
fi
if [[ -e $release/.venv && ! -L $release/.venv ]]; then exit 1; fi
ln -sfn "$environment" "$release/.venv"
reason=migration_check_failed
schema_before=$(fingerprint)
check=0
run_app "$release" "$release/.venv/bin/python" -m app.cli migrate --check || check=$?
[[ $check == 0 || $check == 3 ]]
if [[ $check == 3 ]]; then
    stage=backup
    reason=backup_failed
    status running "$stage"
    run_app "$old" /usr/bin/python3 "$runtime" backup > "$state/pre-migration.dump"
    pg_restore --file=/dev/null "$state/pre-migration.dump"
    stage=updating
    reason=migration_check_failed
    status running "$stage"
    activated=true
    systemctl stop "${units[@]/#/bible-messenger-}"
    run_app "$release" "$release/.venv/bin/python" -m app.cli migrate
fi
stage=updating
reason=activation_failed
status running "$stage"
activated=true
switch_release "$release"
for service in "${units[@]}"; do
    systemctl restart "bible-messenger-$service"
    check_unit "$service" "$release"
done

stage=verifying
reason=service_unhealthy
service=''
status running "$stage"
web_ready http://127.0.0.1:8420/ready
web_ready http://127.0.0.1:8420/panel/
web_ready http://127.0.0.1:8421/ready
systemd-run --quiet --wait --pipe --collect --uid=biblemessenger \
    -p "WorkingDirectory=$release" -p "EnvironmentFile=$config/runtime.env" \
    -p "EnvironmentFile=$config/messengers.env" -p RuntimeMaxSec=60 \
    -p "Environment=PYTHONPATH=$release" \
    -p MemoryMax=256M "$release/.venv/bin/python" "$runtime" apis
for service in "${units[@]}"; do
    check_unit "$service" "$release"
    printf '{"name":"%s","health":"healthy","commit":"%s"}\n' "$service" "$target" >> "$state/checked-services.jsonl"
done
service=''
reason=migration_check_failed
run_app "$release" "$release/.venv/bin/python" -m app.cli migrate --check
