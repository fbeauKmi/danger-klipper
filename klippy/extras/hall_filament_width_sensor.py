# Support for filament width sensor
#
# Copyright (C) 2019  Mustafa YILDIZ <mydiz@hotmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from . import filament_switch_sensor

ADC_REPORT_TIME = 0.500
ADC_SAMPLE_TIME = 0.03
ADC_SAMPLE_COUNT = 15


class HallFilamentWidthSensor:
    def __init__(self, config):
        self.printer = config.get_printer()
        gcode_macro = self.printer.load_object(config, "gcode_macro")
        self.reactor = self.printer.get_reactor()
        self.estimated_print_time = None
        self.pin1 = config.get("adc1")
        self.pin2 = config.get("adc2")
        self.dia1 = config.getfloat("Cal_dia1", 1.5)
        self.dia2 = config.getfloat("Cal_dia2", 2.0)
        self.rawdia1 = config.getint("Raw_dia1", 9500)
        self.rawdia2 = config.getint("Raw_dia2", 10500)
        self.MEASUREMENT_INTERVAL_MM = config.getint("measurement_interval", 10)
        self.nominal_filament_dia = config.getfloat(
            "default_nominal_filament_diameter", above=1
        )
        self.measurement_delay = config.getfloat("measurement_delay", above=0.0)
        self.measurement_max_difference = config.getfloat("max_difference", 0.2)
        self.max_diameter = (
            self.nominal_filament_dia + self.measurement_max_difference
        )
        self.min_diameter = (
            self.nominal_filament_dia - self.measurement_max_difference
        )
        self.diameter = self.nominal_filament_dia
        self.is_active = config.getboolean("enable", False)
        self.runout_dia_min = config.getfloat("min_diameter", 1.0)
        self.runout_dia_max = config.getfloat("max_diameter", self.max_diameter)
        self.is_log = config.getboolean("logging", False)
        # Use the current diameter instead of nominal while the first
        # measurement isn't in place
        self.use_current_dia_while_delay = config.getboolean(
            "use_current_dia_while_delay", False
        )
        runout_distance = config.getfloat("runout_distance", 0.0, minval=0.0)
        self.check_on_print_start = config.getboolean(
            "check_on_print_start", False
        )
        # filament array [position, filamentWidth]
        self.filament_array = []
        self.lastFilamentWidthReading = 0
        self.lastFilamentWidthReading2 = 0
        self.firstExtruderUpdatePosition = 0
        self.filament_width = self.nominal_filament_dia
        # printer objects
        self.toolhead = self.ppins = self.mcu_adc = None
        self.printer.register_event_handler("klippy:ready", self.handle_ready)
        # Start adc
        self.ppins = self.printer.lookup_object("pins")
        self.mcu_adc = self.ppins.setup_pin("adc", self.pin1)
        self.mcu_adc.setup_minmax(ADC_SAMPLE_TIME, ADC_SAMPLE_COUNT)
        self.mcu_adc.setup_adc_callback(ADC_REPORT_TIME, self.adc_callback)
        self.mcu_adc2 = self.ppins.setup_pin("adc", self.pin2)
        self.mcu_adc2.setup_minmax(ADC_SAMPLE_TIME, ADC_SAMPLE_COUNT)
        self.mcu_adc2.setup_adc_callback(ADC_REPORT_TIME, self.adc2_callback)
        # extrude factor updating
        self.extrude_factor_update_timer = self.reactor.register_timer(
            self.extrude_factor_update_event
        )
        # Register commands
        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command("QUERY_FILAMENT_WIDTH", self.cmd_M407)
        self.gcode.register_command(
            "RESET_FILAMENT_WIDTH_SENSOR", self.cmd_ClearFilamentArray
        )
        self.gcode.register_command(
            "DISABLE_FILAMENT_WIDTH_SENSOR", self.cmd_M406
        )
        self.gcode.register_command(
            "ENABLE_FILAMENT_WIDTH_SENSOR", self.cmd_M405
        )
        self.gcode.register_command(
            "QUERY_RAW_FILAMENT_WIDTH", self.cmd_Get_Raw_Values
        )
        self.gcode.register_command(
            "ENABLE_FILAMENT_WIDTH_LOG", self.cmd_log_enable
        )
        self.gcode.register_command(
            "DISABLE_FILAMENT_WIDTH_LOG", self.cmd_log_disable
        )

        self.runout_helper = filament_switch_sensor.RunoutHelper(
            config, self, runout_distance
        )
        if config.get("immediate_runout_gcode", None) is not None:
            self.runout_helper.immediate_runout_gcode = (
                gcode_macro.load_template(config, "immediate_runout_gcode", "")
            )

        self.printer.register_event_handler(
            "print_stats:start_printing", self._handle_printing_smart
        )

        self.printer.register_event_handler(
            "idle_timeout:printing", self._handle_printing
        )

    # Initialization
    def handle_ready(self):
        # Load printer objects
        self.toolhead = self.printer.lookup_object("toolhead")

        self.estimated_print_time = self.printer.lookup_object(
            "mcu"
        ).estimated_print_time

        # Start extrude factor update timer
        self.reactor.update_timer(
            self.extrude_factor_update_timer, self.reactor.NOW
        )

    def _handle_printing(self, *args):
        if not self.runout_helper.smart:
            if self.check_on_print_start:
                self.reset()
                self.runout_helper.note_filament_present(
                    self.runout_dia_min <= self.diameter <= self.runout_dia_max,
                    True,
                    True,
                )

    def _handle_printing_smart(self, *args):
        if self.runout_helper.smart:
            if self.check_on_print_start:
                self.reset()
                self.runout_helper.note_filament_present(
                    self.runout_dia_min <= self.diameter <= self.runout_dia_max,
                    True,
                    True,
                )

    def get_extruder_pos(self, eventtime=None):
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        print_time = self.estimated_print_time(eventtime)
        extruder = self.printer.lookup_object("toolhead").get_extruder()
        return extruder.find_past_position(print_time)

    def adc_callback(self, read_time, read_value):
        # read sensor value
        self.lastFilamentWidthReading = round(read_value * 10000)

    def adc2_callback(self, read_time, read_value):
        # read sensor value
        self.lastFilamentWidthReading2 = round(read_value * 10000)
        # calculate diameter
        diameter_new = round(
            (self.dia2 - self.dia1)
            / (self.rawdia2 - self.rawdia1)
            * (
                (self.lastFilamentWidthReading + self.lastFilamentWidthReading2)
                - self.rawdia1
            )
            + self.dia1,
            2,
        )
        self.diameter = (5.0 * self.diameter + diameter_new) / 6

    def update_filament_array(self, last_epos):
        # Fill array
        if len(self.filament_array) > 0:
            # Get last reading position in array & calculate next
            # reading position
            next_reading_position = (
                self.filament_array[-1][0] + self.MEASUREMENT_INTERVAL_MM
            )
            if next_reading_position <= (last_epos + self.measurement_delay):
                self.filament_array.append(
                    [last_epos + self.measurement_delay, self.diameter]
                )
                if self.is_log:
                    self.gcode.respond_info(
                        "Filament width:%.3f" % (self.diameter)
                    )

        else:
            # add first item to array
            self.filament_array.append(
                [self.measurement_delay + last_epos, self.diameter]
            )
            self.firstExtruderUpdatePosition = (
                self.measurement_delay + last_epos
            )

    def extrude_factor_update_event(self, eventtime):
        # Update extrude factor
        pos = self.toolhead.get_position()
        last_epos = pos[3]
        # Update filament array for lastFilamentWidthReading
        self.update_filament_array(last_epos)
        # Check runout
        self.runout_helper.note_filament_present(
            self.runout_dia_min <= self.diameter <= self.runout_dia_max
        )
        # Does filament exists
        if self.diameter > 0.5:
            if len(self.filament_array) > 0:
                # Get first position in filament array
                pending_position = self.filament_array[0][0]
                if pending_position <= last_epos:
                    # Get first item in filament_array queue
                    item = self.filament_array.pop(0)
                    self.filament_width = item[1]
                else:
                    if (self.use_current_dia_while_delay) and (
                        self.firstExtruderUpdatePosition == pending_position
                    ):
                        self.filament_width = self.diameter
                    elif self.firstExtruderUpdatePosition == pending_position:
                        self.filament_width = self.nominal_filament_dia
                if (self.filament_width <= self.max_diameter) and (
                    self.filament_width >= self.min_diameter
                ):
                    percentage = round(
                        self.nominal_filament_dia**2
                        / self.filament_width**2
                        * 100
                    )
                    self.gcode.run_script("M221 S" + str(percentage))
                else:
                    self.gcode.run_script("M221 S100")
        else:
            self.gcode.run_script("M221 S100")
            self.filament_array = []

        if self.is_active:
            return eventtime + 1
        else:
            return self.reactor.NEVER

    def cmd_M407(self, gcmd):
        response = ""
        if self.diameter > 0:
            response += "Filament dia (measured mm): " + str(self.diameter)
        else:
            response += "Filament NOT present"
        gcmd.respond_info(response)

    def cmd_ClearFilamentArray(self, gcmd):
        self.filament_array = []
        gcmd.respond_info("Filament width measurements cleared!")
        # Set extrude multiplier to 100%
        self.gcode.run_script_from_command("M221 S100")

    def cmd_M405(self, gcmd):
        response = "Filament width sensor Turned On"
        if self.is_active:
            response = "Filament width sensor is already On"
        else:
            self.is_active = True
            # Start extrude factor update timer
            self.reactor.update_timer(
                self.extrude_factor_update_timer, self.reactor.NOW
            )
        gcmd.respond_info(response)

    def cmd_M406(self, gcmd):
        response = "Filament width sensor Turned Off"
        if not self.is_active:
            response = "Filament width sensor is already Off"
        else:
            self.is_active = False
            # Stop extrude factor update timer
            self.reactor.update_timer(
                self.extrude_factor_update_timer, self.reactor.NEVER
            )
            # Clear filament array
            self.filament_array = []
            # Set extrude multiplier to 100%
            self.gcode.run_script_from_command("M221 S100")
        gcmd.respond_info(response)

    def cmd_Get_Raw_Values(self, gcmd):
        response = "ADC1="
        response += " " + str(self.lastFilamentWidthReading)
        response += " ADC2=" + str(self.lastFilamentWidthReading2)
        response += " RAW=" + str(
            self.lastFilamentWidthReading + self.lastFilamentWidthReading2
        )
        gcmd.respond_info(response)

    def get_info(self, gcmd):
        check_on_print_start = gcmd.get_int(
            "CHECK_ON_PRINT_START", None, minval=0, maxval=1
        )
        if check_on_print_start is None:
            gcmd.respond_info(self.get_sensor_status())
            return True
        return False

    def reset_needed(self, enable=None, always_fire_events=None):
        if enable is not None and enable != self.runout_helper.sensor_enabled:
            return True
        if always_fire_events is not None and always_fire_events:
            return True
        return False

    def set_filament_sensor(self, gcmd):
        check_on_print_start = gcmd.get_int(
            "CHECK_ON_PRINT_START", None, minval=0, maxval=1
        )
        if check_on_print_start is not None:
            self.check_on_print_start = check_on_print_start
        # No reset is needed when changing check_on_print_start, so we always
        # return False
        return False

    def reset(self):
        self.runout_helper.reset_runout_distance_info()
        self.runout_helper.note_filament_present(
            self.runout_helper.filament_present, True
        )

    def get_sensor_status(self):
        return (
            "Filament Width Sensor %s: %s\n"
            "Filament Detected: %s\n"
            "Smart: %s\n"
            "Always Fire Events: %s"
            % (
                self.runout_helper.name,
                (
                    "enabled"
                    if self.runout_helper.sensor_enabled
                    else "disabled"
                ),
                "true" if self.runout_helper.filament_present else "false",
                "true" if self.runout_helper.smart else "false",
                "true" if self.runout_helper.always_fire_events else "false",
            )
        )

    def sensor_get_status(self, eventtime=None):
        return self.get_status()

    def get_status(self, eventtime=None):
        status = self.runout_helper.get_status(eventtime)
        status.update(
            {
                "Diameter": self.diameter,
                "Raw": (
                    self.lastFilamentWidthReading
                    + self.lastFilamentWidthReading2
                ),
                "is_active": self.is_active,
                "check_on_print_start": bool(self.check_on_print_start),
            }
        )
        return status

    def cmd_log_enable(self, gcmd):
        self.is_log = True
        gcmd.respond_info("Filament width logging Turned On")

    def cmd_log_disable(self, gcmd):
        self.is_log = False
        gcmd.respond_info("Filament width logging Turned Off")


def load_config(config):
    return HallFilamentWidthSensor(config)
