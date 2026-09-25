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
import shlex
from typing import Any, Callable

log = logging.getLogger("vibe_bridge.acp")

PROTOCOL_VERSION = 1


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
        # Branches par le bridge.
        self.on_update: Callable[[dict], Any] | None = None
        self.on_permission: Callable[[dict], Any] | None = None

    # -- cycle de vie -------------------------------------------------------

    async def start(self) -> None:
        log.info("lancement de l'agent : %s (cwd=%s)", self.command, self.cwd)
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )
        asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._drain_stderr())
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
        result = await self._request("session/new", {"cwd": self.cwd, "mcpServers": []})
        self.session_id = result.get("sessionId")
        if not self.session_id:
            raise AcpError("session/new sans sessionId")
        log.info("session ACP ouverte : %s", self.session_id)

    async def stop(self) -> None:
        self.dead = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AcpError("agent arrête"))
        self._pending.clear()
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
            line = await self.proc.stdout.readline()
            if not line:
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
            outcome = await _maybe_await(self.on_permission, msg.get("params", {}))
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

    async def cancel(self) -> None:
        if self.session_id and not self.dead:
            try:
                await self._request("session/cancel", {
                    "sessionId": self.session_id,
                    "reason": "demande de l'utilisateur",
                }, timeout=10)
            except Exception:
                log.exception("session/cancel a échoué")


_METHOD_NOT_FOUND = {"code": -32601, "message": "method not found"}


async def _maybe_await(fn: Callable, arg: Any) -> Any:
    res = fn(arg)
    if hasattr(res, "__await__"):
        return await res
    return res
