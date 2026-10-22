# Reporting security vulnerabilities

Report suspected vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/luisefigueroa/pulsar-inference-stack/security/advisories/new).
Do not post exploit details, credentials, or sensitive evidence in public issues
or discussions. If you are unsure whether a problem is security-sensitive,
use the private route.

Include the affected Stack commit, a description of the impact, and the smallest
safe reproduction you can provide. For recipe or image-related reports, include
the relevant spec ID and image digest if known. Redact real credentials and
private data even in a private report; synthetic examples are preferred.

Reports may concern the CLI, model-file or archive handling, serving lifecycle,
or the way an upstream component is used by Pulsar. Identify the affected
upstream component when relevant so disclosure can be coordinated appropriately.

## Maintenance scope

Security fixes target the current `main` branch. Older commits do not have a
promised backport schedule. Reports affecting older commits are still welcome;
include the exact revision so the affected scope can be investigated.

The maintainer will coordinate investigation, fixes, and disclosure through the
private report as capacity permits. There is no guaranteed response or resolution
time. Reporter attribution will be agreed before public disclosure.

For ordinary setup questions and non-security defects, see [Support](SUPPORT.md).
