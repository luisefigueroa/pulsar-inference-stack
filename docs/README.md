# Stack documentation

Choose the task you want to complete. These public workflows do not require the
private Workbench. Return to the [project README](../README.md) for an overview.

## Run and maintain Stack

| I want to… | Start here |
| --- | --- |
| Install the prerequisites and open the interactive menu | [Get started](../README.md#before-you-start) |
| Configure cluster membership and storage | [Setup](OPERATIONS.md#configure-topology-and-storage) |
| Obtain model files, prepare nodes, and start a service | [Serving walkthrough](../README.md#serve-a-catalog-recipe) and [detailed procedures](OPERATIONS.md#obtain-prepare-and-start) |
| Configure authentication and send a request | [First API request](OPERATIONS.md#send-your-first-api-request) |
| Inspect a running service | [Service inspection](OPERATIONS.md#inspect-a-running-service) |
| Move model files, create an archive, or restore one | [Storage and recovery](OPERATIONS.md#move-archive-and-restore) |
| Stop serving or reclaim storage | [Stop and cleanup](OPERATIONS.md#stop-and-reclaim-storage) |
| Resolve a blocked or failed operation | [Troubleshooting](OPERATIONS.md#troubleshooting) |
| Find an interactive action or understand a term | [Menu reference](OPERATIONS.md#menu-reference) and [glossary](OPERATIONS.md#terms) |

## Browse recipes and evidence

Open `./pulsar` to browse the catalog interactively. For direct commands and
how to interpret the output, see [Browse and check](OPERATIONS.md#browse-and-check).
Catalog membership, published measurements, local model files, and live service
health answer different questions; a catalog entry does not establish local readiness.

- [Catalog and evidence](../README.md#catalog-and-evidence): spec identity and saved results.
- [Guarded serving](GUARDED_SERVING.md): recipes that require a bounded trial.
- [Memory estimates](MEMORY_ESTIMATES.md): reviewed estimates of resident weights.

## Contribute and get help

| I want to… | Start here |
| --- | --- |
| Propose a change or a serving spec | [Contributing](../CONTRIBUTING.md) |
| Ask a usage question or report a problem | [Support](../SUPPORT.md) |
| Report a suspected vulnerability privately | [Security](../SECURITY.md) |
| Review the format and checks for a catalog contribution | [Catalog contribution review](CONTRIBUTIONS.md) |
| Choose validation for a change | [Testing](TESTING.md) |

## Integrate with Stack

- [Architecture](ARCHITECTURE.md): ownership, identity, storage, and serving concepts.
- [Public contract](CONTRACT.md): CLI operations, JSON results, and compatibility.
- [Serving guard schema](SERVING_GUARD_SCHEMA.md): the guard document format.

## Provenance and design history

- [Third-party software, images, and credits](../THIRD_PARTY_NOTICES.md): the image index, upstream contributors, and license notices.
- Image details: [official vLLM for Nemotron](IMAGE_PROVENANCE_VLLM_0_29.md), [DeepSeek](IMAGE_PROVENANCE_DEEPSEEK_V4_1_FLASH.md), [MiMo](IMAGE_PROVENANCE_MIMO_V2_6_FLASH.md), and [GLM](IMAGE_PROVENANCE_GLM_5_3_FLASH.md).
- [Pulsar source license](../LICENSE).
- [Decision references](DECISIONS.md): design rationale and links to historical plans. Use the current contract for supported interfaces.
