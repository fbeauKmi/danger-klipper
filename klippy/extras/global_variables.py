# Load global variables to replace placeholders in config and macros
#
# Copyright (C) 2024  Frédéric Beaucamp <fbeaukmi@mailo.eu>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import configparser

error = configparser.Error


class GlobalVariables:
    def __init__(self, config):
        self.config = config
        self.vars = {}
        self.options = {}

        try:
            for opt in config.get_prefix_options(""):
                self.vars[opt] = config.get(opt)
        except:
            pass

    def replace(self, config, section, option):
        prefix = "var:"
        v = config.get(section, option)
        if v.startswith(prefix):
            v = v[len(prefix) :].strip()
            if v in self.vars:
                config.set(section, option, self.vars[v])
                if section not in self.options:
                    self.options[section] = {}
                self.options[section][option] = v
            else:
                raise error(
                    "'%s.%s' : Global variable 'var:%s' doesn't exist"
                    % (section, option, v)
                )

    def get_status(self, eventtime):
        return {"variables": self.vars, "replaced": self.options}


GLOBAL_VARIABLES: GlobalVariables = None


def global_variables_replacement(config, section, option):
    global GLOBAL_VARIABLES
    if GLOBAL_VARIABLES is not None:
        GLOBAL_VARIABLES.replace(config, section, option)


def load_config(config):
    global GLOBAL_VARIABLES
    GLOBAL_VARIABLES = GlobalVariables(config)
    return GLOBAL_VARIABLES
