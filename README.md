# Assistente do Residencial Aurora

O morador conversa com um assistente feito com Google ADK. O modelo escolhe o caminho. Cobrança, acesso, apartamento, persistência, regulamento e a exclusividade da reserva ficam no código.

## Arquitetura

A API é FastAPI e sobe em `http://localhost:8000`. O `Runner` executa um `App` resumível. Há um agente principal e três especialistas.

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `concierge` | Recebe o morador e só encaminha. Não tem tool de dados e não recebe o regulamento. | Raiz do `App`, em [`src/aurora/agentes.py`](src/aurora/agentes.py) | Um único ponto de contato, com o contexto enxuto. |
| `reservas` | Disponibilidade, listar, reservar e cancelar as reservas do apartamento da sessão. | `sub_agents`, por `transfer_to_agent` | A confirmação de cobrança nasce aqui e fica na sessão persistida. O Runner devolve a aprovação a quem pediu a tool. |
| `visitantes` | Listar e autorizar visitantes do apartamento da sessão. | `sub_agents`, por `transfer_to_agent` | A autorização sempre pede confirmação, pelo mesmo motivo. |
| `regulamento` | Responde uma dúvida com artigos de um único capítulo. | `AgentTool` chamado pelo concierge | O ADK roda esse especialista numa sessão em memória e só devolve a resposta curta. O capítulo não entra no histórico do morador. |

Os especialistas podem voltar ao concierge e não transferem direto um para o outro (`disallow_transfer_to_parent=False`, `disallow_transfer_to_peers=True`). Bloquear a volta ao pai faz a aprovação responder sem gravar.

Reservas e visitantes são lidos e gravados só por tools, em [`src/aurora/ferramentas.py`](src/aurora/ferramentas.py). O banco do condomínio é SQLite em `var/condominio.db`. As sessões do ADK ficam em `var/sessoes.db`, via `DatabaseSessionService`. Nenhum dos dois depende de container.

Os quatro agentes usam o mesmo modelo, em [`src/aurora/modelo.py`](src/aurora/modelo.py). A fila começa em `GEMINI_MODEL` (vazio: `gemini-3.8-flash`). Se a chamada volta 429, 503 ou 404, a seguinte tenta `gemini-2.5-flash`, depois `gemini-3.5-flash` e `gemini-3.5-flash-lite`. A troca fica valendo para os quatro.

## Garantias

### 1. Cobrança ou acesso só com confirmação

[`src/aurora/ferramentas.py`](src/aurora/ferramentas.py) pede confirmação antes da função rodar. Reserva só quando a área tem taxa e a data está livre. Visitante sempre. Quadra e cancelamento próprio não pedem.

```python
FunctionTool(reservar_area, require_confirmation=_reserva_pede_confirmacao),
FunctionTool(autorizar_visitante, require_confirmation=True),
```

A função de reserva com taxa ainda recusa gravar se `tool_confirmation.confirmed` não for verdadeiro. Texto do chat entra em `/mensagens` como `Part(text=...)`, nunca como `FunctionResponse`.

[`src/aurora/api.py`](src/aurora/api.py), `pendencias`, monta `confirmacoes_pendentes` a partir dos eventos gravados: `FunctionCall` `adk_request_confirmation` sem `FunctionResponse` do mesmo `id`. `detalhes` traz área e data, ou nome e data. A rota de confirmações responde `409` se o `id` não está pendente naquela sessão e, nesse caso, não chama o Runner. Se está, manda `FunctionResponse(name="adk_request_confirmation")` com o `invocation_id` do pedido.

Por que não depende do modelo: quem pausa a tool é o `FunctionTool`. "Já estou confirmando aqui" não gera essa resposta. O `409` é da API, antes do ADK, inclusive para um `id` já respondido.

### 2. Cada sessão pertence a um apartamento

O apartamento entra uma vez, na criação da sessão, no state:

```python
state={CHAVE_APARTAMENTO: apartamento}
```

Toda tool lê esse valor. Nenhuma recebe apartamento como argumento.

```python
def _apartamento(tool_context: ToolContext) -> str:
    return tool_context.state[CHAVE_APARTAMENTO]
```

