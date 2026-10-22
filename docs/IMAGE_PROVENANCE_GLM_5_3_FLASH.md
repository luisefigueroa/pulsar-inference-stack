# GLM 5.3 Flash serving image: provenance and credits

This image derives from the official vLLM 0.31.0 container and adds a Python
overlay for GLM on DGX Spark. Credit for the runtime, kernels, and incorporated
fixes belongs to the upstream projects and contributors below.

Return to [all third-party notices and image records](../THIRD_PARTY_NOTICES.md).

## Published image

- Repository: `ghcr.io/luisefigueroa/glm53-flash-vllm-tp3`
- Platform: `linux/arm64`
- Image manifest: `sha256:7ffa2e55ce2e3af4fae51caa89d03902b4a80cb1507563f17b051f7918dce0d3`
- Image configuration: `sha256:d1a5aca050de9274c6a9d6281ee4a23d3d466ed4d8022177de8ec67a60c1265d`
- Catalog spec: [b13b30c418f3…](../releases/b13b30c418f35c812dfbb69de48a01ef82b5545b1de290d2d9a03906164d257b.json)
- [Public GHCR package](https://github.com/users/luisefigueroa/packages/container/package/glm53-flash-vllm-tp3)

## Base image and overlay

The direct base is the official vLLM 0.31.0 Linux/arm64 image:

```text
docker.io/vllm/vllm-openai@sha256:3f7dd5b777d34d1724456ce71f87385dca288c3bb23029ab27dee358f5d2b971
```

It records vLLM commit
[`db9527a46873454610df6dbedf79a36d6bf1a7f6`](https://github.com/vllm-project/vllm/tree/db9527a46873454610df6dbedf79a36d6bf1a7f6)
and [upstream release build 7746](https://buildkite.com/vllm/release-v2/builds/7746).
All 40 base layer digests and sizes match the first 40 layers of the published
49-layer image. Its history identifies NVIDIA CUDA 13.0.2 and Ubuntu 24.04
ancestry. The exact lower NVIDIA image manifest is not established here.

The applied runtime overlay has SHA-256
`dc066b0617fcdcd7a6604cda21c8c058eff50ad761e99aaf44a87192a8fa4f82`.
It changes 19 Python files, including Triton kernels compiled at runtime.
Pulsar's local changes provide TP3 head/vocabulary padding, the GB10 PDL gate,
pool top-k initialization, and MTP BF16 loading fixes. Native libraries are
inherited from the base; this overlay does not rebuild them.

## Upstream code and contributors

Thanks to the **vLLM Team and contributors** for the runtime and GLM model
implementation. The runtime overlay incorporates the following upstream work.
Authors are identified by their GitHub handles; the linked PRs retain the full
review and commit histories.

| Upstream change | PR author | Applied source commit |
| --- | --- | --- |
| [Slice pure speculative-decode rows, #58763](https://github.com/vllm-project/vllm/pull/58763) | `lucamotz` | `87a4bf664f4b2344eced5a10add977b750d4b7d5` |
| [Keep recurrent state consistent during speculation, #56531](https://github.com/vllm-project/vllm/pull/56531) | `vadiklyutiy` | `0eac15270710ddb7f5296d7244fcd0496c2daa0f` |
| [Bound speculative query widths, #55504](https://github.com/vllm-project/vllm/pull/55504) | `DimensionSTP` | `10cc2f6ae2c9ba7cc5841ece95e27d0562aef1bf` |
| [Sparse-MLA dtype and indexer workspace fixes, #55222](https://github.com/vllm-project/vllm/pull/55222) | `drakosha` | `c37f86e5721f089a27c8e4358c30e435805fcaab` |
| [Keep kpool and QSA writes out of the null block, #59528](https://github.com/vllm-project/vllm/pull/59528) | `ivanium` | `240785b82c299c6c08bc5239e997cf62d67cf7bb` |
| [Avoid the recurrent-KDA grid limit, #56974](https://github.com/vllm-project/vllm/pull/56974) | `NolenLiang` | `ae53b06898bcd4ae13553eb019f7381dab90edb2` |
| [Avoid rank-local indexer normalization compilation, #58979](https://github.com/vllm-project/vllm/pull/58979) | `SiluPanda` | `fcbbd5b7b5f56f50910216d76f9e45f3c019fba0` (the PR head used for this build) |

The last entry pins the head used while the PR was open. Its later merge commit
is not substituted for that recorded input. Runtime changes from all seven PRs
are included. Test changes were kept separately, with three unavailable test
files excluded from the retained test series; they are not part of the image
overlay. The [#58454 kpool fix](https://github.com/vllm-project/vllm/pull/58454)
was already present in vLLM 0.31.0 and was not applied again.

Credit also belongs to **FlashInfer contributors** for FlashInfer 0.7.0.post1
and its prebuilt kernels, **NVIDIA** for CUDA, **Ubuntu and Debian contributors**
for system components, and the authors of PyTorch and other bundled dependencies.
This record covers the published vLLM image; earlier SGLang experiments are
separate artifacts.

## Licenses and verification limits

vLLM and the incorporated vLLM patches use
[Apache-2.0](https://github.com/vllm-project/vllm/blob/db9527a46873454610df6dbedf79a36d6bf1a7f6/LICENSE).
Bundled components retain their respective licenses and notices, including
NVIDIA's container and CUDA terms. The separately acquired GLM weights retain
their model publisher's terms.

On 2026-10-21, registry metadata confirmed the image and base identities,
matching base layer digests/sizes, and recorded overlay/source labels. The
retained build record binds the overlay hash above to this image. Its installed
file and binary checks are historical build evidence; this documentation pass
did not extract image layers, inspect every packaged license, rebuild the image,
or repeat model validation. These credits are not an exhaustive dependency inventory.
