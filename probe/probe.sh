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

echo "##### B. explicit Safe Links redirector_pattern (with escaped hash)"
mkcfg 'redirector_pattern m'"'"'^https?://[a-z0-9.-]+\.safelinks\.protection\.outlook\.com/\?(?:[^\#]*?(?<=[?&]))?url=([^&\#]+)'"'"'i'
tail -2 /tmp/ojfcfg/local.cf
for dom in dbltest.com; do
  echo "--- wrapped / $dom (explicit pattern)"; sa probe/wrapped-$dom.eml "" | rules
  echo "--- config problems:"; sa probe/wrapped-$dom.eml "-D config" | grep -i "invalid" | head -3
  echo "--- which pattern matched:"; sa probe/wrapped-$dom.eml "-D uri" | grep -i "parsed uri pattern" | head -5
done
echo "--- explicit pattern alone: disable the shipped one by checking uri lines"
sa tests/fixtures/safelinks-wrapped.eml "-D uri" | grep -E "parsed uri found" | sed 's/&data=.*//' | head -4

echo "##### C. does rewriting links break a DKIM body hash? (dkimpy, synthetic key, no network)"
pip install -q dkimpy 2>&1 | tail -1
openssl genrsa -out /tmp/k.pem 2048 2>/dev/null
openssl rsa -in /tmp/k.pem -pubout -outform DER 2>/dev/null | base64 -w0 > /tmp/pub.b64
python3 - <<'PY'
import dkim, re
plain = open("tests/fixtures/safelinks-plain.eml","rb").read().replace(b"\n", b"\r\n")
wrapped = open("tests/fixtures/safelinks-wrapped.eml","rb").read().replace(b"\n", b"\r\n")
pub = open("/tmp/pub.b64").read().strip()
txt = ("v=DKIM1; k=rsa; p=%s" % pub).encode()
dns = lambda name, timeout=5: txt
key = open("/tmp/k.pem","rb").read()
sig = dkim.sign(plain, b"s1", b"sender-host.invalid", key, include_headers=[b"from", b"to", b"subject", b"date", b"message-id"])
signed_plain = sig + plain
# the same signature, but with the body of the wrapped message (links rewritten after signing)
head, _, body_wrapped = wrapped.partition(b"\r\n\r\n")
_, _, body_plain = plain.partition(b"\r\n\r\n")
signed_wrapped = sig + head + b"\r\n\r\n" + body_wrapped
print("signed, links untouched      ->", dkim.verify(signed_plain, dnsfunc=dns))
print("same signature, links wrapped ->", dkim.verify(signed_wrapped, dnsfunc=dns))
import logging, sys
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG, format="%(message)s")
dkim.verify(signed_wrapped, logger=logging.getLogger("dkim"), dnsfunc=dns)
PY
