"""SQLite do condomínio. As tools leem e gravam aqui; o modelo não é a fonte dos dados.

Garantia 5: o índice único parcial impede duas reservas ativas da mesma área e data
no instante do INSERT. A escrita abre a própria conexão, com BEGIN IMMEDIATE.
Garantia 1, no banco: `chamada` é UNIQUE, então uma reexecução da mesma tool não duplica.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date

from .config import DADOS, DB_CONDOMINIO

ESQUEMA = """
CREATE TABLE IF NOT EXISTS apartamentos (
    numero TEXT PRIMARY KEY,
    morador TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS areas (
    id TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    taxa REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reservas (
    codigo TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    area TEXT NOT NULL REFERENCES areas(id),
    data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ativa' CHECK (status IN ('ativa', 'cancelada')),
    chamada TEXT UNIQUE
);
CREATE UNIQUE INDEX IF NOT EXISTS reserva_ativa_unica
    ON reservas(area, data) WHERE status = 'ativa';
CREATE TABLE IF NOT EXISTS visitantes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome TEXT NOT NULL,
    data TEXT NOT NULL,
    chamada TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS codigos_emitidos (
    codigo TEXT PRIMARY KEY
);
"""


def _norm(texto: str) -> str:
    base = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(base.lower().replace("-", " ").split())


@contextmanager
def conexao():
    DB_CONDOMINIO.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_CONDOMINIO, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL").fetchone()
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    try:
        yield con
    finally:
        con.close()


@contextmanager
def transacao():
    """BEGIN IMMEDIATE reserva o lock de escrita antes do INSERT."""
    with conexao() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise


def _ler(nome: str):
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


def apartamento_no_arquivo(numero: str) -> bool:
    return any(item["numero"] == numero for item in _ler("apartamentos.json"))


def preparar() -> None:
    """Semeia só se o arquivo do banco ainda não existe. Subir a API não restaura."""
    DB_CONDOMINIO.parent.mkdir(parents=True, exist_ok=True)
    if not DB_CONDOMINIO.exists():
        restaurar_dados()
        return
    with conexao() as con:
        con.executescript(ESQUEMA)


def restaurar_dados() -> None:
    """Recria reservas e visitantes a partir de dados/. Não mexe em sessões nem em códigos já emitidos."""
    DB_CONDOMINIO.parent.mkdir(parents=True, exist_ok=True)
    with conexao() as con:
        con.executescript(ESQUEMA)
    with transacao() as con:
        con.execute("DELETE FROM visitantes")
        con.execute("DELETE FROM reservas")
        con.execute("DELETE FROM areas")
        con.execute("DELETE FROM apartamentos")
        con.executemany(
            "INSERT INTO apartamentos (numero, morador) VALUES (:numero, :morador)",
            _ler("apartamentos.json"),
        )
        con.executemany(
            "INSERT INTO areas (id, nome, taxa) VALUES (:id, :nome, :taxa)",
            _ler("areas.json"),
        )
        reservas = _ler("reservas.json")
        con.executemany(
            "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (:codigo, :apartamento, :area, :data)",
            reservas,
        )
        con.executemany(
            "INSERT OR IGNORE INTO codigos_emitidos (codigo) VALUES (:codigo)",
            reservas,
        )
        con.executemany(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (:apartamento, :nome, :data)",
            _ler("visitantes.json"),
        )


def areas() -> list[dict]:
    with conexao() as con:
        return [dict(row) for row in con.execute("SELECT id, nome, taxa FROM areas ORDER BY nome")]


def resolver_area(valor: str | None) -> dict | None:
    if not valor or not str(valor).strip():
        return None
    chave = _norm(str(valor))
    encontradas = areas()
    for area in encontradas:
        if chave == _norm(area["id"]) or chave == _norm(area["nome"]):
            return area
    if len(chave) < 4:
        return None
    for area in encontradas:
        if _norm(area["nome"]).startswith(chave):
            return area
    return None


def data_valida(texto: str) -> bool:
    if not isinstance(texto, str):
        return False
    try:
        return date.fromisoformat(texto).isoformat() == texto
    except ValueError:
        return False


def reserva_por_chamada(chamada: str | None) -> dict | None:
    if not chamada:
        return None
    with conexao() as con:
        row = con.execute(
            "SELECT codigo, area, data FROM reservas WHERE chamada = ?",
            (chamada,),
        ).fetchone()
    return dict(row) if row else None


def visitante_por_chamada(chamada: str | None) -> dict | None:
    if not chamada:
        return None
    with conexao() as con:
        row = con.execute(
            "SELECT nome, data FROM visitantes WHERE chamada = ?",
            (chamada,),
        ).fetchone()
    return dict(row) if row else None


def data_ocupada(area_id: str, dia: str) -> bool:
    """Só existe ou não. Não devolve código nem apartamento."""
    with conexao() as con:
        return (
            con.execute(
                "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
                (area_id, dia),
            ).fetchone()
            is not None
        )


def reservas_ativas(apartamento: str) -> list[dict]:
    with conexao() as con:
        return [
            dict(row)
            for row in con.execute(
                "SELECT codigo, area, data FROM reservas "
                "WHERE apartamento = ? AND status = 'ativa' ORDER BY data, area",
                (apartamento,),
            )
        ]


def visitantes_do(apartamento: str) -> list[dict]:
    with conexao() as con:
        return [
            dict(row)
            for row in con.execute(
                "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, nome",
                (apartamento,),
            )
        ]


def _novo_codigo(con: sqlite3.Connection) -> str:
    while True:
        codigo = "RSV-" + "".join(secrets.choice("0123456789ABCDEF") for _ in range(6))
        try:
            con.execute("INSERT INTO codigos_emitidos (codigo) VALUES (?)", (codigo,))
            return codigo
        except sqlite3.IntegrityError:
            continue


def criar_reserva(apartamento: str, area_id: str, dia: str, chamada: str | None) -> dict:
    """Grava na hora. O índice único é a exclusividade; a conferência prévia não é."""
    with transacao() as con:
        if chamada:
            ja = con.execute(
                "SELECT codigo, area, data FROM reservas WHERE chamada = ?",
                (chamada,),
            ).fetchone()
            if ja:
                return {"ocupada": False, "codigo": ja["codigo"], "area": ja["area"], "data": ja["data"]}
        codigo = _novo_codigo(con)
        try:
            con.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data, chamada) VALUES (?, ?, ?, ?, ?)",
                (codigo, apartamento, area_id, dia, chamada),
            )
        except sqlite3.IntegrityError:
            if chamada:
                ja = con.execute(
                    "SELECT codigo, area, data FROM reservas WHERE chamada = ?",
                    (chamada,),
                ).fetchone()
                if ja:
                    return {
                        "ocupada": False,
                        "codigo": ja["codigo"],
                        "area": ja["area"],
                        "data": ja["data"],
                    }
            return {"ocupada": True}
        return {"ocupada": False, "codigo": codigo, "area": area_id, "data": dia}


def cancelar_reserva(apartamento: str, area_id: str, dia: str) -> bool:
    """UPDATE lógico. Reserva de outro apartamento não é encontrada, como uma inexistente."""
    with transacao() as con:
        cur = con.execute(
            "UPDATE reservas SET status = 'cancelada' "
            "WHERE apartamento = ? AND area = ? AND data = ? AND status = 'ativa'",
            (apartamento, area_id, dia),
        )
        return cur.rowcount > 0


def autorizar(apartamento: str, nome: str, dia: str, chamada: str | None) -> dict:
    with transacao() as con:
        if chamada:
            ja = con.execute(
                "SELECT nome, data FROM visitantes WHERE chamada = ?",
                (chamada,),
            ).fetchone()
            if ja:
                return {"nome": ja["nome"], "data": ja["data"]}
        con.execute(
            "INSERT INTO visitantes (apartamento, nome, data, chamada) VALUES (?, ?, ?, ?)",
            (apartamento, nome, dia, chamada),
        )
        return {"nome": nome, "data": dia}
