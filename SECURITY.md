# Security

## Who can act

Local handoff previews run without desktop access. On macOS the public CLI, MCP tool and installed `jev-run` share a supervisor. Input requires explicit act mode, an expected foreground, and consent from `jev-brain consent` in an interactive terminal. Revoke consent by removing `~/.config/jev-brain/consent.json`.

Before starting an executor, including for `--arm`, the parent checks the request and evaluates an independent, read-only accessibility observation. That observation binds the app PID to its bundle identifier and reads the front window, document URL and focused element’s role, subrole, title, description and placeholder. Transport errors, unreadable values, a missing role, window title or identity, and focus changes cause refusal. Optional attributes may be absent when AX explicitly reports them as unsupported or empty. Native app expectations are limited to TextEdit, Notes, Finder and Calendar. Recognized browser bundles require an exact HTTP(S) document URL. Other bundles are refused.

The sensitive-surface classifier normalizes compatibility characters and case, checks known clients and sensitive terms, and refuses residual non-ASCII text, hidden control characters and unresolved URL encodings. Ordinary localized interfaces can therefore be refused. A private document with innocuous metadata can still look ordinary. Never pass credentials or private communications. Arguments can be visible to other processes under your account.

The adapter rechecks focus at input time. A press requires a unique exact title or description among elements exposed through the focused window’s AX children. The search retains distinct references at identical coordinates and counts offscreen matches. A failed read, a search over 512 elements, a missing press action, or a centre hit-test that reaches another element stops the step. A write addresses one empty focused field and reads it back. Pixel-only controls and content omitted by the app’s accessibility provider cannot be verified. AX observation and input are separate OS operations. A timeout, interruption or lost acknowledgement can follow a mutation. Inspect the app before retrying.

## Local files and executors

Act attempts open a private append-only JSON Lines audit file before observation or launch. The initial and final records contain a UTC timestamp, random call ID, combined argument length, a known bundle ID when verified, and a fixed outcome code. The program keeps requests, control labels, literal text, window titles, URLs and backend output out of its files. The command returns results to its caller, which may retain them. A crash or storage failure can leave an initial record without a final record. The same account can edit its own log and consent file.

The supervisor sets one 30-second monotonic deadline before validation. It runs native observation in a killable read-only process, caps each subprocess reply at 64 KiB, and closes pipes on termination. The four input strings are limited to 4,096 characters each. MCP reads newline-delimited frames in bounded chunks with a 2,000,000-byte limit. Executor-backed modes require macOS. Windows and Linux support the local preview.

Only use a trusted `jev-run` on the host’s `PATH`. A replacement executable has the host’s permissions, controls its own artifacts, and must enforce the window guard at input time. The included adapter uses local accessibility elements and stores no screen images. Upstream’s full classifier loop has a different data flow, sends state to its configured providers, and writes its own run folder. [Upstream documentation](https://github.com/awlevin/typesafe-computer-use)

## Config and mock server

`jev-brain install` reports the config path and proposed action. With `--yes` it adds a missing server under the current home. Existing jev-brain and systemone entries and references to their policies are preserved under their current names. A new TOML table must reparse to the expected key path and preserve the rest of the parsed document. Incompatible layouts require a manual edit. Keep the mock HTTP service on its default loopback address. Remote binding requires `--allow-remote`. The service offers health and fixed closed-question answers. Desktop input is available through the CLI and local stdio MCP. Treat question text and responses as untrusted data. Keep secrets out of mock requests. HTTP request bodies are excluded from server logs.
