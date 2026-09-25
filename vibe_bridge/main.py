"""vibe-bridge : pont Telegram <-> vibe-acp (ACP sur stdio).

Mono-utilisateur : allowlist TELEGRAM_ALLOWED_USER_IDS obligatoire.
Une session ACP par chat autorisé, prompts traités un à la fois (file).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError, TimedOut
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
from .formatting import split_message

log = logging.getLogger("vibe_bridge")

EDIT_MIN_INTERVAL = 1.0    # secondes entre éditions du message de statut
PERMISSION_TIMEOUT = 300.0  # secondes avant auto-refus d'une permission
PROMPT_TIMEOUT = float(os.environ.get("VIBE_BRIDGE_PROMPT_TIMEOUT", "900"))
WORKSPACE = os.environ.get("VIBE_BRIDGE_WORKSPACE", "/home/vibe/workspace")
AGENT_COMMAND = os.environ.get("ACP_AGENT_COMMAND", "vibe-acp")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_IDS = {
    int(x)
    for x in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").replace(",", " ").split()
    if x
}


async def tg_call(fn: Any, *args: Any, retries: int = 3, **kwargs: Any) -> Any:
    """Appel API Telegram avec reprise sur timeout de connexion.

    On ne reprend que si la requête n'a jamais été envoyée
    (httpx.ConnectTimeout / PoolTimeout) : aucun risque de doublon.
    Leçon du 25/09 : un timeout de connexion ne doit rien perdre.
    """
    for attempt in range(retries + 1):
        try:
            return await fn(*args, **kwargs)
        except TimedOut as err:
            cause = err.__cause__
            if attempt < retries and isinstance(
                cause, (httpx.ConnectTimeout, httpx.PoolTimeout)
            ):
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise


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
        self.text: str = ""
        self.last_edit: float = 0.0
        self.busy = False
        self.perm_future: asyncio.Future[str | None] | None = None

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
        prefix = f"⚙ {self.activity}\n\n" if self.activity else "… "
        body = (prefix + (self.text or "…"))[-4000:]
        if self.status_msg_id is None:
            self.status_msg_id = await self.send(body)
            return
        with contextlib.suppress(TelegramError):
            await tg_call(
                self.bot.edit_message_text,
                chat_id=self.chat_id, message_id=self.status_msg_id, text=body,
            )

    async def finalize(self) -> None:
        """Dernier rendu : la réponse remplace le statut, découpée si longue."""
        parts = split_message(self.text)
        if self.status_msg_id is None:
            for part in parts:
                await self.send(part)
            if not parts:
                await self.send("(fin de cycle, pas de réponse texte)")
            return
        if parts:
            try:
                await tg_call(
                    self.bot.edit_message_text,
                    chat_id=self.chat_id, message_id=self.status_msg_id,
                    text=parts[0][:4000],
                )
            except TelegramError:
                await self.send(parts[0])
            for part in parts[1:]:
                await self.send(part)
        else:
            with contextlib.suppress(TelegramError):
                await tg_call(
                    self.bot.edit_message_text,
                    chat_id=self.chat_id, message_id=self.status_msg_id,
                    text="(fin de cycle, pas de réponse texte)",
                )
        self.status_msg_id = None

    # -- callbacks ACP ----------------------------------------------------------

    async def handle_update(self, update: dict) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text":
                self.text += content.get("text", "")
                await self.refresh_status()
        elif kind in ("tool_call", "tool_call_update"):
            title = update.get("title") or update.get("kind")
            if title:
                self.activity = str(title)
                await self.refresh_status()
        # plan, available_commands_update, etc. : ignorés en v1

    async def handle_permission(self, params: dict) -> str | None:
        """Requête session/request_permission -> boutons inline."""
        options = params.get("options", [])
        if not options:
            return None
        buttons = []
        for opt in options:
            label = (opt.get("option") or {}).get("text") or opt.get("id", "?")
            buttons.append(
                [InlineKeyboardButton(label[:60], callback_data=f"p:{opt['id']}")]
            )
        buttons.append(
            [InlineKeyboardButton("Annuler", callback_data="p:__cancel__")]
        )
        label = self.activity or "Action de l'agent"
        msg_id = await self.send(
            f"🔐 {label}\nAutoriser ?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        self.perm_future = asyncio.get_running_loop().create_future()
        try:
            return await asyncio.wait_for(self.perm_future, PERMISSION_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        finally:
            self.perm_future = None
            if msg_id:
                with contextlib.suppress(Exception):
                    await tg_call(
                        self.bot.delete_message,
                        chat_id=self.chat_id, message_id=msg_id,
                    )

    # -- cycle de vie ------------------------------------------------------------

    async def ensure_session(self) -> None:
        if self.agent and not self.agent.dead and self.worker and not self.worker.done():
            return
        await self._stop_everything()
        await self.start_session()

    async def renew(self) -> None:
        await self._stop_everything()
        await self.start_session()

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

    async def start_session(self) -> None:
        agent = AcpAgent(AGENT_COMMAND, WORKSPACE)
        agent.on_update = self.handle_update
        agent.on_permission = self.handle_permission
        try:
            await agent.start()
        except Exception as e:
            await self.send(f"[impossible de démarrer l'agent : {e}]")
            raise
        self.agent = agent
        self.queue = asyncio.Queue()
        self.worker = asyncio.create_task(self.run_worker())
        await self.send(f"🟢 session ouverte ({str(agent.session_id)[:8]}…)")

    async def run_worker(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            self.busy = True
            self.text = ""
            self.activity = ""
            self.status_msg_id = None
            self.last_edit = 0.0
            agent = self.agent
            if agent is None:
                self.busy = False
                return
            try:
                result = await agent.prompt(item, timeout=PROMPT_TIMEOUT)
                await self.finalize()
                stop = result.get("stopReason")
                if stop and stop != "end_turn":
                    await self.send(f"[cycle terminé : {stop}]")
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    await agent.cancel()
                await self.finalize()
                await self.send("[délai dépassé, cycle annulé]")
            except AcpError as e:
                self.agent = None
                await self.finalize()
                await self.send(
                    f"[erreur agent : {e}]\n"
                    "Session arrêtée — renvoie un message pour en ouvrir une nouvelle."
                )
            except Exception:
                log.exception("cycle en échec")
                await self.finalize()
                await self.send("[erreur inattendue, voir les logs]")
            finally:
                self.busy = False

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
        text=(
            "vibe-bridge — pilotage de Vibe par Telegram\n\n"
            "Un message = un prompt (file d'attente si l'agent est occupé).\n"
            "/new — nouvelle session\n"
            "/session — état de la session\n"
            "/stop — interrompre le cycle et vider la file"
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
        await s.send("aucune session ouverte")
        return
    state = "occupé" if s.busy else "inactif"
    await s.send(
        f"session {s.agent.session_id}\n"
        f"état : {state}\n"
        f"file d'attente : {s.queue.qsize()}"
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update, context)
    if s is None:
        return
    drained = await s.interrupt()
    await s.send(f"[cycle interrompu, {drained} message(s) retirés de la file]")


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
        s.queue.put_nowait(text)
        await s.send("… mis en file d'attente")
    else:
        s.queue.put_nowait(text)


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
    if not data.startswith("p:"):
        return
    if s.perm_future is None or s.perm_future.done():
        return
    option = data[2:]
    s.perm_future.set_result(None if option == "__cancel__" else option)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("erreur non gérée", exc_info=context.error)
    chat = getattr(update, "effective_chat", None)
    if chat is not None:
        with contextlib.suppress(Exception):
            await tg_call(
                context.bot.send_message,
                chat_id=chat.id, text="[erreur interne, voir les logs]",
            )


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
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    log.info(
        "vibe-bridge %s démarre (workspace=%s, agent=%s)",
        __version__, WORKSPACE, AGENT_COMMAND,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
