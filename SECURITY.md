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
| Tool to network | Password sniffed. These switches support **HTTP only**, so every login sends the password in clear text. | Run only from a host on the management VLAN; use a unique password; keep the management VLAN off untrusted ports. This cannot be fixed in software. |
| Tool to wrong device | IP reassigned, or an impostor answering on the switch's IP | Model check, and a MAC pin per switch (strongly recommended). The password is still sent at login, before the MAC can be read, so pinning stops writes to the wrong device but not credential exposure to it. |
| Snapshot and plan files to tool | Tampered files | Parsed as untrusted JSON with full validation; restore also checks that the snapshot's MAC matches the target switch; saved plans are approval tokens only. A fresh plan must match them exactly, so a tampered plan can only cause a refusal. |
| Environment / `.env` to tool | Password leakage | Passwords never accepted as CLI arguments; `.env` must be mode 0600 or the tool refuses; password scrubbed from error messages; never written to snapshots or plans |
| Dependency `tplink-tool` | Supply chain | Pinned to an exact version; reviewed before pinning (it only talks to the configured host). Review again before bumping. |

**Availability:** changes to the management path are refused unless explicitly allowed;
switches are changed furthest-first; every write is read back; the run stops at the first
mismatch, leaving a pre-change snapshot for restore.

**Out of scope:** the security of the switch firmware itself, and attackers who already
have code execution on the machine running this tool.
