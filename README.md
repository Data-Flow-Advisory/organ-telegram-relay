# organ-telegram-relay

A **pure decider** ([organ contract](https://github.com/Data-Flow-Advisory))
extracted from discovery-engine's `app/services/telegram_relay.py`. It reads
facts, returns advice, and **never touches the network** — the spine performs
the actual Telegram HTTP calls.

The source module mixed two concerns: side-effecting Telegram I/O (sendMessage /
answerCallbackQuery / editMessageText, plus DB reads of operator chat_ids) and
the pure *relay policy* taken around that I/O. This organ extracts only the
policy.

## Interface

Input — one JSON object on **stdin** (or the file named by `ORGAN_INPUT`):

```json
{ "state": { "op": "<operation>", ... }, "context": { ... } }
```

Output — one JSON object on **stdout**:

```json
{ "output": { ... }, "rationale": "<why>", "self_metric": { "confidence": 0.0 } }
```

`decide(state, context)` dispatches on `state.op`. Unknown/missing op → safe
no-op (`recognised: false`, confidence 0.0). Exit 0 always means "decided";
non-zero only on unparseable input.

## Operations

| `op` | decides | key output |
|------|---------|-----------|
| `parse_operator_ids` | parse the comma-separated `TELEGRAM_OPERATOR_CHAT_ID` spec | `chat_ids[]`, `rejected[]` |
| `verify_operator` | is a `chat_id` an authorised operator (operator allowlist then user allowlist)? | `authorized`, `matched_source` |
| `verify_webhook` | does the inbound secret-token header match (constant-time)? secret unset → pass | `verified`, `reason` |
| `is_configured` | bot token present **and** ≥1 operator id? | `configured` |
| `parse_correlation` | the chat_id encoded in a `telegram_<chat>_<msg>` correlation_id | `chat_id`, `is_telegram`, `routable` |
| `build_message` | the outbound payload: text truncated to 4000 chars, 1D/2D buttons normalised into `inline_keyboard` | `can_send`, `payload`, `truncated` |
| `broadcast` | recipients + payload for a fan-out to every operator | `can_send`, `recipients[]`, `payload` |

### Faithful mapping from `telegram_relay.py`

- `_operator_chat_ids()` → `parse_operator_ids` (tolerates whitespace/stray
  commas, single int still parses, bad tokens dropped not fatal)
- `verify_operator()` → `verify_operator` (env allowlist first, then per-user
  `User.telegram_chat_id`)
- `verify_webhook_secret()` → `verify_webhook` (constant-time compare; unset
  secret preserves the chat-id-only perimeter)
- `is_configured()` → `is_configured`
- `parse_telegram_correlation()` → `parse_correlation`
- `send_message()` guards + `_build_inline_keyboard()` + `_normalise_button()` →
  `build_message` (no-send when token missing or text empty; button text +
  callback_data capped at 64; dead buttons dropped)
- `broadcast_to_operators()` → `broadcast` (empty `{}` unless configured)

## Examples

```bash
echo '{"state":{"op":"verify_operator","chat_id":12345},
       "context":{"operator_chat_ids":[12345]}}' | python3 organ.py

ORGAN_INPUT=samples/build_message_with_buttons.json python3 organ.py
```

## Hard rules upheld

1. **No side effects** — no network, no DB, no mutation. Facts arrive in `state`.
2. **Deterministic** given the same input (operator ids are sorted; keys stable).
3. **Fail-safe to the conservative verdict** — deny authorisation / refuse to
   send on malformed or empty state, never a confident-wrong "authorised".
4. **Self-contained** — stdlib only (`json`, `os`, `sys`, `secrets`).

## Develop

```bash
python3 -m pytest -q        # 55 tests
python3 check_contract.py   # contract on every sample + empty state
```

CI (`.github/workflows/conformance.yml`) runs both on every push.
