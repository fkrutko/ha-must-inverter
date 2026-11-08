"""Weather forecast based battery discharge control.

When enabled, the controller looks at the forecast for the next solar charging day (the day of
the next sunrise) and sets ``BatteryStopDischargingVoltage`` accordingly:

* good forecast (e.g. sunny): the low ``discharge`` voltage, the battery may be used overnight
  because the sun will charge it again the next day.
* bad forecast (clouds, rain, snow, ...) or no forecast at all: the high ``hold`` voltage, the
  battery is kept full and the loads run from the grid.

The register is only written when the desired value differs from what the inverter reports, so
the inverter memory is not worn out by repeated writes.

A single controller is shared by the Smart Discharge switch (turns the feature on and off) and
the decision/forecast sensors (show what the controller decided).
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import SUN_EVENT_SUNRISE, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.sun import get_astral_event_next
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_SMART_WEATHER_ENTITY,
    CONF_SMART_DISCHARGE_VOLTAGE,
    CONF_SMART_HOLD_VOLTAGE,
    CONF_SMART_GOOD_CONDITIONS,
    DEFAULT_SMART_DISCHARGE_VOLTAGE,
    DEFAULT_SMART_HOLD_VOLTAGE,
    DEFAULT_SMART_GOOD_CONDITIONS,
    WEATHER_CONDITIONS,
    get_ranges_for_model,
)

_LOGGER = logging.getLogger(__name__)

KEY = "BatteryStopDischargingVoltage"
ADDRESS = 20118
COEFF = 0.1

EVALUATE_INTERVAL = timedelta(minutes=15)

# Hours (local time) considered when only an hourly forecast is available
HOURLY_DAYLIGHT_START = 8
HOURLY_DAYLIGHT_END = 17
# Share of good daylight hours required to call the day good
HOURLY_GOOD_SHARE = 0.5

DECISION_DISCHARGE = "discharge"
DECISION_HOLD = "hold"
DECISION_DISABLED = "disabled"
DECISION_OPTIONS = [DECISION_DISCHARGE, DECISION_HOLD, DECISION_DISABLED]

FORECAST_OPTIONS = [*WEATHER_CONDITIONS, "clear-night"]


def is_supported(inverter) -> bool:
    """Only inverters exposing the stop discharging voltage register can use this feature."""
    return KEY in inverter.data


@callback
def async_get_controller(hass: HomeAssistant, entry, inverter) -> "SmartDischargeController":
    """Return the controller shared by the switch and sensors of a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    if "smart_discharge" not in entry_data:
        entry_data["smart_discharge"] = SmartDischargeController(hass, inverter, dict(entry.options))
    return entry_data["smart_discharge"]


