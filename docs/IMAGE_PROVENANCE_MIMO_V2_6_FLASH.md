# MiMo V2.6 Flash serving image: provenance and credits

The Pulsar MiMo V2.6 Flash serving image derives from the official vLLM container
image. Pulsar's additions adapt model loading, loader memory-budget selection, and draft
load configuration. Credit for the underlying runtime, kernels, and loader
belongs to the upstream projects and contributors listed below.

## Published image

- Repository: `ghcr.io/luisefigueroa/mimo-v26-flash-vllm`
- Tag: `v030-dflash-load-config-001`
- Platform: `linux/arm64`
- Immutable image manifest: `sha256:4c87d448318a2f887b6b2cfe65e1dcca1d5859f0e8dcc8c3f6923d28c02d4dd8`
- Image configuration: `sha256:0790aeb54a101305d485564ae88a0f459e5220b4be257c9904738f2afb434fbd`

Use the manifest digest to identify these exact image bytes. The
[GHCR package](https://github.com/users/luisefigueroa/packages/container/package/mimo-v26-flash-vllm)
is public and linked to this repository.

## Base image and lineage

The direct base is the **official vLLM `vllm/vllm-openai:v0.30.0` image**, published
by the vLLM project. Its pinned Linux/arm64 manifest is:

```text
docker.io/vllm/vllm-openai@sha256:4864d46625cbc3307623e29ac742030655e27249feba7b97ec925ce4cc4dfb56
```

The base records vLLM commit
`ced6857afa0ea7b2e3f0846a62e1394e90f15607` and
[upstream release build 7022](https://buildkite.com/vllm/release-v2/builds/7022).
All 37 base layer digests and sizes match the first 37 layers of the published
Pulsar image. Three local overlay builds add nine layers, producing 46 layer
descriptors in total.

The inherited build history identifies NVIDIA CUDA 13.0.2 and Ubuntu 24.04
ancestry. Credit also belongs to **NVIDIA** for the CUDA container and software,
and to the **Ubuntu and Debian contributors** for system components. The
[upstream vLLM Dockerfile](https://github.com/vllm-project/vllm/blob/ced6857afa0ea7b2e3f0846a62e1394e90f15607/docker/Dockerfile)
describes the NVIDIA CUDA final base and PyTorch manylinux build stage.

The exact original lower NVIDIA image manifest was not recorded. The currently
resolved `nvidia/cuda:13.0.2-base-ubuntu24.04` tag does not match the inherited layer
prefix, so its current digest cannot identify that original ancestor. The pinned
vLLM manifest above is the verified direct base.

## Upstream code and contributors

Thanks to the **vLLM Team and contributors** for the serving runtime and model
implementations, and to the following authors for the fixes incorporated or
adapted in this image:

| Upstream change | Author | Source commit |
| --- | --- | --- |
| [MiMo fused FP8 QKV sharding fix, PR #57508](https://github.com/vllm-project/vllm/pull/57508) | `vllmellm` | `211e252d0b4f8429f9b15fc52bdfed07782c7f70` |
| [MiMo BF16 router and MXFP4 support, PR #57784](https://github.com/vllm-project/vllm/pull/57784) | `Zyann7` | `9b2f34cad446f73b1699e8236ec0b611a65f48af` |
| [MiMo QKV pairing fix across loader calls, PR #58142](https://github.com/vllm-project/vllm/pull/58142) | `vllmellm` | `77e52645e9baba15d6b9cd7e09a12e70628b8237` |
| [Draft load-configuration selection from PR #57312](https://github.com/vllm-project/vllm/pull/57312), partially adapted | `liusy58` | `c723a831a81cb4ff89ea6d61b2d15109306ed1bc` |

The PR #57312 adaptation selects the explicit draft loader configuration in the
V2 DFlash path. It incorporates only that portion of the upstream work.

The image also uses **[ScitiX's InstantTensor](https://github.com/scitix/InstantTensor)**
0.2.0, based on upstream commit
`1b124d2a8a37d19907640b62aeea2235666c7162`. Credit belongs to its authors and
contributors. Pulsar adapts its Python memory-budget selection; the inherited
native library remains unchanged.

Other bundled projects include **[PyTorch](https://github.com/pytorch/pytorch)**
2.13.0+cu130, **[FlashInfer](https://github.com/flashinfer-ai/flashinfer)**
0.6.18.post1, **[Hugging Face Transformers](https://github.com/huggingface/transformers)**
5.17.0, and **[Triton](https://github.com/triton-lang/triton)** 3.7.1, alongside
their dependencies. Their teams and contributors retain credit for those
components.

## Licenses and notices

vLLM and InstantTensor use Apache-2.0; see the pinned
[vLLM license](https://github.com/vllm-project/vllm/blob/ced6857afa0ea7b2e3f0846a62e1394e90f15607/LICENSE)
and [InstantTensor license](https://github.com/scitix/InstantTensor/blob/1b124d2a8a37d19907640b62aeea2235666c7162/LICENSE).
Other components retain their respective licenses, including NVIDIA's container
and CUDA terms. Each component's own license governs its use and redistribution.

Inspection of the published image confirms the NVIDIA container license at
`/NGC-DL-CONTAINER-LICENSE` and packaged license files for vLLM, InstantTensor,
PyTorch, FlashInfer, Transformers, and Triton. These credits supplement the
license texts and copyright notices shipped with the components.

Model checkpoints are supplied separately and retain their own licenses and
attribution. This page records image provenance and selected upstream notices;
it is not an exhaustive inventory of every dependency or its redistribution
requirements.
