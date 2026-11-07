#!/usr/bin/env bash
# Image tests, run against the built image with no real mail and no Microsoft account:
#   docker build --build-arg VERSION="$(cat VERSION)" -t ojf-test .
#   tests/image/test-image.sh ojf-test
# Every container runs with a read-only root filesystem and only /state writable.
set -euo pipefail
cd "$(dirname "$0")/../.."

IMAGE=${1:?usage: test-image.sh IMAGE}
want=$(tr -d '[:space:]' < VERSION)

work=$(mktemp -d)
# the containers create files as other uids, so clean up from inside a container
trap 'docker run --rm -v "$work:/w" --entrypoint sh "$IMAGE" -c "rm -rf /w/*" >/dev/null 2>&1 || true; rm -rf "$work" 2>/dev/null || true' EXIT
state="$work/state"
state_other="$work/state-other"
mkdir "$state" "$state_other"
chmod 777 "$state" "$state_other" # any non-root uid must be able to write here, like the real bind mount
cfg_example="$work/cfg-example"
cfg_offline="$work/cfg-offline"
mkdir "$cfg_example" "$cfg_offline"
cp config/local.cf.example "$cfg_example/local.cf"
# same rules, but no DNS: the scoring test must not depend on the network
cp config/local.cf.example "$cfg_offline/local.cf"
printf 'skip_rbl_checks 1\ndns_available no\n' >> "$cfg_offline/local.cf"

fails=0
ok() { echo "   ok   $1"; }
fail() { echo "FAIL: $1" >&2; fails=1; }

selfcheck() { # config dir, user, state dir, then the selfcheck arguments
  local cfg=$1 user=$2 st=$3
  shift 3
  docker run --rm --read-only --user "$user" \
    -v "$st:/state" -v "$cfg:/config:ro" -v "$PWD/tests/fixtures:/fixtures:ro" \
    --entrypoint python "$IMAGE" -m outlook_junk_filter.selfcheck "$@"
}

# 1. version label and APP_VERSION equal the repo's VERSION
label=$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$IMAGE")
app_version=$(docker run --rm --entrypoint printenv "$IMAGE" APP_VERSION)
if [ "$label" = "$want" ] && [ "$app_version" = "$want" ]; then ok "version label and APP_VERSION equal VERSION ($want)"; else fail "label=$label APP_VERSION=$app_version VERSION=$want"; fi
src=$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.source" }}' "$IMAGE")
[ "$src" = "https://github.com/bdelima/outlook-junk-filter" ] && ok "source label" || fail "source label: $src"

# 2. the image runs as uid/gid 1000 by default, with LICENSE and ATTRIBUTION.md bundled
ids=$(docker run --rm --entrypoint sh "$IMAGE" -c 'id -u; id -g; test -f /app/LICENSE && test -f /app/ATTRIBUTION.md && echo bundled' | tr '\n' ' ')
[ "$ids" = "1000 1000 bundled " ] && ok "runs as 1000:1000 and bundles LICENSE and ATTRIBUTION.md" || fail "id/bundle check: $ids"

# 3. read-only root filesystem: only /state is writable
if docker run --rm --read-only --user 1000:1000 -v "$state:/state" --entrypoint sh "$IMAGE" -c 'touch /state/probe && ! touch /app/probe 2>/dev/null && ! touch /tmp/probe 2>/dev/null'; then
  ok "read-only root filesystem, /state writable"
else
  fail "read-only root filesystem check"
fi

# 4. the example config loads cleanly as uid 1000, /config read-only
if out=$(selfcheck "$cfg_example" 1000:1000 "$state" lint 2>&1); then ok "example config lints cleanly as uid 1000 ($out)"; else fail "example config lint: $out"; fi

# 5. it also works as another non-root uid
if out=$(selfcheck "$cfg_offline" 4242:4242 "$state_other" score /fixtures/newsletter.eml 2>&1); then ok "scores as an arbitrary non-root uid"; else fail "uid 4242: $out"; fi

# 6. a synthetic phishing-style fixture scores higher than a synthetic newsletter fixture
phish=$(selfcheck "$cfg_offline" 1000:1000 "$state" score /fixtures/phishing.eml 2>&1) || { fail "phishing score: $phish"; phish=""; }
news=$(selfcheck "$cfg_offline" 1000:1000 "$state" score /fixtures/newsletter.eml 2>&1) || { fail "newsletter score: $news"; news=""; }
echo "      phishing:   $phish"
echo "      newsletter: $news"
if [ -n "$phish" ] && [ -n "$news" ]; then
  p=${phish#score=}; p=${p%% *}
  n=${news#score=}; n=${n%% *}
  if awk -v p="$p" -v n="$n" 'BEGIN { exit !(p > n) }'; then ok "phishing fixture ($p) scores higher than the newsletter fixture ($n)"; else fail "phishing $p is not higher than newsletter $n"; fi
fi

[ "$fails" = 0 ] && echo "PASS: image tests" || exit 1
