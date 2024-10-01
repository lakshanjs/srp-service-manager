import tkinter as tk
from tkinter import scrolledtext, ttk
import subprocess
import os
import threading
import time
import requests
from datetime import datetime
import json
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

class ServiceManager:
    def __init__(self, root):
        self.root = root
        self.root.title("SRP Live Help Service Manager")
        self.root.geometry("800x600")

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(expand=True, fill='both')

        # Load configurations from the config file
        self.services = self.load_config()

        self.service_tabs = {}
        self.processes = {}

        for service_name in self.services:
            tab = ttk.Frame(self.notebook)
            self.notebook.add(tab, text=service_name)
            self.create_service_tab(tab, service_name)

        # Handle window close event to stop all services
        root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def load_config(self):
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

    def save_config(self):
        config_path = get_config_path()
        with open(config_path, 'w') as file:
            json.dump(self.services, file, indent=4)     

    def create_service_tab(self, tab, service_name):
        service_info = self.services[service_name]

        if "command" in service_info:  # For regular services
            # Directory and Command Row for Ngrok and Tika (50% each)
            dir_frame = tk.Frame(tab)
            dir_frame.pack(pady=5, fill='x')

            dir_label = tk.Label(dir_frame, text="Directory:")
            dir_label.pack(side=tk.LEFT, padx=5)

            dir_entry = tk.Entry(dir_frame, width=40)
            dir_entry.insert(0, service_info["dir"])
            dir_entry.pack(side=tk.LEFT, padx=5, fill='x', expand=True)

            if service_name in ["Ngrok", "Tika"]:
                command_label = tk.Label(dir_frame, text="Command:")
                command_label.pack(side=tk.LEFT, padx=5)

                command_entry = tk.Entry(dir_frame, width=40)
                command_entry.insert(0, " ".join(service_info["command"]))
                command_entry.pack(side=tk.LEFT, padx=5, fill='x', expand=True)

                # Save the command entry for later use
                self.service_tabs[service_name] = {"dir_entry": dir_entry, "command_entry": command_entry}
            else:
                # Save the directory entry for later use
                self.service_tabs[service_name] = {"dir_entry": dir_entry}

        elif "url" in service_info:  # For cron-like service
            # URL and Interval Row
            url_frame = tk.Frame(tab)
            url_frame.pack(pady=5, fill='x')

            url_label = tk.Label(url_frame, text="URL:")
            url_label.pack(side=tk.LEFT, padx=5)

            url_entry = tk.Entry(url_frame, width=50)
            url_entry.insert(0, service_info["url"])
            url_entry.pack(side=tk.LEFT, padx=5, fill='x', expand=True)

            interval_label = tk.Label(url_frame, text="Interval (seconds):")
            interval_label.pack(side=tk.LEFT, padx=5)

            interval_entry = tk.Entry(url_frame, width=10)
            interval_entry.insert(0, str(service_info["interval"]))
            interval_entry.pack(side=tk.LEFT, padx=5)

            # Save the URL and interval entry for later use
            self.service_tabs[service_name] = {"url_entry": url_entry, "interval_entry": interval_entry}

        # Actions Row
        actions_frame = tk.Frame(tab)
        actions_frame.pack(pady=5, fill='x')

        # Start Button
        start_button = tk.Button(actions_frame, text="Start Service", command=lambda: self.start_service(service_name))
        start_button.pack(side=tk.LEFT, padx=5)

        # Stop Button
        stop_button = tk.Button(actions_frame, text="Stop Service", command=lambda: self.stop_service(service_name))
        stop_button.pack(side=tk.LEFT, padx=5)

        # Restart Button
        restart_button = tk.Button(actions_frame, text="Restart Service", command=lambda: self.restart_service(service_name))
        restart_button.pack(side=tk.LEFT, padx=5)

        # Clear Log Button
        clear_log_button = tk.Button(actions_frame, text="Clear Log", command=lambda: self.clear_log(service_name))
        clear_log_button.pack(side=tk.LEFT, padx=5)

        # Output Area
        output_area = scrolledtext.ScrolledText(tab, wrap=tk.WORD, width=90, height=20)
        output_area.pack(pady=10, fill='both', expand=True)

        # Save the output area and buttons for later use
        self.service_tabs[service_name].update({
            "start_button": start_button,
            "stop_button": stop_button,
            "restart_button": restart_button,
            "clear_log_button": clear_log_button,
            "output_area": output_area
        })

    def start_service(self, service_name):
        if service_name in self.processes and self.processes[service_name]:
            self.append_output(service_name, "Service is already running.")
            return

        service_info = self.services[service_name]

        # Save the current settings before starting the service
        if "command" in service_info:  # Handle regular subprocess service
            dir_entry = self.service_tabs[service_name]["dir_entry"]
            directory = dir_entry.get()

            if service_name in ["Ngrok", "Tika"]:
                command_entry = self.service_tabs[service_name]["command_entry"]
                command = command_entry.get().split()
                service_info["command"] = command

            service_info["dir"] = directory
            threading.Thread(target=self.run_service, args=(service_name, directory, service_info["command"])).start()

        elif "url" in service_info:  # Handle cron-like service
            url_entry = self.service_tabs[service_name]["url_entry"]
            interval_entry = self.service_tabs[service_name]["interval_entry"]
            url = url_entry.get()
            interval = int(interval_entry.get())
            service_info["url"] = url
            service_info["interval"] = interval
            threading.Thread(target=self.run_cron_service, args=(service_name, url, interval)).start()

        # Save updated configuration
        self.save_config()

    def run_service(self, service_name, service_dir, command):
        self.append_output(service_name, f"Starting {service_name}...")
        if service_dir:
            os.chdir(service_dir)
        # Use CREATE_NO_WINDOW flag to prevent a console window from appearing
        CREATE_NO_WINDOW = 0x08000000
        process = subprocess.Popen(
            command, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT,  
            text=True, 
            bufsize=1, 
            universal_newlines=True,
            creationflags=CREATE_NO_WINDOW
        )
        self.processes[service_name] = process
        self.append_output(service_name, "Service started")
        self.monitor_output(service_name, process)

    def run_cron_service(self, service_name, url, interval):
        self.append_output(service_name, f"Starting {service_name}...")
        self.processes[service_name] = True  # Mark as running
        self.append_output(service_name, "Service started")

        while self.processes[service_name]:  # Continue as long as the service is marked as running
            try:
                # Making the HTTP request
                response = requests.get(url, verify=False)
                
                # Append status code to the output
                self.append_output(service_name, f"Called {url}: {response.status_code}")
                
                # Append the actual response content to the output
                self.append_output(service_name, f"Response: {response.text}")
            
            except requests.RequestException as e:
                self.append_output(service_name, f"Error calling {url}: {e}")
            
            # Wait for the specified interval before the next request
            time.sleep(interval)

    def stop_service(self, service_name):
        if service_name in self.processes and self.processes[service_name]:
            self.append_output(service_name, f"Stopping {service_name}...")

            if self.services[service_name].get("type") == "cron":
                self.processes[service_name] = False  # Stop the loop in cron service
            elif service_name == "Ngrok":
                subprocess.call(["taskkill", "/F", "/IM", "ngrok.exe"])
            elif service_name == "Tika":
                subprocess.call(["taskkill", "/F", "/IM", "java.exe"])
            else:
                self.processes[service_name].terminate()

            self.processes[service_name] = None
            self.append_output(service_name, "Service stopped")
        else:
            self.append_output(service_name, "Service is not running.")

    def restart_service(self, service_name):
        self.stop_service(service_name)
        self.start_service(service_name)

    def monitor_output(self, service_name, process):
        def read_output():
            for output in iter(process.stdout.readline, ''):
                if output:
                    self.append_output(service_name, output.strip())
            process.stdout.close()

        threading.Thread(target=read_output).start()

    def clear_log(self, service_name):
        output_area = self.service_tabs[service_name]["output_area"]
        output_area.delete(1.0, tk.END)

    def append_output(self, service_name, text):
        timestamp = datetime.now().strftime('%Y-%m-%d %I:%M %p')
        output_area = self.service_tabs[service_name]["output_area"]
        output_area.insert(tk.END, f"{timestamp} - {text}\n")
        output_area.insert(tk.END, "-" * 80 + "\n")  # Add a separator line between logs
        output_area.see(tk.END)
        output_area.update_idletasks()  # Ensure the output is updated immediately

    def on_closing(self):
        # Stop all services before closing
        for service_name in self.processes.keys():
            self.stop_service(service_name)
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = ServiceManager(root)
    root.mainloop()
