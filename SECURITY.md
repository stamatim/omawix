# Security Policy

## Supported versions

Security fixes are made against the latest released version of Omawix.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/stamatim/omawix/security/advisories/new) to report a suspected vulnerability. Include reproduction steps, affected versions, impact, and any suggested mitigation when available.

Do not open a public issue or disclose the vulnerability publicly before a fix can be assessed and released. If private vulnerability reporting is unavailable, wait for it to become available rather than placing sensitive details in a public issue.

## Local security boundary

The embedded reader starts `kiwix-serve` on a random loopback port while an archive is open. Loopback prevents access from other machines, but it is shared by accounts on the same multi-user host. Do not open sensitive private archives with Omawix on an untrusted shared machine.
