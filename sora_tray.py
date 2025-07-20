import os
import sys
import time
from threading import Thread
import hid
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction
from PyQt6.QtCore import QPoint, QObject, pyqtSignal

MODEL = "Ninjutso Sora V2"
VID = 0x1915
PID= 0xAE1C
USAGE_PAGE = 0xFFA0

# Settings
POLL_RATE = 300
BATTERY_MEDIUM = 30
BATTERY_LOW = 20

def get_device_path(device_list, usage_page):
    for device in device_list:
        if device['usage_page'] == usage_page:
            return device['path']
    return None

def get_device_list():
    device_list = hid.enumerate(VID, PID)
    if not device_list:
        return None
    return device_list

def get_battery():
    try:
        device_list = get_device_list()
        if not device_list:
            return None
        path = get_device_path(device_list, USAGE_PAGE)
        if not path:
            return None
        
        device = hid.Device(path=path)

        report = [0] * 32
        report[0] = 5
        report[1] = 21
        report[4] = 1
        device.send_feature_report(bytes(report))
        time.sleep(0.09)
        res = device.get_feature_report(5, 32)
        device.close()

        battery = res[9]
        online = res[12]

        return battery, online

    except Exception as e:
        print(f"Error accessing device: {e}")
        return None
    
def resource_path(relative_path):
    # Get path to resource inside PyInstaller bundle
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

# Icon rendering
def create_icon(base_icon_path: str, overlay_colour: str) -> QIcon:
    base_pixmap = QPixmap(base_icon_path)

    size = base_pixmap.size()
    painter = QPainter(base_pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(overlay_colour))
    painter.setPen(QColor(overlay_colour))

    # Draw circle in bottom-left corner
    radius = min(size.width(), size.height()) // 6
    center = QPoint(radius, size.height() - radius)
    painter.drawEllipse(center, radius, radius)

    painter.end()
    return QIcon(base_pixmap)

# Signal bridge
class BatterySignal(QObject):
    updated = pyqtSignal(int, str)

# Main tray class
class BatteryTrayApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.signal = BatterySignal()
        self.signal.updated.connect(self.update_icon)

        # Initial icon
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon(resource_path("res/ninjutso_dfdfdf.ico")))
        self.tray.setToolTip("Ninjutso Sora V2: Initialising")

        # Menu
        self.menu = QMenu()
        self.exit_action = QAction("Exit")
        self.exit_action.triggered.connect(self.exit)
        self.menu.addAction(self.exit_action)
        self.tray.setContextMenu(self.menu)
        self.tray.setVisible(True)

        # Start polling thread
        self.running = True
        self.thread = Thread(target=self.poll_loop, daemon=True)
        self.thread.start()

    def update_icon(self, battery, tooltip):
        if battery > BATTERY_MEDIUM:
            colour = "#000000"
        elif battery > BATTERY_LOW:
            colour = "#ffff00"
        elif battery == -1:
            colour = "#006eff"
        else:
            colour = "#ff0000"
        icon = create_icon(resource_path("res/ninjutso_dfdfdf.ico"), colour)
        self.tray.setIcon(icon)
        self.tray.setToolTip(tooltip)

    def poll_loop(self):
        while self.running:
            result = get_battery()
            if result is None:
                self.signal.updated.emit(-404, "Ninjutso Sora V2: No Mouse Detected")
            else:
                battery, online = result
                if online:
                    self.signal.updated.emit(battery, f"Ninjutso Sora V2: {battery} %")
                else:
                    battery = -1
                    self.signal.updated.emit(battery, f"Ninjutso Sora V2: Charging")
                
            time.sleep(POLL_RATE)

    def exit(self):
        self.running = False
        self.tray.hide()
        QApplication.quit()

    def run(self):
        sys.exit(self.app.exec())

# Entry point
if __name__ == "__main__":
    app = BatteryTrayApp()
    app.run()