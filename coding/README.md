# coding/ — a small coding agent over a plain chat endpoint

Independent of `sbmig`. One file (`agent.py`), Python 3.10+, standard library only (`requests` is used when it is
installed). It turns an API that only offers

```python
post(url, headers={"Authorization": "Bearer <token>"}, json=payload, timeout=...)
payload = {"model": "gpt-6-luna", "need_orgin": True,
           "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]}
```

into an agent that reads and edits source files and runs your tests. No tool-calling API is needed: the model
answers in a small text protocol (`prompts/system.md`) and the script executes it locally — `READ` / `LIST` /
`GREP` for inspection, `=== EDIT` with `SEARCH`/`REPLACE` blocks (or `=== WRITE`, `=== DELETE`) for changes,
`RUN` for commands, `<done>` / `<ask>` to finish. Every edit is shown as a diff and confirmed before it is written
(`--yes` to skip), every edited file is backed up under `coding/.backups/<session>/`, and the whole conversation is
appended to `coding/sessions/<session>.jsonl`.

## Setup (once)

```bash
cp coding/.env.example coding/.env     # then fill in the URL and the token; .env is git-ignored
python coding/agent.py --probe         # prints the raw response JSON and the extracted reply
```

`--probe` is the first thing to run on the company network: it shows the exact JSON the gateway returns. The reply
text is auto-detected for the usual shapes (`choices[0].message.content`, `content`, `response`, `data.content`, …).
If the probe prints `cannot find the reply text`, set `CODING_RESPONSE_PATH` to the dot path shown in the raw
JSON (for example `data.result.content`).

| variable | meaning | default |
|---|---|---|
| `CODING_API_URL` | chat endpoint URL | (required) |
| `CODING_API_KEY` | bearer token (`LLM_API_KEY` is used as a fallback) | (required) |
| `CODING_MODEL` | `model` field | `gpt-6-luna` |
| `CODING_NEED_ORIGIN` | the `need_orgin` payload flag | `true` |
| `CODING_TIMEOUT` | request timeout, seconds | `180` |
| `CODING_RESPONSE_PATH` | dot path to the reply text when auto-detection fails | (auto) |
| `CODING_EXTRA_JSON` | extra top-level payload fields, JSON (`{"temperature": 0}`) | `{}` |
| `CODING_MAX_ROUNDS` | model round-trips per task | `25` |

## Use

```bash
# one task, from the repository root you want to edit
python coding/agent.py "tests/test_labels.py::test_x fails; find the cause in sbmig/kb/labels.py and fix it"

# pre-load the files you know matter (saves a round-trip)
python coding/agent.py --files sbmig/kb/labels.py tests/kb/test_labels.py "add a test for method references"

# interactive: several tasks in one conversation (the model keeps what it read)
python coding/agent.py
you> /files sbmig/plan/jarcheck.py
you> explain how check_jars decides provided_by_ok, then make the message mention the jar path
you> run the tests for it
```

Flags: `--root DIR` (repository to work in, default cwd), `--yes` (apply edits without asking), `--allow-run`
(execute `RUN` commands without asking), `--quiet` (do not echo the model's prose), `--model`, `--url`,
`--timeout`, `--max-rounds`. In chat mode: `/files a b c`, `/yes`, `/run`, `/reset`, `/quit`.

Confirmation prompts accept `y` / `n` / `a` (yes to all for this session) / `q` (abort the task).

## How a task runs

1. The first user message is your task plus a short repository context (`git status`, top-level entries, any
   `--files` contents).
2. The model replies with text plus protocol blocks. Inspection actions run first and their output goes back as
   the next user message; edits are diffed, confirmed, applied and reported back; `RUN` output is reported back.
3. This repeats until the model says `<done>…</done>` (after running tests when there are any), asks you a
   question with `<ask>…</ask>`, or replies with plain text (treated as the answer). `CODING_MAX_ROUNDS` caps it.
4. Old tool outputs are trimmed when the conversation grows past ~160k characters, so long sessions keep working.

Tips for a small model: give it the file names (`--files`), one task at a time, and let it run the tests itself
(`RUN python -m pytest tests/x.py -q`). If an edit is rejected with "SEARCH text not found", the model is told to
READ the file again — that is normal and self-correcting.

## Safety

- Paths are confined to `--root`; anything outside is refused.
- Nothing is written without confirmation unless you pass `--yes`; `RUN` commands ask unless `--allow-run`.
- Backups of every file before each change: `coding/.backups/<session>/<path>` (delete the directory when done).
- The token is only read from the environment / `coding/.env` and never written to the transcript (the transcript
  holds messages only).

## Tests

```bash
python -m pytest coding/tests -q
```

The tests use a scripted fake endpoint; they do not call the network.
