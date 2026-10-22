# Getting help

Start with [Operations](docs/OPERATIONS.md) and its
[troubleshooting guide](docs/OPERATIONS.md#troubleshooting).

- Ask usage and setup questions in
  [Q&A](https://github.com/luisefigueroa/pulsar-inference-stack/discussions/categories/q-a).
- Report bugs, problems with existing serving specs, or documentation errors through
  [Issues](https://github.com/luisefigueroa/pulsar-inference-stack/issues/new/choose).
- Use the feature or serving-spec proposal form to suggest a change. See
  [Contributing](CONTRIBUTING.md) before beginning substantial implementation.
- Report suspected vulnerabilities privately as described in [Security](SECURITY.md).

Users and contributors can open an issue directly, without prior approval or
a proposed fix. Include the observations you have; an incomplete diagnosis or
uncertain reproduction is still useful to report.

## Include useful, safe context

Describe what you tried, what you expected, and what happened. Where relevant,
include:

- The Stack commit, from `git rev-parse HEAD` in the affected checkout.
- The public serving-spec ID and whether execution settings were overridden.
- Controller and serving-node OS/architecture, GPU type, and node count.
- The menu action or a redacted command, followed by the smallest useful error
  excerpt or reproduction steps.

If a detail is unavailable, say so. Label saved observations and historical
measurements so they are not mistaken for current service state. Existing
observations are enough to start a conversation; you do not need to restart a
service or rerun an expensive workload simply to request help.

Issues and discussions are public. Remove tokens, passwords, authorization
headers, hostnames, network addresses, private paths, and sensitive request
content. Inspect screenshots as carefully as text. Do not attach complete
configuration files, topology files, raw experiment captures, or unrestricted
log dumps. Use consistent placeholders when identities matter to the problem.

## Expectations

Support is provided as maintainer and community time permits, with no guaranteed
response time. Hardware availability may limit reproduction. Catalog membership
and historical results do not establish readiness on your installation.
