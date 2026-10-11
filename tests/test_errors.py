"""Classifying GATT write failures and decoding a proxy's SMP failure numbers."""
import pytest

from _loader import api

ESPHOME_AUTH = (
    "Bluetooth GATT Error address=54:64:DE:92:BC:7C handle=23 error=5 "
    "description=Insufficient authentication"
)


@pytest.mark.parametrize(
    "message",
    [
        ESPHOME_AUTH,  # verbatim from a field log
        "Bluetooth GATT Error address=X handle=23 error=15 description=Insufficient encryption",
        "Bluetooth GATT Error address=X handle=23 error=8 description=Insufficient authorization",
        "org.bluez.Error.NotPermitted: Insufficient Authentication",
        "Device not paired",
    ],
)
def test_auth_errors_are_recognised(message):
    assert api._is_auth_error(Exception(message))


@pytest.mark.parametrize(
    "exc",
    [
        # ATT 0x03: the oversized-write path depends on this NOT being "auth".
        Exception("Bluetooth GATT Error address=X handle=23 error=3 description=Write not permitted"),
        Exception("Bluetooth GATT Error address=X handle=23 error=-1 description=Disconnected"),
        Exception("Characteristic not found"),
        TimeoutError("timed out"),
        None,
    ],
)
def test_other_failures_are_not_auth_errors(exc):
    assert not api._is_auth_error(exc)


def test_smp_base_matches_bta_api_h():
    # BTA_DM_AUTH_FAIL_BASE = HCI_ERR_MAX_ERR (0x43) + 10
    assert api.BTA_DM_AUTH_FAIL_BASE == 0x43 + 10


@pytest.mark.parametrize(
    "reported,expected",
    [
        (80, "authentication requirements"),  # seen in the field
        (102, "timed out"),  # seen in the field
        (78, "passkey entry failed"),
        (86, "repeated attempts"),
        (89, "numeric comparison"),
    ],
)
def test_smp_codes_decode(reported, expected):
    detail = api._describe_pair_error(Exception(f"Pairing failed due to error: {reported}"))
    assert detail is not None and expected in detail


@pytest.mark.parametrize("reported", [0, 5, 67, 77, 200])
def test_non_smp_numbers_are_left_alone(reported):
    # <= 77 are plain HCI errors; nothing above 0x19 is defined.
    assert api._describe_pair_error(Exception(f"Pairing failed due to error: {reported}")) is None
