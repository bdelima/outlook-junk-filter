#!/bin/sh
IMG="$1"
rm -rf /tmp/ojfcfg; mkdir -p /tmp/ojfcfg; cp config/local.cf.example /tmp/ojfcfg/local.cf; chmod -R a+rwX /tmp/ojfcfg
sa() { docker run --rm --user 1000:1000 -e HOME=/tmp -v "$PWD:/w:ro" -v /tmp/ojfcfg:/config:ro --entrypoint sh "$IMG" -c "spamassassin --nocreate-prefs --siteconfigpath /config $2 -t < /w/$1" 2>&1; }
sed "s/ojf-synthetic-target.shop/test.surbl.org/g" tests/fixtures/safelinks-wrapped.eml > probe/amp.eml
sed "s/&amp;/\&/g" probe/amp.eml > probe/bare.eml
sed "s/ojf-synthetic-target.shop/test.surbl.org/g" tests/fixtures/safelinks-plain.eml > probe/plain.eml
for v in plain bare amp; do
  echo "##### $v: surbl rule count, then uridnsbl debug"
  echo "rules: $(sa probe/$v.eml '' | grep -c -E '^ *[0-9.]+ URIBL_[A-Z]+_SURBL')"
  sa probe/$v.eml "-D uridnsbl,uri" | grep -E "domains to query|uridnsbl: (domain|skip|query)|test.surbl.org\.multi|lookup" | sed 's/^.*dbg: //; s/&data=.*//' | sort -u | head -12
done
