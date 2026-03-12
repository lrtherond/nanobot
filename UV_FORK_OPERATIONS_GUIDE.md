# Operating This Forked Nanobot with `uv`

This guide is for running the local fork in:

`/Users/laurent/workspaces/nanoagent/mqtt`

without publishing it or installing the PyPI release.

It uses `uv tool run --from ...` as the default workflow, which is a good fit
for a local fork because:

- it runs directly from the local checkout
- it does not require a global install first
- it naturally picks up local source changes on the next invocation

This guide was verified against `uv 0.8.12`.

## Reference Paths

Set a shell variable first:

```bash
export NANOBOT_FORK=/Users/laurent/workspaces/nanoagent/mqtt
```

If your clone lives elsewhere, change that path.

## Recommended Default: Run Directly from the Local Fork

Use `uv tool run --from "$NANOBOT_FORK"` for normal operation.

### Basic Sanity Check

```bash
uv tool run --from "$NANOBOT_FORK" nanobot --version
```

### First-Time Setup

Create or refresh the default config and workspace:

```bash
uv tool run --from "$NANOBOT_FORK" nanobot onboard
```

Show the current config, workspace, and provider status:

```bash
uv tool run --from "$NANOBOT_FORK" nanobot status
```

## Agent Commands

### One-Shot Chat

```bash
uv tool run --from "$NANOBOT_FORK" nanobot agent -m "Hello!"
```

### Interactive Chat

```bash
uv tool run --from "$NANOBOT_FORK" nanobot agent
```

Interactive mode exits with:

- `exit`
- `quit`
- `/exit`
- `/quit`
- `:q`
- `Ctrl+D`

### Chat with a Specific Config and Workspace

```bash
uv tool run --from "$NANOBOT_FORK" nanobot agent \
  -c ~/.nanobot/config.json \
  -w ~/.nanobot/workspace \
  -m "Hello from the fork"
```

### Agent with Runtime Logs

```bash
uv tool run --from "$NANOBOT_FORK" nanobot agent --logs -m "Show me diagnostics"
```

## Gateway Commands

### Start the Default Gateway

```bash
uv tool run --from "$NANOBOT_FORK" nanobot gateway
```

### Start with an Explicit Config

```bash
uv tool run --from "$NANOBOT_FORK" nanobot gateway \
  -c ~/.nanobot/config.json
```

### Start with an Explicit Config, Workspace, and Port

```bash
uv tool run --from "$NANOBOT_FORK" nanobot gateway \
  -c ~/.nanobot/config.json \
  -w ~/.nanobot/workspace \
  -p 18790
```

### Start with Verbose Logging

```bash
uv tool run --from "$NANOBOT_FORK" nanobot gateway \
  -c ~/.nanobot/config.json \
  --verbose
```

## MQTT-Enabled Runs

This fork's MQTT channel depends on `aiomqtt`.

If the config you are running enables `channels.mqtt`, add `--with aiomqtt` to
the `uv tool run` invocation.

### MQTT-Capable Gateway

```bash
uv tool run --from "$NANOBOT_FORK" --with aiomqtt nanobot gateway \
  -c ~/.nanobot/config.json
```

### MQTT-Capable One-Shot Agent

Use this form if you want the local tool environment to include the MQTT
dependency while exercising a config that enables MQTT:

```bash
uv tool run --from "$NANOBOT_FORK" --with aiomqtt nanobot agent \
  -c ~/.nanobot/config.json \
  -m "Hello through the MQTT-enabled fork"
```

### Important Note About First Runs

The first `uv` run may need network access to resolve packages that are not yet
in the local `uv` cache.

After the environment has been created, repeated invocations are much faster
because `uv` reuses the cached tool environment.

## Channel and Provider Lifecycle Commands

### Show Channel Status

```bash
uv tool run --from "$NANOBOT_FORK" nanobot channels status
```

### WhatsApp Login

This requires Node.js and `npm`.

```bash
uv tool run --from "$NANOBOT_FORK" nanobot channels login
```

### Provider Login

OpenAI Codex:

```bash
uv tool run --from "$NANOBOT_FORK" nanobot provider login openai-codex
```

GitHub Copilot:

```bash
uv tool run --from "$NANOBOT_FORK" nanobot provider login github-copilot
```

## Multi-Instance Operations

If you run multiple nanobot instances, use different config files and, when
needed, different workspaces.

### Example: Separate Telegram and MQTT Instances

```bash
uv tool run --from "$NANOBOT_FORK" nanobot gateway \
  -c ~/.nanobot-telegram/config.json
```

```bash
uv tool run --from "$NANOBOT_FORK" --with aiomqtt nanobot gateway \
  -c ~/.nanobot-mqtt/config.json \
  -p 18791
```

If you want isolated memory and sessions, also set a distinct workspace:

```bash
uv tool run --from "$NANOBOT_FORK" --with aiomqtt nanobot gateway \
  -c ~/.nanobot-mqtt/config.json \
  -w ~/.nanobot-mqtt/workspace \
  -p 18791
```

## Updating the Fork

If you are using `uv tool run --from "$NANOBOT_FORK"`, updating the fork is
simple:

```bash
git -C "$NANOBOT_FORK" pull --ff-only
```

Then rerun the same `uv tool run --from ...` command. No separate reinstall is
required.

If the fork changes its dependencies, the next `uv` run will refresh the tool
environment as needed.

## Optional: Persistent Editable Install

If you want a stable `nanobot` command on your `PATH`, install the fork as a
persistent editable `uv` tool.

### Install the Fork Persistently

```bash
uv tool install --editable "$NANOBOT_FORK"
```

### Install the Fork Persistently with MQTT Support

```bash
uv tool install --editable --with aiomqtt "$NANOBOT_FORK"
```

If `uv` warns that its tool bin directory is not on your `PATH`, run:

```bash
uv tool update-shell
```

Then you can use:

```bash
nanobot --version
nanobot status
nanobot agent -m "Hello!"
nanobot gateway -c ~/.nanobot/config.json
```

Because the install is editable, source code changes in the local fork are
picked up without reinstalling the package itself.

If you change dependencies, reinstall forcefully:

```bash
uv tool install --editable --with aiomqtt --force "$NANOBOT_FORK"
```

### Remove the Persistent Install

```bash
uv tool uninstall nanobot-ai
```

## Which Workflow to Choose

Use `uv tool run --from "$NANOBOT_FORK"` when:

- you are actively developing the fork
- you want the least setup
- you are fine prefixing commands with `uv tool run --from ...`

Use `uv tool install --editable "$NANOBOT_FORK"` when:

- you want a plain `nanobot` command on your `PATH`
- you are using this fork as your normal daily driver
- you want to integrate it with other local tooling more easily
