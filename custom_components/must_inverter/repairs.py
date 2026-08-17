from __future__ import annotations

import logging
import voluptuous as vol
import time

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .__init__ import MustInverter
from .const import DOMAIN, format_serial_number, get_serial_number_registers

_LOGGER = logging.getLogger(__name__)


class NoSerialNumber(RepairsFlow):
    """Handler for an issue fixing flow."""

    def __init__(self, hass: HomeAssistant, data: dict[str, str | int | float | None] | None) -> None:
        """Initialize the flow."""
        self._hass = hass
        self._data = data

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        """Handle the first step of a fix flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        """Handle the confirm step of a fix flow."""
        if user_input is not None:
            entry_id = self._data["entry_id"]
            device: MustInverter = self._hass.data[DOMAIN][entry_id]["inverter"]

            old_serial_number = device.data["InverterSerialNumber"]
            new_serial_number = user_input["new_serial"]

            lower = new_serial_number & 0xFFFF
            upper = (new_serial_number >> 16) & 0xFFFF

            # The registries have to use the serial number the way the inverter will report
            # it back after the write, which is not the same on every model.
            reported_serial_number = format_serial_number(device.model, new_serial_number)

            dr_entries = dr.async_entries_for_config_entry(dr.async_get(self._hass), entry_id)

            for device_entry in dr_entries:
                try:
                    _LOGGER.info("updating device identifiers to %s", reported_serial_number)
                    dr.async_get(self._hass).async_update_device(
                        device_entry.id, new_identifiers={(DOMAIN, reported_serial_number)}
                    )
                except Exception as err:
                    _LOGGER.error("failed to update device identifiers: %s", err)

            er_entries = er.async_entries_for_config_entry(er.async_get(self._hass), entry_id)
            prefix = f"{old_serial_number}_"

            for entity_entry in er_entries:
                try:
                    old_id = entity_entry.unique_id

                    if not old_id.startswith(prefix):
                        continue

                    new_id = f"{reported_serial_number}_{old_id[len(prefix):]}"
                    _LOGGER.info("updating entity id from %s to %s", old_id, new_id)
                    er.async_get(self._hass).async_update_entity(entity_entry.entity_id, new_unique_id=new_id)
                except Exception as err:
                    _LOGGER.error("failed to update entity id: %s", err)

            upper_register, lower_register = get_serial_number_registers(device.model)

            await device.write_modbus_data(upper_register, upper)
            await device.write_modbus_data(lower_register, lower)

            self._hass.config_entries.async_schedule_reload(entry_id)

            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({vol.Required("new_serial", default=int(time.time())): int}),
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create flow."""
    if issue_id == "no_serial_number":
        return NoSerialNumber(hass, data)
