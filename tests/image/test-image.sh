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
state_ui="$work/state-ui"
ui_name="ojf-ui-test-$$"
# the containers create files as other uids, so clean up from inside a container
trap 'docker rm -f "$ui_name" >/dev/null 2>&1 || true; docker run --rm -v "$work:/w" --entrypoint sh "$IMAGE" -c "rm -rf /w/*" >/dev/null 2>&1 || true; rm -rf "$work" 2>/dev/null || true' EXIT
state="$work/state"
state_other="$work/state-other"
mkdir "$state" "$state_other" "$state_ui" "$state_ui/logs"
chmod 777 "$state" "$state_other" "$state_ui" "$state_ui/logs" # any non-root uid must be able to write here, like the real bind mount
cfg_example="$work/cfg-example"
cfg_offline="$work/cfg-offline"
mkdir "$cfg_example" "$cfg_offline"
chmod 777 "$cfg_example" "$cfg_offline" # /config is writable too (the review page adds allowlist entries)
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
    -v "$st:/state" -v "$cfg:/config" -v "$PWD/tests/fixtures:/fixtures:ro" \
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

# 3. read-only root filesystem: only /state and /config are writable
if docker run --rm --read-only --user 1000:1000 -v "$state:/state" -v "$cfg_example:/config" --entrypoint sh "$IMAGE" -c 'touch /state/probe && touch /config/probe && ! touch /app/probe 2>/dev/null && ! touch /tmp/probe 2>/dev/null'; then
  ok "read-only root filesystem, /state and /config writable"
else
  fail "read-only root filesystem check"
fi

# 3b. the job itself imports on the image's Python (msal, requests), and a missing client id exits with a message
if docker run --rm --read-only --user 1000:1000 -v "$state:/state" --entrypoint python "$IMAGE" -c 'import outlook_junk_filter.__main__, msal, requests; print("imports ok")' >/dev/null 2>&1; then ok "the job and its dependencies import"; else fail "the job does not import on this image"; fi
set +e
msg=$(docker run --rm --read-only --user 1000:1000 -v "$state:/state" "$IMAGE" 2>&1); code=$?
set -e
if [ "$code" = 2 ] && echo "$msg" | grep -q OJF_CLIENT_ID; then ok "exits with a clear message when OJF_CLIENT_ID is missing"; else fail "missing client id: exit $code: $msg"; fi

# 4. the example config loads cleanly as uid 1000 (only /state and /config writable)
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

# 7. sa-learn teaches Bayes into /state/bayes (what the review page's "teach" actions run), as uid 1000 on a read-only root
nspam=$(docker run --rm --read-only --user 1000:1000 -v "$state:/state" -v "$cfg_offline:/config" -v "$PWD/tests/fixtures:/fixtures:ro"   -e HOME=/state/home -e TMPDIR=/state/tmp --entrypoint sh "$IMAGE" -c   'mkdir -p /state/home /state/tmp /state/bayes && sa-learn --spam --single --siteconfigpath /config < /fixtures/phishing.eml >/dev/null 2>&1 && sa-learn --dump magic --siteconfigpath /config 2>/dev/null | awk "/non-token data: nspam/ {print \$3}"' 2>&1) || true
if [ "${nspam:-0}" -ge 1 ] 2>/dev/null; then ok "sa-learn teaches Bayes ($nspam spam message learned)"; else fail "sa-learn did not learn: nspam='$nspam'"; fi

# 8. the review page starts as uid 1000 on a read-only root filesystem and serves the Decisions page from a fixture log;
#    a hostile subject comes back escaped, and the page sends a Content-Security-Policy
cat > "$state_ui/logs/decisions.jsonl" <<'JSON'
{"ts":"2026-11-06T10:00:00Z","message_id":"M1","internet_message_id":"<1@x.invalid>","received":"2026-11-06T09:00:00Z","from_addr":"evil@phish.invalid","subject":"<script>alert(1)</script>","score":12.5,"rules":["A","B"],"keep_rule_hit":false,"relays_untrusted":"","first_untrusted_ip":"","auth_results_present":true,"mode":"report-only","decision":"would-delete","reason":"score at or above the delete threshold (report-only)","action_error":"","current_message_id":"M1","folder":"junkemail"}
JSON
chmod 666 "$state_ui/logs/decisions.jsonl"
docker run -d --name "$ui_name" --read-only --user 1000:1000 -p 127.0.0.1:60888:60888 -v "$state_ui:/state"   --entrypoint python "$IMAGE" -m outlook_junk_filter.ui >/dev/null
page=""
for _ in $(seq 1 30); do
  page=$(curl -fsS -D "$work/headers" http://127.0.0.1:60888/ 2>/dev/null) && break
  sleep 1
done
if [ -n "$page" ] && echo "$page" | grep -q '&lt;script&gt;alert(1)&lt;/script&gt;' && ! echo "$page" | grep -q '<script'    && grep -qi "content-security-policy: default-src 'none'" "$work/headers"; then
  ok "the review page serves the Decisions page, escaped, with a CSP, as uid 1000 on a read-only root"
else
  fail "review page: $(docker logs "$ui_name" 2>&1 | tail -5)"
fi
docker rm -f "$ui_name" >/dev/null 2>&1 || true

[ "$fails" = 0 ] && echo "PASS: image tests" || exit 1
