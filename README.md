# vibe-bridge

![vibe-bridge](docs/banner.png)

Pont Telegram <-> [vibe-acp](https://github.com/mistralai/mistral-vibe) :
pilote Mistral Vibe depuis Telegram via l'Agent Client Protocol (JSON-RPC 2.0
sur stdio), sans SDK intermédiaire — le client ACP est intégré
(`vibe_bridge/acp.py`, ~150 lignes).

## Fonctionnalités

### Entrées

- **Texte** : un message = un prompt (file d'attente si l'agent est occupé,
  un cycle à la fois)
- **Notes vocales et fichiers audio** : transcription via l'API Voxtral de
  Mistral (`MISTRAL_API_KEY` requise), le texte transcrit devient le prompt
- **Images** : photo Telegram ou image en pièce jointe -> bloc ACP
  `{type: "image", data: <base64>, mimeType}`, la légende sert de consigne
  (défaut : "Décris cette image.")

### Rendu

- Streaming de la réponse édité en place, découpe automatique à 3800 caractères
- Réflexion du modèle affichée en direct pendant les générations longues
  (`agent_thought_chunk`)
- Réponse finale en HTML riche (markdown converti côté pont, repli texte brut
  si Telegram refuse le rendu)
- Indicateur "typing…" pendant toute la durée d'un cycle

### Contrôle

- `/new` — nouvelle session (relance l'agent)
- `/resume` — sessions récentes en boutons (`session/list`), reprise par
  `session/load` au clic
- `/interrupt <consigne>` (alias `/i`) — annule le cycle en cours et enchaîne
  sur la même session : l'agent garde le contexte de son travail partiel
- `/mode` — mode de la session en boutons (ask / accept-edits / auto-approve)
- `/model` — modèle de la session en boutons
- `/session` — état courant — `/stop` — interrompre et vider la file
- `/doctor` — diagnostic complet : pont (uptime, erreurs, reprises timeout),
  token Telegram, clé Mistral, agent (session, mode, modèle), cycle, et
  RAM/swap/disque du conteneur. Répond même sans session active.

### Permissions

- Chaque demande d'autorisation de l'agent -> boutons inline
  (Allow once / session / Always / Deny), retour visuel immédiat au clic,
  auto-refus après 5 minutes
- **Garde-fou** : une permission reçoit toujours une réponse — en cas d'erreur
  du handler, `cancelled` est renvoyé (l'agent ne reste jamais suspendu)

### Robustesse

- Reprise automatique des timeouts de connexion Telegram (requête jamais
  partie = aucun risque de doublon)
- Limite stdio ACP à 32 Mio (le défaut asyncio de 64 Ko tuait le lecteur sur
  les gros messages) ; lecteur annulé **avant** la mort du process agent
  (l'ordre inverse figeait toute la boucle d'événements)
- Allowlist stricte : `TELEGRAM_ALLOWED_USER_IDS` obligatoire, le pont refuse
  de démarrer sans elle

## Configuration (variables d'environnement)

| Variable | Obligatoire | Défaut |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | oui | — |
| `TELEGRAM_ALLOWED_USER_IDS` | oui | — |
| `ACP_AGENT_COMMAND` | non | `vibe-acp` |
| `VIBE_BRIDGE_WORKSPACE` | non | `/home/vibe/workspace` |
| `VIBE_BRIDGE_TRANSCRIBE_MODEL` | non | `voxtral-mini-latest` |
| `VIBE_BRIDGE_STDIO_LIMIT` | non | `33554432` (octets) |
| `MISTRAL_API_KEY` | pour les notes vocales | — |
| `VIBE_BRIDGE_PROMPT_TIMEOUT` | non | `900` (secondes) |
| `VIBE_BRIDGE_LOG_LEVEL` | non | `INFO` |

## Identité et mémoire de l'agent (hors pont, côté vibe-acp)

- `~/.vibe/AGENTS.md` (global) : identité et ton — **l'AGENTS.md du workspace
  n'est pas chargé** en mode ACP (dossier non trusté), seul le global l'est
- `<workspace>/MEMORY.md` : faits durables lus et maintenus par l'agent
  (consigne dans l'AGENTS.md) — markdown simple, sans embeddings
- `~/.vibe/config.toml` : `default_agent = "ask"` pour les permissions sur
  chaque appel d'outil

## Installation (Linux, Python 3.11+)

```bash
git clone https://github.com/eowindel/vibe-bridge.git
cd vibe-bridge
uv venv
uv pip install --python .venv/bin/python -e .
```

## Service systemd

Exemple (adaptez utilisateur, chemins et fichiers d'environnement à votre
installation) :

```ini
[Unit]
Description=vibe-bridge : pont Telegram vers vibe-acp
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

[Install]
WantedBy=multi-user.target
```

Puis `systemctl daemon-reload && systemctl enable --now vibe-bridge`.

## Pièges ACP découverts en production

Chacun a coûté une session de debug — ils sont gérés par le pont, mais utiles
à connaître si vous écrivez votre propre client ACP :

- `session/cancel` doit être une **notification** (une requête avec id est
  rejetée par vibe-acp avec "method not found")
- `session/request_permission` envoie des options `optionId`/`name`
  (pas `id`/`option.text`), et un client doit **toujours** répondre — sinon
  l'agent reste suspendu indéfiniment
- `LoadSessionResponse` n'a pas de champ `sessionId` (l'ID passé dans la
  requête fait foi)
- httpx logge les URL d'API avec le token en INFO -> passer le logger en
  `WARNING`
- la limite stdio par défaut d'asyncio (64 Ko) est mortelle pour un pont ACP :
  un gros message agent tuait le lecteur en silence

## Origine du projet

Conçu pour un usage personnel mono-utilisateur : piloter son agent Vibe depuis
son téléphone, avec validation humaine des actions sensibles. Né de
l'expérience de `telegram-acp-bot` (dépendances sans bornes, réponse de l'agent
perdue sur un timeout réseau avec corruption du sender ACP côté SDK, session
tuée par un échange ACP trop volumineux) : ici, le client ACP est intégré et
minimaliste, la seule dépendance externe est `python-telegram-bot`.
