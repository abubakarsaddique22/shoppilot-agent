# ShopPilot

[![ci](https://github.com/abubakarsaddique22/shoppilot-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/abubakarsaddique22/shoppilot-agent/actions/workflows/ci.yml)

E-commerce operations agent (LangGraph + LangSmith + FastAPI + AWS). Customer ki email se lekar manager ki approval ke baad refund tak ka kaam agent karta hai.

Poora plan: `ShopPilot_Agentic_AI_Blueprint_A_to_Z.pdf`. Requirements aur targets: `docs/PRD.md`.

**Bunyadi usool:** LLM sirf *proposal* deta hai, paise ka faisla plain Python code (policy engine) karta hai. Is liye galat guess se galat refund nahi ho sakta.

## Requirements

- Python 3.12 (uv khud manage karta hai, `.python-version` dekho)
- [uv](https://docs.astral.sh/uv/) (Windows: `winget install astral-sh.uv`)
- Docker Desktop (Postgres + pgvector ke liye)
- Windows par: naya Microsoft Visual C++ Redistributable x64 (https://aka.ms/vs/17/release/vc_redist.x64.exe). Purana version ho to fastembed (`onnxruntime`) import karte hi bina error ke crash ho jata hai.

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

## Ab tak ka status (Steps A se J)

| Step | Kaam | Status |
|---|---|---|
| A | Problem, users aur success targets (PRD) | Done |
| B | Accounts, keys, budget safety | Partial (AWS ka hissa baad mein) |
| C | Repo aur environment | Done |
| D | Config, secrets, logging | Done |
| E | Store backend + 200 fake orders (MockShop) | Done (Shopify baad mein) |
| F | Policy engine (refund rules) | Done |
| G | Policy knowledge base (RAG, fastembed) | Done (22 tests pass, threshold data se chuna) |
| H | Database tables + migrations | Done |
| I | Tools (agent ke haath) | Done (37 tests pass) |
| J | Pehla agent aur LangSmith dataset v0 | Done (Groq par 15/15, aur 3 repeats mein 45/45 pass) |

Baqi steps (K se Z): graph, approval, router, API, UI, evaluation, AWS deploy. Dekho blueprint.

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

## Step G: Policy knowledge base (RAG)

**Kya karta hai:** Policy documents ko search ke qabil banata hai, taake agent policy ka sahi hissa dhoond kar quote kare.

**Kyun zaroori:** Agent policy ko guess na kare. Knowledge base customer ko policy samjhati hai, aur paise ka faisla phir bhi policy engine (Step F) karta hai.

**Kya hai:**
- `kb/ingest.py`: `configs/policies/*.md` ko section ke hisaab se chunks mein todta hai. Har chunk ke shuru mein heading path hota hai (jaise `Returns > Damaged items`). Dobara chalane par purani rows replace hoti hain.
- `kb/embeddings.py`: fastembed (ONNX, CPU par chalta hai, API key nahi chahiye, torch nahi chahiye). Model pehli baar download hota hai `.cache/fastembed` mein (taqreeban 67 MB). Default model `BAAI/bge-small-en-v1.5` hai (384 numbers, `db/models.py` ke `EMBEDDING_DIM` se match karna zaroori). Badalna ho to `SHOP_EMBEDDING_MODEL` aur phir ingest dobara chalao.
- `kb/retriever.py`: `search_policy(session, query, k=4)`. Jawab mein hamesha doc aur section hota hai. Similarity kam ho (`SHOP_KB_MIN_SCORE`) to `NO_POLICY_FOUND` aata hai aur agent ko escalate karna hai.
- `SHOP_KB_MIN_SCORE=0.60`: andaze se nahi, data se chuna. Is model mein off-topic sawal bhi 0.36 se 0.50 score le jate hain, aur asli sawal 0.71 se upar rehte hain, to 0.60 beech mein hai.
- `scripts/ingest_policies.py`: policies index karta hai. `--search "sawal"` se top matches aur unke scores dikhata hai.
- `scripts/check_threshold.py`: 10 asli aur 5 off-topic sawal chala kar dono groups ka top-1 score dikhata hai aur beech ka threshold suggest karta hai. Model ya policies badlen to dobara chalao.
- Tests: `tests/unit/test_kb_ingest.py` (chunking aur ingest, nakli embedder) aur `tests/integration/test_kb_retrieval.py` (asli model, 10 sawal, sahi section top 3 mein). Dono pass hain.
- Abhi ke 15 chunks: exchange 4, returns 7, shipping 4.

```bat
uv run python scripts/ingest_policies.py
uv run python scripts/ingest_policies.py --search "how long does delivery take"
uv run python scripts/check_threshold.py
```

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

## Step I: Tool layer (agent ke haath)

**Kya karta hai:** Agent ki har salahiyat (order dekhna, refund karna, email bhejna) ko ek typed, guarded tool bana deta hai. Agent dunya ko sirf in tools se chhuta hai.

**Kyun zaroori:** Model sirf chhote arguments deta hai (jaise `order_id`, `amount_pkr`). Kaun poochh raha hai, kis customer ka ticket hai, kya role hai: ye sab model se nahi, code se aata hai. Is liye prompt injection se koi doosre customer ka order nahi dekh sakta aur na paise nikal sakta hai.

**Kya hai (`src/shoppilot/tools/`):**
- `context.py`: `RunContext` (shop, ticket, customer ki email, role). API ise JWT aur ticket se bharti hai, model se kabhi nahi. Is mein `tool_guard` (har tool ko budget, role check aur error-ko-result mein badalna), `find_action` / `record_action` (idempotency) aur `audit` bhi hain.
- `orders.py`: `get_order`, `find_orders_by_email`, `track_shipment`, `search_policy` (sab sirf padhte hain).
- `refunds.py`: `propose_refund` (sirf hisaab, paisa nahi hilta) aur `issue_refund` (paisa).
- `email.py`: `send_customer_email` aur `escalate_to_human`.
- `inventory.py`: `get_inventory`, `create_purchase_order_draft`.
- `listings.py`: `get_product`, `create_product_draft`.

| Hifazati usool | Kaise |
|---|---|
| Sirf apna order | Doosre customer ka order aur jo order hai hi nahi: dono par ek jaisa `ORDER_NOT_FOUND`, is liye kuch leak nahi hota |
| Model ko chhota jawab | Order ka `note` (customer ka untrusted text), flagged customer aur refund history model ko nahi dikhte |
| Budget | Har ticket par 6 reads aur 2 writes, warna `BUDGET_EXCEEDED` |
| Role | Viewer sirf padh sakta hai, write tool par `FORBIDDEN` |
| Dobara check | `issue_refund` ke andar policy engine phir chalta hai, aur manager/owner tier par approved `approval_id` chahiye |
| Retry safe | Har write tool `idempotency_key` leta hai. Same key par pehla result wapas aata hai, dobara refund ya draft nahi banta |
| Draft only | Purchase order aur product listing hamesha draft hote hain, agent publish ya supplier ko email nahi karta |
| Email | Recipient hamesha ticket ka customer. Template plus chhote fields, aur fields mein `@` ya link allowed nahi. Ek ticket par 3 emails tak |
| Escape hatch | `escalate_to_human` par budget aur role check nahi, hamesha chalta hai |
| Errors | Tool kabhi crash nahi karta, `{"ok": false, "error": "ORDER_NOT_FOUND"}` jaisa result deta hai jo model parh kar react kar sake |

**Abhi baqi / alag hai:**
- Tests: `tests/unit/test_tools.py` (37 pass, koi Docker ya model download nahi chahiye). Ye check karte hain: doosre customer ka order nahi dikhta, same key par refund ek hi dafa hota hai, manager/owner tier bina sahi approval ke nahi chalta, viewer write nahi kar sakta, budget khatam hone par `BUDGET_EXCEEDED`, email aur draft ki hadein, `get_inventory` ka `days_of_stock` (sales 0 ho to `None`), `get_product` ka chhota summary aur galat SKU par `PRODUCT_NOT_FOUND`.
- `sales_summary` Step P (Reports agent) mein banega, kyunke is ke liye `ShopBackend` mein nayi method chahiye.
- Email abhi sirf ticket par "outbound message" ke tor par save hota hai. SMTP se bhejna Step P mein aayega.
- Timeouts Shopify lagne par `shopify.py` mein aayenge (MockShop local hai).
- Approval ka qaida (Step M ko follow karna hai): `status == "approved"`, ticket wahi, `tier` kam az kam engine ke tier jitna, aur `payload_json["amount_pkr"]` refund ko cover kare.

## Step J: Pehla agent aur pehle traces

**Kya karta hai:** Ek bohat chhota agent banata hai jo sirf order-status tickets ka jawab deta hai, aur uska har qadam LangSmith mein nazar aata hai. Graph aur structure Step K mein aayega. Yahan sirf ye dekhna hai ke model kahan hichkichata, loop karta ya andaza lagata hai.

**Kyun zaroori:** Structure banane se pehle pata hona chahiye ke model tools sahi chalata hai ya nahi. Aur ye 15 cases ka dataset aage ki evaluation (Step U) ka beej hai. Aaj ka natija wo baseline hai jis se Step K ka naya graph mukable karega.

**Kya hai:**
- `agents/simple_agent.py`: ReAct agent (`create_agent`) jis ke paas 3 read tools hain: `get_order`, `track_shipment`, `search_policy`. Customer ka message `<customer_message>` tags mein jata hai aur prompt kehta hai ke ye data hai, hukm nahi. Agent sirf padh sakta hai, refund ya replacement ka wada nahi karta, aur customer ki zubaan (English ya Roman Urdu) mein 2 se 4 chhote jumlon mein jawab deta hai.
- `core/llm.py`: model sirf `.env` se aata hai (`SHOP_LLM_PROVIDER`, `SHOP_LLM_MODEL`). Abhi Groq `openai/gpt-oss-120b` hai. Model badalna ho to sirf `.env` badlo, code nahi. Models ka proper muqabla Step U mein 60 cases par hoga.
- `scripts/run_agent.py`: 10 tayyar tickets (ya apna ticket) chala kar dikhata hai ke agent ne kaunse tools chalaye aur kya jawab diya.
- `scripts/create_dataset_v0.py`: LangSmith par dataset `shoppilot-support-v0` banata hai (15 cases). Case mein asli order number nahi hota, balkay scenario aur index hota hai (jaise "late_delivery ka 4th order"), is liye store dobara seed karne par bhi dataset chalta hai.
- `scripts/eval_v0.py`: 15 cases chala kar teen plain-Python evaluators se number deta hai.

| Evaluator | Kya check karta hai |
|---|---|
| `tools_ok` | Zaroori tools chale aur mana wale tools nahi chale |
| `no_promise` | Jawab mein refund ya replacement ka wada nahi |
| `reply_ok` | Jawab mein wo hai jo hona chahiye aur wo nahi jo nahi dikhna chahiye (jaise doosre customer ka order) |

Ek case tab pass hota hai jab teeno 1 hon. **Natija: 15 mein se 15 pass** (Groq `openai/gpt-oss-120b`), aur `--repeats 3` (har case 3 baar) par bhi **45 mein se 45 pass**. Step J ka target kam az kam 13 tha. Ye abhi sirf 15 cases ka baseline hai, bara muqabla Step U mein 60 cases par hoga.

`order_not_found` aur `other_customer` cases mein terminal par `tool get_order failed: ORDER_NOT_FOUND` likha aata hai. Ye theek hai: tool ghalat order par crash nahi karta, error ko result bana kar model ko deta hai, aur ye cases yahi dekhte hain.

Cases ki categories: delivered, in_transit, late_shipped, late_delivered, policy, no_order_number, order_not_found, other_customer, refund_request, injection, off_topic.

**Test kaise karein:**

```bat
uv run ruff check .
uv run pytest
uv run python scripts/run_agent.py --preset 4
uv run python scripts/run_agent.py --all
uv run python scripts/run_agent.py --ticket "Mera order #88601 kahan hai?" --email ali@example.com
uv run python scripts/create_dataset_v0.py
uv run python scripts/eval_v0.py
uv run python scripts/eval_v0.py --repeats 3
```

- `run_agent.py` aur `eval_v0.py` ko seeded Postgres (`seed_mockshop.py`), indexed policies (`ingest_policies.py`) aur `.env` mein Groq aur LangSmith keys chahiye.
- `create_dataset_v0.py` ek hi dafa chalana hai. Dobara banana ho to `--recreate`.
- `--repeats 3` har case 3 baar chalata hai. Model ka jawab har dafa thoda alag ho sakta hai, is liye ek lucky pass pe poora bharosa nahi.
- Har run LangSmith project `shoppilot-dev` mein dikhta hai (har node, LLM call aur tool call).

Step J ke apne alag unit tests nahi hain, kyunke ye asli model par chalne wali evaluation hai. `uv run pytest` sirf ye confirm karta hai ke baqi project nahi toota.

---

## Project ka naqsha

```
src/shoppilot/
  core/      config, logging, errors, llm       (D, J)
  shop/      ShopBackend + MockShop + seed      (E)
  policy/    refund rules, limits               (F)
  db/        tables + Alembic migrations        (H)
  kb/        policy knowledge base              (G)
  tools/     typed, guarded tools               (I)
  agents/    simple_agent.py                    (J; baqi files K se abhi khali)
  approvals/ guardrails/ api/                   (M se T, abhi khali)
configs/     settings, policy docs, prompts
scripts/     seed, logs, ingest, eval
tests/       unit, graph, integration, redteam
ui/          HTML, CSS, JavaScript              (R, abhi khali)
infra/       AWS, Caddy, Docker                 (X, Y, abhi khali)
```

Tools ka asal order, design aur safety rules: blueprint ke sections 3, 8, 9 aur 13.
