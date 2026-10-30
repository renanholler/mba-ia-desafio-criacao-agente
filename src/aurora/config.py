"""Caminhos, modelo e os identificadores fixos da sessão."""

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parents[2]
load_dotenv(RAIZ / ".env")
if not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"

DADOS = RAIZ / "dados"
VAR = RAIZ / "var"
DB_CONDOMINIO = VAR / "condominio.db"
DB_SESSOES = VAR / "sessoes.db"

# Constantes: o reinício encontra a mesma sessão no SQLite do ADK.
APP_NAME = "residencial_aurora"
USER_ID = "morador"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"


def fila_de_modelos() -> list[str]:
    """Primeiro o modelo pedido. Se a cota ou o serviço falhar, os seguintes."""
    primeiro = GEMINI_MODEL.strip()
    resto = os.environ.get("GEMINI_MODELOS")
    if resto is None or not resto.strip():
        seguintes = ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
    else:
        seguintes = [nome.strip() for nome in resto.split(",") if nome.strip()]
    fila: list[str] = []
    for nome in [primeiro, *seguintes]:
        if nome not in fila:
            fila.append(nome)
    return fila
