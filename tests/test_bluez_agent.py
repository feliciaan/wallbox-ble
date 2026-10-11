"""The BlueZ pairing agent and pairing flow, against a faked system bus."""
import asyncio

import pytest

pytest.importorskip("dbus_fast")
from dbus_fast import DBusError, Variant  # noqa: E402

from _loader import pairing  # noqa: E402

ADDR = "54:64:DE:92:BC:7C"
DEV = "/org/bluez/hci0/dev_54_64_DE_92_BC_7C"
OBJECTS = {
    "/org/bluez/hci0": {"org.bluez.Adapter1": {}},
    DEV: {"org.bluez.Device1": {"Address": Variant("s", ADDR)}},
    "/org/bluez/hci1/dev_54_64_DE_92_BC_7C": {"org.bluez.Device1": {"Address": Variant("s", ADDR.lower())}},
}


def call(agent, name, *args):
    """Invoke as dbus-fast dispatches it: @method() wrappers drop return values."""
    return getattr(type(agent), name).__dict__["__DBUS_METHOD"].fn(agent, *args)


def test_agent_answers_with_the_passkey():
    agent = pairing._build_agent_class()(123456)
    assert call(agent, "RequestPasskey", DEV) == 123456
    assert agent.passkey_requested
    assert call(pairing._build_agent_class()(1234), "RequestPinCode", DEV) == "001234"


def test_agent_rejects_without_a_passkey():
    with pytest.raises(DBusError) as err:
        call(pairing._build_agent_class()(None), "RequestPasskey", DEV)
    assert err.value.type == "org.bluez.Error.Rejected"


def test_agent_exposes_bluez_member_names_and_signatures():
    methods = {m.name: (m.in_signature, m.out_signature)
               for m in pairing._build_agent_class()(1).introspect().methods}
    assert set(methods) == {
        "Release", "RequestPinCode", "DisplayPinCode", "RequestPasskey", "DisplayPasskey",
        "RequestConfirmation", "RequestAuthorization", "AuthorizeService", "Cancel",
    }
    assert methods["RequestPasskey"] == ("o", "u")
    assert methods["DisplayPasskey"] == ("ouq", "")


def test_device_lookup():
    assert pairing._find_device_path(OBJECTS, ADDR, "hci1") == "/org/bluez/hci1/dev_54_64_DE_92_BC_7C"
    assert pairing._find_device_path(OBJECTS, ADDR.lower(), "hci0") == DEV
    assert pairing._find_device_path(OBJECTS, "11:22:33:44:55:66") is None


class FakeDevice:
    def __init__(self, paired=False, error=None):
        self.paired, self.error, self.trusted, self.pair_calls = paired, error, False, 0

    async def get_paired(self):
        return self.paired

    async def call_pair(self):
        self.pair_calls += 1
        if self.error:
            raise self.error
        self.paired = True

    async def set_trusted(self, value):
        self.trusted = value


class Fakes:
    def __init__(self, device):
        self.device, self.removed, self.registered, self.unregistered = device, [], [], []
        self.exported, self.disconnected = {}, False

    # bus
    def export(self, path, iface):
        self.exported[path] = iface

    def unexport(self, path, iface=None):
        self.exported.pop(path, None)

    def disconnect(self):
        self.disconnected = True

    # adapter / agent manager
    async def call_remove_device(self, path):
        self.removed.append(path)

    async def call_register_agent(self, path, capability):
        self.registered.append((path, capability))

    async def call_unregister_agent(self, path):
        self.unregistered.append(path)


@pytest.fixture
def fakes(monkeypatch):
    def install(device):
        f = Fakes(device)

        async def connect():
            return f

        async def objects(_bus):
            return OBJECTS

        async def interface(_bus, _path, name):
            return device if name == pairing.DEVICE_INTERFACE else f

        monkeypatch.setattr(pairing, "_connect_system_bus", connect)
        monkeypatch.setattr(pairing, "_get_managed_objects", objects)
        monkeypatch.setattr(pairing, "_get_interface", interface)
        return f

    return install


def test_pairs_with_a_keyboard_only_agent(fakes):
    device = FakeDevice()
    f = fakes(device)
    assert asyncio.run(pairing.async_ensure_paired(ADDR, 123456, "hci0")) is True
    assert f.registered[0][1] == "KeyboardOnly"
    assert f.unregistered == [f.registered[0][0]] and f.exported == {}
    assert device.trusted and f.disconnected


def test_already_paired_is_a_no_op(fakes):
    device = FakeDevice(paired=True)
    f = fakes(device)
    assert asyncio.run(pairing.async_ensure_paired(ADDR, 123456, "hci0")) is False
    assert device.pair_calls == 0 and f.registered == []


def test_rejected_passkey_drops_the_stale_bond(fakes):
    f = fakes(FakeDevice(error=DBusError("org.bluez.Error.AuthenticationFailed", "no")))
    with pytest.raises(pairing.PairingAuthError):
        asyncio.run(pairing.async_ensure_paired(ADDR, 111111, "hci0"))
    assert f.removed == [DEV] and len(f.unregistered) == 1 and f.disconnected


def test_transient_failure_keeps_the_bond(fakes):
    f = fakes(FakeDevice(error=DBusError("org.bluez.Error.ConnectionAttemptFailed", "x")))
    with pytest.raises(pairing.PairingError) as err:
        asyncio.run(pairing.async_ensure_paired(ADDR, 111111, "hci0"))
    assert not isinstance(err.value, pairing.PairingAuthError)
    assert f.removed == []


def test_unknown_device_means_proxy(fakes):
    fakes(FakeDevice())
    with pytest.raises(pairing.PairingUnsupported):
        asyncio.run(pairing.async_ensure_paired("11:22:33:44:55:66", 123456, "hci0"))
