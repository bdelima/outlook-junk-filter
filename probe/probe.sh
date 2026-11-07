#!/bin/sh
IMG="$1"
mkcfg() { rm -rf /tmp/ojfcfg; mkdir -p /tmp/ojfcfg; cp config/local.cf.example /tmp/ojfcfg/local.cf; echo "$1" >> /tmp/ojfcfg/local.cf; chmod -R a+rwX /tmp/ojfcfg; }
sa() { # $1 = file path (relative to repo), $2 = extra flags
  docker run --rm --user 1000:1000 -e HOME=/tmp -v "$PWD:/w:ro" -v /tmp/ojfcfg:/config:ro --entrypoint sh "$IMG" -c "spamassassin --nocreate-prefs --siteconfigpath /config $2 -t < /w/$1" 2>&1
}
rules() { grep -E '^ *-?[0-9]+\.[0-9]+ [A-Z0-9_]+' | sed 's/  */ /g'; }
mkdir -p /tmp/fx
for dom in dbltest.com test.surbl.org; do
  sed "s/ojf-synthetic-target.shop/$dom/g" tests/fixtures/safelinks-wrapped.eml > /tmp/fx/wrapped-$dom.eml
  sed "s/ojf-synthetic-target.shop/$dom/g" tests/fixtures/safelinks-plain.eml > /tmp/fx/plain-$dom.eml
done
cp /tmp/fx/*.eml probe/ 2>/dev/null

echo "##### A. default config (no redirector line of ours)"
mkcfg ""
for dom in dbltest.com test.surbl.org; do
  for k in plain wrapped; do echo "--- $k / $dom"; sa probe/$k-$dom.eml "" | rules; done
done