Listar e cancelar filtram `apartamento = ?`. Cancelar reserva de outra unidade devolve o mesmo "não encontrada" de uma reserva inexistente. `data_ocupada` devolve só se a data está livre ou ocupada.

Por que não depende do modelo: ele não tem parâmetro para trocar o apartamento. O state não é escrito de novo. A resposta da tool não carrega código, nome ou número de outra unidade.

### 3. Nada se perde no reinício

[`src/aurora/runtime.py`](src/aurora/runtime.py) sobe o `App` com `ResumabilityConfig(is_resumable=True)` e o `DatabaseSessionService` em `var/sessoes.db`. `APP_NAME` e `USER_ID` são constantes, então o reinício acha a mesma sessão. Reservas e visitantes ficam em `var/condominio.db`. Subir a API não restaura: [`preparar`](src/aurora/condominio.py) só semeia se o arquivo do banco ainda não existe. As pendências são relidas dos eventos, não de memória.

Por que não depende do modelo: conversa, state e dados estão em disco.

### 4. O regulamento é consultado, não carregado

O concierge não tem o texto do regulamento na instrução. A dúvida vai para o `AgentTool`:

```python
tools=[AgentTool(agent=regulamento)],
```

[`src/aurora/regulamento.py`](src/aurora/regulamento.py), `buscar`, escolhe um capítulo de [`dados/regulamento.md`](dados/regulamento.md) e devolve no máximo três artigos dele. A sessão do especialista é em memória. No histórico do morador ficam a chamada da tool e a resposta curta, não os capítulos de outros assuntos. A piscina aos domingos fecha às 20h, no art. 22.

Por que não depende do modelo: a tool não tem os outros capítulos para colar, e o arquivo inteiro não entra nas instruções nem no histórico.

### 5. Dois moradores, uma reserva

O índice único parcial vale no `INSERT`, não numa consulta feita antes:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS reserva_ativa_unica
    ON reservas(area, data) WHERE status = 'ativa';
```

A escrita abre a própria conexão e faz `BEGIN IMMEDIATE`, com `busy_timeout`. `IntegrityError` vira `{"ocupada": true}` e a API responde `200`. O lock de asyncio é por sessão, então as duas aprovações correm juntas. Cancelar faz `UPDATE status = 'cancelada'`. O código novo entra em `codigos_emitidos` na mesma transação. A restauração não esvazia essa tabela, então um código cancelado não volta.

Por que não depende do modelo: quem decide o vencedor é o SQLite, no instante da gravação.

## Como rodar

### Pré-requisitos

- [uv](https://docs.astral.sh/uv/). Ele usa o Python 3.12 de [`.python-version`](.python-version) se precisar.
- Uma chave do [Google AI Studio](https://aistudio.google.com/apikey).
- Nenhum banco externo. Os arquivos SQLite ficam em `var/`.

### Variáveis do `.env`

Copie [`.env.example`](.env.example) para `.env`. O exemplo só tem os nomes, sem valores. O `.env` não entra no Git.

| Variável | Obrigatória | O que é |
|---|---|---|
| `GOOGLE_API_KEY` | sim | Chave do Google AI Studio. |
| `GOOGLE_GENAI_USE_VERTEXAI` | não | Vazio usa o AI Studio (`FALSE`). |
| `GEMINI_MODEL` | não | Primeiro modelo da fila. Vazio usa `gemini-3.8-flash`. |
| `GEMINI_MODELOS` | não | Próximos modelos, separados por vírgula. Vazio tenta `gemini-2.5-flash`, `gemini-3.5-flash` e `gemini-3.5-flash-lite`. |

### Comandos

```bash
cp .env.example .env
uv sync
uv run restaurar
uv run api
```

`uv run restaurar` recria reservas e visitantes a partir de `dados/` e preserva sessões e códigos já emitidos. `uv run api` escuta em `http://127.0.0.1:8000`. Para continuar a conversa depois de Ctrl+C, suba de novo só com `uv run api`, sem restaurar.

Conferência inicial:

```bash
curl -s http://localhost:8000/apartamentos/101/reservas
curl -s http://localhost:8000/apartamentos/302/visitantes
```

A primeira lista a `RSV-1377`. A segunda lista Marina Duarte.
