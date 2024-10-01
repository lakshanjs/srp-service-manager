import os
import tkinter as tk
import threading
from PIL import Image
import pystray
from pystray import MenuItem as Item

class TrayManager:
    def __init__(self, root, service_manager, app_name="SRP Service Manager"):
        self.root = root
        self.service_manager = service_manager
        self.app_name = app_name
        self.tray_icon = None
        # Dynamically get the path to the icon file
        self.icon_path = os.path.join(os.path.dirname(__file__), 'icon.ico')  # Ensure icon.ico is in the same folder
        self.set_window_icon()

    def set_window_icon(self):
        """Set the window icon to the same .ico file"""
        if os.path.exists(self.icon_path):
            self.root.iconbitmap(self.icon_path)  # Set the Tkinter window icon
        else:
            print(f"Error: icon file not found at {self.icon_path}")

    def setup_tray(self):
        """Sets up tray functionality."""
        # Capture minimize event by binding the 'iconify' event
        self.root.bind("<Unmap>", self.on_minimize)
        # Handle the close button separately, ensuring it stops services and quits
        self.root.protocol("WM_DELETE_WINDOW", self.exit_application)

    def on_minimize(self, event=None):
        """Handle minimize to tray when the window is minimized."""
        if self.root.state() == 'iconic':  # Only minimize to tray when minimized
            self.minimize_to_tray()

    def minimize_to_tray(self):
        """Minimizes the window and starts the system tray icon."""
        self.root.withdraw()  # Hide the window
        # Check if the icon file exists before trying to open it
        if not os.path.exists(self.icon_path):
            print(f"Error: icon file not found at {self.icon_path}")
            return

        image = Image.open(self.icon_path)

        # Define the tray menu with 'Show' and 'Exit' options
        self.menu = pystray.Menu(
            Item('Show', self.restore_window),
            Item('Exit', self.exit_application)
        )

        # Create the tray icon and bind the click event to restore the window
        self.tray_icon = pystray.Icon(self.app_name, image, menu=self.menu)
        tray_thread = threading.Thread(target=self.run_tray_icon, daemon=True)
        tray_thread.start()

    def run_tray_icon(self):
        """Starts the tray icon and handles left-click for restoring the window."""
        self.tray_icon.run(setup=self.bind_tray_click)

    def bind_tray_click(self, icon=None):
        """Binds the tray icon click event to restore the window."""
        self.tray_icon.visible = True  # Ensure the tray icon is visible
        self.tray_icon.icon.click = self.restore_window  # Bind the click event to restore the window

    def restore_window(self, icon=None, item=None):
        """Restore the window from the system tray."""
        if self.tray_icon:
            self.tray_icon.stop()  # Stop the tray icon
        self.root.deiconify()  # Show the main window
        self.root.state('normal')  # Restore the window to normal state

    def exit_application(self, icon=None, item=None):
        """Exit the application by stopping services and removing the tray icon."""
        self.service_manager.stop_all_services()  # Stop all services
        if self.tray_icon:
            self.tray_icon.stop()  # Remove the tray icon
        self.root.quit()  # Close the Tkinter window
        os._exit(0)  # Forcefully exit the Python process without raising SystemExit
