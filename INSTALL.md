# Installing vibe-bridge

A complete setup, from a fresh Linux host to a running Telegram bot.
If you use an AI coding agent, you can skip to the
[one-prompt install](#one-prompt-install-with-your-ai-agent) — it drives
everything for you.

## Prerequisites

| Requirement | How to get it |
|---|---|
| Linux host (or container), Python 3.11+ | `python3 --version` |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **vibe-acp** (the agent this bridge drives) | `uv tool install mistral-vibe` → binary `vibe-acp` |
| **Mistral API key** | https://console.mistral.ai → API keys |
| **Telegram bot token** | chat with [@BotFather](https://t.me/botfather) → `/newbot` → copy the token |
| **Your Telegram numeric user ID** | chat with [@userinfobot](https://t.me/userinfobot) — it replies with your ID |

> The bot token and your user ID are what makes the bot **yours**: the bridge
> refuses to start without an allowlist, and only allowlisted users can talk
> to it.

## Manual install

### 1. Get the code and install

```bash
git clone https://github.com/eowindel/vibe-bridge.git
cd vibe-bridge
uv venv
uv pip install --python .venv/bin/python -e .
```

### 2. Environment files

Create a dedicated user for the service if you want one (the examples use
`vibe` with `/home/vibe`). All files below should be readable only by that
user (`chmod 600`).

`~/.vibe/.env` — the agent's own key (also used by voice notes):

```
MISTRAL_API_KEY=your-mistral-api-key
```

`bot.token.env` — the secrets that identify the bot and its owner:

```
TELEGRAM_BOT_TOKEN=123456789:AA-your-botfather-token
TELEGRAM_ALLOWED_USER_IDS=123456789
```

`bot.env` — the bridge configuration:

```
ACP_AGENT_COMMAND=/home/vibe/.local/bin/vibe-acp
# Optional — enable the HTTP automation endpoint:
# VIBE_BRIDGE_HTTP_TOKEN=generate-a-random-token
# VIBE_BRIDGE_HTTP_ALLOWED_IPS=127.0.0.1
```

All optional variables are in the
[README configuration table](README.md#configuration-environment-variables).

### 3. systemd service

Adapt user, paths and env files to your setup, then
`systemctl daemon-reload && systemctl enable --now vibe-bridge`:

```ini
[Unit]
Description=vibe-bridge : Telegram gateway to vibe-acp
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=vibe
Group=vibe
WorkingDirectory=/home/vibe
Environment=HOME=/home/vibe
EnvironmentFile=/home/vibe/bot.env
EnvironmentFile=/home/vibe/bot.token.env
EnvironmentFile=/home/vibe/.vibe/.env
ExecStart=/home/vibe/vibe-bridge/.venv/bin/vibe-bridge
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

### 4. Verify

1. `systemctl status vibe-bridge` — active, logs clean (`journalctl -u vibe-bridge -f`)
2. Send `/start` to your bot on Telegram — it should answer with the help
3. Send `/doctor` — full diagnostics of the bridge, the agent and the host

### 5. (Optional) Give the agent an identity

`vibe-acp` reads a global `~/.vibe/AGENTS.md` — identity, tone, rules — loaded
into every session (the workspace one is **not** loaded in ACP mode). A small
`MEMORY.md` in the working directory, maintained by the agent itself, gives it
durable memory. See the [README](README.md#agent-identity-and-memory-outside-the-bridge-on-the-vibe-acp-side).

## One-prompt install (with your AI agent)

Copy-paste this into any capable coding agent (Claude Code, Cursor, Copilot,
Mistral Vibe…) running **on the target machine** — it will drive the whole
installation:

```text
Install the "vibe-bridge" project (https://github.com/eowindel/vibe-bridge) on
this machine and set it up as a systemd service. It is a Telegram bridge for
Mistral Vibe over the Agent Client Protocol.

First read the repository's README.md and INSTALL.md, then:

1. Check and install the prerequisites if missing: Python 3.11+, uv, and
   vibe-acp (uv tool install mistral-vibe). Verify each one.
2. Clone the repository, create a uv venv, install the package editable with
   its dependencies.
3. Ask me interactively for these values — never guess or invent them:
   - my Mistral API key (console.mistral.ai)
   - a Telegram bot token (I will create one with @BotFather if I don't have one)
   - my Telegram numeric user ID (@userinfobot can tell me)
   - whether I want the optional HTTP automation endpoint (POST /prompt)
4. Write the environment files described in INSTALL.md, with chmod 600, in
   the locations your systemd unit will reference:
   the agent .env (MISTRAL_API_KEY), bot.token.env (TELEGRAM_BOT_TOKEN,
   TELEGRAM_ALLOWED_USER_IDS), bot.env (ACP_AGENT_COMMAND and, if I opted in,
   the HTTP token and allowed IPs).
5. Create the systemd unit from INSTALL.md with the right user, paths and
   EnvironmentFile lines; daemon-reload, enable and start it.
6. Verify: service active, journal clean, and tell me to send /doctor to my
   bot on Telegram. Ask me to confirm the bot answered.
7. Report back: service status, files created, and anything left to do.

Do not print or log the tokens anywhere. If a step fails, show me the error
and propose a fix before continuing.
```
