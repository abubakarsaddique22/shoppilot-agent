# ShopPilot

E-commerce operations agent (LangGraph + LangSmith + FastAPI + AWS). Customer ki email se lekar manager ki approval ke baad refund tak ka kaam agent karta hai.

Poora plan: `ShopPilot_Agentic_AI_Blueprint_A_to_Z.pdf`. Requirements aur targets: `docs/PRD.md`.

**Bunyadi usool:** LLM sirf *proposal* deta hai, paise ka faisla plain Python code (policy engine) karta hai. Is liye galat guess se galat refund nahi ho sakta.

## Requirements

- Python 3.12 (uv khud manage karta hai, `.python-version` dekho)
- [uv](https://docs.astral.sh/uv/) (Windows: `winget install astral-sh.uv`)
- Docker Desktop (Postgres + pgvector ke liye)

## Quick start (Windows cmd)

```bat
copy .env.example .env
uv sync
docker compose up -d db
uv run alembic upgrade head
uv run python scripts/seed_mockshop.py
uv run pytest
uv run uvicorn shoppilot.api.main:app --reload
```

`.env` mein apni `SHOP_LLM_API_KEY` aur `LANGSMITH_API_KEY` daalo. API docs: http://127.0.0.1:8000/docs

## Ab tak ka status (Steps A se H)

| Step | Kaam | Status |
|---|---|---|
| A | Problem, users aur success targets (PRD) | Done |
| B | Accounts, keys, budget safety | Partial (AWS ka hissa baad mein) |
| C | Repo aur environment | Done |
| D | Config, secrets, logging | Done |
| E | Store backend + 200 fake orders (MockShop) | Done (Shopify baad mein) |
| F | Policy engine (refund rules) | Done |
| G | Policy knowledge base (RAG) | Pending |
| H | Database tables + migrations | Done |

Baqi steps (I se Z): tools, agents, approval, API, UI, evaluation, AWS deploy. Dekho blueprint.

---

## Step A: Problem, users aur success criteria

**Kya karta hai:** Code likhne se pehle tay karta hai ke agent kis ke liye hai, kaunsa kaam karega aur kaise pata chalega ke wo sahi chal raha hai.

**Kyun zaroori:** Bina target ke "agent theek chal raha hai" sirf ehsaas hota hai. Targets number mein hon to baad mein evaluation mein check ho sakte hain.

**Kya hai:** `docs/PRD.md`
- Users: store owner, support staff, customer.
- Tasks: order status, refund ya replacement, product sawal, low-stock purchase order, product listing, daily report.
- Measurable targets: task success kam az kam 0.90, wrong-refund rate bilkul 0, policy-violating action bilkul 0, sahi escalation kam az kam 0.95, automatic ticket ka p95 time 15 second se kam.
- Autonomy levels: L0 sirf padhna, L1 draft banana, L2 hadd ke andar khud kaam karna.

## Step B: Accounts aur safety

**Kya karta hai:** LLM key, LangSmith key aur (baad mein) AWS account ko safe tareeqe se set karta hai.

**Kyun zaroori:** Keys Git mein chali jayein ya AWS ka bill achanak aa jaye to project ruk sakta hai.

**Kya hai:**
- Keys sirf `.env` mein hain, aur `.env` `.gitignore` mein hai. Git mein sirf `.env.example` jata hai (nakli values ke saath).
- LangSmith tracing ki key aur free LLM key.
- AWS account, MFA aur Budget alert abhi baqi hain. Ye Step X se pehle karna hai.

## Step C: Repository aur environment

**Kya karta hai:** Project ko aisa banata hai ke koi bhi clone karke chala sake.

**Kyun zaroori:** Reproducible setup, aur code ki quality automatic check hoti rehti hai.

**Kya hai:**
- `pyproject.toml` aur `uv.lock`: dependencies pinned. `src/shoppilot/` layout.
- `ruff` (lint), `mypy` (types), `pytest` (tests), `pre-commit` (commit se pehle check): `.pre-commit-config.yaml`.
- `docker-compose.yml`: local Postgres (pgvector image).
- `.github/workflows/ci.yml`: har push par ruff aur pytest chalta hai.
- `.vscode/`: editor settings aur debug config.

## Step D: Configuration, secrets aur logging

**Kya karta hai:** Settings code se alag rakhta hai, aur har event ka saaf record (log) banata hai.

**Kyun zaroori:** Model ya refund limit badalna ek line ka kaam hona chahiye, code edit ka nahi. Aur jab kuch ghalat ho to logs se pata chale kya hua.

**Kya hai:**
- `core/config.py`: `Settings` class (pydantic-settings). Environment variables `SHOP_` prefix se parhta hai.
- `configs/settings.dev.yaml`: limits, retries, timeouts (refund limit waghera).
- `core/logging.py`: JSON logs, har line mein `request_id`, `ticket_id`, `thread_id`. Emails aur secrets mask hote hain.
- `core/errors.py`: custom errors (`OrderNotFound`, `PolicyDenied` ...). API hamesha `{"error": {...}}` format mein jawab deti hai, traceback sirf logs mein.
- `api/main.py`: abhi sirf shuruaat: logging, request_id, `/health`, `/ready`.
- Logs dekhne ke commands: `docs/logging.md`.

## Step E: Store backend (MockShop)

**Kya karta hai:** Agent ko ek dukaan deta hai jis par wo kaam kare, bina asli Shopify ya asli customers ke.

**Kyun zaroori:** Nakli dukaan fast hai, har test mein dobara fresh ho jati hai, aur Shopify API ki wajah se kaam rukta nahi. Agent sirf `ShopBackend` interface se baat karta hai, is liye baad mein Shopify lagane par agent ka code nahi badalta.

**Kya hai:**
- `shop/base.py`: `ShopBackend` interface (`get_order`, `track_shipment`, `create_refund` ...) aur data models.
- `shop/mock_models.py`, `shop/mockshop.py`: nakli dukaan (Shopify jaise table names).
- `shop/seed.py`, `scripts/seed_mockshop.py`: 200 fake orders (late, damaged, COD, already refunded, injection attempt waghera), PKR amounts ke saath.
- `shop/shopify.py`: khali. Asli Shopify baad mein.
- `tests/unit/test_mockshop.py`: 25 tests.

```bat
uv run python scripts/seed_mockshop.py
```

Jab asli Shopify lagega to MockShop tables ki zaroorat nahi rahegi, sirf `base.py` aur `shopify.py` chalte rahenge.

## Step F: Policy engine (refund rules)

**Kya karta hai:** Refund ka faisla karta hai: kitna refund ho sakta hai aur kaun approve karega.

**Kyun zaroori:** Paise ke rules prompt mein nahi, tested code mein hone chahiye. Prompt ko model ghalat samajh sakta hai, code nahi.

**Kya hai:**
- `policy/refund_rules.py`: `evaluate_refund(...)` (pure function: na network, na database, na LLM). Jawab: tier, allowed amount, wajah aur policy reference.
- `policy/limits.py`: limits YAML se aati hain.
- `configs/policies/*.md`: insani zubaan mein policies (returns, shipping, exchange). Step G inhe knowledge base banayega.
- `tests/unit/test_refund_rules.py`: boundary tests (jaise bilkul PKR 3,000 aur bilkul din 14).

| Tier | Kab | Kaun faisla karta hai |
|---|---|---|
| auto | 14 din ke andar ya 5 din se zyada late, PKR 3,000 tak, prepaid, pehle koi refund nahi | Agent khud |
| manager | PKR 3,001 se 15,000, ya COD, ya damaged | Manager approval |
| owner | PKR 15,000 se zyada, ya flagged customer, ya 90 din mein 2 refunds | Owner approval |
| deny | Window se bahar, pehle se refunded, order abhi due nahi | Policy engine (insaan ki zaroorat nahi) |

Hard rules jo koi approval nahi tod sakti: refund kabhi (paid minus pehle ke refunds) se zyada nahi, aur ek order par ek hi open refund.

## Step G: Policy knowledge base (abhi baqi)

Policy documents ko chunks mein tod kar pgvector mein rakhna, taake agent `search_policy` tool se policy quote kar sake. Files (`kb/ingest.py`, `kb/retriever.py`) abhi khali hain.

## Step H: Database schema aur migrations

**Kya karta hai:** ShopPilot ka apna data Postgres mein rakhta hai (wo data jo Shopify ke paas hota hi nahi).

**Kyun zaroori:** Tickets, approvals aur audit record ke bagair agent na wapas aa kar approval ke baad kaam jaari rakh sakta hai, na ye bata sakta hai ke kisne kya kiya.

**Kya hai:** `db/models.py` (`AppBase`), `db/migrations/` (Alembic), `tests/unit/test_db_models.py`.

| Table | Kis liye |
|---|---|
| `tickets`, `messages` | Customer ki email aur ticket ka status |
| `approvals` | Manager/owner ka faisla (pending, approved, rejected, expired) |
| `actions` | Har side effect (jaise refund). `idempotency_key` unique hai, is liye retry par double refund nahi hota |
| `audit_log` | Kisne kab kya kiya. Sirf add hota hai: Postgres trigger UPDATE/DELETE rok deta hai |
| `users` | ShopPilot ke login aur roles (viewer, support, manager, owner, admin) |
| `policy_chunks` | Policy search ka vector database (Step G isse use karega) |
| `report_runs` | Daily report ka record |

```bat
uv run alembic upgrade head
uv run alembic check
uv run pytest tests/unit/test_db_models.py
```

MockShop tables (Step E) alag `MockBase` mein hain aur Alembic unhe nahi chhoota.

---

## Project ka naqsha

```
src/shoppilot/
  core/      config, logging, errors            (D)
  shop/      ShopBackend + MockShop + seed      (E)
  policy/    refund rules, limits               (F)
  db/        tables + Alembic migrations        (H)
  kb/        policy knowledge base              (G, abhi khali)
  tools/ agents/ approvals/ guardrails/ api/    (I se T, abhi khali)
configs/     settings, policy docs, prompts
scripts/     seed, logs, ingest, eval
tests/       unit, graph, integration, redteam
ui/          HTML, CSS, JavaScript              (R, abhi khali)
infra/       AWS, Caddy, Docker                 (X, Y, abhi khali)
```

Tools ka asal order, design aur safety rules: blueprint ke sections 3, 8, 9 aur 13.
