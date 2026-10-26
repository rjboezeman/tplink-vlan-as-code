# Security

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** on this repository
(Security tab, then "Report a vulnerability"). Do not open a public issue for security problems.

## Threat model

**Assets:** the switch admin password, and the integrity and availability of the L2 network
(a bad VLAN change can isolate hosts, or bridge a trusted VLAN into an untrusted one).

**Trust boundaries and how they are handled:**

| Boundary | Risk | Mitigation |
|---|---|---|
| Config file to tool | Malformed or malicious values sent to the switch, or rendered as HTML | Strict schema; unknown keys rejected; VLAN names limited to `[A-Za-z0-9_.-]{1,10}`; hosts must be a bare IPv4 address or hostname; diagram labels HTML-escaped |
| Tool to network | Password sniffed. These switches support **HTTP only**, so every login sends the password in clear text. | Run only from a host on the management VLAN; use a unique password; keep the management VLAN off untrusted ports. This cannot be fixed in software. The client sends the password only in the POST body, never in a URL. It ignores `HTTP_PROXY` and similar settings, so the password cannot detour through a proxy, and it refuses redirects. |
| Switch to tool | A hostile or spoofed device returns crafted pages | Responses are capped at 1 MiB; page data is read by a parser that accepts only plain JavaScript literals and never evaluates anything; parsed values are type-checked before use |
| Tool to wrong device | IP reassigned, or an impostor answering on the switch's IP | Model check, and a MAC pin per switch (strongly recommended). The password is still sent at login, before the MAC can be read, so pinning stops writes to the wrong device but not credential exposure to it. |
| Snapshot and plan files to tool | Tampered files | Parsed as untrusted JSON with full validation; restore also checks that the snapshot's MAC matches the target switch; saved plans are approval tokens only. A fresh plan must match them exactly, so a tampered plan can only cause a refusal. |
| Environment / `.env` to tool | Password leakage | Passwords never accepted as CLI arguments; `.env` must be mode 0600 or the tool refuses; password scrubbed from error messages; never written to snapshots or plans |
| Dependencies | Supply chain | None at runtime: standard library only. Development tools (pytest, ruff) are not installed for normal use. |

**Availability:** changes to the management path are refused unless explicitly allowed;
switches are changed furthest-first; every write is read back; the run stops at the first
mismatch, leaving a pre-change snapshot for restore.

**Out of scope:** the security of the switch firmware itself, and attackers who already
have code execution on the machine running this tool.