class SmartDischargeController:
    """Evaluates the forecast and writes the stop discharging voltage."""

    def __init__(self, hass: HomeAssistant, inverter, options: dict[str, Any]):
        self.hass = hass
        self.inverter = inverter
        self.weather_entity: str | None = options.get(CONF_SMART_WEATHER_ENTITY) or None
        self.discharge_voltage = float(options.get(CONF_SMART_DISCHARGE_VOLTAGE, DEFAULT_SMART_DISCHARGE_VOLTAGE))
        self.hold_voltage = float(options.get(CONF_SMART_HOLD_VOLTAGE, DEFAULT_SMART_HOLD_VOLTAGE))
        self.good_conditions = set(options.get(CONF_SMART_GOOD_CONDITIONS, DEFAULT_SMART_GOOD_CONDITIONS))

        self.enabled = False
        self.target_date: date | None = None
        self.forecast_condition: str | None = None
        self.forecast_source: str | None = None
        self.decision: str | None = None
        self.last_evaluated: datetime | None = None
        self.last_error: str | None = None

        self._lock = asyncio.Lock()
        self._unsub_interval = None
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ listeners

    @callback
    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update_callback)

        @callback
        def remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return remove

    @callback
    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    # ------------------------------------------------------------------ enable / disable

    async def async_enable(self) -> None:
        self.enabled = True
        if self._unsub_interval is None:
            self._unsub_interval = async_track_time_interval(self.hass, self._async_interval, EVALUATE_INTERVAL)
        self._notify()
        await self.async_evaluate()

    @callback
    def async_disable(self) -> None:
        # The voltage is left as it is, the user takes over manual control again.
        self.enabled = False
        self.decision = None
        self._stop_interval()
        self._notify()

    @callback
    def async_shutdown(self) -> None:
        self._stop_interval()

    def _stop_interval(self) -> None:
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

    async def _async_interval(self, _now=None) -> None:
        await self.async_evaluate()

    # ------------------------------------------------------------------ attributes

    @property
    def attributes(self) -> dict[str, Any]:
        return {
            "weather_entity": self.weather_entity,
            "good_conditions": sorted(self.good_conditions),
            "discharge_voltage": self.discharge_voltage,
            "hold_voltage": self.hold_voltage,
            "target_date": self.target_date.isoformat() if self.target_date else None,
            "forecast_condition": self.forecast_condition,
            "forecast_source": self.forecast_source,
            "decision": self.decision,
            "last_evaluated": self.last_evaluated.isoformat() if self.last_evaluated else None,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ logic

    def _next_charging_day(self) -> date:
        """Day of the next sunrise, in the evening this is tomorrow, after midnight it is today."""
        try:
            sunrise = get_astral_event_next(self.hass, SUN_EVENT_SUNRISE)
            return dt_util.as_local(sunrise).date()
        except Exception:  # noqa: BLE001 - polar regions etc, fall back to the calendar
            now = dt_util.now()
            return now.date() if now.hour < 6 else now.date() + timedelta(days=1)

    async def _async_get_forecast(self, forecast_type: str) -> list[dict[str, Any]] | None:
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": self.weather_entity, "type": forecast_type},
                blocking=True,
                return_response=True,
            )
        except Exception as ex:  # noqa: BLE001 - unsupported forecast type, entity missing...
            _LOGGER.debug("unable to get %s forecast from %s: %s", forecast_type, self.weather_entity, ex)
            return None

        forecast = (response or {}).get(self.weather_entity, {}).get("forecast")
        return forecast or None

    @staticmethod
    def _entry_local_datetime(entry: dict[str, Any]) -> datetime | None:
        value = entry.get("datetime")
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = dt_util.parse_datetime(value)
        else:
            return None
        return dt_util.as_local(parsed) if parsed is not None else None

    async def _async_forecast_is_good(self, target: date) -> bool | None:
        """Return True/False for a good/bad forecast on the target day, None when unknown."""
        daily = await self._async_get_forecast("daily")
        if daily:
            for entry in daily:
                when = self._entry_local_datetime(entry)
                if when is not None and when.date() == target:
                    condition = entry.get("condition")
                    self.forecast_condition = condition
                    self.forecast_source = "daily"
                    return condition in self.good_conditions

        # Some weather integrations only offer hourly forecasts
        hourly = await self._async_get_forecast("hourly")
        if hourly:
            conditions = []
            for entry in hourly:
                when = self._entry_local_datetime(entry)
                if (
                    when is not None
                    and when.date() == target
                    and HOURLY_DAYLIGHT_START <= when.hour < HOURLY_DAYLIGHT_END
                ):
                    conditions.append(entry.get("condition"))

            if conditions:
                good = sum(1 for condition in conditions if condition in self.good_conditions)
                share = good / len(conditions)
                # Report the most common condition of the day
                self.forecast_condition = max(set(conditions), key=conditions.count)
                self.forecast_source = f"hourly ({good}/{len(conditions)} good hours)"
                return share >= HOURLY_GOOD_SHARE

        self.forecast_condition = None
        self.forecast_source = None
        return None

    def _voltage_in_range(self, voltage: float) -> bool:
        ranges = get_ranges_for_model(self.inverter.model)
        if KEY not in ranges:
            return True
        try:
            allowed = ranges[KEY](self.inverter.data)
        except Exception:  # noqa: BLE001 - missing battery voltage grade
            return True
        return allowed.min - 1e-6 <= voltage <= allowed.max + 1e-6

    async def async_evaluate(self) -> None:
        """Check the forecast and write the stop discharging voltage if needed."""
        if not self.enabled:
            return

        async with self._lock:
            self.last_evaluated = dt_util.now()
            self.last_error = None

            try:
                if not self.weather_entity:
                    self.last_error = "no weather entity configured, set one in the integration options"
                    _LOGGER.warning("smart discharge: %s", self.last_error)
                    return

                self.target_date = self._next_charging_day()
                good = await self._async_forecast_is_good(self.target_date)

                if good is None:
                    # Unknown forecast: play safe and keep the battery charged
                    self.last_error = f"no forecast available for {self.target_date} from {self.weather_entity}"
                    _LOGGER.warning("smart discharge: %s, holding battery", self.last_error)

                self.decision = DECISION_DISCHARGE if good else DECISION_HOLD
                voltage = self.discharge_voltage if good else self.hold_voltage

                if not self._voltage_in_range(voltage):
                    self.last_error = f"{voltage} V is outside the range supported by the inverter"
                    _LOGGER.error("smart discharge: %s", self.last_error)
                    return

                raw = int(round(voltage / COEFF))
                current = self.inverter.data.get(KEY)

                _LOGGER.debug(
                    "smart discharge: target %s, condition %s (%s), decision %s, voltage %s (current raw %s)",
                    self.target_date,
                    self.forecast_condition,
                    self.forecast_source,
                    self.decision,
                    voltage,
                    current,
                )

                if current == raw:
                    return

                _LOGGER.info(
                    "smart discharge: forecast for %s is %s, setting battery stop discharging voltage to %s V",
                    self.target_date,
                    self.forecast_condition,
                    voltage,
                )
                response = await self.inverter.write_modbus_data(ADDRESS, raw)

                if response is None or response.isError():
                    self.last_error = f"failed writing battery stop discharging voltage: {response}"
                    _LOGGER.error("smart discharge: %s", self.last_error)
                else:
                    self.inverter.data[KEY] = raw  # optimistic update
            except Exception as ex:  # noqa: BLE001 - never break the interval timer
                self.last_error = str(ex)
                _LOGGER.exception("smart discharge: error evaluating forecast")
            finally:
                self._notify()


