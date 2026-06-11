#!/usr/bin/env python3
"""
Telegram-Relay Organ — pure decision logic extracted from discovery-engine's
``app/services/telegram_relay.py``.

A pure, stdlib-only decider that reads ``{state, context}`` JSON on stdin (or via
the ``ORGAN_INPUT`` env var) and writes ``{output, rationale, self_metric}`` on
stdout. It NEVER touches the network — the original module mixed two things:

  1. side-effecting Telegram HTTP I/O (sendMessage / answerCallbackQuery /
     editMessageText), plus DB reads of operator chat_ids, and
  2. pure *relay policy* — the decisions taken around that I/O.

This organ extracts only (2). Given a relay request it decides:

  - which chat_ids are authorised operators (``verify_operator``),
  - whether an inbound webhook's secret header is valid (``verify_webhook``),
  - whether the bot is configured well enough to send (``is_configured``),
  - the chat_id encoded in a ``telegram_<chat>_<msg>`` correlation id
    (``parse_correlation``),
  - the comma-separated operator-id env var parsed to a clean set
    (``parse_operator_ids``),
  - the exact outbound message payload — text truncated to Telegram's cap and a
    1D/2D button spec normalised into Telegram's ``inline_keyboard`` wire shape
    (``build_message``),
  - the set of recipients + payload for a fan-out to every operator
    (``broadcast``).

Faithful mapping from telegram_relay.py:
  - ``_operator_chat_ids()``        -> op="parse_operator_ids"
  - ``verify_operator()``           -> op="verify_operator"
  - ``verify_webhook_secret()``     -> op="verify_webhook" (constant-time compare)
  - ``is_configured()``             -> op="is_configured"
  - ``parse_telegram_correlation()``-> op="parse_correlation"
  - ``send_message()`` guards +
    ``_build_inline_keyboard()`` +
    ``_normalise_button()``         -> op="build_message"
  - ``broadcast_to_operators()``    -> op="broadcast"

Contract:
  INPUT:  {"state": {"op": "<operation>", ...}, "context": {...}}
  OUTPUT: {"output": {...}, "rationale": "<why>", "self_metric": {"confidence": 0.0, ...}}

The organ is pure: all inputs arrive via JSON, it makes no DB/network calls, it
is deterministic, and it fails safe — deny authorisation / refuse to send — on
malformed or empty state. A "no / not-authorised / cannot-send" verdict is still
exit 0; the organ succeeded at *deciding*.
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple


# Mirrors telegram_relay._MAX_TELEGRAM_CHARS.
_MAX_TELEGRAM_CHARS = 4000
# Telegram caps inline-button text and callback_data at 64 chars/bytes.
_BUTTON_TEXT_CAP = 64
_CALLBACK_DATA_CAP = 64

_CORRELATION_PREFIX = "telegram_"

_KNOWN_OPS = {
    "parse_operator_ids",
    "verify_operator",
    "verify_webhook",
    "is_configured",
    "parse_correlation",
    "build_message",
    "broadcast",
}


# --------------------------------------------------------------------------- #
# Helpers (pure)
# --------------------------------------------------------------------------- #
def _coerce_int(value: Any) -> Optional[int]:
    """Best-effort int coercion. Rejects bool (an int subclass) explicitly so a
    JSON ``true`` never silently becomes chat_id 1. Returns None on failure."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (ValueError, AttributeError):
            return None
    return None


def _parse_operator_ids(raw: Any) -> Tuple[List[int], List[str]]:
    """Parse a comma-separated operator-id spec into (accepted, rejected).

    Mirrors ``telegram_relay._operator_chat_ids``: tolerates whitespace and
    stray commas; a single int still parses; non-integer tokens are rejected
    (logged in the source, collected here) rather than poisoning the whole set.

    Accepts either the raw env-var string or an already-split JSON list.
    Accepted ids are de-duplicated and returned sorted for determinism.
    """
    tokens: List[str] = []
    if isinstance(raw, list):
        tokens = [str(t) for t in raw]
    elif raw is None:
        tokens = []
    else:
        tokens = str(raw).split(",")

    accepted: set = set()
    rejected: List[str] = []
    for tok in tokens:
        s = str(tok).strip()
        if not s:
            continue
        cid = _coerce_int(s)
        if cid is None:
            rejected.append(s)
        else:
            accepted.add(cid)
    return sorted(accepted), rejected


def _id_set(values: Any) -> set:
    """Coerce a JSON list (or scalar) of chat-ids into a set of ints, dropping
    anything that won't coerce. Used for the operator/user allowlists handed in
    via context."""
    out: set = set()
    if values is None:
        return out
    if not isinstance(values, list):
        values = [values]
    for v in values:
        cid = _coerce_int(v)
        if cid is not None:
            out.add(cid)
    return out


