# Official vLLM 0.29.0 image: provenance and credits

The NVIDIA Nemotron 3.5 Lightning catalog spec uses the upstream vLLM image
directly. It does not reference a Pulsar image overlay. Credit for the serving
runtime and image belongs to the **vLLM Team and contributors**, and to the
authors of its bundled components.

Return to [all third-party notices and image records](../THIRD_PARTY_NOTICES.md).

## Catalog image

- Repository: `docker.io/vllm/vllm-openai`
- Recorded upstream tag: `v0.29.0`
- Catalog manifest index: `sha256:c2914767605584b6d8f45686b82de173ecc99e781897aa3d0a66dacd72c51ae1`
- Linux/arm64 child manifest: `sha256:18372a7224938643461b846fb64c5c9d3d6e9727e82caf2dc3043e620c9d4d7a`
- Linux/arm64 configuration: `sha256:785fd756b4ae1cacda7612cb859dbf227330e8ced9a6fdea42e9379128bebce0`
- Catalog spec: [139908cf23bb…](../releases/139908cf23bbee82400df7eee3ec40e452d96ca223f6577661a7c07f40a6974d.json)

The catalog pins the multi-platform index. The arm64 manifest identifies the
platform image selected for DGX Spark; these are different objects and digests.
The tag is a readable label, while the digests identify the recorded artifacts.

## Upstream source and components

The image configuration records vLLM commit
[`98dff2a81d747d1dba01a47f939f48c3526d4206`](https://github.com/vllm-project/vllm/tree/98dff2a81d747d1dba01a47f939f48c3526d4206)
and [upstream release build 6292](https://buildkite.com/vllm/release-v2/builds/6292).
See the [upstream Dockerfile at that commit](https://github.com/vllm-project/vllm/blob/98dff2a81d747d1dba01a47f939f48c3526d4206/docker/Dockerfile)
for its build stages and component installation.

The configuration and build history identify NVIDIA CUDA 13.0.2 and Ubuntu
24.04 ancestry. Credit also belongs to **NVIDIA**, **Ubuntu and Debian
contributors**, and the authors of bundled Python packages and native libraries.
No Pulsar patch series is recorded for this catalog image.

## Licenses and verification limits

vLLM uses [Apache-2.0](https://github.com/vllm-project/vllm/blob/98dff2a81d747d1dba01a47f939f48c3526d4206/LICENSE).
Other bundled components retain their own licenses and notices, including
NVIDIA's container and CUDA terms. The separately acquired Nemotron weights
retain their model publisher's terms.

On 2026-10-21, registry metadata confirmed the catalog index, its arm64 child,
the configuration identity, and the recorded source/build labels. This check
did not download image layers, inspect packaged license files, rebuild the
image, or repeat model validation. The exact lower NVIDIA base-image manifest
is not established by this record.
