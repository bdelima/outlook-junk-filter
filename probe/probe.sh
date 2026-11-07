#!/bin/sh
IMG="$1"
rm -rf /tmp/ojfcfg; mkdir -p /tmp/ojfcfg; cp config/local.cf.example /tmp/ojfcfg/local.cf; chmod -R a+rwX /tmp/ojfcfg
sa() { docker run --rm --user 1000:1000 -e HOME=/tmp -v "$PWD:/w:ro" -v /tmp/ojfcfg:/config:ro --entrypoint sh "$IMG" -c "spamassassin --nocreate-prefs --siteconfigpath /config $2 -t < /w/$1" 2>&1; }
for f in safelinks-wrapped safelinks-plain; do
  echo "##### URIs (offline, -L): $f"
  sa tests/fixtures/$f.eml "-L -D uri" | grep -E "dbg: uri: (cleaned|parsed uri found)" | sed 's/^.*dbg: //; s/&data=.*//; s/&amp;data=.*//' | sort -u
done
echo "##### body as SpamAssassin parses it: href values"
sa tests/fixtures/safelinks-wrapped.eml "-L -D uri,html" | grep -i -E "href|amp" | head -8
echo "##### 5 repeat runs, online, wrapped test.surbl.org vs plain (flakiness check)"
for dom in test.surbl.org; do
  sed "s/ojf-synthetic-target.shop/$dom/g" tests/fixtures/safelinks-wrapped.eml > probe/w.eml
  sed "s/ojf-synthetic-target.shop/$dom/g" tests/fixtures/safelinks-plain.eml > probe/p.eml
  for i in 1 2 3; do
    echo "wrapped run $i: $(sa probe/w.eml '' | grep -c -E '^ *[0-9.]+ URIBL_[A-Z]+_SURBL')  plain run $i: $(sa probe/p.eml '' | grep -c -E '^ *[0-9.]+ URIBL_[A-Z]+_SURBL')"
  done
done
