#!/bin/sh
IMG="$1"
rm -rf /tmp/ojfcfg; mkdir -p /tmp/ojfcfg; cp config/local.cf.example /tmp/ojfcfg/local.cf; chmod -R a+rwX /tmp/ojfcfg
sa() { docker run --rm --user 1000:1000 -e HOME=/tmp -v "$PWD:/w:ro" -v /tmp/ojfcfg:/config:ro --entrypoint sh "$IMG" -c "spamassassin --nocreate-prefs --siteconfigpath /config $2 -t < /w/$1" 2>&1; }
python3 - <<'PY'
import email, email.policy, re
for name in ("wrapped", "plain"):
    m = email.message_from_bytes(open(f"tests/fixtures/safelinks-{name}.eml","rb").read(), policy=email.policy.SMTP)
    html = m.get_content().replace("ojf-synthetic-target.shop", "test.surbl.org").replace("ojf-synthetic-target.shop".replace(".","%2E"), "test.surbl.org")
    assert "ojf-synthetic" not in html and "test.surbl.org" in html, name
    m.set_content(html, subtype="html", cte="quoted-printable")
    open(f"probe/{name}-surbl.eml","wb").write(m.as_bytes())
    print(name, "decoded body mentions test.surbl.org:", html.count("test.surbl.org"), "times")
PY
for v in plain wrapped; do
  echo "##### $v / test.surbl.org (3 runs)"
  for i in 1 2 3; do echo "run $i: SURBL rules fired = $(sa probe/$v-surbl.eml '' | grep -c -E '^ *[0-9.]+ URIBL_[A-Z]+_SURBL')"; done
  sa probe/$v-surbl.eml '' | grep -E '^ *[0-9.]+ URIBL_[A-Z]+_SURBL' | sed 's/  */ /g' | cut -c1-70
done
