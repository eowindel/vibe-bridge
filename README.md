# vibe-bridge

Pont Telegram <-> [vibe-acp](https://github.com/mistralai/mistral-vibe) :
pilote Mistral Vibe depuis Telegram via l'Agent Client Protocol (JSON-RPC 2.0
sur stdio), sans SDK intermédiaire — le client ACP est intégré (`vibe_bridge/acp.py`).

## Fonctionnalités

- Une session ACP par chat, prompts en file (un à la fois)
- Streaming de la réponse édité en place, découpe automatique à 3800 caractères
- Demandes de permission de l'agent -> boutons inline (Autoriser / Annuler),
  auto-refus après 5 minutes
- Reprise automatique des timeouts de connexion Telegram (requête jamais
  partie = aucun risque de doublon)
- Allowlist stricte : `TELEGRAM_ALLOWED_USER_IDS` obligatoire
- Commandes : `/new`, `/session`, `/stop`, `/start`

## Configuration (variables d'environnement)

| Variable | Obligatoire | Défaut |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | oui | — |
| `TELEGRAM_ALLOWED_USER_IDS` | oui | — |
| `ACP_AGENT_COMMAND` | non | `vibe-acp` |
| `VIBE_BRIDGE_WORKSPACE` | non | `/home/vibe/workspace` |
| `VIBE_BRIDGE_PROMPT_TIMEOUT` | non | `900` (secondes) |
| `VIBE_BRIDGE_LOG_LEVEL` | non | `INFO` |

## Installation (CT vibe-svc, Debian 13)

```bash
git clone <repo> /home/vibe/vibe-bridge
cd /home/vibe/vibe-bridge
uv venv
uv pip install --python .venv/bin/python -e .
```

## Service systemd

`/etc/systemd/system/vibe-bridge.service` :

```ini
[Unit]
Description=vibe-bridge : pont Telegram vers vibe-acp (CT 121)
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
ExecStart=/home/vibe/vibe-bridge/.venv/bin/vibe-bridge
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Puis `systemctl daemon-reload && systemctl enable --now vibe-bridge`.

## Contexte

Développé pour l'homelab d'Arno (CT 121 `vibe-svc`, Proxmox). Remplace
`telegram-acp-bot` (trois bugs bloquants en une soirée : dépendances sans
bornes, réponse perdue sur timeout réseau + corruption du sender ACP,
session tuée par un échange ACP trop volumineux). Historique complet dans
`PLAN-vibe-svc.md` du workspace.
