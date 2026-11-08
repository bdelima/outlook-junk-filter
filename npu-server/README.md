# ovms-npu

The stock [OpenVINO Model Server](https://github.com/openvinotoolkit/model_server) image (`openvino/model_server:latest-gpu`)
with one change: Intel's NPU driver library `libze_intel_npu` is replaced with release **1.38.0**.

Why: the stock image bundles driver 1.35.0. On a host whose NPU kernel driver and firmware are 1.38.0, the first
inference fails with `zeCommandListUpdateMutableCommandsExp ... ZE_RESULT_ERROR_INVALID_ARGUMENT`. With the 1.38.0
library in the container it runs (tested on a Meteor Lake host, Debian 13, kernel 6.12, model
`OpenVINO/Qwen3-8B-int4-cw-ov`). If your host runs a different driver version, this image is probably the wrong fix:
the container driver should match the host's NPU firmware.

It serves an OpenAI-style chat API (`/v3/chat/completions`) on the NPU. outlook-junk-filter can use it to rate
messages; it is also a general local language-model server.

## Use

The image is published as `bdelima/ovms-npu:<version>` (amd64). Pin a version tag.

1. Make a models folder owned by the user the container will run as, and download and register a model once
   (about 5 GB; use `sudo docker` if your user needs it):

   ```bash
   mkdir -p models
   docker run --rm -u $(id -u):$(id -g) -v $(pwd)/models:/models:rw bdelima/ovms-npu:1.0.0 --pull --source_model OpenVINO/Qwen3-8B-int4-cw-ov --model_repository_path /models --target_device NPU --task text_generation --cache_dir /models/.ov_cache --max_prompt_len 2000
   docker run --rm -u $(id -u):$(id -g) -v $(pwd)/models:/models:rw bdelima/ovms-npu:1.0.0 --add_to_config --config_path /models/config.json --model_name OpenVINO/Qwen3-8B-int4-cw-ov --model_path /models/OpenVINO/Qwen3-8B-int4-cw-ov
   ```

2. Run it with `docker-compose.example.yml`: set `PUID` and `PGID` to the user that owns the models folder, `MODELS_DIR` to that
   folder, and `RENDER_GID` to the output of `stat -c %g /dev/dri/render*`.

3. Test:

   ```bash
   curl -s http://localhost:8000/v3/chat/completions -H "Content-Type: application/json" -d '{"model":"OpenVINO/Qwen3-8B-int4-cw-ov","max_tokens":60,"chat_template_kwargs":{"enable_thinking":false},"messages":[{"role":"user","content":"Say hello"}]}'
   ```

The first request after a start compiles the model for the NPU and is slow; later ones are not. The server answers one
request at a time. There is no authentication: keep the port on a trusted network.

## Build and release

Everything is pinned: the base image by digest and Intel's release archive by SHA-256 (the `ARG`s at the top of the
`Dockerfile`). Changing the driver or server means editing those in a reviewed PR. CI (`npu-server-ci.yml`) builds and
tests the image, with no NPU needed; a merged bump of `npu-server/VERSION` triggers `npu-server-publish.yml`, which
waits for approval in the `release` Environment before pushing to Docker Hub and creating a GitHub Release tagged
`ovms-npu-<version>`.

## Licenses

See the repository `ATTRIBUTION.md` (copied into the image under `/licenses`). The model is downloaded by you at
setup time and is not part of the image.
