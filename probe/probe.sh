#!/bin/sh
# Temporary investigation script (not part of the release). Runs inside the published image.
IMG="$1"
run() { docker run --rm --user 1000:1000 -e HOME=/tmp -v "$PWD:/w:ro" --entrypoint sh "$IMG" -c "$1"; }

echo "=== 1. does the shipped ruleset define a redirector pattern for safelinks? ==="
run 'grep -rIn "redirector_pattern" /var/lib/spamassassin /usr/share/spamassassin 2>/dev/null | head -20; echo ---; grep -rIn -i "safelinks" /var/lib/spamassassin /usr/share/spamassassin /usr/share/perl5/Mail 2>/dev/null | head'
echo "=== 2. redirector_pattern docs in this SpamAssassin ==="
run 'spamassassin --version | head -2; f=$(find /usr -name Conf.pm -path "*SpamAssassin*" | head -1); grep -n -B2 -A14 "^=item redirector_pattern" "$f"'
echo "=== 3. which attributes does the HTML parser take URIs from? ==="
run 'f=$(find /usr -name HTML.pm -path "*SpamAssassin*" | head -1); echo $f; grep -n -i "originalsrc" "$f" || echo "originalsrc: not mentioned"; grep -n "href\|%elements_with\|uri_attr\|{uri}" "$f" | head -30'

mkcfg() { rm -rf /tmp/ojfcfg; mkdir -p /tmp/ojfcfg; cp config/local.cf.example /tmp/ojfcfg/local.cf; chmod -R a+rwX /tmp/ojfcfg; }
sa() { # $1 = fixture, $2 = extra flags
  docker run --rm --user 1000:1000 -e HOME=/tmp -v "$PWD:/w:ro" -v /tmp/ojfcfg:/config:ro --entrypoint sh "$IMG" -c "spamassassin --nocreate-prefs --siteconfigpath /config $2 -t < /w/tests/fixtures/$1" 2>&1
}
mkcfg
for f in safelinks-wrapped.eml safelinks-plain.eml; do
  echo "=== 4. URIs SpamAssassin extracts: $f (default config) ==="
  sa $f "-D uri,uridnsbl" | grep -i -E "uri: (parsed|found|cleaned)|domains to query|uridnsbl:.*(lookup|query)|redirect" | sed 's/^.*\] //' | sort -u | head -30
  echo "--- score and rules: $f ---"
  sa $f "" | grep -E "^X-Spam-(Status|Report)|^\*|Content analysis|pts rule" | head -30
done

echo "=== 5. wrapped, with a redirector_pattern for Safe Links ==="
cat >> /tmp/ojfcfg/local.cf <<'CFG'
redirector_pattern /^https?:\/\/[a-z0-9.-]+\.safelinks\.protection\.outlook\.com\/\?(?:[^#]*?&)?url=([^&#]+)/i
CFG
sa safelinks-wrapped.eml "-D uri,uridnsbl" | grep -i -E "uri: (parsed|found|cleaned)|domains to query|redirect" | sed 's/^.*\] //' | sort -u | head -30
sa safelinks-wrapped.eml "" | grep -E "^X-Spam-(Status|Report)|pts rule" | head -20
