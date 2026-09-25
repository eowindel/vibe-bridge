"""vibe-bridge : pont Telegram <-> vibe-acp (ACP sur stdio).

Mono-utilisateur : allowlist TELEGRAM_ALLOWED_USER_IDS obligatoire.
Une session ACP par chat autorisé, prompts traités un à la fois (file).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import time
from typing import Any

import httpx
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import __version__
from .acp import AcpAgent, AcpError
from .formatting import md_to_html, split_message

log = logging.getLogger("vibe_bridge")

EDIT_MIN_INTERVAL = 1.0    # secondes entre éditions du message de statut
PERMISSION_TIMEOUT = 300.0  # secondes avant auto-refus d'une permission
PROMPT_TIMEOUT = float(os.environ.get("VIBE_BRIDGE_PROMPT_TIMEOUT", "900"))
WORKSPACE = os.environ.get("VIBE_BRIDGE_WORKSPACE", "/home/vibe/workspace")
AGENT_COMMAND = os.environ.get("ACP_AGENT_COMMAND", "vibe-acp")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
TRANSCRIBE_MODEL = os.environ.get("VIBE_BRIDGE_TRANSCRIBE_MODEL", "voxtral-mini-latest")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_IDS = {
    int(x)
    for x in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").replace(",", " ").split()
    if x
}


LANG_FILE = os.path.expanduser("~/.vibe-bridge.lang")


def _load_lang() -> str:
    """VIBE_BRIDGE_LANG, ecrasee par le dernier choix /language (fichier)."""
    lang = os.environ.get("VIBE_BRIDGE_LANG", "fr")
    try:
        with open(LANG_FILE, encoding="ascii") as f:
            saved = f.read().strip()
        if saved in ("fr", "en"):
            lang = saved
    except OSError:
        pass
    return lang


def set_lang(lang: str) -> None:
    """Change la langue du pont et la persiste pour les redemarrages."""
    global LANG
    LANG = lang
    try:
        with open(LANG_FILE, "w", encoding="ascii") as f:
            f.write(lang)
    except OSError:
        log.warning("impossible de persister la langue : %s", LANG_FILE)


LANG = _load_lang()


def L(fr: str, en: str) -> str:
    """Message visible cote Telegram, selon la langue du pont."""
    return en if LANG == "en" else fr


# Statistiques internes pour /doctor.
STATS = {
    "started_at": time.time(),
    "tg_retries": 0,
    "tg_last_ok": 0.0,
    "mistral_last_ok": 0.0,
    "errors": 0,
}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


async def tg_call(fn: Any, *args: Any, retries: int = 3, **kwargs: Any) -> Any:
    """Appel API Telegram avec reprise sur timeout de connexion.

    On ne reprend que si la requête n'a jamais été envoyée
    (httpx.ConnectTimeout / PoolTimeout) : aucun risque de doublon.
    Leçon du 25/09 : un timeout de connexion ne doit rien perdre.
    """
    for attempt in range(retries + 1):
        try:
            res = await fn(*args, **kwargs)
            STATS["tg_last_ok"] = time.time()
            return res
        except TimedOut as err:
            cause = err.__cause__
            if attempt < retries and isinstance(
                cause, (httpx.ConnectTimeout, httpx.PoolTimeout)
            ):
                STATS["tg_retries"] += 1
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise


async def transcribe_audio(data: bytes, filename: str) -> str:
    """Transcription d'un audio via l'API Voxtral de Mistral."""
    if not MISTRAL_API_KEY:
        raise RuntimeError(L("MISTRAL_API_KEY absente du pont", "MISTRAL_API_KEY missing from the bridge"))
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.mistral.ai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"},
            data={"model": TRANSCRIBE_MODEL},
            files={"file": (filename, data, "audio/ogg")},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"API transcription {resp.status_code} : {resp.text[:200]}")
    text = (resp.json().get("text") or "").strip()
    STATS["mistral_last_ok"] = time.time()
    return text


class ChatSession:
    """Une session ACP + sa file de prompts, pour un chat autorisé."""

    def __init__(self, bot: Any, chat_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.agent: AcpAgent | None = None
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.worker: asyncio.Task[None] | None = None
        self.status_msg_id: int | None = None
        self.activity: str = ""
        self.thought: str = ""
        self.text: str = ""
        self.last_edit: float = 0.0
        self.busy = False
        self.perm_future: asyncio.Future[str | None] | None = None
        self.redirect_pending = False

    # -- telegram ------------------------------------------------------------

    async def send(self, text: str, reply_markup: Any = None) -> int | None:
        try:
            msg = await tg_call(
                self.bot.send_message,
                chat_id=self.chat_id, text=text[:4000], reply_markup=reply_markup,
            )
            return msg.message_id
        except TelegramError:
            log.exception("send_message a échoué")
            return None

    # -- rendu du cycle --------------------------------------------------------

    async def refresh_status(self) -> None:
        """Édition en place du message de statut, throttlée."""
        now = time.monotonic()
        if now - self.last_edit < EDIT_MIN_INTERVAL:
            return
        self.last_edit = now
        if self.text:
            body = self.text[-4000:]
        elif self.activity:
            body = f"⚙ {self.activity}"[:4000]
        else:
            body = "…"
        if self.status_msg_id is None:
            self.status_msg_id = await self.send(body)
            return
        with contextlib.suppress(TelegramError):
            await tg_call(
                self.bot.edit_message_text,
                chat_id=self.chat_id, message_id=self.status_msg_id, text=body,
            )

    async def _deliver(self, text: str, edit_status: bool = False) -> None:
        """Livraison riche : HTML d'abord, repli texte brut si Telegram refuse.
        edit_status=True remplace le message de statut courant au lieu d'envoyer."""
        html_text = md_to_html(text)[:4000]
        plain_text = text[:4000]
        if edit_status and self.status_msg_id is not None:
            mid = self.status_msg_id
            self.status_msg_id = None
            try:
                await tg_call(
                    self.bot.edit_message_text, chat_id=self.chat_id,
                    message_id=mid, text=html_text, parse_mode="HTML",
                )
                return
            except BadRequest:
                pass
            try:
                await tg_call(
                    self.bot.edit_message_text, chat_id=self.chat_id,
                    message_id=mid, text=plain_text,
                )
                return
            except TelegramError:
                pass  # édition impossible : envoyer en message neuf
        try:
            await tg_call(
                self.bot.send_message, chat_id=self.chat_id,
                text=html_text, parse_mode="HTML",
            )
        except BadRequest:
            await self.send(plain_text)

    async def finalize(self) -> None:
        """Dernier rendu : la réponse remplace le statut, découpée si longue."""
        parts = split_message(self.text)
        if parts == [""]:
            parts = []  # réponse vide : ne jamais éditer avec un texte vide
        if not parts:
            text = L("(fin de cycle, pas de réponse texte)", "(cycle ended, no text reply)")
            if self.status_msg_id is not None:
                mid = self.status_msg_id
                self.status_msg_id = None
                with contextlib.suppress(TelegramError):
                    await tg_call(
                        self.bot.edit_message_text, chat_id=self.chat_id,
                        message_id=mid, text=text,
                    )
            else:
                await self.send(text)
            return
        for i, part in enumerate(parts):
            await self._deliver(part, edit_status=(i == 0))

    # -- callbacks ACP ----------------------------------------------------------

    async def handle_update(self, update: dict) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text":
                self.text += content.get("text", "")
                await self.refresh_status()
        elif kind == "agent_thought_chunk":
            # Aperçu de la réflexion du modèle : preuve de vie pendant les
            # longues générations (derniers ~200 caractères accumulés).
            content = update.get("content", {})
            if content.get("type") == "text" and content.get("text"):
                self.thought = (self.thought + content["text"])[-200:]
                self.activity = "💭 " + self.thought.replace("\n", " ").strip()[-90:]
                await self.refresh_status()
        elif kind in ("tool_call", "tool_call_update"):
            title = update.get("title") or update.get("kind")
            if title:
                self.activity = str(title)
                self.thought = ""
                await self.refresh_status()
        # plan, available_commands_update, etc. : ignorés en v1

    async def handle_permission(self, params: dict) -> str | None:
        """Requête session/request_permission -> boutons inline."""
        options = params.get("options", [])
        if not options:
            return None
        buttons = []
        for opt in options:
            option_id = opt.get("optionId") or opt.get("id")
            label = opt.get("name") or opt.get("optionId") or "?"
            buttons.append(
                [InlineKeyboardButton(label[:60], callback_data=f"p:{option_id}")]
            )
        buttons.append(
            [InlineKeyboardButton(L("Annuler", "Cancel"), callback_data="p:__cancel__")]
        )
        label = self.activity or L("Action de l'agent", "Agent action")
        msg_id = await self.send(
            L(f"🔐 {label}\nAutoriser ?", f"🔐 {label}\nAllow?"),
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        if msg_id is None:
            # Impossible d'afficher les boutons : refuser proprement.
            return None
        self.perm_future = asyncio.get_running_loop().create_future()
        try:
            return await asyncio.wait_for(self.perm_future, PERMISSION_TIMEOUT)
        except asyncio.TimeoutError:
            with contextlib.suppress(TelegramError):
                await tg_call(
                    self.bot.edit_message_text,
                    chat_id=self.chat_id, message_id=msg_id,
                    text=L("🔐 délai dépassé — refusé", "🔐 timed out — denied"),
                )
            return None
        finally:
            self.perm_future = None

    # -- cycle de vie ------------------------------------------------------------

    async def ensure_session(self) -> None:
        if self.agent and not self.agent.dead and self.worker and not self.worker.done():
            return
        await self._stop_everything()
        await self.start_session()

    async def renew(self) -> None:
        await self._stop_everything()
        await self.start_session()

    async def resume_session(self, session_id: str) -> None:
        """Reprend une session passée : remplace la session courante."""
        await self._stop_everything()
        try:
            await self.start_session(load_session_id=session_id)
        except Exception:
            return

    async def _stop_everything(self) -> None:
        if self.agent:
            with contextlib.suppress(Exception):
                await self.agent.cancel()
        await self.stop_worker()
        if self.agent:
            await self.agent.stop()
        self.agent = None

    async def stop_worker(self) -> None:
        if self.worker and not self.worker.done():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.put_nowait(None)
            try:
                await asyncio.wait_for(asyncio.shield(self.worker), 10)
            except (asyncio.TimeoutError, Exception):
                self.worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.worker

    async def start_session(self, load_session_id: str | None = None) -> None:
        agent = AcpAgent(AGENT_COMMAND, WORKSPACE)
        agent.on_update = self.handle_update
        agent.on_permission = self.handle_permission
        try:
            await agent.start(load_session_id=load_session_id)
        except Exception as e:
            await self.send(L(f"[impossible de démarrer l'agent : {e}]", f"[cannot start the agent: {e}]"))
            raise
        self.agent = agent
        self.queue = asyncio.Queue()
        self.worker = asyncio.create_task(self.run_worker())
        label = L("↩️ session reprise", "↩️ session resumed") if load_session_id else L("🟢 session ouverte", "🟢 session opened")
        await self.send(f"{label} ({str(agent.session_id)[:8]}…)")

    async def _keep_typing(self) -> None:
        """Indicateur 'typing…' en haut du chat, renouvelé pendant le cycle
        (l'action Telegram n'affiche que ~5 s par envoi)."""
        while True:
            with contextlib.suppress(TelegramError):
                await tg_call(
                    self.bot.send_chat_action,
                    chat_id=self.chat_id, action=ChatAction.TYPING,
                )
            await asyncio.sleep(4.0)

    async def run_worker(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            self.busy = True
            self.text = ""
            self.thought = ""
            self.activity = ""
            self.status_msg_id = None
            self.last_edit = 0.0
            agent = self.agent
            if agent is None:
                self.busy = False
                return
            try:
                typing_task = asyncio.create_task(self._keep_typing())
                result = await agent.prompt(item, timeout=PROMPT_TIMEOUT)
                await self.finalize()
                stop = result.get("stopReason")
                if stop == "cancelled" and self.redirect_pending:
                    self.redirect_pending = False
                    await self.send(L("↪️ interrompu — nouvelle consigne en cours…", "↪️ interrupted — new instruction in progress…"))
                elif stop and stop != "end_turn":
                    await self.send(L(f"[cycle terminé : {stop}]", f"[cycle ended: {stop}]"))
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    await agent.cancel()
                await self.finalize()
                await self.send(L("[délai dépassé, cycle annulé]", "[timed out, cycle cancelled]"))
            except AcpError as e:
                self.agent = None
                await self.finalize()
                await self.send(L(
                    f"[erreur agent : {e}]\n"
                    "Session arrêtée — renvoie un message pour en ouvrir une nouvelle.",
                    f"[agent error: {e}]\n"
                    "Session stopped — send a message to open a new one."
                ))
            except Exception:
                log.exception("cycle en échec")
                await self.finalize()
                await self.send(L("[erreur inattendue, voir les logs]", "[unexpected error, see logs]"))
            finally:
                typing_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await typing_task
                self.busy = False

    async def interrupt_and_redirect(self, text: str) -> None:
        """/interrupt : annule le cycle courant, la consigne enchaîne
        sur la même session — l'agent garde le contexte de son travail
        partiel (le tour annulé reste dans son historique)."""
        if self.busy and self.agent:
            self.redirect_pending = True
            with contextlib.suppress(Exception):
                await self.agent.cancel()
            self.queue.put_nowait([text_block(text)])  # traité dès la fin du cycle annulé
        else:
            self.queue.put_nowait([text_block(text)])

    async def interrupt(self) -> int:
        """/stop : annule le cycle courant et vide la file."""
        drained = 0
        while True:
            try:
                self.queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break
        if self.agent:
            with contextlib.suppress(Exception):
                await self.agent.cancel()
        return drained


# -- handlers Telegram --------------------------------------------------------


def session_for(update: Update, context: ContextTypes.DEFAULT_TYPE) -> ChatSession | None:
    user = update.effective_user
    if user is None or user.id not in ALLOWED_USER_IDS:
        log.warning("utilisateur non autorisé ignoré : %s", user.id if user else "?")
        return None
    chat = update.effective_chat
    if chat is None:
        return None
    sessions: dict[int, ChatSession] = context.bot_data.setdefault("sessions", {})
    if chat.id not in sessions:
        sessions[chat.id] = ChatSession(context.bot, chat.id)
    return sessions[chat.id]


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if session_for(update, context) is None:
        return
    await tg_call(
        context.bot.send_message,
        chat_id=update.effective_chat.id,
        text=L(
            "vibe-bridge — pilotage de Vibe par Telegram\n\n"
            "Un message = un prompt (file d'attente si l'agent est occupé).\n"
            "Une note vocale = transcription puis prompt.\n"
            "Une image = analysée (sa légende sert de consigne).\n\n"
            "/new — nouvelle session\n"
            "/doctor — diagnostic du pont\n"
            "/session — état de la session\n"
            "/stop — interrompre le cycle et vider la file\n"
            "/interrupt <consigne> — arrêter le cycle en cours et le remplacer "
            "(alias /i)\n"
            "/language — langue du pont\n",
            "vibe-bridge — drive Vibe from Telegram\n\n"
            "One message = one prompt (queued if the agent is busy).\n"
            "A voice note is transcribed then sent as a prompt.\n"
            "An image is analyzed (its caption is the instruction).\n\n"
            "/new — new session\n"
            "/doctor — bridge diagnostics\n"
            "/session — session state\n"
            "/stop — interrupt the cycle and clear the queue\n"
            "/interrupt <instruction> — stop the current cycle and replace it "
            "(alias /i)\n"
            "/language — bridge language\n",
        ),
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update, context)
    if s is None:
        return
    await s.renew()


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update, context)
    if s is None:
        return
    if not s.agent:
        await s.send(L("aucune session ouverte", "no open session"))
        return
    state = L("occupé", "busy") if s.busy else L("inactif", "idle")
    await s.send(L(
        f"session {s.agent.session_id}\n"
        f"état : {state}\n"
        f"file d'attente : {s.queue.qsize()}",
        f"session {s.agent.session_id}\n"
        f"state: {state}\n"
        f"queue: {s.queue.qsize()}"
    ))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update, context)
    if s is None:
        return
    drained = await s.interrupt()
    await s.send(L(f"[cycle interrompu, {drained} message(s) retirés de la file]", f"[cycle interrupted, {drained} message(s) removed from the queue]"))


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/resume : liste les sessions récentes en boutons, reprise au clic."""
    s = session_for(update, context)
    if s is None:
        return
    try:
        await s.ensure_session()
    except Exception:
        return
    try:
        sessions = await s.agent.list_sessions()
    except Exception as e:
        await s.send(L(f"[impossible de lister les sessions : {e}]", f"[cannot list sessions: {e}]"))
        return
    current = s.agent.session_id if s.agent else None
    entries = [e for e in sessions if e.get("sessionId") != current][:8]
    if not entries:
        await s.send(L("aucune autre session à reprendre", "no other session to resume"))
        return
    buttons = []
    for e in entries:
        title = (e.get("title") or e.get("sessionId", "?"))[:48]
        date = (e.get("updatedAt") or "?")[:10]
        buttons.append([InlineKeyboardButton(
            f"{title} — {date}",
            callback_data=f"r:{e['sessionId']}",
        )])
    buttons.append([InlineKeyboardButton(L("Annuler", "Cancel"), callback_data="r:__cancel__")])
    await s.send(
        L(
            "Sessions récentes — laquelle reprendre ?\n"
            "(la reprise remplace la session courante)",
            "Recent sessions — which one to resume?\n"
            "(resuming replaces the current session)",
        ),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _read_meminfo() -> tuple[int, int, int]:
    """(ram utilisée Mio, ram totale Mio, swap utilisé Mio)."""
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                k, _, v = line.partition(":")
                if v:
                    info[k] = int(v.strip().split()[0]) // 1024
    except OSError:
        return -1, -1, -1
    swap = info.get("SwapTotal", 0) - info.get("SwapFree", 0)
    used = info.get("MemTotal", 0) - info.get("MemAvailable", 0)
    return used, info.get("MemTotal", 0), swap


def _fmt_age(ts: float) -> str:
    if not ts:
        return L("jamais", "never")
    d = time.time() - ts
    if d < 60:
        return f"{int(d)} s"
    if d < 3600:
        return f"{int(d / 60)} min"
    return f"{int(d / 3600)} h"


async def cmd_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/doctor : diagnostic du pont, de l'agent et du conteneur."""
    s = session_for(update, context)
    if s is None:
        return
    try:
        me = await tg_call(context.bot.get_me)
        tg_line = f"token OK (@{me.username})"
    except Exception as e:
        tg_line = L(f"[erreur : {e}]", f"[error: {e}]")
    if s.agent and not s.agent.dead:
        mode = s.agent.modes.get("currentModeId") or "?"
        opt = next((o for o in s.agent.config_options
                    if o.get("id") == "model"), {})
        model = opt.get("currentValue") or "?"
        agent_line = (L(f"vivant — session {str(s.agent.session_id)[:8]}… — "
                       f"mode {mode} — modèle {model}",
                       f"alive — session {str(s.agent.session_id)[:8]}… — "
                       f"mode {mode} — model {model}"))
    else:
        agent_line = L("aucun agent actif", "no active agent")
    used, total, swap = _read_meminfo()
    ram_line = L(f"{used}/{total} Mio", f"{used}/{total} MiB") if total > 0 else "?"
    try:
        st = os.statvfs("/")
        disk_total = st.f_blocks * st.f_frsize / 2**30
        disk_free = st.f_bavail * st.f_frsize / 2**30
        disk_line = L(f"{disk_total - disk_free:.1f}/{disk_total:.0f} Go", f"{disk_total - disk_free:.1f}/{disk_total:.0f} GiB")
    except OSError:
        disk_line = "?"
    uptime = time.time() - STATS["started_at"]
    h, m = int(uptime // 3600), int(uptime % 3600 // 60)
    text = L(
        f"🩺 vibe-bridge {__version__} — diagnostic\n"
        f"├─ pont : actif depuis {h}h{m:02d} — erreurs : {STATS['errors']}\n"
        f"├─ Telegram : {tg_line} — reprises timeout : {STATS['tg_retries']}\n"
        f"├─ Mistral : clé {'présente' if MISTRAL_API_KEY else 'ABSENTE'} — "
        f"dernier appel pont : {_fmt_age(STATS['mistral_last_ok'])}\n"
        f"├─ agent : {agent_line}\n"
        f"├─ cycle : {'occupé' if s.busy else 'inactif'} — file : {s.queue.qsize()}\n"
        f"└─ CT : RAM {ram_line} — swap {swap} Mio — disque {disk_line}",
        f"🩺 vibe-bridge {__version__} — diagnostics\n"
        f"├─ bridge: up for {h}h{m:02d} — errors: {STATS['errors']}\n"
        f"├─ Telegram: {tg_line} — timeout retries: {STATS['tg_retries']}\n"
        f"├─ Mistral: key {'present' if MISTRAL_API_KEY else 'MISSING'} — "
        f"last bridge call: {_fmt_age(STATS['mistral_last_ok'])}\n"
        f"├─ agent: {agent_line}\n"
        f"├─ cycle: {'busy' if s.busy else 'idle'} — queue: {s.queue.qsize()}\n"
        f"└─ CT: RAM {ram_line} — swap {swap} MiB — disk {disk_line}"
    )
    await tg_call(context.bot.send_message,
                  chat_id=update.effective_chat.id, text=text)


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mode : change le mode de la session (ask / accept-edits / auto-approve)."""
    s = session_for(update, context)
    if s is None:
        return
    try:
        await s.ensure_session()
    except Exception:
        return
    agent = s.agent
    if not agent or not agent.modes.get("availableModes"):
        await s.send(L("aucun mode disponible", "no modes available"))
        return
    current = agent.modes.get("currentModeId")
    buttons = []
    for m in agent.modes["availableModes"]:
        mark = "● " if m.get("id") == current else "○ "
        buttons.append([InlineKeyboardButton(
            f"{mark}{m.get('name') or m['id']}", callback_data=f"m:{m['id']}")])
    buttons.append([InlineKeyboardButton(L("Annuler", "Cancel"), callback_data="m:__cancel__")])
    await s.send(L("Mode de la session :", "Session mode:"), reply_markup=InlineKeyboardMarkup(buttons))


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/model : change le modèle de la session (via configOptions)."""
    s = session_for(update, context)
    if s is None:
        return
    try:
        await s.ensure_session()
    except Exception:
        return
    agent = s.agent
    opt = next((o for o in (agent.config_options or []) if o.get("id") == "model"), None)
    if not opt:
        await s.send(L("aucun modèle configurable", "no configurable model"))
        return
    current = opt.get("currentValue")
    buttons = []
    for v in opt.get("options", []):
        mark = "● " if v.get("value") == current else "○ "
        label = f"{mark}{v.get('name') or v['value']}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"v:{v['value']}")])
    buttons.append([InlineKeyboardButton(L("Annuler", "Cancel"), callback_data="v:__cancel__")])
    await s.send(L("Modèle de la session :", "Session model:"), reply_markup=InlineKeyboardMarkup(buttons))


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/language : choisit la langue du pont (persiste entre redemarrages)."""
    s = session_for(update, context)
    if s is None:
        return
    buttons = [
        [InlineKeyboardButton(f"{'●' if LANG == 'fr' else '○'} Français",
                              callback_data="lang:fr")],
        [InlineKeyboardButton(f"{'●' if LANG == 'en' else '○'} English",
                              callback_data="lang:en")],
    ]
    await s.send(L("Langue du pont :", "Bridge language:"),
                  reply_markup=InlineKeyboardMarkup(buttons))


async def cmd_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/interrupt <consigne> (alias /i) : arrête le cycle en cours et
    le remplace immédiatement par cette consigne, même session."""
    s = session_for(update, context)
    if s is None:
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await s.send(L(
            "Usage : /interrupt <consigne>\n"
            "Arrête le cycle en cours et enchaîne sur cette consigne "
            "(l'agent garde le contexte de ce qu'il faisait).",
            "Usage: /interrupt <instruction>\n"
            "Stops the current cycle and follows up with this instruction "
            "(the agent keeps the context of what it was doing)."
        ))
        return
    try:
        await s.ensure_session()
    except Exception:
        return
    await s.interrupt_and_redirect(text)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update, context)
    if s is None:
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    try:
        await s.ensure_session()
    except Exception:
        return  # le message d'erreur a déjà été envoyé par start_session
    if s.busy:
        s.queue.put_nowait([text_block(text)])
        await s.send(L("… mis en file d'attente", "… queued"))
    else:
        s.queue.put_nowait([text_block(text)])


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Note vocale (ou fichier audio) -> transcription Voxtral -> prompt."""
    s = session_for(update, context)
    if s is None:
        return
    media = update.message.voice or update.message.audio
    if media is None:
        return
    try:
        await s.ensure_session()
    except Exception:
        return
    status_id = await s.send(L("🎙 transcription…", "🎙 transcribing…"))
    try:
        tg_file = await tg_call(context.bot.get_file, media.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        filename = getattr(media, "file_name", None) or "note.oga"
        text = await transcribe_audio(data, filename)
    except Exception as e:
        log.exception("échec de transcription")
        if status_id:
            with contextlib.suppress(TelegramError):
                await tg_call(
                    context.bot.edit_message_text,
                    chat_id=s.chat_id, message_id=status_id,
                    text=L(f"[transcription impossible : {e}]", f"[transcription failed: {e}]"),
                )
        return
    if not text:
        if status_id:
            with contextlib.suppress(TelegramError):
                await tg_call(
                    context.bot.edit_message_text,
                    chat_id=s.chat_id, message_id=status_id,
                    text=L("[silence : rien de transcrit]", "[silence: nothing transcribed]"),
                )
        return
    if status_id:
        with contextlib.suppress(TelegramError):
            await tg_call(
                context.bot.edit_message_text,
                chat_id=s.chat_id, message_id=status_id,
                text=f"🎙 {text[:1000]}",
            )
    if s.busy:
        s.queue.put_nowait([text_block(text)])
        await s.send(L("… mis en file d'attente", "… queued"))
    else:
        s.queue.put_nowait([text_block(text)])


async def on_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo ou image en pièce jointe -> bloc image ACP + légende en consigne."""
    s = session_for(update, context)
    if s is None:
        return
    msg = update.message
    media = msg.photo[-1] if msg.photo else getattr(msg, "document", None)
    if media is None:
        return
    caption = (msg.caption or "").strip() or L("Décris cette image.", "Describe this image.")
    try:
        await s.ensure_session()
    except Exception:
        return
    status_id = await s.send(L("🖼 image reçue…", "🖼 image received…"))
    try:
        tg_file = await tg_call(context.bot.get_file, media.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        mime = getattr(media, "mime_type", None) or "image/jpeg"
        blocks = [
            {
                "type": "image",
                "data": base64.b64encode(data).decode("ascii"),
                "mimeType": mime,
            },
            text_block(caption),
        ]
    except Exception as e:
        log.exception("échec de téléchargement de l'image")
        if status_id:
            with contextlib.suppress(TelegramError):
                await tg_call(
                    context.bot.edit_message_text,
                    chat_id=s.chat_id, message_id=status_id,
                    text=L(f"[image impossible : {e}]", f"[image failed: {e}]"),
                )
        return
    if status_id:
        with contextlib.suppress(TelegramError):
            await tg_call(
                context.bot.edit_message_text,
                chat_id=s.chat_id, message_id=status_id,
                text=f"🖼 {caption[:300]}",
            )
    if s.busy:
        s.queue.put_nowait(blocks)
        await s.send(L("… mis en file d'attente", "… queued"))
    else:
        s.queue.put_nowait(blocks)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    s = session_for(update, context)
    if query is None:
        return
    with contextlib.suppress(TelegramError):
        await query.answer()
    if s is None:
        return
    data = query.data or ""
    if data.startswith("lang:"):
        target = data[4:]
        if target in ("fr", "en"):
            set_lang(target)
            with contextlib.suppress(TelegramError):
                await _register_commands(context.bot)
        label = "français" if target == "fr" else "English"
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(L(f"langue : {label}", f"language: {label}"))
        return
    if data.startswith("m:") or data.startswith("v:"):
        prefix, target = data[:2], data[2:]
        config_id = "mode" if prefix == "m:" else "model"
        if target == "__cancel__":
            with contextlib.suppress(TelegramError):
                await query.edit_message_text(L("annulé", "cancelled"))
            return
        try:
            await s.agent.set_config_option(config_id, target)
        except Exception as e:
            with contextlib.suppress(TelegramError):
                await query.edit_message_text(L(f"[échec : {e}]", f"[failed: {e}]"))
            return
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(f"{config_id} : {target}")
        return
    if data.startswith("r:"):
        target = data[2:]
        if target == "__cancel__":
            with contextlib.suppress(TelegramError):
                await query.edit_message_text(L("reprise annulée", "resume cancelled"))
            return
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(L("↩️ reprise de la session…", "↩️ resuming session…"))
        await s.resume_session(target)
        return
    if not data.startswith("p:"):
        return
    fut = s.perm_future
    if fut is None or fut.done():
        return
    option = data[2:]
    # Retour visuel immédiat : les boutons disparaissent au clic.
    with contextlib.suppress(TelegramError):
        label = L("annulé", "cancelled") if option == "__cancel__" else option
        await query.edit_message_text(f"🔐 permission : {label}")
    fut.set_result(None if option == "__cancel__" else option)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    STATS["errors"] += 1
    log.exception("erreur non gérée", exc_info=context.error)
    chat = getattr(update, "effective_chat", None)
    if chat is not None:
        with contextlib.suppress(Exception):
            await tg_call(
                context.bot.send_message,
                chat_id=chat.id, text=L("[erreur interne, voir les logs]", "[internal error, see logs]"),
            )


async def _register_commands(bot: Any) -> None:
    """Enregistre le menu de commandes Telegram (setMyCommands persiste
    par bot ; rappele apres un /language pour rafraichir les descriptions)."""
    with contextlib.suppress(TelegramError):
        await bot.set_my_commands([
            BotCommand("new", L("nouvelle session", "new session")),
            BotCommand("resume", L("reprendre une session passée", "resume a past session")),
            BotCommand("mode", L("changer le mode (ask, auto...)", "change the mode (ask, auto...)")),
            BotCommand("model", L("changer le modèle", "change the model")),
            BotCommand("session", L("état de la session", "session state")),
            BotCommand("doctor", L("diagnostic du pont", "bridge diagnostics")),
            BotCommand("stop", L("interrompre et vider la file", "interrupt and clear the queue")),
            BotCommand("interrupt", L("rediriger l'agent en pleine tâche", "redirect the agent mid-task")),
            BotCommand("start", L("aide", "help")),
            BotCommand("language", L("changer la langue", "change the language")),
        ])


async def _post_init(app: Application) -> None:
    await _register_commands(app.bot)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("VIBE_BRIDGE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx journalise les URL d'API Telegram (avec le token) en INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN manquant")
    if not ALLOWED_USER_IDS:
        raise SystemExit("TELEGRAM_ALLOWED_USER_IDS manquant (allowlist obligatoire)")
    app: Application = ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("doctor", cmd_doctor))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler(["interrupt", "i"], cmd_interrupt))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    log.info(
        "vibe-bridge %s démarre (workspace=%s, agent=%s)",
        __version__, WORKSPACE, AGENT_COMMAND,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
