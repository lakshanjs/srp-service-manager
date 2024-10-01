import tkinter as tk
from tkinter import ttk
from service import ServiceManager
from tray_manager import TrayManager
from ui_manager import UIManager

if __name__ == "__main__":
    root = tk.Tk()
    root.title("SRP Live Help Service Manager")
    root.geometry("800x600")

    # Initialize service manager
    service_manager = ServiceManager()  
    ui_manager = UIManager(root, service_manager)
    tray_manager = TrayManager(root, service_manager)

    # Create a notebook (tabbed interface) to hold the service tabs
    notebook = ttk.Notebook(root)
    notebook.pack(expand=True, fill='both')

    # Create tabs for each service
    for service_name, service_info in service_manager.services.items():
        tab = tk.Frame(notebook)
        notebook.add(tab, text=service_name)  # Add each tab to the notebook
        ui_manager.create_service_tab(
            tab, service_name, service_info,
            service_manager.start_service, service_manager.stop_service,
            service_manager.restart_service, service_manager.clear_log
        )

    # Set up the tray functionality
    tray_manager.setup_tray()

    root.mainloop()
