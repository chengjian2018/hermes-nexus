# hermes-nexus: Phrase 4 Interaction Service

hermes-nexus is the mock interaction service used by `interactive-task-food` Phrase 4.
It orchestrates an LLM-powered multi-turn phone-call simulation where the LLM plays
the user (e.g. company admin calling a restaurant) and a TerminalChannel plays the
restaurant side (real terminal input or mock script).

Project path: `~/py_projects/hermes-nexus`
Conda env: `hermes_nexus` (Python 3.11)

## Why not the base conda env

The base env (Python 3.13) lacks `pydantic-core` wheels -- pip install fails with
`ResolutionImpossible`. Always use the `hermes_nexus` env:

```bash
/Users/chengjian/miniforge3/envs/hermes_nexus/bin/python
```

## Architecture

```
Phrase 3 JSON (sections + interaction_objects)
    |
    v
build_prompt.py  -> sections formatted as system_prompt
   (role: admin calling restaurant; restaurant info from interaction_objects[0])
    |
    v
chat.py (ChatSession)  -> multi-turn LLM dialog
   detects [CONVERSATION_COMPLETE] marker -> ends session
    |
    v
channel.py (TerminalChannel)  -> terminal or mock I/O
    |
    v
returns {task_id, messages, status}
```

## Key design points

1. **Role mapping**: `role="assistant"` = LLM playing the caller (admin/user side).
   `role="user"` = restaurant staff (channel side). First user message is a fixed
   opener: "您好，餐馆预定，请问您有什么需要？"

2. **Two independent mock layers**:
   - `mock_mode` (channel): True = preset script for restaurant replies, False = real terminal input
   - `use_mock_llm` (LLM): True when no `LLM_API_KEY` set, uses hardcoded admin-style responses
   These are orthogonal. You can have mock LLM + real terminal, real LLM + mock channel, etc.

3. **Conversation end**: LLM appends `[CONVERSATION_COMPLETE]` to its reply when all
   key info is confirmed. ChatSession strips the marker, sends the clean reply, closes
   the channel, sets status="completed".

4. **Prompt role is hardcoded**: `SYSTEM_PROMPT_TEMPLATE` in `build_prompt.py` is
   fixed to "望京恒电公司行政人员打电话预定团建聚餐". To generalize, modify the
   template or parameterize it.

## Calling from Agent (recommended: direct Python)

```python
import sys, os, json

NEXUS = os.path.expanduser("~/py_projects/hermes-nexus")
sys.path.insert(0, NEXUS)
from src.chat import ChatSession

# request_data = Phrase 3 JSON output (sections + interaction_objects + summary + task_id)
session = ChatSession()  # reads LLM_API_KEY / LLM_BASE_URL / LLM_MODEL from env
result = session.run(
    request_data=request_data,
    mock_mode=True,         # True=scripted restaurant replies, False=real terminal
    mock_responses=[...],   # only when mock_mode=True
    max_turns=30,
)
# result = {"task_id": str, "messages": [{role, content}, ...], "status": str}
```

## LLM env vars (all optional)

| Var | Default | Notes |
|-----|---------|-------|
| `LLM_API_KEY` | (empty) | Empty -> mock LLM fallback |
| `LLM_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible |
| `LLM_MODEL` | `deepseek-v4-flash` | |

## HTTP API (alternative)

```bash
# Start server
cd ~/py_projects/hermes-nexus && python main.py  # port 8000

# Chat
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" -d @phrase3.json

# Health
curl http://localhost:8000/api/v1/health

# Prompt preview (no dialog)
curl "http://localhost:8000/api/v1/prompt/preview?summary=test&task_id=t1"
```

## execute_code limitation

`execute_code` runs in the base conda env (Python 3.13), which lacks the `openai`
package and has no `pydantic-core` wheels. hermes-nexus imports `openai` at module
level, so `from src.chat import ChatSession` fails in execute_code.

**Workaround**: write a temp script and run it via terminal with the hermes_nexus
env python, or use `terminal` with `/Users/chengjian/miniforge3/envs/hermes_nexus/bin/python -c '...'`.
Avoid inline `-c` with complex f-strings (backslash escaping issues); write to a
temp .py file instead.

## Verified test run (2026-08-03)

36 tests, 34 pass. 2 failures are test-assertion bugs (not code bugs):
- `test_run_max_turns_reached`: asserts `len(messages) <= 2*max_turns+1` but
  doesn't account for the fixed opener message
- `test_mock_llm_greeting`: asserts `messages[0]["role"] == "assistant"` but
  the first message is the fixed user opener

End-to-end mock run (mock LLM + mock channel, sample JSON): 6 messages,
status=completed, conversation terminated correctly on confirmation keyword.

## File map

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app: POST /api/v1/chat, GET /health, GET /prompt/preview |
| `src/build_prompt.py` | sections -> system_prompt; formats items, conflict detection, restaurant info |
| `src/chat.py` | ChatSession: prompt -> channel -> LLM loop -> [CONVERSATION_COMPLETE] detection -> return |
| `src/channel.py` | TerminalChannel: real terminal or mock script I/O, auto-close on exhaust/exit command |
| `run_demo.py` | CLI demo: interactive / --mock / --api modes |
| `望京恒电团建_用餐需求.json` | Sample Phrase 3 output (23-person team dinner, 4 candidate restaurants) |
| `test/` | pytest: test_build_prompt, test_channel, test_chat |
