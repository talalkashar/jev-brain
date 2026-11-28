# Three things to try

Run these from the checkout. For commands that start with `systemone`, activate the environment from the [install instructions](../README.md#install-the-cli).

## 1. Inspect a proposed handoff

An agent proposes counting the windows in Notes. Pass the same text your agent would send and inspect the JSON before integrating the command.

```sh
systemone use --target Notes --request "count the windows"
```

Exit 0 with `"started": false` means the preview succeeded and the runner stayed idle. You can try this on a machine without Notes.

## 2. Check your caller's refusal path

```sh
systemone use --target Mail --request "count the windows"
```

Expect exit 2, an empty stdout and `refusing a sensitive surface: mail` on stderr. Handle the refusal before parsing stdout. To exercise both cases together, run `python3 examples/preview.py` from the checkout.

## 3. Test a queue integration with a fixed answer

Start the local mock in one terminal. Leave it running while you try the request.

```sh
systemone serve --mock
```

From the checkout in a second terminal, send the [sample build failure](queue.json).

```sh
curl --fail --silent --show-error -H 'Content-Type: application/json' \
  --data-binary @examples/queue.json http://127.0.0.1:18100/decide
```

Look for `"model": "mock"` and the label `ci`. The mock always picks the first choice. To send the same question with the installed CLI, activate the virtual environment in that terminal and run the following command.

```sh
systemone decide --state "The build failed on main" \
  --question "route:choice:Which queue should take this?" \
  --criteria ci="Build, test, and deploy failures" \
  --criteria product="User-facing defects"
```

Stop the server with Ctrl-C. If the port is occupied, choose another with `serve --mock --port 18101` and update the URL. The CLI accepts `--url http://127.0.0.1:18101` after `decide`.

### Other question shapes

The HTTP body contains `state` (a string or null) and a `questions` object keyed by names you choose. Each question has `type`, `instructions` and optional `criteria`.

| Type | Criteria | Fixed mock result |
| --- | --- | --- |
| `choice` | Up to 64 label/description pairs | First label |
| `score` | Ordered list of up to 16 labels | Middle index and label, rounded down |
| `noul` | Omitted or null | Probability 0.5 |

Send at most 32 questions, 2,000 characters per instruction and 100,000 characters of state. `GET /health` reports whether the mock is running.
