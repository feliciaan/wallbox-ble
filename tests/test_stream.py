"""split_messages: reassembling replies and resyncing after a late one.

Fragments are real replies from a Pulsar Max (fw 6.13.6) behind an ESPHome
proxy, cut into 20-byte notifications as the charger sends them.
"""
import json

import pytest

from _loader import api

R_DAT = {"id": 594, "r": {"L1": 0, "L2": 0, "L3": 0, "cp": 0.0, "cur": 32, "den": 0,
                          "en": 0, "gen": 0, "grid": 0, "mid": 1, "ocpp": 1, "ps": 0,
                          "s": 0, "st": 6, "usid": 1}}
R_STA = {"id": 915, "r": {"charger_status": 6, "error_code": 0, "external_meter_status": 2,
                          "lock_status": 1, "max_available_current": 32,
                          "max_charging_current": 32, "mid_status": 1, "ocpp_status": 1,
                          "phases_connection": 0, "power_sharing_status": 0}}
FW = {"c": "ATLAS", "db": 125, "fw": 20202, "id": 613, "p": "prj20-pulsar-max-pro",
      "r": 20202, "s": "6.13.6"}  # keys sorted: "id" is not first


def wire(message):
    return json.dumps(message, separators=(",", ":"))


def pieces(text, size=20):
    return [text[i:i + size] for i in range(0, len(text), size)]


def feed(chunks):
    """Feed notifications one by one, as get_parsed_response does."""
    out, buf = [], ""
    for chunk in chunks:
        messages, buf = api.split_messages(buf + chunk)
        out.extend(messages)
    return out, buf


def test_reply_reassembled_from_notifications():
    out, rest = feed(pieces(wire(R_DAT)))
    assert out == [R_DAT] and rest == ""


def test_keys_not_starting_with_id():
    out, _ = feed(pieces(wire(FW)))
    assert out == [FW]


@pytest.mark.parametrize("cut", range(1, 200, 7))
def test_late_tail_in_front_of_the_next_reply(cut):
    """Field case: r_sta timed out after part of its reply arrived; the rest
    lands in the next request's buffer, in front of that request's reply.
    Before, nothing after the tail ever parsed and the next request failed
    too."""
    late = wire(R_STA)
    cut = min(cut, len(late) - 1)
    tail = late[cut:]
    out, rest = feed(pieces(tail) + pieces(wire(R_DAT)))
    assert R_DAT in out and rest == ""


@pytest.mark.parametrize("cut", range(1, 200, 9))
def test_tail_and_next_reply_share_a_notification(cut):
    late = wire(R_STA)
    cut = min(cut, len(late) - 1)
    out, rest = feed(pieces(late[cut:] + wire(R_DAT)))
    assert R_DAT in out and rest == ""


def test_tail_starting_at_a_nested_object():
    """A tail that happens to begin with the inner object of the late reply."""
    late = wire(R_DAT)
    inner = late.index('{"L1"')
    out, _ = feed(pieces(late[inner:]) + pieces(wire(R_STA)))
    assert out[-1] == R_STA


def test_back_to_back_replies():
    out, rest = feed(pieces(wire(R_STA) + wire(R_DAT) + wire(FW)))
    assert out == [R_STA, R_DAT, FW] and rest == ""


@pytest.mark.parametrize("cut", range(1, len(wire({"id": 7, "r": {"enabled": True, "time": -12.5e1, "x": None, "y": False}}))))
def test_every_split_point_waits_for_the_rest(cut):
    """Truncated literals and numbers (tr|ue, -1|2.5e1, nu|ll) are incomplete,
    not garbage."""
    message = {"id": 7, "r": {"enabled": True, "time": -12.5e1, "x": None, "y": False}}
    text = wire(message)
    out, rest = api.split_messages(text[:cut])
    assert out == [] and rest == text[:cut]
    out, rest = api.split_messages(rest + text[cut:])
    assert out == [message] and rest == ""


def test_garbage_only_is_dropped():
    assert api.split_messages('ocpp_status":1}}') == ([], "")
    assert api.split_messages("}}{") == ([], "{")  # may become '{"'


def test_error_reply():
    message = {"error": {"code": 4}, "id": 12}
    out, _ = feed(pieces(wire(message)))
    assert out == [message]
