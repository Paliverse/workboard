# Security Policy

## Supported versions

Security fixes go into the latest release. Please upgrade (`workboard upgrade`) before reporting.

| Version | Supported |
|---|---|
| 0.1.x | Yes |
| < 0.1 (pre-release previews) | No |

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting:

**<https://github.com/Paliverse/workboard/security/advisories/new>**

Do not open a public issue, pull request or discussion for a suspected vulnerability. Include:

- the affected version (`workboard version --json`), operating system and install channel;
- what an attacker needs (for example: a web page the user visits, another local account, or write access to a project);
- steps to reproduce, and the impact you observed.

We aim to acknowledge reports within a week and will keep you updated in the advisory. Once a fix ships, we publish the advisory and credit you unless you prefer otherwise.

## Threat model

WorkBoard is a local, single-user tool. Board data is plain files in your projects and in `~/.workboard`, and the server is meant to be reached only by your own browser.

What WorkBoard defends against:

- **Remote network access.** The server binds `127.0.0.1` only and never listens on external interfaces.
- **Malicious web pages and DNS rebinding.** Requests whose `Host` header isn't `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>` are rejected with 403. Requests that carry a foreign `Origin` are rejected, and JSON writes require `Content-Type: application/json`.
- **Stopping the server.** `POST /api/shutdown` requires the random token stored in `~/.workboard/server.json` in the `X-WorkBoard-Token` header.
- **Hostile attachments.** Downloads are served as `application/octet-stream` with `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox`. Attachment ids are opaque, sizes are capped at 10 MiB, and exports verify size and SHA-256 and never overwrite files.
- **Data loss from concurrent writers.** Every write is locked, fsynced, atomically replaced and backed up. Conflicts fail visibly.
- **Tampered downloads from the install scripts.** Both scripts verify release archives against `SHA256SUMS` before installing.

What is out of scope:

- **Processes running as you.** Any program running under your account can read and modify your boards, read `server.json` (including the shutdown token) and call the local API. WorkBoard doesn't authenticate local callers, and actor labels (`--actor`, `WORKBOARD_ACTOR`) are attribution, not authentication.
- **Other accounts on the same machine.** Loopback ports are reachable by every local account, and the board API has no per-user authentication. Don't run the server on a shared multi-user host with untrusted users.
- **Card content given to agents.** Comments, notes and attachments are untrusted data. The bundled agent skill tells agents never to follow instructions embedded in them or execute downloads. Prompt-injection resistance ultimately depends on the agent you use.
- **Secure deletion.** Detached attachments and deleted boards are kept on disk for recovery. WorkBoard never securely erases data.

Reports that break any of the defenses above are in scope. Examples include reaching the server from another machine, bypassing the Host or Origin checks, path traversal through board names or attachment ids, script injection into the board UI from card content, or leaking the shutdown token to a web page.
