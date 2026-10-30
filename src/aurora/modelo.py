"""Um modelo Gemini, com troca automática quando a cota ou o serviço falha."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from pydantic import Field
from typing_extensions import override

from .config import fila_de_modelos

logger = logging.getLogger("aurora.modelo")

_CODIGOS_PARA_TROCAR = {404, 408, 429, 500, 502, 503, 504}
_posicao = 0
_trava = asyncio.Lock()
_clientes: dict[str, Gemini] = {}


def _deve_trocar(erro: BaseException) -> bool:
    codigo = getattr(erro, "code", None)
    if codigo in _CODIGOS_PARA_TROCAR:
        return True
    causa = erro.__cause__
    if causa is not None and causa is not erro and _deve_trocar(causa):
        return True
    texto = str(erro)
    return "RESOURCE_EXHAUSTED" in texto or "UNAVAILABLE" in texto


def _cliente(nome: str) -> Gemini:
    cliente = _clientes.get(nome)
    if cliente is None:
        cliente = Gemini(model=nome)
        _clientes[nome] = cliente
    return cliente


class ModeloComFallback(BaseLlm):
    """Tenta a fila em ordem e fica no primeiro modelo que responder."""

    modelos: list[str] = Field(default_factory=list)

    @override
    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        global _posicao
        if not self.modelos:
            raise RuntimeError("Nenhum modelo Gemini configurado.")
        async with _trava:
            inicio = min(_posicao, len(self.modelos) - 1)
        ultimo: BaseException | None = None
        for indice in range(inicio, len(self.modelos)):
            nome = self.modelos[indice]
            self.model = nome
            llm_request.model = nome
            emitiu = False
            try:
                async for resposta in _cliente(nome).generate_content_async(llm_request, stream):
                    emitiu = True
                    yield resposta
                async with _trava:
                    _posicao = indice
                return
            except Exception as erro:
                if emitiu or not _deve_trocar(erro) or indice == len(self.modelos) - 1:
                    raise
                ultimo = erro
                async with _trava:
                    if _posicao <= indice:
                        _posicao = indice + 1
                proximo = self.modelos[indice + 1]
                logger.warning("Modelo %s indisponível (%s). Tentando %s.", nome, erro, proximo)
        if ultimo is not None:
            raise ultimo


def criar_modelo() -> ModeloComFallback:
    fila = fila_de_modelos()
    return ModeloComFallback(model=fila[0], modelos=fila)


modelo = criar_modelo()
