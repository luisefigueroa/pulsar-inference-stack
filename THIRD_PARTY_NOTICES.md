# Third-party notices

Pulsar builds on work by upstream projects, image publishers, and individual
contributors. This is the entry point for their attribution, source provenance,
and license notices. The [Pulsar source license](LICENSE) is separate from the
licenses of those components and of model weights.

## Serving images

The five catalog specs at Stack commit
`e87c263a2e374eaea6003326fb2ef1afaab52d42` reference four distinct images.
The pages below identify the exact image, its upstream base and sources, and
the patches carried by Pulsar. Two DeepSeek specs share one image record.

| Catalog model | Image and provenance | Main upstream credits |
| --- | --- | --- |
| NVIDIA Nemotron 3.5 Lightning | [Official vLLM 0.29.0 image](docs/IMAGE_PROVENANCE_VLLM_0_29.md), index `c29147676055…` | vLLM Team and contributors; NVIDIA; bundled dependency authors |
| DeepSeek V4.1 Flash 1017 | [Pulsar DeepSeek image](docs/IMAGE_PROVENANCE_DEEPSEEK_V4_1_FLASH.md), manifest `b0a66ba6b276…` | vLLM contributors; Tech2Wild/Kai; FlashInfer; NVIDIA CUTLASS and CCCL; spdlog |
| MiMo V2.6 Flash RL | [Pulsar MiMo image](docs/IMAGE_PROVENANCE_MIMO_V2_6_FLASH.md), manifest `4c87d448318a…` | vLLM contributors; ScitiX InstantTensor; NVIDIA; bundled dependency authors |
| GLM 5.3 Flash NVFP4 | [Pulsar GLM image](docs/IMAGE_PROVENANCE_GLM_5_3_FLASH.md), manifest `7ffa2e55ce2e…` | vLLM contributors; FlashInfer; NVIDIA; bundled dependency authors |

The abbreviated digests in this table are navigation aids. Use the complete
identities in the linked records and specs. Registry manifest and configuration
metadata were checked on 2026-10-21. Build and patch details also use retained
build records; the individual pages state the limits of that evidence.

All four images use vLLM and include software from other projects. Credit also
belongs to NVIDIA for CUDA, to Ubuntu and Debian contributors for system
components, and to the authors of PyTorch, Transformers, Triton, FlashInfer,
and their dependencies where included. Each component retains its own license
and notices. These selected credits are not an exhaustive dependency inventory
or an independent audit of every image's license files.

The repository's [optional Dockerfile](Dockerfile) accepts a caller-selected,
digest-pinned base. It does not define an additional fixed catalog image;
attribution for a resulting image depends on the base and changes actually used.

## Model weights

Model checkpoints are obtained separately from the publisher and are not
redistributed in this repository. The spec records the model repository and
exact commit, including any separately required draft-model snapshot. Consult
the model card and license at that revision for the publisher's terms and
attribution. Image provenance does not replace model provenance.

## Gum

This repository redistributes an unmodified Linux ARM64 executable from
[Charmbracelet Gum v0.17.0](https://github.com/charmbracelet/gum/releases/tag/v0.17.0).

Copyright (c) 2022-2024 Charmbracelet, Inc

Gum is distributed under the MIT License. The complete upstream copyright and
permission notice is included at [`third_party/gum/LICENSE`](third_party/gum/LICENSE).

Vendored artifact provenance and checksums are recorded in
[`third_party/gum/VERSION`](third_party/gum/VERSION). Gum is used only by the
optional guided terminal UI; all core launch and validation paths remain Bash
scripts and work without it.

## Maintaining these records

When a contribution introduces or changes an image or carries upstream code,
update this index and the relevant provenance record. Include the exact image
and base digests, source repository and commit, upstream PRs and contributors,
what was changed or omitted, and applicable license and notice locations.
Distinguish upstream work from local adaptations. Identify missing evidence
explicitly and retain records for previously documented image digests.

Keep build credentials, machine identities, private paths, and raw diagnostics
out of public records. A compact public summary is sufficient; private tooling
is not required to propose one. See [Contributing](CONTRIBUTING.md).
