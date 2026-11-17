# MiniMax M3.1 Flash serving image: provenance and credits

This image is the official vLLM MiniMax-M3.1-Flash container with one
replaced Python file: the upstream reasoning parser for this model. The
runtime, model implementation, kernels and the parser belong to the upstream
projects and authors credited below.

Return to [all third-party notices and image records](../THIRD_PARTY_NOTICES.md).

## Published image

- Repository: `ghcr.io/luisefigueroa/minimax-m31-flash-vllm-tp4`
- Platform: `linux/arm64`
- Image manifest: `sha256:268b14f1473ed4ec040dd0e9247018014f8507d540920da2f8c75d30c89a9950`
- Image configuration: `sha256:d6afebbaa720514226666452fd2ac2bc8d7cdc5ad5c34bad167ad5521697fdd5`
- Catalog spec: [949f8d185c94…](../releases/949f8d185c94b3ba15a78ad9e6151a22d9b8b70ef20d036c5f18ef38cd9a1d8f.json)
- [Public GHCR package](https://github.com/users/luisefigueroa/packages/container/package/minimax-m31-flash-vllm-tp4)

## Base image

The upstream container base is:

```text
docker.io/vllm/vllm-openai@sha256:5d76347628973fb80519a919a2f2740b6645b62cba3fe2bd87b25f111bd0e948
```

It was published with the tag `minimax-m31-flash-arm64-cu134` and created on
2026-10-22, before the model's upstream pull request merged. Its vLLM version
string is `0.1.dev23341+g8744dd2fb`; the commit `8744dd2fb` does not resolve in
the public vLLM repository, and the image labels give the build commit as
`unknown`. The image has PyTorch 2.14.0 (CUDA 13.0), FlashInfer 0.7.2 and NCCL
2.30.7. All 43 base layers are the first 43 layers of the published 45-layer
image.

## Change made by Pulsar

The two added layers replace `vllm/reasoning/minimax_m3_reasoning_parser.py`
with the file of upstream vLLM at commit
[`dbd49af2223553d077eb839f610ad1aa84677391`](https://github.com/vllm-project/vllm/blob/dbd49af2223553d077eb839f610ad1aa84677391/vllm/reasoning/minimax_m3_reasoning_parser.py),
and add a small JSON record of that change. The file of the base image is
byte-identical to the upstream file before that commit, so the change is
exactly the upstream change to this file: 15 added lines. In the base image,
the parser reads the full prompt to learn if the reasoning has ended. The
chat template puts the end marker in the system message, so a streamed
response sent its reasoning as content. The upstream file limits that check
to the last message.

The install step verified the hash of the file before and after the change.
A comparison of every non-bytecode file of the installed vLLM package (5,615
files) in the base and the built image found this file as the only difference.
No model, attention, speculative-decoding or compiled file changed.

## Upstream code and contributors

Thanks to the **vLLM Team and contributors** for the serving runtime, and to
the MiniMax and vLLM contributors who built the MiniMax-M3.1-Flash model
support and the official image.

| Upstream change | PR author | Applied source commit and scope |
| --- | --- | --- |
| [Add MiniMax-M3.1-Flash Model and Kernels, #64111](https://github.com/vllm-project/vllm/pull/64111) | `zyongye` | `dbd49af2223553d077eb839f610ad1aa84677391`; the reasoning parser file only |

NVIDIA, Ubuntu/Debian, and the other bundled runtime dependencies retain credit
for their components as described in the [central notices](../THIRD_PARTY_NOTICES.md).

## Licenses and verification limits

vLLM and the applied parser file use
[Apache-2.0](https://github.com/vllm-project/vllm/blob/dbd49af2223553d077eb839f610ad1aa84677391/LICENSE).
Kernel libraries and other dependencies retain their own licenses and notices.
The separately acquired MiniMax-M3.1-Flash weights, with the DSpark draft
in the same repository, retain their publisher's terms.

The image and base manifest and configuration metadata were checked on
2026-11-16. The change and its verification come from the retained build
record. The two added layers and the image metadata were checked for private
data before the push. This page is a provenance summary, not a complete
source bundle or transitive dependency inventory.
