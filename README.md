# jev-brain

Give your agent one way to operate a Mac. Claude, Codex, Grok, or another caller decides the goal and names one step. jev-brain checks the request. With your consent, `jev-run` presses the named control or enters the supplied text. The agent reads the result and decides what comes next.

```text
Your agent ── one step ──► jev-brain ── guarded request ──► jev-run ──► Mac
    ▲                                                          │
    └────────────── outcome, action, confidence, steps ─────────┘
```

## 60-second quick start

Preview a step from a clone. You need Python 3.11+ for the CLI. Use Python 3.12+ if you also want the optional Mac runner.

```sh
git clone https://github.com/talalkashar/jev-brain.git
cd jev-brain
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
jev-brain use --target Notes --request "Count the open Notes windows"
```

Look for `"mode": "dry-run"` and `"started": false`. The preview checks the handoff locally. It leaves Notes alone and returns no window count. It needs no account, desktop permissions, or model access.

## Install the macOS runner

For live input, use macOS 14+ and a Python 3.12+ virtual environment. From the same clone and activated environment, install the optional adapter. This command installs Aaron Levin’s **typesafe-computer-use** from the revision used by the adapter, its Mac dependencies, and the `jev-run` executable.

```sh
python -m pip install -e ./runner
jev-brain doctor
```

