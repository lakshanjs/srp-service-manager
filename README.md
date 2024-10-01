
# SRP Service Manager

A service management tool that provides the ability to start, stop, and monitor multiple services, like Centrifugo, Tika, and custom cron tasks, from a simple interface.

## Features

- Start and stop services like Centrifugo, Tika, and cron tasks.
- Monitor the output logs of each service.
- Minimize the application to the system tray.
- Supports configuration from JSON (`srpconf.json`).
- Includes `icon.ico` for the application and system tray icon.

## Requirements

- Python 3.x
- PyInstaller
- Additional Python packages (install via `requirements.txt`)

### Installing Dependencies

To install the required Python libraries, run:

```bash
pip install -r requirements.txt
```

## How to Build the Executable

To create an executable from the Python script using PyInstaller, use the following command:

```bash
pyinstaller --onefile --icon=icon.ico --noconsole --add-data "srpconf.json;." --add-data "icon.ico;." main.py
```

### Explanation:
- **`--onefile`**: Bundles everything into a single `.exe` file.
- **`--icon=icon.ico`**: Sets the icon for the application.
- **`--noconsole`**: Prevents the console window from showing when the application runs.
- **`--add-data "srpconf.json;."`**: Adds the configuration file to the executable.
- **`--add-data "icon.ico;."`**: Includes the icon file for use in the system tray.

The built executable will be located in the `dist/` directory after running this command.

## Usage

After building the executable, you can run it by navigating to the `dist/` folder and launching the `.exe` file.

## Configuration

The application uses `srpconf.json` to configure the services it manages. Make sure the configuration file is present in the same directory as the executable.
