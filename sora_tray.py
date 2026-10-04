import os
import sys
import socket
import struct
import hid
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction
from PyQt6.QtCore import QPoint, QTimer, QSocketNotifier, QProcess

MODEL = "Ninjutso Sora V2"
VID = 0x1915
PID_WIRELESS = 0xAE1C  # 2.4 GHz receiver
PID_WIRED = 0xAE11     # Mouse connected by cable (charging)
USAGE_PAGE = 0xFFA0

# Settings
POLL_RATE = 60           # Seconds between battery queries
BATTERY_MEDIUM = 25
BATTERY_LOW = 10
HOTPLUG_SETTLE = 500     # ms to wait after a USB event before querying the mouse
RELINK_RECHECK = 5000    # ms until a second query, the mouse needs a moment to reconnect to the receiver

# Overlay colours
COLOUR_MEDIUM = "#ffff00"
COLOUR_LOW = "#ff0000"
COLOUR_CHARGING = "#006eff"
COLOUR_FULL = "#00c000"
COLOUR_OFFLINE = "#808080"

# Device states
NO_RECEIVER = "no_receiver"
OFFLINE = "offline"
CHARGING = "charging"
FULL = "full"
WIRELESS = "wireless"

# "hidapi" also installs a module named hid, but it cannot find the mouse
if not hasattr(hid, "Device"):
    sys.exit('Wrong hid module: uninstall "hidapi" and install "hid" instead.')

def get_device_path(pid):
    for device in hid.enumerate(VID, pid):
        if device['usage_page'] == USAGE_PAGE:
            return device['path']
    return None

def send_battery_request(path):
    device = hid.Device(path=path)
    report = [0] * 32
    report[0] = 5
    report[1] = 21
    report[4] = 1
    device.send_feature_report(bytes(report))
    return device

def read_battery_response(device):
    try:
        res = device.get_feature_report(5, 32)
    finally:
        device.close()
    # battery, charging, full_charge, online
    return res[9], res[10], res[11], res[12]

def resource_path(relative_path):
    # Get path to resource inside PyInstaller bundle
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

# Icon rendering
def create_icon(base_pixmap: QPixmap, overlay_colour: str) -> QIcon:
    pixmap = QPixmap(base_pixmap)

    if overlay_colour:
        size = pixmap.size()
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(overlay_colour))
        painter.setPen(QColor(overlay_colour))
        # Draw circle in bottom-left corner
        radius = min(size.width(), size.height()) // 6
        center = QPoint(radius, size.height() - radius)
        painter.drawEllipse(center, radius, radius)
        painter.end()

    return QIcon(pixmap)

# Listens for udev events, the socket is only woken up by the kernel when a device is added or removed
class HotplugMonitor:
    NETLINK_KOBJECT_UEVENT = 15
    UDEV_GROUP = 2
    HID_IDS = (f":{VID:04X}:{PID_WIRELESS:04X}.".encode(), f":{VID:04X}:{PID_WIRED:04X}.".encode())

    def __init__(self, callback):
        self.callback = callback
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW | socket.SOCK_NONBLOCK | socket.SOCK_CLOEXEC,
                                  self.NETLINK_KOBJECT_UEVENT)
        self.sock.bind((0, self.UDEV_GROUP))
        self.notifier = QSocketNotifier(self.sock.fileno(), QSocketNotifier.Type.Read)
        self.notifier.activated.connect(self.on_readable)

    def on_readable(self):
        relevant = False
        while True:
            try:
                data = self.sock.recv(16384)
            except BlockingIOError:
                break
            if self.is_relevant(data):
                relevant = True
        if relevant:
            self.callback()

    def is_relevant(self, data):
        # libudev message: "libudev\0", magic, header size, properties offset, properties length, ...
        if not data.startswith(b"libudev\0") or len(data) < 24:
            return False
        properties_off, properties_len = struct.unpack_from("=II", data, 16)
        properties = dict(entry.split(b"=", 1)
                          for entry in data[properties_off:properties_off + properties_len].split(b"\0")
                          if b"=" in entry)
        if properties.get(b"SUBSYSTEM") != b"hidraw":
            return False
        devpath = properties.get(b"DEVPATH", b"")
        return any(hid_id in devpath for hid_id in self.HID_IDS)

