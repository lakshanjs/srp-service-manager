import json
import os
import sys

CONFIG_FILE_NAME = "srpconf.json"

def get_config_path():
    # Determine the path to the config file
    if getattr(sys, 'frozen', False):
        # If running as a compiled .exe
        return os.path.join(os.path.dirname(sys.executable), CONFIG_FILE_NAME)
    else:
        # If running as a script
        return CONFIG_FILE_NAME

def load_config():
    config_path = get_config_path()
    try:
        with open(config_path, 'r') as file:
            return json.load(file)
    except FileNotFoundError:
        # Default service configurations if the config file doesn't exist
        default_config = {
            "Main Centrifugo": {
                "dir": "D:\\wamp\\www\\srplh\\servers\\cfgo\\",
                "command": ["centrifugo", "-c", "config.json"]
            },
            "CS Centrifugo": {
                "dir": "D:\\wamp\\www\\srplh\\servers\\cfgo\\",
                "command": ["centrifugo", "-c", "csconfig.json"]
            },
            "Worker": {
                "dir": "D:\\wamp\\www\\srplh\\servers\\workers\\",
                "command": ["php", "worker.php"]
            },
            "Cron Task": {
                "type": "cron",
                "url": "https://srplh.test/cron/tasks",
                "interval": 60  # Run every 60 seconds
            },
            "Memcached": {
                "dir": "C:\\memcached\\bin\\",
                "command": ["memcached.exe"]
            },
            "Ngrok": {
                "dir": "",  # Empty by default
                "command": ["ngrok", "http", "--domain=in-unicorn-smart.ngrok-free.app", "80"]
            },
            "Tika": {
                "dir": "C:\\Tika",
                "command": ["java", "-jar", "tika-server-standard-2.8.0.jar"]
            }
        }
        
        # Save the default configuration to srpconf.json
        with open(config_path, 'w') as file:
            json.dump(default_config, file, indent=4)
        
        return default_config

def save_config(services):
    config_path = get_config_path()
    with open(config_path, 'w') as file:
        json.dump(services, file, indent=4)
