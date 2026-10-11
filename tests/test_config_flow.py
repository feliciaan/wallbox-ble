"""Passcode parsing in the config flow."""
import pytest

pytest.importorskip("voluptuous")
from _loader import load  # noqa: E402

config_flow = load("config_flow")


@pytest.mark.parametrize(
    "user_input,expected",
    [
        ({"pin": "123456"}, (123456, None)),
        ({"pin": "  123 456 "}, (123456, None)),  # copied from the app with spaces
        ({"pin": "12-34-56"}, (123456, None)),
        ({"pin": "000123"}, (123, None)),  # leading zeros are a valid passkey
        ({"pin": ""}, (None, None)),  # charger without a passcode
        (None, (None, None)),
        ({"pin": "1234567"}, (None, "invalid_pin")),  # > 999999 is not a BLE passkey
        ({"pin": "abc123"}, (None, "invalid_pin")),
    ],
)
def test_parse_pin(user_input, expected):
    assert config_flow._parse_pin(user_input) == expected


def test_stored_pin_round_trips_through_the_form():
    schema = config_flow._pin_schema(123)
    assert schema({})["pin"] == "000123"
    assert config_flow._parse_pin(schema({})) == (123, None)