class _SmartDischargeEntity:
    """Common plumbing for entities backed by the controller."""

    _attr_has_entity_name = True

    def __init__(self, controller: SmartDischargeController, suffix: str):
        self._controller = controller
        self._attr_unique_id = f"{controller.inverter.data['InverterSerialNumber']}_{suffix}"

    @property
    def device_info(self) -> dict[str, Any] | None:
        return self._controller.inverter._device_info()

    @property
    def available(self) -> bool:
        return KEY in self._controller.inverter.data

    async def _async_listen(self) -> None:
        self.async_on_remove(self._controller.async_add_listener(self.async_write_ha_state))


class SmartDischargeSwitch(_SmartDischargeEntity, SwitchEntity, RestoreEntity):
    """Switch enabling weather forecast based control of the battery stop discharging voltage."""

    _attr_translation_key = "smartdischarge"

    def __init__(self, controller: SmartDischargeController):
        super().__init__(controller, "SmartDischarge")

    async def async_added_to_hass(self) -> None:
        await self._async_listen()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state == STATE_ON:
            self._controller.enabled = True

            # Weather entities may not be loaded yet, wait for Home Assistant to finish starting
            async def _started(_hass: HomeAssistant) -> None:
                if self._controller.enabled:
                    await self._controller.async_enable()

            self.async_on_remove(async_at_started(self.hass, _started))

    async def async_will_remove_from_hass(self) -> None:
        self._controller.async_shutdown()

    @property
    def is_on(self) -> bool:
        return self._controller.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._controller.async_enable()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._controller.async_disable()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._controller.attributes


class SmartDischargeDecisionSensor(_SmartDischargeEntity, SensorEntity):
    """What the controller decided: discharge, hold, or disabled."""

    _attr_translation_key = "smartdischargedecision"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = DECISION_OPTIONS

    def __init__(self, controller: SmartDischargeController):
        super().__init__(controller, "SmartDischargeDecision")

    async def async_added_to_hass(self) -> None:
        await self._async_listen()

    @property
    def native_value(self) -> str | None:
        if not self._controller.enabled:
            return DECISION_DISABLED
        return self._controller.decision

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self._controller
        voltage = None
        if c.enabled and c.decision is not None:
            voltage = c.discharge_voltage if c.decision == DECISION_DISCHARGE else c.hold_voltage
        return {
            "target_voltage": voltage,
            "target_date": c.target_date.isoformat() if c.target_date else None,
            "last_evaluated": c.last_evaluated.isoformat() if c.last_evaluated else None,
            "last_error": c.last_error,
        }


class SmartDischargeForecastSensor(_SmartDischargeEntity, SensorEntity):
    """Forecast condition of the next solar charging day."""

    _attr_translation_key = "smartdischargeforecast"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = FORECAST_OPTIONS

    def __init__(self, controller: SmartDischargeController):
        super().__init__(controller, "SmartDischargeForecast")

    async def async_added_to_hass(self) -> None:
        await self._async_listen()

    @property
    def native_value(self) -> str | None:
        condition = self._controller.forecast_condition
        return condition if condition in FORECAST_OPTIONS else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self._controller
        return {
            "target_date": c.target_date.isoformat() if c.target_date else None,
            "forecast_source": c.forecast_source,
            "raw_condition": c.forecast_condition,
            "good_day": None if c.forecast_condition is None else c.forecast_condition in c.good_conditions,
            "weather_entity": c.weather_entity,
        }


@callback
def async_create_switches(hass: HomeAssistant, entry, inverter) -> list[SwitchEntity]:
    if not is_supported(inverter):
        return []
    return [SmartDischargeSwitch(async_get_controller(hass, entry, inverter))]


@callback
def async_create_sensors(hass: HomeAssistant, entry, inverter) -> list[SensorEntity]:
    if not is_supported(inverter):
        return []
    controller = async_get_controller(hass, entry, inverter)
    return [SmartDischargeDecisionSensor(controller), SmartDischargeForecastSensor(controller)]
