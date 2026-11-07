#!/bin/sh
IMG="$1"
echo "=== spamassassin --help: cf option"
docker run --rm --entrypoint sh "$IMG" -c 'spamassassin --help 2>&1 | grep -i -B1 -A2 "\-\-cf"'
mkdir -p /tmp/cfg; cp config/local.cf.example /tmp/cfg/local.cf; printf 'skip_rbl_checks 1\ndns_available no\n' >> /tmp/cfg/local.cf; chmod -R a+rwX /tmp/cfg
run() { docker run --rm --user 1000:1000 -e HOME=/tmp -v "$PWD:/w:ro" -v /tmp/cfg:/config:ro --entrypoint sh "$IMG" -c "$1" 2>&1; }
echo "=== M1: --cf include, ed-spam"
run "spamassassin --nocreate-prefs --siteconfigpath /config --cf='include /w/rules/ojf-rules.cf' -t < /w/tests/fixtures/ed-spam.eml" | grep -E "^X-Spam-Status|OJF_ED|lint|error|failed" | head -5
echo "=== M1: --cf include, health newsletter (must NOT fire)"
run "spamassassin --nocreate-prefs --siteconfigpath /config --cf='include /w/rules/ojf-rules.cf' -t < /w/tests/fixtures/health-newsletter.eml" | grep -E "^X-Spam-Status|OJF_ED" | head -3
echo "=== M1: lint with --cf include"
run "spamassassin --lint --nocreate-prefs --siteconfigpath /config --cf='include /w/rules/ojf-rules.cf'; echo lint-exit=\$?"
echo "=== M2: file placed in the sys rules dir (as root, scratch copy)"
docker run --rm --user 0 -e HOME=/tmp -v "$PWD:/w:ro" -v /tmp/cfg:/config:ro --entrypoint sh "$IMG" -c 'ls -d /var/lib/spamassassin/[0-9]*; for d in /var/lib/spamassassin/[0-9]* /usr/share/spamassassin; do cp /w/rules/ojf-rules.cf $d/99_ojf.cf; done; spamassassin --nocreate-prefs --siteconfigpath /config -t < /w/tests/fixtures/ed-spam.eml | grep -E "^X-Spam-Status|OJF_ED"' 2>&1 | head -6
