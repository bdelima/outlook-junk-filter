#!/usr/bin/env bash
# Image tests for ovms-npu, run against the built image. No NPU is needed (CI runners have none), so this checks the
# image contents, not inference:
#   docker build -f npu-server/Dockerfile --build-arg VERSION="$(cat npu-server/VERSION)" -t ovms-npu-test .
#   npu-server/test.sh ovms-npu-test
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=${1:?usage: test.sh IMAGE}
want=$(tr -d '[:space:]' < npu-server/VERSION)
fails=0
ok() { echo "   ok   $1"; }
fail() { echo "FAIL: $1" >&2; fails=1; }
sh_in() { docker run --rm --entrypoint sh "$IMAGE" -c "$1" 2>&1; }

label=$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$IMAGE")
app=$(docker run --rm --entrypoint printenv "$IMAGE" APP_VERSION)
[ "$label" = "$want" ] && [ "$app" = "$want" ] && ok "version label and APP_VERSION equal npu-server/VERSION ($want)" || fail "label=$label APP_VERSION=$app VERSION=$want"
drv=$(docker inspect --format '{{ index .Config.Labels "io.github.bdelima.npu-driver" }}' "$IMAGE")
[ "$drv" = "1.38.0" ] && ok "driver label is 1.38.0" || fail "driver label: $drv"

libs=$(sh_in 'ls /usr/lib/x86_64-linux-gnu/ | grep "^libze_intel_npu"' | tr '\n' ' ')
[ "$libs" = "libze_intel_npu.so libze_intel_npu.so.1 libze_intel_npu.so.1.38.0 " ] && ok "only the 1.38.0 NPU driver library is present, with its symlinks" || fail "NPU libs: $libs"
target=$(sh_in 'readlink -f /usr/lib/x86_64-linux-gnu/libze_intel_npu.so')
[ "$target" = "/usr/lib/x86_64-linux-gnu/libze_intel_npu.so.1.38.0" ] && ok "libze_intel_npu.so resolves to 1.38.0" || fail "symlink resolves to $target"
missing=$(sh_in 'ldd /usr/lib/x86_64-linux-gnu/libze_intel_npu.so.1 | grep "not found" || true')
[ -z "$missing" ] && ok "the driver library has no missing dependencies" || fail "missing libs: $missing"

ver=$(sh_in '/ovms/bin/ovms --version | head -1')
echo "$ver" | grep -q "OpenVINO Model Server" && ok "the server runs ($ver)" || fail "ovms --version: $ver"
ids=$(sh_in 'id -un')
[ "$ids" = "ovms" ] && ok "default user is still ovms" || fail "default user: $ids"
sh_in 'test -f /licenses/LICENSE && test -f /licenses/ATTRIBUTION.md' >/dev/null && ok "LICENSE and ATTRIBUTION.md are bundled in /licenses" || fail "licenses missing"
sh_in 'ls /usr/lib/x86_64-linux-gnu/libopenvino_intel_npu_compiler.so /ovms/lib/libopenvino_intel_npu_compiler.so' >/dev/null && ok "the base image's own NPU compiler is untouched" || fail "NPU compiler libraries missing"
exit $fails
