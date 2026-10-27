# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private advisories:
[Report a vulnerability](https://github.com/somejavadev/electric-fence/security/advisories/new).

Don't open a public issue or discussion for a security problem. Include the
Home Assistant version, the Electric Fence version, and steps to reproduce.
You can expect a first reply within a few days. Once a fix is released, the
advisory is published, with credit to you unless you'd rather not be named.

## In scope

- A way to get past Electric Fence's protection: avoiding a ban, a slowdown or
  a record that should have happened.
- Electric Fence slowing down, blocking or breaking Home Assistant logins.
- Electric Fence exposing data it stores (IP addresses, usernames, setting
  values) to someone who shouldn't see it, or storing a value it should have
  hidden.
- Injection through stored data, such as a crafted username shown in the
  panel, a notification, a download or a forwarded event.

Problems in Home Assistant itself go to
[Home Assistant's security policy](https://www.home-assistant.io/security/).

## Supported versions

Only the latest release gets security fixes. Please update before reporting.

## Verifying a release

Each release's `electric_fence.zip`, which is what the Marketplace installs, is
built by GitHub Actions from the release tag. It comes with a SHA-256 checksum
(`electric_fence.zip.sha256`) and a signed build provenance attestation. To
check a downloaded zip:

```bash
sha256sum --check electric_fence.zip.sha256
gh attestation verify electric_fence.zip --repo somejavadev/electric-fence
```

The attestation shows that the zip was built by this repository's release
workflow, from the tagged commit.
