"""Client ACP minimaliste : JSON-RPC 2.0 newline-delimited sur stdio.

Pas de SDK externe volontairement : la surface utile du protocole est petite
(initialize, session/new, session/prompt, notifications session/update,
requete session/request_permission) et la mainmise sur la gestion d'erreur
est la lecon retenue de telegram-acp-bot (voir PLAN-vibe-svc.md).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
from typing import Any, Callable

log = logging.getLogger("vibe_bridge.acp")

PROTOCOL_VERSION = 1

# Limite du StreamReader stdio (defaut asyncio : 64 Ko !). Un message agent
# plus gros tuait silencieusement le lecteur et gelait le pont (lecon du 26/09).
STDIO_LIMIT = int(os.environ.get("VIBE_BRIDGE_STDIO_LIMIT", str(32 * 1024 * 1024)))


class AcpError(Exception):
    """Erreur de transport ou de protocole ACP."""


class AcpAgent:
    """Un process agent (ex. vibe-acp) et sa session ACP courante."""

    def __init__(self, command: str, cwd: str) -> None:
        self.command = shlex.split(command)
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self.session_id: str | None = None
        self.dead = False
        self.modes: dict = {}
        self.config_options: list[dict] = []
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        # Branches par le bridge.
        self.on_update: Callable[[dict], Any] | None = None
        self.on_permission: Callable[[dict], Any] | None = None

    # -- cycle de vie -------------------------------------------------------

    async def start(self, load_session_id: str | None = None) -> None:
        log.info("lancement de l'agent : %s (cwd=%s)", self.command, self.cwd)
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            limit=STDIO_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        result = await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {},
            "clientInfo": {"name": "vibe-bridge", "version": "0.1.0"},
        })
        caps = result.get("agentCapabilities", {})
        log.info(
            "initialize OK (protocol %s, loadSession=%s)",
            result.get("protocolVersion"), caps.get("loadSession"),
        )
        if load_session_id:
            result = await self._request("session/load", {
                "sessionId": load_session_id,
                "cwd": self.cwd,
                "mcpServers": [],
            })
            # LoadSessionResponse n'a pas de sessionId : la session reprise
            # garde l'identifiant qu'on vient de passer.
            self.session_id = result.get("sessionId") or load_session_id
        else:
            result = await self._request("session/new", {"cwd": self.cwd, "mcpServers": []})
            self.session_id = result.get("sessionId")
        if not self.session_id:
            raise AcpError("session/new/load sans sessionId")
        self.modes = result.get("modes") or {}
        self.config_options = result.get("configOptions") or []
        log.info("session ACP ouverte : %s%s", self.session_id,
                 " (reprise)" if load_session_id else "")

    async def stop(self) -> None:
        self.dead = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AcpError("agent arrêté"))
        self._pending.clear()
        # Couper le lecteur AVANT de tuer le process : readline() sur le
        # stdout d'un process tué peut être réveillé en continu sans jamais
        # voir l'EOF, et figer toute la boucle d'événements (constaté le
        # 26/09 — py-spy montrait readuntil en boucle active).
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader_task = None
        self._stderr_task = None
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:
                with contextlib.suppress(Exception):
                    self.proc.kill()
        self.proc = None
        self.session_id = None

    # -- transport ----------------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            try:
                line = await self.proc.stdout.readline()
            except ValueError as exc:
                # Message plus gros que STDIO_LIMIT : le lecteur doit mourir
                # proprement (dead + echec des requetes pendantes), pas
                # silencieusement.
                log.error("message agent au-dela de la limite stdio (%s) : %s",
                          STDIO_LIMIT, exc)
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("ligne non-JSON de l'agent : %.200s", line)
                continue
            try:
                await self._dispatch(msg)
            except Exception:
                log.exception("erreur de dispatch d'un message agent")
        log.warning("sortie stdout de l'agent fermée")
        self.dead = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AcpError("connexion agent fermée"))
        self._pending.clear()

    async def _dispatch(self, msg: dict) -> None:
        if "id" in msg and "method" in msg:
            await self._handle_request(msg)
        elif "id" in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(AcpError(f"erreur JSON-RPC : {msg['error']}"))
                else:
                    fut.set_result(msg.get("result", {}))
        elif "method" in msg:
            if msg["method"] == "session/update":
                params = msg.get("params", {})
                if params.get("sessionId") == self.session_id and self.on_update:
                    await _maybe_await(self.on_update, params.get("update", {}))
            else:
                log.debug("notification ignorée : %s", msg["method"])

    async def _handle_request(self, msg: dict) -> None:
        """Requêtes agent -> client (permissions, FS...)."""
        method = msg["method"]
        if method == "session/request_permission":
            if self.on_permission is None:
                await self._respond(msg["id"], error=_METHOD_NOT_FOUND)
                return
            try:
                outcome = await _maybe_await(self.on_permission, msg.get("params", {}))
            except Exception:
                # Ne jamais laisser une permission sans réponse : l'agent
                # resterait suspendu indéfiniment (leçon du 25/09).
                log.exception("erreur dans le gestionnaire de permission")
                outcome = None
            if outcome is None:
                result: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}
            else:
                result = {"outcome": {"outcome": "selected", "optionId": outcome}}
            await self._respond(msg["id"], result=result)
        else:
            # fs/read_text, fs/write_text... : vibe-acp a ses propres outils,
            # on ne sert pas le système de fichiers du client.
            log.warning("requête agent non gérée : %s", method)
            await self._respond(msg["id"], error=_METHOD_NOT_FOUND)

    async def _respond(self, request_id: Any, result: dict | None = None,
                       error: dict | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result or {}
        await self._write(payload)

    async def _write(self, payload: dict) -> None:
        if self.dead or not self.proc or not self.proc.stdin:
            raise AcpError("agent arrête")
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            log.debug("agent stderr : %.400s", line.decode(errors="replace").strip())

    async def _request(self, method: str, params: dict | None = None,
                       timeout: float = 120.0) -> dict:
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._write({"jsonrpc": "2.0", "id": rid,
                               "method": method, "params": params or {}})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    # -- haut niveau ----------------------------------------------------------

    async def prompt(self, text: str, timeout: float = 900.0) -> dict:
        if self.dead:
            raise AcpError("agent arrête")
        if not self.session_id:
            raise AcpError("aucune session ACP ouverte")
        return await self._request("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}],
        }, timeout=timeout)

    async def set_config_option(self, config_id: str, value: str) -> None:
        """session/set_config_option : change un option de session (mode,
        modele...) ; met a jour l'etat local pour l'affichage."""
        await self._request("session/set_config_option", {
            "sessionId": self.session_id,
            "configId": config_id,
            "value": value,
        })
        for opt in self.config_options:
            if opt.get("id") == config_id:
                opt["currentValue"] = value
        if config_id == "mode" and isinstance(self.modes, dict):
            self.modes["currentModeId"] = value

    async def list_sessions(self) -> list[dict]:
        """session/list : sessions connues de l'agent (récentes d'abord)."""
        result = await self._request("session/list", {})
        return result.get("sessions", [])

    async def cancel(self) -> None:
        """session/cancel — en NOTIFICATION, sans id.

        Envoyee comme requete (avec id), vibe-acp la rejette avec
        'method not found' ; en notification, le prompt en cours retourne
        stopReason=cancelled (verifie par probe le 26/09).
        """
        if self.session_id and not self.dead:
            try:
                await self._write({
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {
                        "sessionId": self.session_id,
                        "reason": "demande de l'utilisateur",
                    },
                })
            except Exception:
                log.exception("session/cancel a échoué")


_METHOD_NOT_FOUND = {"code": -32601, "message": "method not found"}


async def _maybe_await(fn: Callable, arg: Any) -> Any:
    res = fn(arg)
    if hasattr(res, "__await__"):
        return await res
    return res
