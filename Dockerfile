FROM python:3-slim

ARG VERSION=0.0.0
ARG REVISION=unknown

LABEL org.opencontainers.image.title="outlook-junk-filter" \
      org.opencontainers.image.description="Scores the Junk folder of two personal Outlook accounts with SpamAssassin and deletes obvious phishing" \
      org.opencontainers.image.source="https://github.com/bdelima/outlook-junk-filter" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

# SpamAssassin from Debian. The Perl modules are the ones its DNS, SPF and DKIM checks
# need (Debian lists them as recommends, which --no-install-recommends skips); gnupg is
# needed by sa-update to verify the rule updates.
# sa-update: exit 0 = updated, 1 = no update needed; 2 or higher fails the build.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      spamassassin ca-certificates gnupg \
      libnet-dns-perl libmail-spf-perl libmail-dkim-perl libio-socket-ssl-perl \
 && rc=0; sa-update || rc=$?; \
    if [ "$rc" -ge 2 ]; then echo "sa-update failed with $rc" >&2; exit 1; fi \
 && rm -rf /var/lib/apt/lists/*

# No home directory: the job points HOME and TMPDIR under /state.
RUN groupadd --gid 1000 ojf \
 && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin ojf \
 && mkdir -p /state /config \
 && chown 1000:1000 /state

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
COPY LICENSE ATTRIBUTION.md /app/

# The release version, read-only for the operator: label above and APP_VERSION must equal the tag.
ENV APP_VERSION=${VERSION} \
    TZ=UTC \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 1000:1000

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD ["python", "-m", "outlook_junk_filter.healthcheck"]

ENTRYPOINT ["python", "-m", "outlook_junk_filter"]
