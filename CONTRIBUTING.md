# Contributing to Pulsar Inference Stack

Pulsar is a maintainer-led project. Bug reports, documentation corrections,
focused code changes, and feature or serving-spec proposals are welcome. The
maintainer decides project scope, merges, releases, and catalog publication.

## Start with the problem

Search existing [issues](https://github.com/luisefigueroa/pulsar-inference-stack/issues)
and [discussions](https://github.com/luisefigueroa/pulsar-inference-stack/discussions)
first. Users and contributors can open a public issue directly to report bugs,
problems with existing serving specs, or documentation errors. No prior approval
or proposed fix is required. Describe what you observed and include the context
available to you, even if you cannot yet reproduce or diagnose the problem.
A small documentation correction can also be proposed directly in an issue.

For a feature or a new or changed serving spec, use the proposal form. Explain
the outcome you need and the smallest change you think would help. Wait for
agreement on scope before substantial implementation work.

For usage questions, see [Support](SUPPORT.md). Report suspected vulnerabilities
through the private route in [Security](SECURITY.md).

## Submit an agreed change

Pull-request creation is currently restricted to repository collaborators.
Other contributors can work in a public fork and link their branch, exact
commit, or patch in the agreed issue. The maintainer can bring the change into
a review PR while preserving contributor attribution. Repository write access
is not required to use this route.

Base new work on current `main`, keep changes focused, and include:

- The issue addressed and the effect on users.
- Checks actually performed, their results, and any remaining uncertainty.
- Relevant compatibility or documentation changes.

Agreement to explore a change does not guarantee that it will be merged.
Reviews may request a smaller scope or a different approach.

## Validation and attribution

Follow [Testing](docs/TESTING.md) to select checks appropriate to the change.
Distinguish automated checks, focused tests, and physical serving results.
Report skipped or unavailable validation honestly; access to serving hardware
is not a prerequisite for reporting bugs or contributing documentation and
many software changes.

AI-assisted contributions are welcome. Contributors remain responsible for
understanding and reviewing their submissions, checking factual claims, and
reporting only validation that actually happened. Preserve applicable licenses
and upstream attribution; see [LICENSE](LICENSE) and
[third-party notices](THIRD_PARTY_NOTICES.md).

For a new or changed image or an adapted upstream patch, include its source
and credits following [Maintaining these records](THIRD_PARTY_NOTICES.md#maintaining-these-records).
Use exact digests and source commits, describe local changes, and identify
any provenance you cannot establish.

## Propose a serving spec

vLLM-based specs generally have the quickest path to merge because they use
Stack's established serving integration. Proposals using
[SGLang](https://docs.sglang.io/),
[TensorRT-LLM](https://nvidia.github.io/TensorRT-LLM/), or other established
inference engines are also welcome.

Identify the engine and its version, and describe any Stack integration needed
to validate, launch, observe, and stop the proposed recipe. Agree on that scope
with the maintainer before substantial implementation. Required integration
must be in place before the contribution can be accepted.

Pulsar strongly prefers weights published by frontier or other well-known model
labs, including quantizations published by those labs. Experimental personal
quantizations are considered with additional scrutiny. If proposing one, explain
the choice and identify the upstream model and revision, the quantization method,
and the publisher and exact revision of the resulting weights. All weight sources
follow the same validation requirements below.

Contributors proposing a new or changed serving spec must provide the spec and
supporting evidence of at least minimal validation and testing on their own
DGX environment. Describe the exact configuration tested, the DGX hardware and
node count, the checks performed, observed results, and known limitations.
Share compact, redacted evidence through the agreed issue and fork or patch.

Acceptance requires the maintainer's subsequent validation of accuracy,
stability, performance, and throughput to pass before the PR is accepted.
Contributor testing provides the starting evidence for that review. Follow the
[catalog contribution guide](docs/CONTRIBUTIONS.md) for the public spec and
evidence format.

## Public and private work

Stack's public workflows and checks must remain usable without the private
Workbench. Workbench owns the maintainer's experiments and qualification
history. Claims about a recipe need evidence for that exact configuration.

Before posting, inspect text, patches, screenshots, and commit metadata for
credentials, host identities, addresses, private paths, and raw experiment data.
Share only the minimum redacted material needed for review.

Treat participants respectfully and keep criticism focused on the work. The
maintainer may close off-topic submissions and restrict abusive participation.