def _normalise_button(b: Any) -> Optional[Dict[str, str]]:
    """Coerce a caller-facing button dict into Telegram's wire shape.

    Accepts ``{"label": ..., "callback_data": ...}`` (operator-friendly) or the
    raw Telegram ``{"text": ..., "callback_data": ...}`` shape. Returns None when
    the dict has no text, or neither callback_data nor url — a dead button is
    dropped rather than rejected so one malformed entry can't lose the whole
    keyboard. Mirrors ``telegram_relay._normalise_button``.
    """
    if not isinstance(b, dict):
        return None
    text = b.get("text") or b.get("label") or ""
    if not text:
        return None
    out: Dict[str, str] = {"text": str(text)[:_BUTTON_TEXT_CAP]}
    cb = b.get("callback_data")
    url = b.get("url")
    if cb:
        out["callback_data"] = str(cb)[:_CALLBACK_DATA_CAP]
    elif url:
        out["url"] = str(url)
    else:
        return None
    return out


def _build_inline_keyboard(buttons: Any) -> Optional[List[List[Dict[str, str]]]]:
    """Normalise a 1D or 2D button spec into Telegram's 2D ``inline_keyboard``.

    Returns None when the input is None/empty/invalid so the caller can omit
    ``reply_markup`` entirely. A 1D list becomes a single-column keyboard (one
    button per row); a 2D list is taken row-by-row. Mirrors
    ``telegram_relay._build_inline_keyboard``.
    """
    if not buttons or not isinstance(buttons, list):
        return None
    is_2d = all(isinstance(row, list) for row in buttons)
    rows: List[List[Dict[str, str]]] = []
    if is_2d:
        for row in buttons:
            norm = [nb for nb in (_normalise_button(b) for b in row) if nb]
            if norm:
                rows.append(norm)
    else:
        for b in buttons:
            nb = _normalise_button(b)
            if nb:
                rows.append([nb])
    return rows or None


def _constant_time_eq(a: str, b: str) -> bool:
    """Constant-time string equality, mirroring ``secrets.compare_digest`` use in
    ``telegram_relay.verify_webhook_secret`` (avoids timing side-channels)."""
    import secrets as _secrets

    return _secrets.compare_digest(str(a), str(b))


