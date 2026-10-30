"""Busca lexical em dados/regulamento.md. Devolve no máximo três artigos de um capítulo."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from .config import DADOS

MAX_ARTIGOS = 3
_VAZIAS = set(
    "a o as os de da do das dos e em no na nos nas um uma uns umas para por com sem que "
    "qual quais como quando onde ate sobre ao aos ser sao esta estao meu minha ate que".split()
)


def _normalizar(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()


def _termos(texto: str) -> set[str]:
    termos = set()
    for palavra in re.findall(r"[a-z0-9]+", _normalizar(texto)):
        if len(palavra) < 3 or palavra in _VAZIAS:
            continue
        termos.add(palavra[:6])
    return termos


@lru_cache(maxsize=1)
def _capitulos() -> tuple[dict, ...]:
    texto = (DADOS / "regulamento.md").read_text(encoding="utf-8")
    capitulos = []
    for bloco in re.split(r"(?m)^## ", texto)[1:]:
        titulo, _, corpo = bloco.partition("\n")
        artigos = [parte.strip() for parte in re.split(r"(?m)^(?=\*\*Art\. )", corpo) if parte.strip()]
        capitulos.append(
            {
                "titulo": titulo.strip(),
                "termos_titulo": _termos(titulo),
                "artigos": tuple((artigo, _termos(artigo)) for artigo in artigos),
            }
        )
    return tuple(capitulos)


def buscar(tema: str) -> dict | None:
    """Um único capítulo. Artigos de outros assuntos não saem daqui."""
    termos = _termos(tema or "")
    if not termos:
        return None

    def nota(capitulo: dict) -> int:
        no_titulo = len(termos & capitulo["termos_titulo"])
        no_corpo = sum(len(termos & termos_artigo) for _, termos_artigo in capitulo["artigos"])
        return no_titulo * 10 + no_corpo

    melhor = max(_capitulos(), key=nota)
    if nota(melhor) == 0:
        return None
    ordenados = sorted(melhor["artigos"], key=lambda item: len(termos & item[1]), reverse=True)
    escolhidos = [texto for texto, termos_artigo in ordenados if termos & termos_artigo][:MAX_ARTIGOS]
    if not escolhidos:
        escolhidos = [ordenados[0][0]]
    return {"capitulo": melhor["titulo"], "artigos": escolhidos[:MAX_ARTIGOS]}