In **System Settings → Privacy & Security → Accessibility**, allow the app that hosts the CLI or agent. Run `doctor` from that host. It checks Python, the runner on `PATH`, and the current process’s permission status. The included adapter needs Accessibility alone. Doctor reports **Screen Recording** for information because it cannot tell whether an external runner captures the screen. A runner that captures the screen needs that permission. [Upstream installation and permissions](https://github.com/awlevin/typesafe-computer-use/tree/44ca11f0935b021b73020825da054b5c92cc1288#install)

If you already use a guarded `jev-run`, put its directory first on `PATH`. It must accept a positional goal, `--expect`, `--target`, `--text`, `--steps 1`, `--handoffs 0`, and `--act`. It must enforce one mutation, recheck focus at input time, and return a recognized JSON outcome with a truthful `steps_taken` count (zero on preview, one on confirmed input). A replacement executable controls its own files. Install both bundled packages in one environment. Upstream’s standalone command is `clicker` and has a different interface. [Upstream command definition](https://github.com/awlevin/typesafe-computer-use/blob/44ca11f0935b021b73020825da054b5c92cc1288/typesafe_computer_use/cli.py)

To install Aaron’s standalone classifier CLI separately, clone `https://github.com/awlevin/typesafe-computer-use`, change into that checkout, and run `uv sync`. Copy its `.env.example` to `.env` there and configure `TYPESAFE_API_KEY` yourself. The upstream executable is `.venv/bin/clicker`. [Upstream setup](https://github.com/awlevin/typesafe-computer-use/tree/44ca11f0935b021b73020825da054b5c92cc1288#install)

## Try one real step

1. Run `jev-brain consent` **yourself in a terminal** and enter `ALLOW`. This enables future explicit act requests for your local account.
2. Open a **new, empty TextEdit document**. Return to the terminal and run `sh examples/textedit.sh`.
3. During the five-second delay, bring the empty document to the front and place the insertion point in it. The demo asks jev to enter `Hello from jev-brain.` and reads the field back.

The demo runs this command after the delay. An MCP caller can send the same step while the document is already in front.

```sh
jev-brain use --act --expect TextEdit --target 'focused field' \
  --request 'Write a greeting in the empty document' --text 'Hello from jev-brain.'
```

For a button, pass its exact accessibility title or description as `--target` and omit `--text`. The adapter checks the whole exposed window tree, including offscreen elements, up to 512 elements. It requires one match that supports a press and is visible at its centre. For text, use `--target 'focused field'` on an empty editable field. Request a press and a write in separate calls. [Notes preview](examples/notes-preview.sh) · [TextEdit demo](examples/textedit.sh)

| Result field | How to read it |
| --- | --- |
| `outcome` | For example, `target clicked`, `text typed`, `wrong window`, `sensitive`, or `timeout`. |
| `clicked`, `typed` | Confirmed control label and literal text, when available. Each call can confirm one mutation. |
| `confidence` | Runner-supplied confidence, or `null`. The exact-control adapter uses no classifier. |
| `steps` | Runner-reported count, zero before launch, or `null` when the runner cannot report it. |

Check the outcome before issuing another step. If a run times out, is interrupted, or reports unconfirmed input, the screen may already have changed.

## Safety and local data

- A normal `use` call checks the handoff locally on any supported OS. On macOS, `--arm` and `--act` both require a verified foreground before starting an executor. Input also requires local consent. The installed `jev-run` entry point uses the same supervisor.
- Executor-backed calls support TextEdit, Notes, Finder and Calendar by bundle identity. Browser input and armed browser previews are disabled. Even an exact URL gives no independent approval for its origin. This covers regional cloud portals, custom management domains and ordinary sites alike.
- The parent reads the foreground directly and checks the focused control and any named target before launching a process. It examines role, subrole, title, description and placeholder and refuses recognized banking, mail, messaging, password managers, student records, admin consoles and login or MFA. An unknown app or failed required read stops the call. Unicode normalization followed by conservative checks can also refuse ordinary localized labels. Accessibility metadata can omit private content.
- The bundled backend starts in an isolated Python interpreter using the installed environment. After reading a button’s metadata, it rechecks the foreground app, window ownership and centre hit-test before pressing. For a write, it checks that the same field is still focused and empty. macOS exposes each check and each input as separate operations. Focus, visibility or field content can still change in between.
- One 30-second deadline bounds the caller’s wait for the audit, foreground observation, process startup, execution and pipe cleanup. A startup helper must report ready and receive the supervisor’s grant within the deadline to start the executor. A stalled process constructor can finish later. Its helper receives no grant and is reaped when the OS returns. A slow initial audit prevents input. If the final record cannot be confirmed, the result says `audit indeterminate` and includes the prior `step_outcome` and step count. Check the app before retrying. Ctrl-C stops the supervised process group.
- Act attempts append a timestamp, random call ID, combined argument length, verified app bundle when available, and a fixed outcome code to `~/.config/jev-brain/audit.jsonl`. Request text, typed text, control labels, window titles, URLs and arbitrary executor output stay out of this file. The bundled runner stores no screenshots.
- To revoke consent, remove `~/.config/jev-brain/consent.json`. Programs with shell or file access under your user account share your permissions. Only connect agents and executors you trust. [Security details](SECURITY.md)

## MCP and older commands

Run `jev-brain-mcp` as a stdio MCP server. Its `use` tool runs locally without an HTTP service. For example, the TextEdit call’s arguments are:

```json
{"target":"focused field","request":"Write a greeting in the empty document","expect":"TextEdit","text":"Hello from jev-brain.","act":true}
```

Omit `act` for a preview. The tool returns JSON in its text content and in `structuredContent`. Run `jev-brain install` for a summary of proposed changes to Claude Code, Cursor and Codex. Add `--yes` to apply them. Existing jev-brain and legacy systemone entries keep their name and settings, including disabled states. The installer recognizes Python module invocations through literal `env` and `uv run` wrappers and ignores arguments owned by an identified Python script or module. An opaque launcher, inline Python command, or unrecognized wrapper option requires manual review of the named entry. A conflicting MCP policy also produces a diagnostic. The installer also declines TOML layouts that cannot accept a verified new table. Ensure the agent’s `PATH` includes the virtual environment’s `bin` directory so it can find both commands.

The old `systemone` and `systemone-mcp` commands forward to the new commands and print a deprecation line on stderr. Python code can import `jev_brain`. Existing `health` and `decide` clients can still use the local closed-question mock, started with `jev-brain serve --mock`. It returns fixed answers. Set `JEV_BRAIN_URL` to change its default loopback URL. `SYSTEMONE_URL` is the fallback.

## Credits and cost

Aaron Levin wrote **typesafe-computer-use**, the program whose macOS accessibility functions power the optional adapter. TypeSafe makes **Jev**, the classifier used by Aaron’s full computer-use loop. The core package is MIT-licensed and has no runtime dependencies. Both the local handoff preview and the included exact-control adapter work without model credentials. [Aaron Levin’s project](https://github.com/awlevin/typesafe-computer-use) · [MIT license](LICENSE)

For a model-backed executor, TypeSafe lists Jev 1.14 and 1.13 at **$0.042 per million input tokens**, with free output tokens. Aaron reports **about $0.0002 per step** and **about $0.003 for 12 steps**. His one-screenshot comparison measured 0.13–0.38 seconds and $0.0002 for the classifier, and 5.2 seconds and $0.032 for the frontier model. These are his measurements. [TypeSafe pricing](https://docs.typesafe.ai/models) · [Upstream benchmark](https://github.com/awlevin/typesafe-computer-use/blob/44ca11f0935b021b73020825da054b5c92cc1288/README.md#why-a-classifier)
