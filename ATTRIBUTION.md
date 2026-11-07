# Attribution

This repository's own code is licensed under the MIT License (see `LICENSE`).
It builds on, uses, or redistributes the third-party projects below. Each
keeps its own license and copyright; nothing here relicenses them.

<!--
RULE: every repo keeps this file current. Any PR that adds a dependency, base
image, downloaded or bundled binary, vendored asset, or derived code updates
this file in the same PR. If an upstream has no stated license, say so here
and say how that was handled.
-->

The Docker image is built from the official `python:3-slim` image (Debian) and installs the Debian packages and
Python packages below. The licenses were read from the sources named in each entry; CI prints the Debian
`copyright` summaries of the installed packages on every run (see `.github/workflows/ci.yml`).

## Python packages (pinned in `requirements.txt`, installed into the image)

- **msal** 1.39.0: https://github.com/AzureAD/microsoft-authentication-library-for-python
  - **License:** MIT (Microsoft Corporation). Read from the package metadata.
  - **How it's used:** library dependency; signs in to the personal Microsoft mailbox (device code flow) and
    refreshes tokens. Installed with `pip`, not modified or vendored.
- **requests** 2.34.2: https://github.com/psf/requests
  - **License:** Apache-2.0 (Kenneth Reitz and contributors). Read from the package metadata.
  - **How it's used:** library dependency; makes the Microsoft Graph HTTPS calls. Installed with `pip`, not modified
    or vendored. Its own dependencies (urllib3, certifi, idna, charset-normalizer) and msal's (PyJWT, cryptography)
    come in with `pip` under their own licenses.

## Debian packages installed in the image

- **Apache SpamAssassin** (Debian package `spamassassin`, 4.0.1): https://spamassassin.apache.org/
  - **License:** Apache-2.0 (Apache Software Foundation). Read from the package's Debian `copyright` file.
  - **How it's used:** the scoring engine. The job runs the `spamassassin` command (not spamd) on each message; its
    rules are fetched with `sa-update` when the image is built. Not modified. The image copies Debian's `.pre`
    plugin-loading files into the directory SpamAssassin reads them from, because the job passes
    `--siteconfigpath /config`.
- **Perl modules SpamAssassin uses for network, SPF and DKIM checks:** `libnet-dns-perl`, `libmail-spf-perl`,
  `libmail-dkim-perl`, `libio-socket-ssl-perl`. Licenses per their Debian `copyright` files: libnet-dns-perl is a mix
  of "Artistic or GPL-1+" and other permissive terms stated in that file; libmail-spf-perl is BSD-3-Clause;
  libmail-dkim-perl and libio-socket-ssl-perl are "Artistic or GPL-1+". Installed from Debian, not modified.
- **gnupg** (GPL-3+ and others, per its Debian `copyright` file) is used only at build time by `sa-update` to verify
  the rule updates. **ca-certificates** (MPL-2.0 for the certificate data, GPL-2+ for the scripts) lets the job
  verify HTTPS to Microsoft. Both are installed from Debian, not modified.

## Base image

- **python:3-slim** (the official Docker image of Python on Debian): https://hub.docker.com/_/python
  - **License:** Python is under the Python Software Foundation License (https://docs.python.org/3/license.html);
    Debian's packages carry their own licenses (see each package's `/usr/share/doc/<package>/copyright` in the
    image). The tag floats, so the image takes the current Python and Debian when it is built.
  - **How it's used:** the base of the image.

## Services

- **Microsoft Graph** and the **Microsoft identity platform** are called as web APIs only. No Microsoft code is
  included beyond the `msal` library above, and the job uses only the delegated `Mail.ReadWrite` permission.

## Trademarks

Microsoft, Outlook, Outlook.com, Live.com and Microsoft Graph are trademarks of Microsoft Corporation. Apache
SpamAssassin and Apache are trademarks of the Apache Software Foundation. Debian is a registered trademark of
Software in the Public Interest, Inc. Python is a trademark of the Python Software Foundation. This is an
unofficial project, not affiliated with or endorsed by any of them.
