# DeepSeek V4.1 Flash serving image: provenance and credits

This image combines the official vLLM container base with a rebuilt vLLM runtime,
GB10/TP3 adaptations, and a sequence of DSpark and frontend patches. The underlying
runtime, kernels, and adapted fixes belong to the upstream projects and authors
credited below.

Return to [all third-party notices and image records](../THIRD_PARTY_NOTICES.md).

## Published image

- Repository: `ghcr.io/luisefigueroa/deepseek-v41-flash-vllm-tp3`
- Platform: `linux/arm64`
- Image manifest: `sha256:b0a66ba6b2762bdd7b9d04708e52aef1e170b296d2ef3fa4a37dfb62e8b0cc7d`
- Image configuration: `sha256:a8532eba6ee9bf387871413e6b788280a169c5bb2ae9c5d7c65f0451009c50c1`
- Catalog specs: [154ea1726af4…](../releases/154ea1726af4dd9f4a9e4ef14be4d476d0c7082ff0851b6e5ab19e8921dd8b3f.json) and [e5957c46d433…](../releases/e5957c46d433180640165d888f7543ee31cb81dfb725e93b6259a4c83827dc60.json)
- [Public GHCR package](https://github.com/users/luisefigueroa/packages/container/package/deepseek-v41-flash-vllm-tp3)

Both specs use these same image bytes. Their recipe settings remain separate
from image identity and attribution.

## Base image and rebuilt runtime

The upstream container base is:

```text
docker.io/vllm/vllm-openai@sha256:a551e05307cd2e0092139d84db32af9c97e67d2eeeff072d21e429131d8c23f0
```

It records vLLM nightly commit `8a728663c1c3eeace834a95f5654fa653cc1998c`
and [upstream build 6097](https://buildkite.com/vllm/release-v2/builds/6097).
All 37 base layer digests and sizes match the first 37 layers of the published
52-layer image. Its inherited history identifies NVIDIA CUDA 13.0.2 and Ubuntu
24.04 ancestry; the exact lower NVIDIA image manifest is not established here.

The installed vLLM was rebuilt from
[`e47aa780bccf59f59dfa2cbb18e17a10b4fe69ba`](https://github.com/vllm-project/vllm/tree/e47aa780bccf59f59dfa2cbb18e17a10b4fe69ba)
plus the recorded patches. The inherited `VLLM_BUILD_COMMIT` label still
describes the upstream container base. It does not identify this rebuilt
runtime by itself.

The retained native-build record identifies the intermediate image
`sha256:ac2e8e95b28f3856d1a74be2f111a44ad131fd69b7e32ff2bb2850d16c18a71a`
and combined native-build patch
`sha256:866549db450c285f593c04d6c63a5d8ecfb1b534cf4d82b50493e9b5b1d48400`.
Five subsequent Python overlays lead to the published image.

## Upstream code and contributors

Thanks to the **vLLM Team and contributors** for the serving runtime, model
implementation, and DSpark support.

The Engram disk reader and input staging, projection-padding approach,
streaming loader, and GB10 page/indexer corrections adapt
**Tech2Wild/Kai's [DeepSeek DGX Spark reference](https://github.com/tonyd2wild/DeepSeek-V4.1-Flash-vLLM-DGX-Spark/tree/fc725ecf10869c184f4347dd73336536d395753c)**
at commit `fc725ecf10869c184f4347dd73336536d395753c`.
Pulsar adds scoped TP3 validation, row/header/loading guards, and the recorded
build integration. The reference project's performance results are not Pulsar
qualification evidence.

The image also incorporates these vLLM fixes. Authors are identified by their
upstream GitHub handles; each linked PR retains the wider review and commit history.

| Upstream change | PR author | Applied source commit and scope |
| --- | --- | --- |
| [KV-only DSpark context insertion, #56441](https://github.com/vllm-project/vllm/pull/56441) | `0z5a` | `6ecd97f1b7fb13991065370f6d6be3dffe9fe1db`; adapted to the older cache ABI, with TP3 draft padding |
| [Keep the compressor ring out of the null block, #58560](https://github.com/vllm-project/vllm/pull/58560) | `bluemelov1` | `49e0f4797853da0d1da7fdfe794f327fb5d5843d`; compressor runtime hunk |
| [Parse tool calls without the outer DSML wrapper, #55954](https://github.com/vllm-project/vllm/pull/55954) | `sfeng33` | `d98c8c030b648f4178da16977424cdadef1fb596`; DeepSeek V4 parser hunk only |
| [Handle missing string attributes, #56271](https://github.com/vllm-project/vllm/pull/56271) | `wtdcode` | `39e33db7f3b10ebd3aca6a0008f2990978f118e7`; adapted without the separate #56141 parser change |
| [Reasoning-effort mappings, #58316](https://github.com/vllm-project/vllm/pull/58316) | `zhecfy` | `ddd6fbca148a867aad1fcab7ec72f582b9977db4`; Python tokenizer hunk only, without the Rust frontend changes |

The later local overlays propagate target settings into the draft model and
adapt draft vocabulary/head padding and expert bookkeeping for TP3. An early
draft-shard filter was removed in the fourth overlay; it is not active in this
published image. The fifth overlay contains the three frontend backports above.
These overlays do not rebuild native libraries.

## Kernel and dependency sources

Credit belongs to **FlashInfer contributors**, **NVIDIA CUTLASS and CCCL
contributors**, and **spdlog contributors** for the following recorded inputs:

| Component | Pinned source |
| --- | --- |
| FlashInfer 0.7.0rc1 | [07869c61ba581e6d6b8ad8d142f4a6c89b707cc1](https://github.com/flashinfer-ai/flashinfer/tree/07869c61ba581e6d6b8ad8d142f4a6c89b707cc1) |
| CUTLASS for the vLLM stable extension | [cb4247394dd82148787aed73e5dc7cef33cbf862](https://github.com/NVIDIA/cutlass/tree/cb4247394dd82148787aed73e5dc7cef33cbf862) |
| CUTLASS for FlashInfer | [b46b16d003484063bca4ed365e44095c4c6ed633](https://github.com/NVIDIA/cutlass/tree/b46b16d003484063bca4ed365e44095c4c6ed633) |
| CCCL for FlashInfer | [16bd510c9b712e82b0ab6cbb630d8e29ba1f7116](https://github.com/NVIDIA/cccl/tree/16bd510c9b712e82b0ab6cbb630d8e29ba1f7116) |
| spdlog for FlashInfer | [c3aed4b68373955e1cc94307683d44dca1515d2b](https://github.com/gabime/spdlog/tree/c3aed4b68373955e1cc94307683d44dca1515d2b) |

NVIDIA, Ubuntu/Debian, and the other bundled runtime dependencies retain credit
for their components as described in the [central notices](../THIRD_PARTY_NOTICES.md).

## Licenses and verification limits

vLLM and its upstream patches use
[Apache-2.0](https://github.com/vllm-project/vllm/blob/e47aa780bccf59f59dfa2cbb18e17a10b4fe69ba/LICENSE).
The Tech2Wild reference carries an
[MIT repository notice](https://github.com/tonyd2wild/DeepSeek-V4.1-Flash-vLLM-DGX-Spark/blob/fc725ecf10869c184f4347dd73336536d395753c/LICENSE),
copyright (c) 2026 Tech2wild, alongside Apache-2.0 source headers where present.
The native-build context retains those notices. Kernel libraries and other
dependencies retain their own licenses and notices. The separately acquired
DeepSeek target and draft weights retain their publishers' terms.

The image and base manifest/configuration metadata were checked on 2026-10-21.
Source and patch attribution also uses retained native-build and overlay
records, including the intermediate artifact identity above. This pass did not
extract image layers to recheck installed files or license placement, rebuild
the image, or rerun serving validation. This page is a provenance summary,
not a complete source bundle or transitive dependency inventory.
