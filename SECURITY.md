# Security

## Who can act

The agent supplies each step. A local preview starts no process. An executor preview (`--arm`) can inspect the desktop. For real input, the CLI and stdio MCP server share one gate. It requires explicit act mode, a nonempty expected app or URL, and consent from `jev-brain consent` in an interactive terminal. Revoke consent by removing `~/.config/jev-brain/consent.json`.

The gate checks all four user-supplied strings for recognized sensitive surfaces before looking at the desktop or launching the executor. It also checks the observed front app, window title, document URL, and secure-field role. Missing accessibility information can prevent a run. Never submit banking, mail, messages, credentials, student information, admin tasks, or login and MFA workflows. A keyword and window check cannot recognize every private document or custom app.

The optional macOS runner checks the window and the element again immediately before input. It sends an accessibility press to the named element, or sets the value of an empty focused field and reads it back. Ambiguous matches and incomplete searches stop the step. A change after a successful press or during a timeout may leave partial input. Inspect the result before retrying.

## Local files and executors

Act calls open a private append-only JSON Lines audit file before launch and record a request and a final outcome under the same call ID. If logging cannot start, the call stops. A crash or storage failure can leave a request without a final record. Logs include the target and request. Keep both free of secrets. Recognized sensitive inputs are redacted. Literal text and screenshots are excluded. The same local account can edit its own log and consent file.

Only use a trusted `jev-run` on the host’s `PATH`. A replacement executable has the host’s permissions, controls its own artifacts, and must enforce the window guard at input time. The included adapter uses local accessibility elements and stores no screen images. Upstream’s full classifier loop has a different data flow, sends state to its configured providers, and writes its own run folder. [Upstream documentation](https://github.com/awlevin/typesafe-computer-use)

## Config and mock server

`jev-brain install` previews config. With `--yes` it edits agent configuration under the current home. Keep the mock HTTP service on its default loopback address. Remote binding requires `--allow-remote`. The service offers health and fixed closed-question answers. Desktop input is available through the CLI and local stdio MCP. Treat question text and responses as untrusted data. Keep secrets out of mock requests. HTTP request bodies are excluded from server logs.
