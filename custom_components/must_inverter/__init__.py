import asyncio
import logging
import contextlib

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient, AsyncModbusUdpClient
from pymodbus.exceptions import ModbusIOException

from homeassistant.const import (
    CONF_NAME,
    CONF_DEVICE,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_MODE,
    CONF_HOST,
    CONF_PORT,
    CONF_TIMEOUT,
)

from .const import (
    DOMAIN,
    CONF_BAUDRATE,
    CONF_PARITY,
    CONF_STOPBITS,
    CONF_BYTESIZE,
    CONF_RETRIES,
    CONF_RECONNECT_DELAY,
    CONF_RECONNECT_DELAY_MAX,
    CONF_DEVICE_ID,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_DEVICE_ID,
    INVALID_SERIAL_NUMBERS,
    get_sensors_for_model,
    get_serial_number_registers,
    MODEL_PV1900,
    MODEL_PH1100,
    MODEL_EP3300,
)

from .mapper import (
    convert_ph1100_partArr1,
    convert_ph1100_partArr2,
    convert_ph1100_partArr3,
    convert_ph1100_partArr4,
    convert_partArr2,
    convert_partArr3,
    convert_partArr4,
    convert_partArr5,
    convert_partArr6,
    convert_battery_status,
    convert_pv_data,
    convert_ep3300_display,
    convert_ep3300_control,
    convert_ep3300_switch_points,
    convert_ep3300_power_saving,
    convert_ep3300_serial,
)
# from .utils.register_monitor import RegisterMonitor

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.TIME,
]
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass, config):
    hass.data[DOMAIN] = {}
    return True  # Return boolean to indicate that initialization was successful.


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the must inverter component."""
    try:
        entry.async_on_unload(entry.add_update_listener(async_reload_entry))

        # Initialize inverter with model-specific sensors
        inverter = MustInverter(hass, entry)

        successConnecting = await inverter.connect()

        if not successConnecting:
            raise ConfigEntryNotReady("Unable to connect to modbus device")

        successReading = await inverter._async_refresh_modbus_data()

        if not successReading:
            raise ConfigEntryNotReady("Unable to read from modbus device")

        model = inverter.model
        sensors = get_sensors_for_model(model)
        _LOGGER.debug("Setting up Must Inverter with model: %s", model)

        _async_migrate_ep3300_serial_number(hass, entry, inverter)

        # Store sensors to be used by platforms
        hass.data[DOMAIN][entry.entry_id] = {"inverter": inverter, "sensors": sensors}

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Removing register monitor as we've found all needed registers
        # If you need to add it back, uncomment the following lines and set the ranges to scan in register_monitor.py
        # monitor = RegisterMonitor(hass)

        # Add monitoring to the update cycle, but at a slower rate
        # async def delayed_monitor():
        #     """Run monitor at a slower rate to avoid overwhelming the device."""
        #     try:
        #         while True:
        #             await monitor.scan_ranges(inverter)
        #             await asyncio.sleep(300)  # Run every 5 minutes
        #     except asyncio.CancelledError:
        #         _LOGGER.debug("Register monitor task cancelled")
        #         raise

        # monitor_task = asyncio.create_task(delayed_monitor())

        # Store the task so we can cancel it later
        # hass.data[DOMAIN][entry.entry_id]["monitor_task"] = monitor_task

        return True
    except ConfigEntryNotReady as ex:
        raise ex
    except Exception as ex:
        _LOGGER.exception("Error setting up modbus device", exc_info=True)
        raise ConfigEntryNotReady("Unknown error connecting to modbus device") from ex


@callback
def _async_migrate_ep3300_serial_number(hass: HomeAssistant, entry: ConfigEntry, inverter: "MustInverter") -> None:
    """Move EP3300 registry entries over to the serial number format of the vendor tool.

    The serial number used to be decoded as a single 32 bit integer, but the inverter
    reports it as both registers concatenated as zero padded decimals. It is part of
    every unique id, so the existing entries are renamed instead of being orphaned.
    """
    if inverter.model != MODEL_EP3300:
        return

    high, low = get_serial_number_registers(MODEL_EP3300)

    if high not in inverter.registers or low not in inverter.registers:
        return

    old_serial = inverter.registers[high] << 16 | inverter.registers[low]
    new_serial = inverter.data["InverterSerialNumber"]

    if str(old_serial) == str(new_serial):
        return

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device({(DOMAIN, old_serial)}) or device_registry.async_get_device(
        {(DOMAIN, str(old_serial))}
    )

    if device is not None:
        _LOGGER.info("migrating device identifier from %s to %s", old_serial, new_serial)
        device_registry.async_update_device(device.id, new_identifiers={(DOMAIN, new_serial)})

    entity_registry = er.async_get(hass)

    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        prefix = f"{old_serial}_"

        if not entity.unique_id.startswith(prefix):
            continue

        new_unique_id = f"{new_serial}_{entity.unique_id[len(prefix):]}"

        try:
            _LOGGER.info("migrating unique id from %s to %s", entity.unique_id, new_unique_id)
            entity_registry.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)
        except ValueError:
            # An entity already claims the new id, the old one is a leftover
            _LOGGER.warning("could not migrate %s, %s is already taken", entity.unique_id, new_unique_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Cancel the monitor task
        if monitor_task := hass.data[DOMAIN][entry.entry_id].get("monitor_task"):
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task

        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener, called when the config entry options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


class MustInverter:
    def __init__(self, hass, entry: ConfigEntry):
        self._hass = hass
        self._callbacks = []  # Store callbacks separately
        self._entry = entry
        # Check both data and options

        common = {
            "timeout": entry.options[CONF_TIMEOUT],
            "retries": entry.options[CONF_RETRIES],
            "reconnect_delay": entry.options[CONF_RECONNECT_DELAY],
            "reconnect_delay_max": entry.options[CONF_RECONNECT_DELAY_MAX],
        }

        match entry.options[CONF_MODE]:
            case "serial":
                self._client = AsyncModbusSerialClient(
                    entry.options[CONF_DEVICE],
                    baudrate=entry.options[CONF_BAUDRATE],
                    stopbits=entry.options[CONF_STOPBITS],
                    bytesize=entry.options[CONF_BYTESIZE],
                    parity=entry.options[CONF_PARITY],
                    **common,
                )
            case "tcp":
                self._client = AsyncModbusTcpClient(entry.options[CONF_HOST], port=entry.options[CONF_PORT], **common)
            case "udp":
                self._client = AsyncModbusUdpClient(entry.options[CONF_HOST], port=entry.options[CONF_PORT], **common)
            case _:
                raise Exception("Invalid mode")

        self._client.rts = False
        self._client.dtr = False
        self._lock = asyncio.Lock()
        self._device_id = entry.options.get(CONF_DEVICE_ID, DEFAULT_DEVICE_ID)
        self._scan_interval = timedelta(seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        self._reading = False
        self.registers = {}
        self.data = {}

    @callback
    def async_add_must_inverter_sensor(self, update_callback):
        # This is the first sensor, set up interval.
        if not self._callbacks:
            self._unsub_interval_method = async_track_time_interval(
                self._hass, self._async_refresh_modbus_data, self._scan_interval
            )

        self._callbacks.append(update_callback)

    @callback
    def async_remove_must_inverter_sensor(self, update_callback):
        self._callbacks.remove(update_callback)

        if not self._callbacks:
            """stop the interval timer upon removal of last sensor"""
            self._unsub_interval_method()
            self._unsub_interval_method = None
            self.close()

    async def _async_refresh_modbus_data(self, now=None):
        if not await self._check_and_reopen():
            # if not connected, skip
            _LOGGER.warning("not connected, skipping refresh")
            return False

        try:
            update_result = await self.read_modbus_data()

            if update_result:
                for update_callback in self._callbacks:
                    update_callback()
        except Exception:
            _LOGGER.exception("error reading inverter data", exc_info=True)
            update_result = False

        return update_result

    @property
    def name(self):
        return self._entry.options.get(CONF_NAME, self.data.get("InverterMachineType", self.model))

    @property
    def model(self):
        from_config = self._entry.options.get(CONF_MODEL)
        detected = self.data.get("InverterMachineType")

        if from_config == "autodetect":
            return detected

        return from_config or detected

    @property
    def has_extra_registers(self):
        return self.model == MODEL_PV1900

    def close(self):
        _LOGGER.info("closing modbus client")
        self._client.close()

    async def _check_and_reopen(self):
        if not self._client.connected:
            _LOGGER.info("modbus client is not connected, trying to reconnect")
            return await self.connect()

        return self._client.connected

    async def connect(self):
        result = False

        _LOGGER.debug("connecting to %s:%s", self._client.comm_params.host, self._client.comm_params.port)

        async with self._lock:
            result = await self._client.connect()

        if result:
            _LOGGER.info(
                "successfully connected to %s:%s", self._client.comm_params.host, self._client.comm_params.port
            )
        else:
            _LOGGER.warning(
                "not able to connect to %s:%s", self._client.comm_params.host, self._client.comm_params.port
            )
        return result

    async def _write_register(self, address, value, use_fc16):
        """Write a single register using either FC16 (write multiple) or FC06 (write single)."""
        if use_fc16:
            return await self._client.write_registers(address=address, values=[value], device_id=self._device_id)
        return await self._client.write_register(address=address, value=value, device_id=self._device_id)

    async def write_modbus_data(self, address, value):
        await self._check_and_reopen()

        # The EP3300 firmware ignores FC06 and only answers FC16, which is the only function
        # code the vendor tool ever uses for this family. Other models answer FC06.
        use_fc16 = self.model == MODEL_EP3300

        _LOGGER.debug("writing modbus data: %s %s (fc%s)", address, value, "16" if use_fc16 else "06")
        async with self._lock:
            if use_fc16:
                # The inverter needs a moment between the previous read and a write, the vendor
                # tool waits 200ms after every read before sending anything else.
                await asyncio.sleep(0.2)

            try:
                response = await self._write_register(address, value, use_fc16)
            except ModbusIOException as ex:
                _LOGGER.debug("no response writing modbus data with fc%s: %s", "16" if use_fc16 else "06", ex)
                response = None

            if response is None or response.isError():
                # Fall back to the other function code, firmware support varies between models.
                _LOGGER.debug("retrying write of %s with fc%s", address, "06" if use_fc16 else "16")
                response = await self._write_register(address, value, not use_fc16)

        if response.isError():
            _LOGGER.error("error writing modbus data: %s", response)
        else:
            _LOGGER.debug("successfully wrote modbus data: %s %s", address, value)

        # await self._async_refresh_modbus_data()
        return response

    async def read_modbus_data(self):
        _LOGGER.debug("reading modbus data")

        if self._reading:
            _LOGGER.warning(
                "skipping reading modbus data, previous read still in progress, make sure your scan interval is not too low"
            )
            return False
        self._reading = True

        # Reading all the control messages at once (ranges 20016-20100 and/or
        # 10009-10100 are problematic) causes the inverter to shut down
        # immediately and the charger stops working afterwards (all registers
        # return zeroes). Requires the grid/batteries/PV to be disconnected and
        # the inverter restarted for it to come back online.
        if self.model == MODEL_PH1100:
            registersAddresses = [
                (10102, 10119, convert_ph1100_partArr1),  # Charger Control Messages
                (15104, 15119, convert_ph1100_partArr2),  # Charger Display Messages
                (20001, 20003, convert_ph1100_partArr3),  # Inverter Control Messages
                (25225, 25339, convert_ph1100_partArr4),  # Inverter Display Messages
            ]
        elif self.model == MODEL_EP3300:
            registersAddresses = [
                (30000, 30026, convert_ep3300_display),        # Display Messages
                (31000, 31009, convert_ep3300_control),        # Control Messages
                (31014, 31015, convert_ep3300_switch_points),  # Inverter/grid switch points
                (31020, 31023, convert_ep3300_power_saving),   # Power saving and priorities
                (31200, 31201, convert_ep3300_serial),         # Serial number
            ]
        else:
            # Base register ranges for all models
            registersAddresses = [
                # (10000, 10008, convert_partArr1), # Charger Control Messages
                (10101, 10124, convert_partArr2),  # Charger Control Messages
                (15201, 15221, convert_partArr3),  # Charger Display Messages
                (20000, 20016, convert_partArr4),  # Inverter Control Messages
                (20101, 20214, convert_partArr5),  # Inverter Control Messages
                (25201, 25279, convert_partArr6),  # Inverter Display Messages
            ]

            # Add PV19-specific register ranges if needed
            # Keep register ranges for pv separate to not overload the inverter
            if self.has_extra_registers:
                registersAddresses.extend(
                    [
                        (113, 114, convert_battery_status),  # Battery Status (SoC, SoH)
                        (15207, 15208, convert_pv_data),  # PV1 Data (Current, Power)
                        (16205, 16208, convert_pv_data),  # PV2 Data (Voltage, Current, Power)
                    ]
                )

        read = {}

        for register in registersAddresses:
            if self.has_extra_registers:
                # Add a small delay between reading standard and PV19-specific registers
                # to prevent any potential issues
                await asyncio.sleep(0.1)

            start = register[0]
            end = register[1]
            count = end - start + 1

            try:
                _LOGGER.debug("reading modbus data from %s to %s (%s)", start, end, count)

                if not await self._check_and_reopen():
                    break

                async with self._lock:
                    response = await self._client.read_holding_registers(
                        address=start, count=count, device_id=self._device_id
                    )
                if response.isError():
                    _LOGGER.error("error reading modbus data at address %s: %s", start, response)
                elif len(response.registers) != count:
                    _LOGGER.warning(
                        "wrong number of registers read at address %s, expected %s, got %s - this usually is caused by concurrent access to modbus (usb/wifi/rs485), please remove other devices",
                        start,
                        count,
                        len(response.registers),
                    )
                    # Log the actual values received
                    _LOGGER.debug("Received registers (wrong): %s", response.registers)

                    ir.async_create_issue(
                        self._hass,
                        DOMAIN,
                        "mismatched_registers",
                        is_fixable=False,
                        severity=ir.IssueSeverity.ERROR,
                        translation_key="mismatched_registers",
                        is_persistent=False,
                        learn_more_url="https://github.com/mukaschultze/ha-must-inverter/issues/47",
                    )
                else:
                    _LOGGER.debug("Correctly read %s registers from start %s", count, start)
                    for i in range(count):
                        read[start + i] = response.registers[i]
                    self.data.update(register[2](read))
            except asyncio.exceptions.CancelledError:
                _LOGGER.warning("cancelled modbus read")
                raise
            except:
                _LOGGER.error("error reading modbus data at address %s", start, exc_info=True)

        _LOGGER.debug("finished reading modbus data, %s", read)
        self.registers = read
        self._reading = False
        # _LOGGER.debug("Data: %s", self.data)

        if self.data["InverterSerialNumber"] in INVALID_SERIAL_NUMBERS:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                "no_serial_number",
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="no_serial_number",
                data={"entry_id": self._entry.entry_id},
            )

        return True

    def _device_info(self):
        return {
            "identifiers": {(DOMAIN, self.data["InverterSerialNumber"])},
            "name": self.name,
            "manufacturer": "Must Solar",
            "model": self.data.get("InverterMachineType", self.model),
            "hw_version": self.data.get("InverterHardwareVersion"),
            "sw_version": self.data.get("InverterSoftwareVersion"),
            "serial_number": self.data["InverterSerialNumber"],
        }
