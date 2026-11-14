# Official vLLM 0.32.0 image: provenance and credits

The two NVIDIA Nemotron 3.5 Super VL catalog specs use the upstream vLLM image
directly. They do not reference a Pulsar image overlay. Credit for the serving
runtime and image belongs to the **vLLM Team and contributors**, and to the
authors of its bundled components.

Return to [all third-party notices and image records](../THIRD_PARTY_NOTICES.md).

## Catalog image

- Repository: `docker.io/vllm/vllm-openai`
- Recorded upstream tag: `v0.32.0`
- Catalog manifest index: `sha256:befccf0e7e4089aa23926bb6f887a85c0d46c2a99e6de5c5ffdf5cb7957c5b34`
- Linux/arm64 child manifest: `sha256:6ff5ff0ddf3658d2c2d677dac5ca2d4726ce0156b4371166d918181b210bd4bc`
- Linux/arm64 configuration: `sha256:4ceb6c60eed291c1d179c35674bd8ece7acfd4f9abe8b9be83a6a4dcc3c70ea2`
- Catalog specs: [6c1066bd2007…](../releases/6c1066bd2007917b90c586dc56fba5fe0e4b52ca877b3b7e8c11c266a5cd8d9a.json)
  for one node and [27dc06de6e33…](../releases/27dc06de6e33042b164c8a4a2400cd02ec43e642e7ba8e116b905a5e72dd68b2.json)
  for two nodes

Both specs pin the same multi-platform index. The arm64 manifest identifies the
platform image selected for DGX Spark; these are different objects and digests.
The tag is a readable label, while the digests identify the recorded artifacts.

## Upstream source and components

The image configuration records vLLM commit
[`5df63a7203cdb9328d824a27b682e77c7b9928be`](https://github.com/vllm-project/vllm/tree/5df63a7203cdb9328d824a27b682e77c7b9928be)
and [upstream release build 8622](https://buildkite.com/vllm/release-v2/builds/8622).
The upstream `v0.32.0` Git tag names the same commit.
See the [upstream Dockerfile at that commit](https://github.com/vllm-project/vllm/blob/5df63a7203cdb9328d824a27b682e77c7b9928be/docker/Dockerfile)
for its build stages and component installation.

The configuration and build history identify NVIDIA CUDA 13.0.2 and Ubuntu
24.04 ancestry. Credit also belongs to **NVIDIA**, **Ubuntu and Debian
contributors**, and the authors of bundled Python packages and native libraries.
No Pulsar patch series is recorded for this catalog image.

## Licenses and verification limits

vLLM uses [Apache-2.0](https://github.com/vllm-project/vllm/blob/5df63a7203cdb9328d824a27b682e77c7b9928be/LICENSE).
Other bundled components retain their own licenses and notices, including
NVIDIA's container and CUDA terms. The separately acquired Nemotron weights
retain their model publisher's terms.

On 2026-11-11, registry metadata confirmed the catalog index, its arm64 child,
the configuration identity, and the recorded source/build labels. The registry
tag `v0.32.0` resolved to the catalog index on that date. This check did not
download image layers, inspect packaged license files, rebuild the image, or
repeat model validation. The exact lower NVIDIA base-image manifest is not
established by this record.
