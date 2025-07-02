import yaml

GLOBALS_PATH = "config/globals.yaml"

global_vars = yaml.safe_load(open(GLOBALS_PATH))

def save_globals():
    yaml.dump(global_vars, open(GLOBALS_PATH, "w"))
