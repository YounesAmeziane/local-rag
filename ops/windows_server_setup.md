# Windows Server setup runbook

For deploying local-rag on a Windows Server machine. Written for someone else
to execute — the assistant that wrote this has no access to your server and
has not run these steps against real hardware. Each `.ps1` script here was
syntax-verified (parsed, not executed) and reviewed carefully, but treat this
as a reviewed draft, not a tested procedure. Read each script before running it.

See `README.md` for how the application itself works, and
`SERVER_MIGRATION.md` for the decisions behind this runbook (why Ollama over
vLLM/LM Studio, why RBAC isn't built yet, etc).

## Prerequisites

- Windows Server with the ODBC Driver 17/18 for SQL Server installed.
- Network/firewall access from this server to your SQL Server instance.
- A plain local disk path for data that is **not** under a synced/redirected
  folder (OneDrive, KFM-redirected Desktop/Documents) — this project hit a
  real corruption risk from exactly that before. Pick something like
  `D:\local-rag-data\`.
- This repo, cloned or copied onto the server.
- Python 3.10+, `pip install -r requirements.txt`.
- **NSSM** (https://nssm.cc/download) — needed to run `qdrant.exe` as a
  Windows Service (see step 2; a plain `sc.exe create` does not work against
  it, confirmed on a real deployment).

## 1. Decide on a model quant (VRAM-dependent — self-serve)

I don't know this server's GPU/VRAM, so this is a decision table, not a fixed
recommendation:

| Available VRAM | Suggested approach |
|---|---|
| ~8–12 GB | A smaller/more aggressively quantized model than the 27B this project developed against (e.g. a Q4 quant of a smaller model). Full re-validation against `test_sql.py`/`test_chat.py`/`test_docs.py` needed — quality will differ from what's documented in README §13. |
| ~16–24 GB | `qwen3.8-27b` at a Q4–Q5 GGUF quant should fit with headroom for context + `OLLAMA_NUM_PARALLEL` slots. |
| ~24–48 GB | The Q6_K quant this project developed and validated against (23.36 GB on disk) fits comfortably — closest to the results already documented. |
| 48 GB+ | Q6_K or even a higher-precision quant; more VRAM mainly buys headroom for `OLLAMA_NUM_PARALLEL` and larger context, not necessarily better answers at this model size. |

Check actual VRAM with `nvidia-smi` (NVIDIA) or the equivalent for your GPU
vendor. When in doubt, start with the Q6_K quant already proven in this
project and drop down only if it doesn't fit.

## 2. Qdrant

Download **NSSM** first: https://nssm.cc/download (the win64 build, unless
this is a 32-bit box). `qdrant.exe` is a plain console app — it doesn't
implement the Windows Service Control Protocol, so registering it directly
via `sc.exe create` does not work (confirmed on a real run: it fails with
Event ID 7000/7009, "did not respond to the start or control request in a
timely fashion," no matter what arguments/env vars are set). NSSM wraps a
plain exe so it behaves as a real service.

Run `install_qdrant_service.ps1` (as Administrator):
```powershell
.\install_qdrant_service.ps1 -QdrantExe "C:\qdrant\qdrant.exe" -NssmExe "C:\nssm\nssm.exe" -DataDir "C:\local-rag-data\qdrant"
```
This registers Qdrant as an auto-starting Windows Service via NSSM, with its
storage pointed at the given (non-synced) directory and its stdout/stderr
logged to `qdrant-service.log` inside that same directory. It starts empty — after it's up,
run `python ingest.py` and `python ingest_docs.py` from this repo (against
`QDRANT_HOST=127.0.0.1`) to populate the `data_dictionary` and `documents`
collections.

## 3. Ollama (chat/reasoning server)

1. Install Ollama: https://ollama.com/download/windows
2. Run `setup_ollama.ps1` (as Administrator):
   ```powershell
   .\setup_ollama.ps1 -NumParallel 4 -ModelTag qwen3.8-27b
   ```
   `-NumParallel` should roughly match your expected concurrent-user count —
   this project measured locally (LM Studio, same underlying concurrency
   tradeoff) that a higher parallel count divides the context window across
   more slots, so don't set it far higher than you actually need.
3. Read the script's own output carefully — it explicitly flags two things it
   can't verify without your server: whether Ollama registered as a real
   Windows Service (vs. a per-user background app that won't survive an
   unattended reboot), and whether the exact model tag pulled successfully or
   needs the manual Modelfile fallback.
4. Embeddings (`nomic-embed-text`) also run through this same Ollama instance
   — `ollama pull nomic-embed-text` if it isn't already there.

## 4. SQL Server read-only login

Run `sql\create_readonly_login.sql` (already in this repo, unchanged) against
your SQL Server if it hasn't been run already. Then fill in
`DB_READONLY_USER`/`DB_READONLY_PASSWORD` in the server `.env` (step 5) — the
connection pool (`db_pool.py`) and `ingest.py` both pick this up automatically
once set; without it, everything falls back to the service's own Windows
identity (a startup warning fires from `config.py` if only one of the two is
set, not both).

## 5. Application `.env`

Copy `ops\server.env.example` to `.env` in the repo root and fill in the
placeholders (`DB_SERVER`, the read-only login, etc). This differs from the
repo-root `.env.example` (which is the local-dev/LM Studio version) mainly in
pointing `REASON_BASE_URL` at Ollama instead of LM Studio, and in setting
`DB_POOL_SIZE` for concurrent load.

## 6. Run the app

```powershell
python webui.py
```
Verify: `curl http://127.0.0.1:8080/state` should report the Ollama model as
online. Ask a test question through the UI at `http://127.0.0.1:8080` — try
one SQL-route question (e.g. "how many active rules are there?") since that
exercises the DB pool + Ollama end-to-end together.

**Not solved by this runbook:** `webui.py`'s `HOST` is hardcoded `127.0.0.1`
in the code — it is only reachable from this machine itself as shipped.
Making it reachable from other machines on the network requires a code change
(bind `0.0.0.0`) plus, at minimum, a shared-token guard, since there is
currently no authentication at all. See `SERVER_MIGRATION.md` /
`README.md` §8 before deciding to expose it further — this was left
unsolved deliberately (pending a real identity source), not by oversight.

## 7. Running `webui.py` unattended (no one logged in)

Not scripted here, since the right mechanism depends on how you've set up
Ollama (step 3) and how strict your server's policies are. Two common options:
a Windows Service wrapper (e.g. NSSM) around `python webui.py`, or a
Scheduled Task set to "Run whether user is logged on or not" with the trigger
set to "At startup." Either way, make sure whatever account runs it has the
same environment (`.env` picked up correctly, VPN/network access to SQL
Server if needed) as when you tested it interactively in step 6.