# Main tray class
class BatteryTrayApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.base_pixmap = QPixmap(resource_path("res/ninjutso_dfdfdf.ico"))
        self.icons = {}
        self.battery_notifystate = "charged"
        self.pending_device = None

        # Initial icon
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(self.get_icon(""))
        self.tray.setToolTip(f"{MODEL}: Initialising")

        # Menu
        self.menu = QMenu()
        self.exit_action = QAction("Exit")
        self.exit_action.triggered.connect(self.exit)
        self.menu.addAction(self.exit_action)
        self.tray.setContextMenu(self.menu)
        self.tray.setVisible(True)

        # Regular polling, battery level changes do not create events
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self.query)
        self.poll_timer.start(POLL_RATE * 1000)

        # Instant updates when the cable or receiver gets plugged in or out
        self.hotplug_timer = QTimer()
        self.hotplug_timer.setSingleShot(True)
        self.hotplug_timer.timeout.connect(self.query)
        self.relink_timer = QTimer()
        self.relink_timer.setSingleShot(True)
        self.relink_timer.timeout.connect(self.query)
        try:
            self.hotplug = HotplugMonitor(self.on_hotplug)
        except OSError as e:
            print(f"Hotplug events unavailable, falling back to polling only: {e}")

        QTimer.singleShot(0, self.query)

    def get_icon(self, colour):
        if colour not in self.icons:
            self.icons[colour] = create_icon(self.base_pixmap, colour)
        return self.icons[colour]

    def on_hotplug(self):
        # Several hidraw nodes appear at once, query only after they have settled
        self.hotplug_timer.start(HOTPLUG_SETTLE)
        self.relink_timer.start(RELINK_RECHECK)

    def query(self):
        if self.pending_device:
            return
        # Prefer the cable, while charging the receiver only reports the mouse as offline
        for pid in (PID_WIRED, PID_WIRELESS):
            try:
                path = get_device_path(pid)
                if not path:
                    continue
                self.pending_device = (pid, send_battery_request(path))
                # Give the mouse time to answer without blocking the event loop
                QTimer.singleShot(90, self.read_response)
                return
            except Exception as e:
                print(f"Error accessing device: {e}")
        self.update_state(NO_RECEIVER, 0)

    def read_response(self):
        pid, device = self.pending_device
        self.pending_device = None
        try:
            battery, charging, full_charge, online = read_battery_response(device)
        except Exception as e:
            print(f"Error accessing device: {e}")
            self.update_state(NO_RECEIVER, 0)
            return

        if pid == PID_WIRED or charging or full_charge:
            self.update_state(FULL if full_charge else CHARGING, battery)
        elif online:
            self.update_state(WIRELESS, battery)
        else:
            self.update_state(OFFLINE, 0)

    def update_state(self, state, battery):
        if state == NO_RECEIVER:
            colour = COLOUR_OFFLINE
            tooltip = f"{MODEL}: No Receiver Detected"
        elif state == OFFLINE:
            colour = COLOUR_OFFLINE
            tooltip = f"{MODEL}: Offline"
        elif state == CHARGING:
            colour = COLOUR_CHARGING
            tooltip = f"{MODEL}: Charging {battery} %" if battery else f"{MODEL}: Charging"
            self.battery_notifystate = "charging"
        elif state == FULL:
            colour = COLOUR_FULL
            tooltip = f"{MODEL}: Fully Charged"
            if self.battery_notifystate != "full":
                self.battery_notifystate = "full"
                self.notify(f"{MODEL} is fully charged.")
        else:
            tooltip = f"{MODEL}: {battery} %"
            if battery > BATTERY_MEDIUM:
                colour = ""
                self.battery_notifystate = "charged"
            elif battery > BATTERY_LOW:
                colour = COLOUR_MEDIUM
                if self.battery_notifystate != "medium":
                    self.battery_notifystate = "medium"
                    self.notify(f"{MODEL} battery charge is low at {battery} %, consider charging.")
            else:
                colour = COLOUR_LOW
                if self.battery_notifystate != "low":
                    self.battery_notifystate = "low"
                    self.notify(f"{MODEL} battery charge is CRITICAL at {battery} %, consider charging.")

        self.tray.setIcon(self.get_icon(colour))
        self.tray.setToolTip(tooltip)

    def notify(self, message):
        QProcess.startDetached("notify-send", ["-a", MODEL, "-i", "input-mouse", "", message])

    def exit(self):
        self.tray.hide()
        QApplication.quit()

    def run(self):
        sys.exit(self.app.exec())

# Entry point
if __name__ == "__main__":
    app = BatteryTrayApp()
    app.run()
