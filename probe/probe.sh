#!/bin/sh
IMG="openvino/model_server@sha256:a928903fe429f1e3e27107cc45932d30d95febfd2d1159dc50869c3eac76fc58"
docker pull -q "$IMG" >/dev/null
echo "=== config"; docker inspect --format 'user={{.Config.User}} entrypoint={{.Config.Entrypoint}} cmd={{.Config.Cmd}} arch={{.Architecture}}' "$IMG"
echo "=== os and tools"; docker run --rm --entrypoint sh "$IMG" -c 'head -2 /etc/os-release; for t in dpkg-deb ar tar xz ldconfig ldd sha256sum find; do printf "%s: " $t; command -v $t || echo MISSING; done; id'
echo "=== npu libs"; docker run --rm --entrypoint sh "$IMG" -c 'ls -l /usr/lib/x86_64-linux-gnu/libze_* /usr/lib/x86_64-linux-gnu/libopenvino_intel_npu* 2>&1; find / -name "libopenvino_intel_npu_compiler*" 2>/dev/null; find / -name "libopenvino.so*" 2>/dev/null | head -3'
echo "=== ovms version"; docker run --rm --entrypoint sh "$IMG" -c '/ovms/bin/ovms --version 2>&1 | head -5'
echo "=== labels"; docker inspect --format '{{json .Config.Labels}}' "$IMG"
