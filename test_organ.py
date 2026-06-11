#!/usr/bin/env python3
"""Tests for the Telegram-Relay organ.

Run from anywhere — imports are path-independent (the test inserts the organ's
own directory onto sys.path so `python -m pytest` works regardless of cwd).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import organ  # noqa: E402


# --------------------------------------------------------------------------- #
# Contract shape
# --------------------------------------------------------------------------- #
def _assert_contract(result):
    assert isinstance(result, dict)
    for key in ("output", "rationale", "self_metric"):
        assert key in result, f"missing {key}"
    assert isinstance(result["self_metric"], dict)
    c = result["self_metric"]["confidence"]
    assert isinstance(c, (int, float)) and 0.0 <= c <= 1.0
    assert isinstance(result["rationale"], str) and result["rationale"]


def test_empty_state_is_safe_noop():
    r = organ.decide({}, {})
    _assert_contract(r)
    assert r["output"]["recognised"] is False
    assert r["self_metric"]["confidence"] == 0.0


def test_unknown_op_fails_safe():
    r = organ.decide({"op": "frobnicate"}, {})
    _assert_contract(r)
    assert r["output"]["recognised"] is False
    assert r["self_metric"]["confidence"] == 0.0


def test_non_dict_state_and_context():
    r = organ.run_organ({"state": "nope", "context": 7})
    _assert_contract(r)
    assert r["output"]["recognised"] is False


def test_run_organ_never_raises():
    for bad in [None, [], "x", 5, {"state": None}]:
        r = organ.run_organ(bad)
        _assert_contract(r)


# --------------------------------------------------------------------------- #
# parse_operator_ids
# --------------------------------------------------------------------------- #
def test_parse_operator_ids_basic():
    r = organ.decide({"op": "parse_operator_ids", "raw": "12345,67890,11223"}, {})
    _assert_contract(r)
    assert r["output"]["chat_ids"] == [11223, 12345, 67890]  # sorted
    assert r["output"]["rejected"] == []
    assert r["output"]["count"] == 3


def test_parse_operator_ids_single_int_backwards_compatible():
    r = organ.decide({"op": "parse_operator_ids", "raw": "12345"}, {})
    assert r["output"]["chat_ids"] == [12345]


def test_parse_operator_ids_tolerates_whitespace_and_stray_commas():
    r = organ.decide({"op": "parse_operator_ids", "raw": " 1 , ,2,  3 ,"}, {})
    assert r["output"]["chat_ids"] == [1, 2, 3]
    assert r["output"]["rejected"] == []


def test_parse_operator_ids_collects_bad_tokens():
    r = organ.decide({"op": "parse_operator_ids", "raw": "1,abc,2"}, {})
    assert r["output"]["chat_ids"] == [1, 2]
    assert r["output"]["rejected"] == ["abc"]
    assert r["self_metric"]["rejected"] == 1


def test_parse_operator_ids_dedupes():
    r = organ.decide({"op": "parse_operator_ids", "raw": "5,5,5"}, {})
    assert r["output"]["chat_ids"] == [5]


def test_parse_operator_ids_accepts_list():
    r = organ.decide({"op": "parse_operator_ids", "raw": [10, "20", 10]}, {})
    assert r["output"]["chat_ids"] == [10, 20]


def test_parse_operator_ids_empty():
    r = organ.decide({"op": "parse_operator_ids", "raw": ""}, {})
    assert r["output"]["chat_ids"] == []
    assert r["output"]["count"] == 0


# --------------------------------------------------------------------------- #
# verify_operator
# --------------------------------------------------------------------------- #
def test_verify_operator_matches_operator_list():
    r = organ.decide(
        {"op": "verify_operator", "chat_id": 12345},
        {"operator_chat_ids": [12345, 99], "user_chat_ids": []},
    )
    assert r["output"]["authorized"] is True
    assert r["output"]["matched_source"] == "operator"


def test_verify_operator_matches_user_list():
    r = organ.decide(
        {"op": "verify_operator", "chat_id": 7},
        {"operator_chat_ids": [1], "user_chat_ids": [7, 8]},
    )
    assert r["output"]["authorized"] is True
    assert r["output"]["matched_source"] == "user"


def test_verify_operator_operator_takes_precedence():
    r = organ.decide(
        {"op": "verify_operator", "chat_id": 5},
        {"operator_chat_ids": [5], "user_chat_ids": [5]},
    )
    assert r["output"]["matched_source"] == "operator"


def test_verify_operator_unknown_denied():
    r = organ.decide(
        {"op": "verify_operator", "chat_id": 404},
        {"operator_chat_ids": [1], "user_chat_ids": [2]},
    )
    assert r["output"]["authorized"] is False
    assert r["output"]["matched_source"] is None


def test_verify_operator_none_chat_id_denied():
    r = organ.decide({"op": "verify_operator", "chat_id": None}, {"operator_chat_ids": [1]})
    assert r["output"]["authorized"] is False


def test_verify_operator_bool_chat_id_rejected():
    # JSON `true` must not coerce to chat_id 1.
    r = organ.decide({"op": "verify_operator", "chat_id": True}, {"operator_chat_ids": [1]})
    assert r["output"]["authorized"] is False


def test_verify_operator_string_chat_id_coerced():
    r = organ.decide({"op": "verify_operator", "chat_id": "12345"}, {"operator_chat_ids": [12345]})
    assert r["output"]["authorized"] is True


# --------------------------------------------------------------------------- #
# verify_webhook
# --------------------------------------------------------------------------- #
def test_verify_webhook_no_secret_passes():
    r = organ.decide({"op": "verify_webhook", "header_value": "anything"}, {})
    assert r["output"]["verified"] is True
    assert r["output"]["reason"] == "no_secret_configured"


def test_verify_webhook_match():
    r = organ.decide(
        {"op": "verify_webhook", "header_value": "s3cr3t"},
        {"webhook_secret": "s3cr3t"},
    )
    assert r["output"]["verified"] is True
    assert r["output"]["reason"] == "match"


def test_verify_webhook_mismatch():
    r = organ.decide(
        {"op": "verify_webhook", "header_value": "wrong"},
        {"webhook_secret": "s3cr3t"},
    )
    assert r["output"]["verified"] is False
    assert r["output"]["reason"] == "mismatch"


def test_verify_webhook_missing_header_when_secret_set():
    r = organ.decide({"op": "verify_webhook"}, {"webhook_secret": "s3cr3t"})
    assert r["output"]["verified"] is False
    assert r["output"]["reason"] == "missing_header"


def test_verify_webhook_secret_from_state():
    r = organ.decide(
        {"op": "verify_webhook", "header_value": "abc", "webhook_secret": "abc"}, {}
    )
    assert r["output"]["verified"] is True


# --------------------------------------------------------------------------- #
# is_configured
# --------------------------------------------------------------------------- #
def test_is_configured_true():
    r = organ.decide(
        {"op": "is_configured"},
        {"token_present": True, "operator_chat_ids": [1]},
    )
    assert r["output"]["configured"] is True


def test_is_configured_no_token():
    r = organ.decide(
        {"op": "is_configured"},
        {"token_present": False, "operator_chat_ids": [1]},
    )
    assert r["output"]["configured"] is False


def test_is_configured_no_operators():
    r = organ.decide(
        {"op": "is_configured"},
        {"token_present": True, "operator_chat_ids": []},
    )
    assert r["output"]["configured"] is False


def test_is_configured_token_string_presence():
    r = organ.decide(
        {"op": "is_configured"},
        {"token": "bot:abc", "operator_chat_ids": [1]},
    )
    assert r["output"]["configured"] is True


def test_is_configured_parses_raw_operator_spec():
    r = organ.decide(
        {"op": "is_configured"},
        {"token_present": True, "operator_chat_id": "1,2,3"},
    )
    assert r["output"]["configured"] is True
    assert r["output"]["operator_count"] == 3


# --------------------------------------------------------------------------- #
# parse_correlation
# --------------------------------------------------------------------------- #
def test_parse_correlation_valid():
    r = organ.decide({"op": "parse_correlation", "correlation_id": "telegram_12345_678"}, {})
    assert r["output"]["chat_id"] == 12345
    assert r["output"]["is_telegram"] is True
    assert r["output"]["routable"] is True


def test_parse_correlation_not_telegram():
    r = organ.decide({"op": "parse_correlation", "correlation_id": "github_issue_1"}, {})
    assert r["output"]["chat_id"] is None
    assert r["output"]["is_telegram"] is False
    assert r["output"]["routable"] is False


def test_parse_correlation_prefix_but_malformed():
    r = organ.decide({"op": "parse_correlation", "correlation_id": "telegram_only"}, {})
    assert r["output"]["chat_id"] is None
    assert r["output"]["is_telegram"] is True
    assert r["output"]["routable"] is False


def test_parse_correlation_non_int_chat():
    r = organ.decide({"op": "parse_correlation", "correlation_id": "telegram_abc_1"}, {})
    assert r["output"]["chat_id"] is None
    assert r["output"]["routable"] is False


def test_parse_correlation_none():
    r = organ.decide({"op": "parse_correlation", "correlation_id": None}, {})
    assert r["output"]["is_telegram"] is False


# --------------------------------------------------------------------------- #
# build_message
# --------------------------------------------------------------------------- #
def test_build_message_basic():
    r = organ.decide(
        {"op": "build_message", "chat_id": 5, "text": "hello"},
        {"token_present": True},
    )
    assert r["output"]["can_send"] is True
    assert r["output"]["payload"]["chat_id"] == 5
    assert r["output"]["payload"]["text"] == "hello"
    assert r["output"]["has_buttons"] is False


def test_build_message_no_token_cannot_send():
    r = organ.decide(
        {"op": "build_message", "chat_id": 5, "text": "hi"},
        {"token_present": False},
    )
    assert r["output"]["can_send"] is False
    assert r["output"]["reason"] == "no_token"


def test_build_message_empty_text_cannot_send():
    r = organ.decide(
        {"op": "build_message", "chat_id": 5, "text": "   "},
        {"token_present": True},
    )
    assert r["output"]["can_send"] is False
    assert r["output"]["reason"] == "empty_text"


def test_build_message_truncates_long_text():
    long = "x" * 5000
    r = organ.decide(
        {"op": "build_message", "chat_id": 5, "text": long},
        {"token_present": True},
    )
    assert r["output"]["truncated"] is True
    assert len(r["output"]["payload"]["text"]) == 4000


def test_build_message_custom_max_chars():
    r = organ.decide(
        {"op": "build_message", "chat_id": 5, "text": "abcdef"},
        {"token_present": True, "max_chars": 3},
    )
    assert r["output"]["payload"]["text"] == "abc"
    assert r["output"]["truncated"] is True


def test_build_message_1d_buttons_become_single_column():
    r = organ.decide(
        {
            "op": "build_message",
            "chat_id": 5,
            "text": "pick",
            "buttons": [
                {"label": "Approve", "callback_data": "a"},
                {"label": "Reject", "callback_data": "r"},
            ],
        },
        {"token_present": True},
    )
    kb = r["output"]["payload"]["reply_markup"]["inline_keyboard"]
    assert kb == [
        [{"text": "Approve", "callback_data": "a"}],
        [{"text": "Reject", "callback_data": "r"}],
    ]
    assert r["output"]["has_buttons"] is True


def test_build_message_2d_buttons_kept_as_rows():
    r = organ.decide(
        {
            "op": "build_message",
            "chat_id": 5,
            "text": "pick",
            "buttons": [
                [{"label": "A", "callback_data": "a"}, {"label": "B", "callback_data": "b"}],
                [{"label": "C", "callback_data": "c"}],
            ],
        },
        {"token_present": True},
    )
    kb = r["output"]["payload"]["reply_markup"]["inline_keyboard"]
    assert len(kb) == 2
    assert len(kb[0]) == 2
    assert kb[1] == [{"text": "C", "callback_data": "c"}]


def test_build_message_url_button():
    r = organ.decide(
        {
            "op": "build_message",
            "chat_id": 5,
            "text": "link",
            "buttons": [{"label": "Open", "url": "https://x.test"}],
        },
        {"token_present": True},
    )
    kb = r["output"]["payload"]["reply_markup"]["inline_keyboard"]
    assert kb == [[{"text": "Open", "url": "https://x.test"}]]


def test_build_message_dead_button_dropped():
    r = organ.decide(
        {
            "op": "build_message",
            "chat_id": 5,
            "text": "x",
            "buttons": [
                {"label": "ok", "callback_data": "a"},
                {"label": "noaction"},  # neither callback_data nor url
                {"callback_data": "b"},  # no text
            ],
        },
        {"token_present": True},
    )
    kb = r["output"]["payload"]["reply_markup"]["inline_keyboard"]
    assert kb == [[{"text": "ok", "callback_data": "a"}]]


def test_build_message_all_dead_buttons_no_markup():
    r = organ.decide(
        {"op": "build_message", "chat_id": 5, "text": "x", "buttons": [{"label": "dead"}]},
        {"token_present": True},
    )
    assert "reply_markup" not in r["output"]["payload"]
    assert r["output"]["has_buttons"] is False


def test_build_message_button_text_capped_at_64():
    r = organ.decide(
        {
            "op": "build_message",
            "chat_id": 5,
            "text": "x",
            "buttons": [{"label": "z" * 100, "callback_data": "y" * 100}],
        },
        {"token_present": True},
    )
    btn = r["output"]["payload"]["reply_markup"]["inline_keyboard"][0][0]
    assert len(btn["text"]) == 64
    assert len(btn["callback_data"]) == 64


def test_build_message_token_string_presence():
    r = organ.decide(
        {"op": "build_message", "chat_id": 5, "text": "hi"},
        {"token": "bot:abc"},
    )
    assert r["output"]["can_send"] is True


# --------------------------------------------------------------------------- #
# broadcast
# --------------------------------------------------------------------------- #
def test_broadcast_to_all_operators():
    r = organ.decide(
        {"op": "broadcast", "text": "alert"},
        {"token_present": True, "operator_chat_ids": [3, 1, 2]},
    )
    assert r["output"]["can_send"] is True
    assert r["output"]["recipients"] == [1, 2, 3]  # sorted
    assert r["output"]["payload"]["text"] == "alert"
    assert "chat_id" not in r["output"]["payload"]


def test_broadcast_not_configured_no_recipients():
    r = organ.decide(
        {"op": "broadcast", "text": "alert"},
        {"token_present": False, "operator_chat_ids": [1]},
    )
    assert r["output"]["can_send"] is False
    assert r["output"]["recipients"] == []
    assert r["output"]["reason"] == "not_configured"


def test_broadcast_no_operators_no_send():
    r = organ.decide(
        {"op": "broadcast", "text": "alert"},
        {"token_present": True, "operator_chat_ids": []},
    )
    assert r["output"]["can_send"] is False
    assert r["output"]["reason"] == "not_configured"


def test_broadcast_empty_text():
    r = organ.decide(
        {"op": "broadcast", "text": ""},
        {"token_present": True, "operator_chat_ids": [1]},
    )
    assert r["output"]["can_send"] is False
    assert r["output"]["reason"] == "empty_text"


def test_broadcast_carries_buttons():
    r = organ.decide(
        {"op": "broadcast", "text": "alert", "buttons": [{"label": "OK", "callback_data": "ok"}]},
        {"token_present": True, "operator_chat_ids": [1]},
    )
    kb = r["output"]["payload"]["reply_markup"]["inline_keyboard"]
    assert kb == [[{"text": "OK", "callback_data": "ok"}]]


def test_broadcast_parses_raw_operator_spec():
    r = organ.decide(
        {"op": "broadcast", "text": "hi"},
        {"token_present": True, "operator_chat_id": "10,20"},
    )
    assert r["output"]["recipients"] == [10, 20]


# --------------------------------------------------------------------------- #
# Determinism + CLI / contract harness
# --------------------------------------------------------------------------- #
def test_determinism():
    inp = {
        "op": "build_message",
        "chat_id": 9,
        "text": "y" * 6000,
        "buttons": [{"label": "A", "callback_data": "a"}],
    }
    ctx = {"token_present": True}
    a = organ.decide(dict(inp), dict(ctx))
    b = organ.decide(dict(inp), dict(ctx))
    assert a == b


def test_cli_via_organ_input_env(tmp_path):
    sample = tmp_path / "in.json"
    sample.write_text(json.dumps({"state": {"op": "verify_operator", "chat_id": 1},
                                  "context": {"operator_chat_ids": [1]}}))
    env = os.environ.copy()
    env["ORGAN_INPUT"] = str(sample)
    out = subprocess.run([sys.executable, str(HERE / "organ.py")],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0
    data = json.loads(out.stdout)
    assert data["output"]["authorized"] is True


def test_cli_invalid_json_exits_nonzero():
    env = os.environ.copy()
    env["ORGAN_INPUT"] = "{not json"
    out = subprocess.run([sys.executable, str(HERE / "organ.py")],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 1
    data = json.loads(out.stdout)
    assert data["self_metric"]["confidence"] == 0.0


def test_all_samples_conform():
    samples_dir = HERE / "samples"
    for sample in sorted(samples_dir.glob("*.json")):
        data = json.loads(sample.read_text())
        r = organ.run_organ(data)
        _assert_contract(r)