# --------------------------------------------------------------------------- #
# Operation handlers
# --------------------------------------------------------------------------- #
def _op_parse_operator_ids(state: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    raw = state.get("raw")
    if raw is None:
        raw = state.get("operator_chat_id")
    accepted, rejected = _parse_operator_ids(raw)
    output = {
        "chat_ids": accepted,
        "rejected": rejected,
        "count": len(accepted),
    }
    rationale = (
        f"Parsed operator-id spec into {len(accepted)} valid chat_id(s)"
        + (f"; dropped {len(rejected)} non-integer token(s): {rejected}." if rejected else ".")
    )
    confidence = 1.0 if (accepted or not rejected) else 0.7
    return {
        "output": output,
        "rationale": rationale,
        "self_metric": {"confidence": confidence, "accepted": len(accepted), "rejected": len(rejected)},
    }


def _op_verify_operator(state: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    cid = _coerce_int(state.get("chat_id"))
    operator_ids = _id_set(context.get("operator_chat_ids"))
    user_ids = _id_set(context.get("user_chat_ids"))

    if cid is None:
        return {
            "output": {"authorized": False, "matched_source": None},
            "rationale": "chat_id missing or non-integer — denied (fail-safe; never leak the bot to unknowns).",
            "self_metric": {"confidence": 1.0, "reason": "no_chat_id"},
        }

    matched_source: Optional[str] = None
    if cid in operator_ids:
        matched_source = "operator"
    elif cid in user_ids:
        matched_source = "user"

    authorized = matched_source is not None
    rationale = (
        f"chat_id {cid} matched the {matched_source} allowlist — authorised."
        if authorized
        else f"chat_id {cid} not in operator ({len(operator_ids)}) or user ({len(user_ids)}) allowlists — denied."
    )
    return {
        "output": {"authorized": authorized, "matched_source": matched_source},
        "rationale": rationale,
        "self_metric": {"confidence": 1.0, "operator_count": len(operator_ids), "user_count": len(user_ids)},
    }


def _op_verify_webhook(state: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    # The secret can be configured via context (preferred) or state.
    expected = context.get("webhook_secret", state.get("webhook_secret"))
    expected = (str(expected).strip() if expected is not None else "") or None
    header = state.get("header_value")
    header = str(header) if header is not None else None

    if expected is None:
        # No secret configured — preserve the chat-id-only perimeter (opt-in secret).
        return {
            "output": {"verified": True, "reason": "no_secret_configured"},
            "rationale": "No webhook secret configured — verification skipped (opt-in); chat-id gate still applies.",
            "self_metric": {"confidence": 1.0, "secret_configured": False},
        }
    if not header:
        return {
            "output": {"verified": False, "reason": "missing_header"},
            "rationale": "Webhook secret configured but request carried no secret-token header — rejected.",
            "self_metric": {"confidence": 1.0, "secret_configured": True},
        }
    verified = _constant_time_eq(expected, header)
    return {
        "output": {"verified": verified, "reason": "match" if verified else "mismatch"},
        "rationale": (
            "Secret-token header matched the configured secret (constant-time) — accepted."
            if verified
            else "Secret-token header did NOT match the configured secret — rejected."
        ),
        "self_metric": {"confidence": 1.0, "secret_configured": True},
    }


def _op_is_configured(state: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    # token presence can be declared either as a bool or by handing the raw token.
    token_present = context.get("token_present")
    if token_present is None:
        token = (context.get("token") or state.get("token") or "")
        token_present = bool(str(token).strip())
    else:
        token_present = bool(token_present)

    operator_ids = _id_set(context.get("operator_chat_ids"))
    if "operator_chat_ids" not in context:
        # fall back to parsing a raw spec if that's what was provided.
        accepted, _ = _parse_operator_ids(context.get("operator_chat_id", state.get("operator_chat_id")))
        operator_ids = set(accepted)

    configured = bool(token_present) and bool(operator_ids)
    rationale = (
        "Configured — bot token present AND at least one operator chat_id set."
        if configured
        else f"Not configured — token_present={token_present}, operator_ids={len(operator_ids)}."
    )
    return {
        "output": {"configured": configured, "token_present": bool(token_present), "operator_count": len(operator_ids)},
        "rationale": rationale,
        "self_metric": {"confidence": 1.0},
    }


def _op_parse_correlation(state: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    cid_raw = state.get("correlation_id")
    correlation_id = str(cid_raw) if cid_raw is not None else ""

    chat_id: Optional[int] = None
    is_telegram = bool(correlation_id) and correlation_id.startswith(_CORRELATION_PREFIX)
    if is_telegram:
        parts = correlation_id.split("_")
        if len(parts) >= 3:
            chat_id = _coerce_int(parts[1])

    # Mirror source: prefix present but malformed body -> chat_id None, not telegram-routable.
    routable = chat_id is not None
    rationale = (
        f"correlation_id encodes telegram chat_id {chat_id} — outbound reply routes there."
        if routable
        else (
            "correlation_id has the telegram_ prefix but no parseable chat_id — not routable."
            if is_telegram
            else "correlation_id is not a telegram_ correlation — no Telegram outbound."
        )
    )
    return {
        "output": {"chat_id": chat_id, "is_telegram": is_telegram, "routable": routable},
        "rationale": rationale,
        "self_metric": {"confidence": 1.0},
    }


def _build_payload(chat_id: Optional[int], text: Any, buttons: Any, max_chars: int) -> Tuple[Dict[str, Any], bool]:
    """Construct the outbound message payload + whether text was truncated."""
    body = str(text) if text is not None else ""
    truncated = len(body) > max_chars
    body = body[:max_chars]
    payload: Dict[str, Any] = {"text": body}
    if chat_id is not None:
        payload["chat_id"] = chat_id
    markup = _build_inline_keyboard(buttons)
    if markup is not None:
        payload["reply_markup"] = {"inline_keyboard": markup}
    return payload, truncated


def _op_build_message(state: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    token_present = context.get("token_present")
    if token_present is None:
        token = (context.get("token") or "")
        token_present = bool(str(token).strip())
    else:
        token_present = bool(token_present)

    max_chars = _coerce_int(context.get("max_chars")) or _MAX_TELEGRAM_CHARS
    chat_id = _coerce_int(state.get("chat_id"))
    text = state.get("text")
    has_text = bool(str(text).strip()) if text is not None else False

    # send_message() returns False (no-send) when token missing OR text empty.
    can_send = bool(token_present) and has_text

    payload, truncated = _build_payload(chat_id, text, state.get("buttons"), max_chars)

    if not token_present:
        reason = "no_token"
    elif not has_text:
        reason = "empty_text"
    else:
        reason = "ok"

    rationale = (
        f"Message ready to send to chat_id {chat_id}"
        + (" (text truncated to Telegram's cap)" if truncated else "")
        + (f"; {len(payload.get('reply_markup', {}).get('inline_keyboard', []))} keyboard row(s)." if "reply_markup" in payload else ".")
        if can_send
        else (
            "Cannot send — bot token not present (mirrors send_message returning False)."
            if reason == "no_token"
            else "Cannot send — text is empty (mirrors send_message early-return)."
        )
    )
    return {
        "output": {
            "can_send": can_send,
            "reason": reason,
            "payload": payload,
            "truncated": truncated,
            "has_buttons": "reply_markup" in payload,
        },
        "rationale": rationale,
        "self_metric": {
            "confidence": 1.0,
            "can_send": can_send,
            "truncated": truncated,
            "keyboard_rows": len(payload.get("reply_markup", {}).get("inline_keyboard", [])),
        },
    }


def _op_broadcast(state: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    token_present = context.get("token_present")
    if token_present is None:
        token = (context.get("token") or "")
        token_present = bool(str(token).strip())
    else:
        token_present = bool(token_present)

    operator_ids = _id_set(context.get("operator_chat_ids"))
    if "operator_chat_ids" not in context:
        accepted, _ = _parse_operator_ids(context.get("operator_chat_id"))
        operator_ids = set(accepted)
    recipients = sorted(operator_ids)

    max_chars = _coerce_int(context.get("max_chars")) or _MAX_TELEGRAM_CHARS
    text = state.get("text")
    has_text = bool(str(text).strip()) if text is not None else False

    # broadcast_to_operators returns {} (no recipients) unless is_configured().
    configured = bool(token_present) and bool(recipients)
    can_send = configured and has_text

    payload, truncated = _build_payload(None, text, state.get("buttons"), max_chars)

    if not configured:
        reason = "not_configured"
    elif not has_text:
        reason = "empty_text"
    else:
        reason = "ok"

    rationale = (
        f"Broadcast to {len(recipients)} operator(s): {recipients}"
        + (" (text truncated)" if truncated else "")
        + "."
        if can_send
        else (
            "No broadcast — bot not configured (token + at least one operator id required)."
            if reason == "not_configured"
            else "No broadcast — text is empty."
        )
    )
    return {
        "output": {
            "can_send": can_send,
            "reason": reason,
            "recipients": recipients if can_send else [],
            "payload": payload,
            "truncated": truncated,
        },
        "rationale": rationale,
        "self_metric": {"confidence": 1.0, "recipient_count": len(recipients) if can_send else 0},
    }


_DISPATCH = {
    "parse_operator_ids": _op_parse_operator_ids,
    "verify_operator": _op_verify_operator,
    "verify_webhook": _op_verify_webhook,
    "is_configured": _op_is_configured,
    "parse_correlation": _op_parse_correlation,
    "build_message": _op_build_message,
    "broadcast": _op_broadcast,
}


# --------------------------------------------------------------------------- #
# Contract entrypoint
# --------------------------------------------------------------------------- #
def decide(state: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Pure relay-policy decision, dispatched on ``state.op``.

    Returns ``{output, rationale, self_metric}`` per CONTRACT.md. Unknown or
    missing ops fail safe: deny / no-send with low confidence.
    """
    if not isinstance(state, dict):
        state = {}
    if not isinstance(context, dict):
        context = {}

    op = str(state.get("op", "") or "").strip().lower()
    handler = _DISPATCH.get(op)
    if handler is None:
        return {
            "output": {"op": op, "recognised": False},
            "rationale": (
                f"Unrecognised op {op!r} — fail-safe no-op. Expected one of "
                f"{sorted(_KNOWN_OPS)}."
            ),
            "self_metric": {"confidence": 0.0, "op_recognised": False},
        }
    return handler(state, context)


def run_organ(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Top-level entry: parse {state, context}, never raise (fail-safe)."""
    try:
        if not isinstance(input_data, dict):
            input_data = {}
        return decide(input_data.get("state", {}), input_data.get("context", {}))
    except Exception as exc:  # fail-safe: conservative no-op
        return {
            "output": {"recognised": False},
            "rationale": f"Error during decision, failing safe to no-op: {exc}",
            "self_metric": {"confidence": 0.0},
        }


def main() -> None:
    """CLI entry: read JSON from ORGAN_INPUT (value or file path) or stdin."""
    try:
        input_str = os.environ.get("ORGAN_INPUT")
        if input_str:
            if os.path.isfile(input_str):
                with open(input_str, "r") as f:
                    input_str = f.read()
        else:
            input_str = sys.stdin.read()

        input_data = json.loads(input_str) if input_str.strip() else {}
        result = run_organ(input_data)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except json.JSONDecodeError as exc:
        json.dump(
            {
                "output": {"recognised": False},
                "rationale": f"Invalid JSON input: {exc}",
                "self_metric": {"confidence": 0.0},
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
