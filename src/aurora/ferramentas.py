"""Tools. Nenhuma recebe apartamento: o valor sai de tool_context.state."""

from __future__ import annotations

import asyncio

from google.adk.tools import FunctionTool
from google.adk.tools import ToolContext

from . import condominio
from . import regulamento

CHAVE_APARTAMENTO = "apartamento"


def _apartamento(tool_context: ToolContext) -> str:
    return tool_context.state[CHAVE_APARTAMENTO]


def _chamada(tool_context: ToolContext) -> str | None:
    return getattr(tool_context, "function_call_id", None)


def _cobranca_aprovada(tool_context: ToolContext) -> bool:
    confirmacao = getattr(tool_context, "tool_confirmation", None)
    if confirmacao is None:
        return False
    if isinstance(confirmacao, dict):
        return bool(confirmacao.get("confirmed"))
    return bool(getattr(confirmacao, "confirmed", False))


def listar_areas() -> dict:
    """Lista as áreas comuns reserváveis, com id, nome e taxa em reais. Taxa 0 não gera cobrança."""
    return {"areas": condominio.areas()}


def consultar_disponibilidade(area: str, data: str) -> dict:
    """Diz se a área está livre ou ocupada na data. Não informa de quem é a reserva.

    Args:
        area: id da área: salao-de-festas, churrasqueira ou quadra.
        data: data no formato AAAA-MM-DD.
    """
    info = condominio.resolver_area(area)
    if not info:
        return {"erro": "Área desconhecida. Use salao-de-festas, churrasqueira ou quadra."}
    if not condominio.data_valida(data):
        return {"erro": "Data inválida. Use AAAA-MM-DD."}
    return {"area": info["id"], "data": data, "ocupada": condominio.data_ocupada(info["id"], data)}


def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento desta conversa."""
    return {"reservas": condominio.reservas_ativas(_apartamento(tool_context))}


def _reserva_pede_confirmacao(area: str, data: str, tool_context: ToolContext | None = None) -> bool:
    """Taxa maior que zero e data livre. Quadra e data já ocupada não pedem confirmação.

    O ADK chama este predicado com os mesmos argumentos da tool, inclusive o contexto.
    """
    del tool_context
    info = condominio.resolver_area(area)
    if info is None or info["taxa"] <= 0:
        return False
    if not condominio.data_valida(data):
        return False
    return not condominio.data_ocupada(info["id"], data)


async def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área para o apartamento desta conversa.

    Área com taxa só é gravada depois que o morador aprova a cobrança pela rota de confirmações.

    Args:
        area: id da área: salao-de-festas, churrasqueira ou quadra.
        data: data no formato AAAA-MM-DD.
    """
    info = condominio.resolver_area(area)
    if not info:
        return {"erro": "Área desconhecida. Use salao-de-festas, churrasqueira ou quadra."}
    if not condominio.data_valida(data):
        return {"erro": "Data inválida. Use AAAA-MM-DD."}
    chamada = _chamada(tool_context)
    ja = condominio.reserva_por_chamada(chamada)
    if ja:
        return {"reservado": True, "codigo": ja["codigo"], "area": ja["area"], "data": ja["data"], "taxa": info["taxa"]}
    if condominio.data_ocupada(info["id"], data):
        return {"ocupada": True}
    if info["taxa"] > 0 and not _cobranca_aprovada(tool_context):
        return {"erro": "A cobrança não foi aprovada pelo aplicativo."}
    resultado = await asyncio.to_thread(
        condominio.criar_reserva,
        _apartamento(tool_context),
        info["id"],
        data,
        chamada,
    )
    if resultado["ocupada"]:
        return {"ocupada": True}
    return {
        "reservado": True,
        "codigo": resultado["codigo"],
        "area": resultado["area"],
        "data": resultado["data"],
        "taxa": info["taxa"],
    }


def cancelar_minha_reserva(area: str, data: str, tool_context: ToolContext) -> dict:
    """Cancela uma reserva ativa do apartamento desta conversa. Não pede confirmação.

    Args:
        area: id da área: salao-de-festas, churrasqueira ou quadra.
        data: data no formato AAAA-MM-DD.
    """
    info = condominio.resolver_area(area)
    if not info:
        return {"erro": "Área desconhecida. Use salao-de-festas, churrasqueira ou quadra."}
    if not condominio.data_valida(data):
        return {"erro": "Data inválida. Use AAAA-MM-DD."}
    if condominio.cancelar_reserva(_apartamento(tool_context), info["id"], data):
        return {"cancelada": True, "area": info["id"], "data": data}
    return {"cancelada": False, "motivo": "Reserva não encontrada."}


def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista as autorizações de visita do apartamento desta conversa."""
    return {"visitantes": condominio.visitantes_do(_apartamento(tool_context))}


async def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante. Só grava depois da aprovação pela rota de confirmações.

    Args:
        nome: nome do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    chamada = _chamada(tool_context)
    ja = condominio.visitante_por_chamada(chamada)
    if ja:
        return {"autorizado": True, "nome": ja["nome"], "data": ja["data"]}
    if not _cobranca_aprovada(tool_context):
        return {"erro": "A autorização não foi aprovada pelo aplicativo."}
    nome_limpo = (nome or "").strip()
    if not nome_limpo:
        return {"erro": "Informe o nome do visitante."}
    if not condominio.data_valida(data):
        return {"erro": "Data inválida. Use AAAA-MM-DD."}
    gravado = await asyncio.to_thread(
        condominio.autorizar,
        _apartamento(tool_context),
        nome_limpo,
        data,
        chamada,
    )
    return {"autorizado": True, "nome": gravado["nome"], "data": gravado["data"]}


def consultar_regulamento(tema: str) -> dict:
    """Busca no regulamento os artigos do único capítulo que trata do tema.

    Args:
        tema: assunto da dúvida, com as palavras da pergunta.
    """
    achado = regulamento.buscar(tema)
    if not achado:
        return {"encontrado": False}
    return {"encontrado": True, **achado}


TOOLS_RESERVAS = [
    FunctionTool(listar_areas),
    FunctionTool(consultar_disponibilidade),
    FunctionTool(listar_minhas_reservas),
    FunctionTool(reservar_area, require_confirmation=_reserva_pede_confirmacao),
    FunctionTool(cancelar_minha_reserva),
]
TOOLS_VISITANTES = [
    FunctionTool(listar_meus_visitantes),
    FunctionTool(autorizar_visitante, require_confirmation=True),
]
TOOLS_REGULAMENTO = [FunctionTool(consultar_regulamento)]
