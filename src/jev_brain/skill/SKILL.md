---
name: jev-brain
description: Pass one caller-selected desktop step to jev through the CLI or local stdio MCP.
---

# Computer use

Choose a single sub-goal and use an API when it covers the requested operation. Preview a desktop step with `jev-brain use --target LABEL --request GOAL`. The caller owns the decision.

For user-authorized input, supply `--act --expect APP_OR_EXACT_URL` and a specific control. Use `--text` for literal content. The target `focused field` selects a focused input. The user grants local permission with `jev-brain consent` in their own terminal. MCP exposes the same step as `use` and takes a boolean `act`.

Read the JSON outcome after each call. A timeout or interruption can follow partial input. On a missing target, focus mismatch, or ambiguous control, reassess the front window before requesting another step. Keep sensitive surfaces and private data out of requests. The audit includes the target and request.

For setup, use `jev-brain doctor` and the [README](https://github.com/talalkashar/jev-brain#install-the-macos-runner). The optional `health` and `decide` tools use the local mock server. Its closed-question answers are fixed.
