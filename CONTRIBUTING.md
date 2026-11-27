# Contributing

Keep the caller in charge of each sub-goal. The default installation uses the Python standard library. The optional macOS runner integrates with Aaron Levin’s upstream package.

Run these checks from the clone:

```sh
python -m unittest discover -s tests
python -m compileall -q src runner tests
```

Exercise the shipped CLI and MCP entry points. Use a temporary home, a fake foreground window, and a recording `jev-run` on an isolated `PATH` for act-mode tests. Tests must leave the desktop alone. Include regression coverage for changes to the gate or the executor. Keep credentials and user data out of fixtures and logs.

The code is available under the [MIT license](LICENSE).
