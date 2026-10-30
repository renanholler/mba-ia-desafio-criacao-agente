"""Contrato HTTP do assistente. A confirmação só entra por esta rota, nunca pelo texto."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi import HTTPException
from google.genai import types
from pydantic import BaseModel

from . import condominio
from .config import APP_NAME
from .config import USER_ID
from .ferramentas import CHAVE_APARTAMENTO
from .runtime import app_adk
from .runtime import runner

logger = logging.getLogger("aurora.api")

CONFIRMACAO = "adk_request_confirmation"
_travas: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class NovaSessao(BaseModel):
    apartamento: str


class Mensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


def _mapa(valor) -> dict:
    if valor is None:
        return {}
    if isinstance(valor, dict):
        return valor
    if hasattr(valor, "model_dump"):
        return valor.model_dump()
    try:
        return dict(valor)
    except (TypeError, ValueError):
        return {}


def _partes(evento) -> list:
    conteudo = getattr(evento, "content", None)
    if not conteudo or not conteudo.parts:
        return []
    return list(conteudo.parts)


def _detalhes(args) -> tuple[str, dict]:
    original = _mapa(_mapa(args).get("originalFunctionCall"))
    nome = original.get("name") or "acao"
    originais = _mapa(original.get("args"))
    if nome == "reservar_area":
        info = condominio.resolver_area(str(originais.get("area") or ""))
        area = info["id"] if info else originais.get("area")
        return nome, {"area": area, "data": originais.get("data")}
    if nome == "autorizar_visitante":
        nome_visita = originais.get("nome")
        if isinstance(nome_visita, str):
            nome_visita = nome_visita.strip()
        return nome, {"nome": nome_visita, "data": originais.get("data")}
    return nome, {}


def pendencias(sessao) -> list[dict]:
    """FunctionCall adk_request_confirmation ainda sem FunctionResponse do mesmo id."""
    abertas: dict[str, dict] = {}
    for evento in sessao.events:
        for parte in _partes(evento):
            chamada = parte.function_call
            if chamada and chamada.name == CONFIRMACAO and chamada.id:
                acao, detalhes = _detalhes(chamada.args)
                abertas[chamada.id] = {
                    "id": chamada.id,
                    "acao": acao,
                    "detalhes": detalhes,
                    "invocation_id": getattr(evento, "invocation_id", None),
                    "chamada_original": _mapa(_mapa(chamada.args).get("originalFunctionCall")).get("id"),
                }
            resposta = parte.function_response
            if resposta and resposta.name == CONFIRMACAO and resposta.id:
                abertas.pop(resposta.id, None)
    return list(abertas.values())


def _publicas(itens: list[dict]) -> list[dict]:
    return [{"id": item["id"], "acao": item["acao"], "detalhes": item["detalhes"]} for item in itens]


async def _sessao(session_id: str):
    sessao = await runner.session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    if sessao is None:
        raise HTTPException(status_code=404, detail="Sessão inexistente.")
    return sessao


def _tool_respondeu(sessao, chamada_original: str | None) -> bool:
    if not chamada_original:
        return False
    for evento in sessao.events:
        for parte in _partes(evento):
            resposta = parte.function_response
            if resposta and resposta.id == chamada_original:
                return True
    return False


async def _executar(session_id: str, mensagem: types.Content, invocation_id: str | None = None) -> dict:
    textos: list[str] = []
    kwargs = {"user_id": USER_ID, "session_id": session_id, "new_message": mensagem}
    if invocation_id:
        kwargs["invocation_id"] = invocation_id
    async for evento in runner.run_async(**kwargs):
        if evento.author == "user" or evento.partial or not evento.content:
            continue
        for parte in evento.content.parts or []:
            if parte.text and not parte.thought:
                textos.append(parte.text.strip())
    sessao = await _sessao(session_id)
    return {
        "resposta": "\n".join(texto for texto in textos if texto),
        "confirmacoes_pendentes": _publicas(pendencias(sessao)),
    }


@asynccontextmanager
async def _ciclo(_app: FastAPI) -> AsyncIterator[None]:
    condominio.preparar()
    yield


app = FastAPI(title="Assistente do Residencial Aurora", lifespan=_ciclo)


@app.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao):
    apartamento = corpo.apartamento.strip()
    if not condominio.apartamento_no_arquivo(apartamento):
        raise HTTPException(status_code=404, detail="Apartamento inexistente.")
    sessao = await runner.session_service.create_session(
        app_name=app_adk.name,
        user_id=USER_ID,
        state={CHAVE_APARTAMENTO: apartamento},
    )
    return {"session_id": sessao.id}


@app.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: Mensagem):
    await _sessao(session_id)
    async with _travas[session_id]:
        sessao = await _sessao(session_id)
        abertas = pendencias(sessao)
        if abertas:
            return {"resposta": "", "confirmacoes_pendentes": _publicas(abertas)}
        mensagem = types.Content(role="user", parts=[types.Part(text=corpo.texto)])
        return await _executar(session_id, mensagem)


@app.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, corpo: RespostaConfirmacao):
    await _sessao(session_id)
    async with _travas[session_id]:
        sessao = await _sessao(session_id)
        abertas = {item["id"]: item for item in pendencias(sessao)}
        pendente = abertas.get(corpo.id)
        if pendente is None:
            raise HTTPException(
                status_code=409,
                detail="Não existe confirmação pendente com esse id nesta sessão.",
            )
        resposta = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=corpo.id,
                        name=CONFIRMACAO,
                        response={"confirmed": corpo.confirmado},
                    )
                )
            ],
        )
        saida = await _executar(session_id, resposta, pendente.get("invocation_id"))
        if corpo.confirmado:
            atual = await _sessao(session_id)
            if not _tool_respondeu(atual, pendente.get("chamada_original")):
                logger.warning(
                    "Confirmação %s aprovada na sessão %s sem execução da tool original.",
                    corpo.id,
                    session_id,
                )
        return saida


@app.get("/sessoes/{session_id}/eventos")
async def eventos(session_id: str):
    sessao = await _sessao(session_id)
    return [evento.model_dump(mode="json", by_alias=True) for evento in sessao.events]


@app.get("/apartamentos/{numero}/reservas")
async def reservas_do_apartamento(numero: str):
    return condominio.reservas_ativas(numero)


@app.get("/apartamentos/{numero}/visitantes")
async def visitantes_do_apartamento(numero: str):
    return condominio.visitantes_do(numero)


def main() -> None:
    uvicorn.run("aurora.api:app", host="127.0.0.1", port=8000)
