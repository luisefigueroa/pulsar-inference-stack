# Official vLLM nightly image of commit 647a7686: provenance and credits

The two Inkling-2-Mini catalog specs use an upstream vLLM nightly image
directly. They do not reference a Pulsar image overlay. Credit for the serving
runtime and image belongs to the **vLLM Team and contributors**, and to the
authors of its bundled components.

Return to [all third-party notices and image records](../THIRD_PARTY_NOTICES.md).

## Catalog image

- Repository: `docker.io/vllm/vllm-openai`
- Recorded upstream tag: `nightly-647a76866b4559a74e27d1c6b55fbffe85c07ce4`
- Catalog manifest index: `sha256:7a46a81f0539b4a1b407fe2c29abbe0b130eeee4e4597f4ed357863af9e36086`
- Linux/arm64 child manifest: `sha256:135d65d5db70272271d40f4bd438f10b6fe2eb78a7df3d2c80b947243f33c804`
- Linux/arm64 configuration: `sha256:3080ce287065a27e557f9f8b91c43c63f4cd7ca1a29b380bec13fc3cf336aca8`
- Catalog specs: [369d30ce8d6b…](../releases/369d30ce8d6ba5f5ab5f0503c4db423f8cb8ccc28c6950546cb6d15a6c27b0a5.json)
  for a 262,144-token context and [3f30050631c3…](../releases/3f30050631c3b605cef34763f8def7a5e79a64ea7e9f047eaaeff71ed4873857.json)
  for a 1,048,576-token context, both on one node

Both specs pin the same multi-platform index. The arm64 manifest identifies the
platform image selected for DGX Spark; these are different objects and digests.
The tag is a readable label, while the digests identify the recorded artifacts.

This is a nightly build, not a release. Upstream can remove nightly tags and
their images. No Pulsar copy of this image exists, so the two specs can be
started only while the upstream registry keeps this index, or from a copy that
a node already holds.

## Upstream source and components

The image configuration records vLLM commit
[`647a76866b4559a74e27d1c6b55fbffe85c07ce4`](https://github.com/vllm-project/vllm/tree/647a76866b4559a74e27d1c6b55fbffe85c07ce4)
and [upstream release build 11433](https://buildkite.com/vllm/release-v2/builds/11433).
The commit is on upstream `main`; no release tag names it. The installed
package reports vLLM `0.34.0rc1.dev1133+g647a76866`.
See the [upstream Dockerfile at that commit](https://github.com/vllm-project/vllm/blob/647a76866b4559a74e27d1c6b55fbffe85c07ce4/docker/Dockerfile)
for its build stages and component installation.

The specs need this build because the Inkling 2 Mini model support
([vLLM pull request 69028](https://github.com/vllm-project/vllm/pull/69028))
was merged into `main` after the `v0.34.0` release. Credit for that support,
including the speculative draft that spec `3f30050631c3…` uses, belongs to its
upstream authors.

The configuration identifies NVIDIA CUDA 13.2.1 and Ubuntu 24.04 ancestry.
Credit also belongs to **NVIDIA**, **Ubuntu and Debian contributors**, and the
authors of bundled Python packages and native libraries.
No Pulsar patch series is recorded for this catalog image.

## Licenses and verification limits

vLLM uses [Apache-2.0](https://github.com/vllm-project/vllm/blob/647a76866b4559a74e27d1c6b55fbffe85c07ce4/LICENSE).
Other bundled components retain their own licenses and notices, including
NVIDIA's container and CUDA terms. The separately acquired Inkling-2-Mini
weights retain their model publisher's terms.

On 2026-11-23, registry metadata confirmed the catalog index, its arm64 child,
the configuration identity, and the recorded source/build labels. The registry
tag `nightly-647a76866b4559a74e27d1c6b55fbffe85c07ce4` resolved to the catalog
index on that date. The arm64 image was pulled by digest and served both
specs' baseline runs. This check did not inspect packaged license files or
rebuild the image. The exact lower NVIDIA base-image manifest is not
established by this record.
