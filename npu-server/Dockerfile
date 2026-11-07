# bdelima/ovms-npu: the stock OpenVINO Model Server image with Intel's NPU driver library brought up to date.
#
# Why: the stock image bundles NPU driver 1.35.0. A host whose NPU firmware and kernel driver are 1.38.0 (Intel's
# current release) fails the first inference with a Level Zero "invalid argument" error. Replacing the single library
# libze_intel_npu with the 1.38.0 build fixes it (tested on a Meteor Lake host). Nothing else in the image changes.
#
# Both inputs are pinned: the base image by digest, Intel's release archive by SHA-256 (the digest GitHub publishes
# for the release asset). To move to a newer driver or server, change the three ARGs in a reviewed PR.
ARG BASE=openvino/model_server@sha256:a928903fe429f1e3e27107cc45932d30d95febfd2d1159dc50869c3eac76fc58

FROM ${BASE} AS fetch
USER root
ARG NPU_DRIVER_VERSION=1.38.0
ARG NPU_DRIVER_ARCHIVE=linux-npu-driver-v1.38.0.20260910-34487311128-ubuntu2404.tar.gz
ARG NPU_DRIVER_SHA256=1efcd4b60c22abee751d8f2705962cbcc2a569de45c7e0e670cf08afbfcdc1d2
ADD --checksum=sha256:${NPU_DRIVER_SHA256} \
    https://github.com/intel/linux-npu-driver/releases/download/v${NPU_DRIVER_VERSION}/${NPU_DRIVER_ARCHIVE} /npu.tar.gz
RUN mkdir /debs /unpacked \
 && tar -xf /npu.tar.gz -C /debs \
 && dpkg-deb -x /debs/intel-level-zero-npu_*.deb /unpacked \
 && test -f /unpacked/usr/lib/x86_64-linux-gnu/libze_intel_npu.so.${NPU_DRIVER_VERSION}

FROM ${BASE}
ARG BASE
ARG VERSION=dev
ARG REVISION=unknown
ARG NPU_DRIVER_VERSION=1.38.0
LABEL org.opencontainers.image.title="ovms-npu" \
      org.opencontainers.image.description="OpenVINO Model Server with Intel NPU driver ${NPU_DRIVER_VERSION}" \
      org.opencontainers.image.source="https://github.com/bdelima/outlook-junk-filter" \
      org.opencontainers.image.licenses="Apache-2.0 AND MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.base.name="${BASE}" \
      io.github.bdelima.npu-driver="${NPU_DRIVER_VERSION}"
USER root
COPY --from=fetch /unpacked/usr/lib/x86_64-linux-gnu/libze_intel_npu.so.${NPU_DRIVER_VERSION} /usr/lib/x86_64-linux-gnu/
RUN rm -f /usr/lib/x86_64-linux-gnu/libze_intel_npu.so.1.35.0 \
 && ln -sf libze_intel_npu.so.${NPU_DRIVER_VERSION} /usr/lib/x86_64-linux-gnu/libze_intel_npu.so.1 \
 && ln -sf libze_intel_npu.so.1 /usr/lib/x86_64-linux-gnu/libze_intel_npu.so \
 && ldconfig \
 && ! ldd /usr/lib/x86_64-linux-gnu/libze_intel_npu.so.1 | grep "not found"
COPY LICENSE ATTRIBUTION.md /licenses/
ENV APP_VERSION=${VERSION}
# the base image's user and entrypoint are kept
USER ovms
