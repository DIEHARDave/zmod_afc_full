# AFCProject Automated Filament Changer Software
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""
Flashforge AD5X platform adapter: lets any AFC unit (BoxTurtle, NightOwl,
QuattroBox, HTLF, ViViD, Claymore, ACE, ...) run on an AD5X under Zmod or
Kiln OS, in place of the IFS or next to it.

The unit side needs nothing here: AFC drives those units itself. What an
AD5X lacks is on the printer side:

- Its toolhead filament sensor is analog (query_adc "temperature_sensor
  filamentValue", present at <= 0.30 or >= 0.72), not a pin AFC can use for
  pin_tool_start. This module turns it into a `filament_motion_sensor <name>`
  object that `[AFC_extruder] u1_filament_sensor_name: <name>` accepts: AFC
  then polls it as a software endstop (PollingEndstopWrapper) for toolhead
  homing, the same path the Snapmaker U1 uses.
- The ADC only reports every 0.3 s stock; homing against it would overshoot.
  `adc_report_time` shortens it before the MCU is configured.
- The firmware's own toolhead runout sensor (Zmod's head_switch_sensor,
  Kiln's adc_filament_sensor) would pause a print during every AFC unload.
  `disable_sensors` switches those off; AFC handles runout through this one.
- Cut, park, purge and wipe are the firmware's macros, wired to AFC's
  tool_cut_cmd/park_cmd/poop_cmd/wipe_cmd in templates/AFC_FF_<fw>.cfg.

[AFC_FF] must be created before [AFC_extruder]: put it first in the config
(on Kiln, KILN_AFC's bootstrap creates it early).
"""
from __future__ import annotations

import logging

from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from configfile import ConfigWrapper


class FFRunoutHelper:
    """
    The part of Klipper's RunoutHelper that AFC_extruder uses on a
    u1_filament_sensor_name object: it replaces note_filament_present and
    _runout_event_handler with its own callbacks and reads filament_present.
    """
    def __init__(self, printer: Any, name: str, event_delay: float) -> None:
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.name = name
        self.filament_present = False
        self.sensor_enabled = True
        self.runout_pause = False
        self.runout_gcode = None
        self.insert_gcode = None
        self.event_delay = event_delay
        self.min_event_systime = 0.
        self._runout_event_handler: Any = None

    def note_filament_present(self, is_filament_present: bool, force: bool = False) -> None:
        """
        Record a new sensor state. On a runout while printing, calls the
        runout handler AFC installed (lane.handle_toolhead_runout), as
        Klipper's RunoutHelper does.

        AFC_extruder wraps this method: its wrapper calls this one first,
        then its own tool_start_callback.

        :param is_filament_present: True when filament is at the sensor
        :param force: Unused, kept for AFC's call signature
        """
        if is_filament_present == self.filament_present:
            return
        self.filament_present = is_filament_present
        eventtime = self.reactor.monotonic()
        if (is_filament_present
                or not self.sensor_enabled
                or eventtime < self.min_event_systime
                or self._runout_event_handler is None):
            return
        idle_timeout = self.printer.lookup_object("idle_timeout", None)
        printing = (idle_timeout is not None
                    and idle_timeout.get_status(eventtime)["state"] == "Printing")
        if printing:
            self.min_event_systime = self.reactor.NEVER
            self.reactor.register_callback(self._runout_event_handler)

    def get_status(self, eventtime: float) -> Dict[str, Any]:
        return {"filament_detected": bool(self.filament_present),
                "enabled": bool(self.sensor_enabled)}


class AFC_FF:
    """
    [AFC_FF]: the AD5X toolhead sensor as an AFC toolhead sensor, plus the
    firmware hand-over AFC needs to own filament changes with any unit.
    """
    def __init__(self, config: ConfigWrapper) -> None:
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get("sensor_name", "ff_head")
        self.adc_name = config.get("head_adc", "temperature_sensor filamentValue")
        self.present_below = config.getfloat("present_below", 0.30, minval=0., maxval=1.)
        self.present_above = config.getfloat("present_above", 0.72, minval=0., maxval=1.)
        self.poll_time = config.getfloat("poll_time", 0.02, above=0.)
        self.adc_report_time = config.getfloat("adc_report_time", 0.05, minval=0.)
        self.disable_sensors: List[str] = [
            s.strip() for s in config.get("disable_sensors", "").split(",") if s.strip()]
        self.runout_helper = FFRunoutHelper(self.printer, self.name,
                                            config.getfloat("event_delay", 3., above=0.))
        self.value: Optional[float] = None
        self._adc: Any = None
        self._timer: Any = None

        # AFC_extruder looks the sensor up with load_object(), which returns an
        # object already registered under that name
        self.printer.add_object(f"filament_motion_sensor {self.name}", self)
        # At mcu_identify, before the MCUs are configured: by then every ADC
        # is registered with query_adc, whatever the section order
        self.printer.load_object(config, "query_adc")
        self.printer.register_event_handler("klippy:mcu_identify", self._speed_up_adc)
        self.firmware = config.getchoice("firmware", {"auto": "auto", "zmod": "zmod",
                                                      "kiln": "kiln", "none": "none"}, "auto")
        if self.firmware == "auto":
            self.firmware = ("zmod" if config.has_section("zmod")
                             else "kiln" if config.has_section("ifs") else "none")
        self._runout_dropped = False
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("afc_stepper:register_macros",
                                            self._handle_register_macros)

    def _handle_register_macros(self, lane: Any) -> None:
        """
        Zmod defines a SET_RUNOUT macro (a stub for its built-in AFC panel)
        that blocks AFC's per-lane SET_RUNOUT; drop it before AFC registers
        its command. AFC_IFS does the same for IFS lanes.
        """
        if self.firmware != "zmod" or self._runout_dropped:
            return
        self._runout_dropped = True
        gcode = self.printer.lookup_object("gcode")
        if "SET_RUNOUT" not in getattr(gcode, "mux_commands", {}):
            gcode.register_command("SET_RUNOUT", None)
            logging.info("AFC_FF: Zmod's SET_RUNOUT macro dropped for AFC's")

    def _speed_up_adc(self) -> None:
        """
        Shorten the head ADC's report time. Runs at klippy:mcu_identify,
        before the MCUs are configured (the report time goes into the ADC's
        MCU config at connect).
        """
        if not self.adc_report_time:
            return
        query_adc = self.printer.lookup_object("query_adc", None)
        mcu_adc = getattr(query_adc, "adc", {}).get(self.adc_name)
        current = getattr(mcu_adc, "_report_time", None)
        if not current or current <= self.adc_report_time:
            return
        mcu_adc._report_time = self.adc_report_time
        logging.info("AFC_FF: %s report time %.3f -> %.3f s",
                     self.adc_name, current, self.adc_report_time)

    def _handle_connect(self) -> None:
        query_adc = self.printer.lookup_object("query_adc", None)
        if query_adc is None or self.adc_name not in query_adc.adc:
            raise self.printer.config_error(
                f"[AFC_FF]: no ADC named '{self.adc_name}' (see QUERY_ADC)")

    def _handle_ready(self) -> None:
        for name in self.disable_sensors:
            obj = self.printer.lookup_object(name, None)
            helper = getattr(obj, "runout_helper", None)
            if helper is None:
                logging.info("AFC_FF: sensor '%s' not found, not disabled", name)
                continue
            helper.sensor_enabled = False
            logging.info("AFC_FF: firmware sensor '%s' disabled, AFC owns runout", name)
        self._timer = self.reactor.register_timer(self._poll, self.reactor.NOW)

    def read_present(self) -> Optional[bool]:
        """
        :return Optional[bool]: True/False from the head ADC, None while unreadable
        """
        query_adc = self.printer.lookup_object("query_adc", None)
        adc = query_adc.adc.get(self.adc_name) if query_adc is not None else None
        if adc is None:
            return None
        try:
            _, value = adc.get_last_value()
        except Exception:
            return None
        self.value = value
        return value <= self.present_below or value >= self.present_above

    def _poll(self, eventtime: float) -> float:
        present = self.read_present()
        if present is not None and present != self.runout_helper.filament_present:
            # Looked up each time: AFC_extruder replaces it with its wrapper
            self.runout_helper.note_filament_present(present)
        return eventtime + self.poll_time

    @property
    def runout_buttun_state(self) -> bool:
        """
        The raw state AFC's PollingEndstopWrapper and
        get_toolhead_pre_sensor_state read (name as AFC spells it).
        Read fresh, not from the last poll, so homing stops as early as the
        ADC allows.
        """
        present = self.read_present()
        if present is None:
            return bool(self.runout_helper.filament_present)
        return present

    def get_status(self, eventtime: float) -> Dict[str, Any]:
        status = self.runout_helper.get_status(eventtime)
        status["value"] = self.value
        return status


def load_config(config: ConfigWrapper) -> AFC_FF:
    return AFC_FF(config)
