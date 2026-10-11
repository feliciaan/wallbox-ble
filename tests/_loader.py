"""Load the integration's modules without Home Assistant installed.

The package ``__init__`` imports half of Home Assistant, so the modules under
test are loaded by path under a synthetic ``wallbox_ble`` package, with the
few Home Assistant / bleak names they touch stubbed out.
"""
import importlib.util
import pathlib
import sys
import types

COMPONENT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "wallbox_ble"


def _stub(name, **attrs):
    module = sys.modules.get(name) or types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _ConfigFlow:
    def __init_subclass__(cls, **kwargs):
        pass


_stub("homeassistant")
_stub("homeassistant.config_entries", ConfigFlow=_ConfigFlow, CONN_CLASS_LOCAL_POLL="local_poll")
_stub("homeassistant.components")
_stub("homeassistant.components.onboarding", async_is_onboarded=lambda hass: True)
_stub(
    "homeassistant.components.bluetooth",
    BluetoothServiceInfo=object,
    async_ble_device_from_address=lambda *a, **k: None,
    async_discovered_service_info=lambda *a, **k: [],
    async_scanner_devices_by_address=lambda *a, **k: [],
)
_stub("homeassistant.const", CONF_ADDRESS="address")
_stub("bleak", BleakClient=object)
_stub("bleak_retry_connector", establish_connection=None)

_package = types.ModuleType("wallbox_ble")
_package.__path__ = [str(COMPONENT)]
sys.modules["wallbox_ble"] = _package


def load(name):
    """Import ``custom_components/wallbox_ble/<name>.py`` as wallbox_ble.<name>."""
    full = f"wallbox_ble.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, COMPONENT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


const = load("const")
pairing = load("pairing")
api = load("api")
