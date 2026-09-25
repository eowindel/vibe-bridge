"""Découpe et mise en forme pour Telegram : limite 4096, repli texte brut."""
from __future__ import annotations

import html
import re

# Marge sous la limite Telegram (4096) pour préfixes et erreurs.
MAX_CHUNK = 3800
MAX_MESSAGES = 6

_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")


def _inline(md: str) -> str:
    """Markdown en ligne -> HTML Telegram (après échappement)."""
    text = html.escape(md)
    text = _LINK.sub(r'<a href="\2">\1</a>', text)
    text = _INLINE_CODE.sub(lambda m: "<code>" + m.group(1) + "</code>", text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    return text


def md_to_html(md: str) -> str:
    """Markdown (sous-ensemble LLM) -> HTML pour parse_mode='HTML'.

    Couvre : blocs de code ```, code en ligne, gras, italique, liens,
    titres (-> gras), listes à puces. Le reste passe échappé. L'appelant
    doit prévoir un repli en texte brut si Telegram refuse le rendu.
    """
    out: list[str] = []
    in_code = False
    for line in md.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            out.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(line))
            continue
        if stripped.startswith("#"):
            out.append("<b>" + _inline(stripped.lstrip("#").strip()) + "</b>")
            continue
        if stripped[:2] in ("- ", "* "):
            out.append("• " + _inline(stripped[2:]))
            continue
        out.append(_inline(line))
    return "\n".join(out)


def split_message(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Découpe aux frontières de paragraphe puis de ligne, en dernier
    recours au milieu du texte."""
    text = text.strip()
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if len(parts) > MAX_MESSAGES:
        parts = parts[:MAX_MESSAGES]
        parts.append("… (réponse tronquée)")
    return parts


def plain(text: str) -> str:
    """Texte Telegram sûr : entités HTML échappées, aucun parse mode requis."""
    return html.escape(text)
