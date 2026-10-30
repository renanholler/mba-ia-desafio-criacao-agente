"""Restaura reservas e visitantes. Sessões e códigos já emitidos permanecem."""

from .condominio import restaurar_dados


def main() -> None:
    restaurar_dados()
    print("Reservas e visitantes restaurados a partir de dados/. Sessões e códigos emitidos preservados.")
