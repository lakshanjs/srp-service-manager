import os
import subprocess
import threading
import requests
import time
from datetime import datetime
import config_manager as config

class ServiceManager:
    def __init__(self):
        # Load services from the configuration file
        self.services = config.load_config()
        self.processes = {}
        self.stop_flags = {}  # Stop flags to handle long-running tasks like cron

    def start_service(self, service_name, service_tabs, append_output, save_config):
        service_info = self.services[service_name]
        if service_name in self.processes and self.processes[service_name]:
            append_output(service_name, "Service is already running.")
            return

        # Save the current settings before starting the service
        if "command" in service_info:
            dir_entry = service_tabs[service_name]["dir_entry"]
            directory = dir_entry.get()

            if service_name in ["Ngrok", "Tika"]:
                command_entry = service_tabs[service_name]["command_entry"]
                command = command_entry.get().split()
                service_info["command"] = command

            service_info["dir"] = directory
            # Start the service in a new thread
            threading.Thread(target=self.run_service, args=(service_name, service_info["command"], directory, append_output)).start()

        elif "url" in service_info:
            # Start a cron-like service (polling a URL)
            url_entry = service_tabs[service_name]["url_entry"]
            interval_entry = service_tabs[service_name]["interval_entry"]
            url = url_entry.get()
            interval = int(interval_entry.get())
            service_info["url"] = url
            service_info["interval"] = interval
            self.stop_flags[service_name] = threading.Event()  # Create a stop flag
            # Start the cron service in a new thread
            threading.Thread(target=self.run_cron_service, args=(service_name, url, interval, append_output)).start()

        # Save updated configuration
        save_config()

    def run_service(self, service_name, command, service_dir, append_output):
        """Starts the service without opening a new command window and captures its output."""
        try:
            os.chdir(service_dir)  # Change to the service directory

            # Use CREATE_NO_WINDOW to prevent command window from appearing
            CREATE_NO_WINDOW = 0x08000000

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
                text=True,  # Ensure text mode for proper line-by-line reading
                bufsize=1   # Line-buffered output
            )

            self.processes[service_name] = process  # Store the process in a dictionary
            append_output(service_name, "Service started.")

            # Read stdout and stderr from the process line-by-line
            def read_output(pipe, service_name):
                for line in iter(pipe.readline, ''):
                    if line:
                        append_output(service_name, line.strip())  # Append each log line to the output
                pipe.close()

            # Start threads to read stdout and stderr simultaneously
            threading.Thread(target=read_output, args=(process.stdout, service_name)).start()
            threading.Thread(target=read_output, args=(process.stderr, service_name)).start()

        except Exception as e:
            append_output(service_name, f"Error starting service: {e}")


    def run_cron_service(self, service_name, url, interval, append_output):
        """Starts a cron-like service that polls a URL at regular intervals."""
        append_output(service_name, f"Starting {service_name}...")
        self.processes[service_name] = True
        append_output(service_name, "Service started")

        stop_flag = self.stop_flags[service_name]  # Get the stop flag for this service

        while not stop_flag.is_set():  # Continue running until the stop flag is set
            try:
                response = requests.get(url, verify=False)
                append_output(service_name, f"Called {url}: {response.status_code}")
                append_output(service_name, f"Response: {response.text}")
            except requests.RequestException as e:
                append_output(service_name, f"Error calling {url}: {e}")

            stop_flag.wait(interval)  # Pause for the interval, but allow for interruption

    def stop_service(self, service_name, append_output):
        """Stops the service."""
        if service_name in self.processes and self.processes[service_name]:
            append_output(service_name, f"Stopping {service_name}...")

            if service_name == "Ngrok":
                subprocess.call(["taskkill", "/F", "/IM", "ngrok.exe"])
            elif service_name == "Tika":
                subprocess.call(["taskkill", "/F", "/IM", "java.exe"])
            elif "url" in self.services[service_name]:  # Cron-like services
                if service_name in self.stop_flags:
                    self.stop_flags[service_name].set()  # Set the stop flag to stop the cron task
            elif isinstance(self.processes[service_name], subprocess.Popen):
                self.processes[service_name].terminate()

            self.processes[service_name] = None
            append_output(service_name, "Service stopped")
        else:
            append_output(service_name, "Service is not running.")

    def stop_all_services(self):
        """Stop all running services."""
        for service_name in self.services:
            if service_name in self.processes and self.processes[service_name]:
                self.stop_service(service_name, lambda name, msg: None)  # Stop service without appending output

    def restart_service(self, service_name, service_tabs, append_output, save_config):
        """Restarts the service by stopping it and starting it again."""
        self.stop_service(service_name, append_output)
        self.start_service(service_name, service_tabs, append_output, save_config)

    def clear_log(self, service_name, service_tabs):
        """Clears the output log for the service."""
        output_area = service_tabs[service_name]["output_area"]
        output_area.delete(1.0, 'end')

    def monitor_output(self, service_name, process, append_output):
        """Monitors the output from the service process."""
        def read_output():
            for output in iter(process.stdout.readline, ''):
                if output:
                    append_output(service_name, output.strip())
            process.stdout.close()

        threading.Thread(target=read_output).start()
