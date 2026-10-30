"""App resumível, Runner e sessões do ADK em SQLite."""

from google.adk.apps import App
from google.adk.apps import ResumabilityConfig
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService

from .agentes import concierge
from .config import APP_NAME
from .config import DB_SESSOES

app_adk = App(
    name=APP_NAME,
    root_agent=concierge,
    resumability_config=ResumabilityConfig(is_resumable=True),
)


def criar_runner() -> Runner:
    DB_SESSOES.parent.mkdir(parents=True, exist_ok=True)
    sessoes = DatabaseSessionService(f"sqlite+aiosqlite:///{DB_SESSOES}")
    return Runner(app=app_adk, session_service=sessoes)


runner = criar_runner()
