import tkinter as tk
from tkinter import scrolledtext
import config_manager  # Add this import to handle saving the configuration

class UIManager:
    def __init__(self, root, service_manager):
        self.root = root
        self.service_manager = service_manager
        self.service_tabs = {}

    def create_service_tab(self, tab, service_name, service_info, start_service, stop_service, restart_service, clear_log):
        # Directory and Command Row
        if "command" in service_info:
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

                self.service_tabs[service_name] = {"dir_entry": dir_entry, "command_entry": command_entry}
            else:
                self.service_tabs[service_name] = {"dir_entry": dir_entry}

        elif "url" in service_info:
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

            self.service_tabs[service_name] = {"url_entry": url_entry, "interval_entry": interval_entry}

        # Actions Row
        actions_frame = tk.Frame(tab)
        actions_frame.pack(pady=5, fill='x')

        start_button = tk.Button(actions_frame, text="Start Service", command=lambda: start_service(service_name, self.service_tabs, self.append_output, self.save_config))
        start_button.pack(side=tk.LEFT, padx=5)

        stop_button = tk.Button(actions_frame, text="Stop Service", command=lambda: stop_service(service_name, self.append_output))
        stop_button.pack(side=tk.LEFT, padx=5)

        restart_button = tk.Button(actions_frame, text="Restart Service", command=lambda: restart_service(service_name, self.service_tabs, self.append_output, self.save_config))
        restart_button.pack(side=tk.LEFT, padx=5)

        clear_log_button = tk.Button(actions_frame, text="Clear Log", command=lambda: clear_log(service_name, self.service_tabs))
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

    def append_output(self, service_name, text):
        output_area = self.service_tabs[service_name]["output_area"]
        output_area.insert(tk.END, text + '\n')
        output_area.see(tk.END)

    def save_config(self):
        config_manager.save_config(self.service_manager.services)
