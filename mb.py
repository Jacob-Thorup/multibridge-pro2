#!/usr/bin/env python3
"""Native control for a Blackmagic Multibridge Pro 2 on Apple Silicon.

The installed Multibridge kernel extension cannot load on this Mac. This
program repeats the two USB steps that extension performs: it copies the
Cypress FX2 image out of that kext into the box, then reads and writes the
converter registers. It does not rewrite the USB identity EEPROM.
"""

import ctypes
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VENDOR = 0x04B4
PRODUCT_PRO2 = 0xBD13
BCD_BOOT = 0x0001
BCD_RUN = 0x0100

KEXT = Path(
    "/Library/Extensions/Blackmagic_Multibridge_Driver.kext"
    "/Contents/MacOS/Blackmagic_Multibridge_Driver"
)
# Virtual address of MultibridgeUSBFirmwarePro2, length from the no-firmware start().
FIRMWARE_VA = 0xF5C0
FIRMWARE_LEN = 0x1525
CONST_VA = 0x9430
CONST_FILE_OFF = 37936

LIBUSB_PATHS = (
    "/opt/homebrew/lib/libusb-1.0.dylib",
    "/opt/homebrew/opt/libusb/lib/libusb-1.0.dylib",
    "/usr/local/lib/libusb-1.0.dylib",
)

HOST = "127.0.0.1"
PORT = 8765

ANALOG_INPUTS = (
    ("component", "Use Analog Component Inputs"),
    ("composite", "Use Analog NTSC/PAL Input"),
    ("s-video", "Use S-Video Input"),
)
ANALOG_INPUT_BITS = {"component": 0x00, "composite": 0x02, "s-video": 0x06}

# Analog-to-SDI mode shares register 0 bits 2:0 with the analog jack.
# Setter: 2 clears the bits, 8 writes 3, 1 writes 1.
SDI_CONVERSION = (
    (2, "Analog In to SDI 4:2:2 Out"),
    (8, "Analog In to SDI 4:4:4 RGB Out"),
    (1, "Analog In to SDI 4:4:4 YUV Out"),
)
SDI_CONVERSION_BITS = {2: 0, 8: 3, 1: 1}
SDI_CONVERSION_FROM_BITS = {0: 2, 1: 1, 2: 2, 3: 8, 4: 1, 5: 1, 6: 2}

# Register 0 bit 3. Set means code 0, clear means code 3.
SDI_FORMAT = ((3, "4:2:2"), (0, "4:4:4"))

# Register 1 bit 0. Clear is code 1, set is code 4.
ANALOG_OUTPUT = (
    (1, "SDI In to YUV Analog Out"),
    (4, "SDI In to RGB Analog Out"),
)

SDI_CHANNELS = (
    (0, "SDI Channel 1 & 2"),
    (1, "SDI Channel 3 & 4"),
    (2, "SDI Channel 5 & 6"),
    (3, "SDI Channel 7 & 8"),
    (4, "SDI Channel 9 & 10"),
    (5, "SDI Channel 11 & 12"),
    (6, "SDI Channel 13 & 14"),
    (7, "SDI Channel 15 & 16"),
    (255, "Don't Embed"),
)
# Model 6 rejects these embed destinations and also reports them as Don't Embed.
EMBED_BLOCKED = {1, 6, 7}

# Hardware embed index 0..5. The driver fills analog pairs, then AES pairs.
EMBED_PORTS = (
    (0, "Analog Audio In 1 & 2"),
    (1, "Analog Audio In 3 & 4"),
    (2, "AES/EBU In 1 & 2"),
    (3, "AES/EBU In 3 & 4"),
    (4, "AES/EBU In 5 & 6"),
    (5, "AES/EBU In 7 & 8"),
)
# De-embed index 2 is invalid. Shift is the nibble position in the register.
DEEMBED_PORTS = (
    (0, "Analog Audio Out 1 & 2", 8, 0),
    (1, "Analog Audio Out 3 & 4", 8, 4),
    (3, "AES/EBU Out 1 & 2", 9, 0),
    (4, "AES/EBU Out 3 & 4", 9, 4),
    (5, "AES/EBU Out 5 & 6", 10, 4),
    (6, "AES/EBU Out 7 & 8", 10, 0),
    (7, "Monitoring 1 & 2", 11, 4),
    (8, "Audio output 8", 11, 0),
)

# Pro 2 is driver model 6. The utility's model table selects DataEclipse.bin
# and the raw programming path for models 5, 6, and 7. Expected FPGA version is 0x1E.
CONVERTER_IMAGE = Path(
    "/Applications/Blackmagic Multibridge Utility.app/Contents/Resources/DataEclipse.bin"
)
EP_OUT = 0x02
FPGA_VERSION_REG = 0x43
FPGA_VERSION = 0x1E


def load_libusb():
    for path in LIBUSB_PATHS:
        if Path(path).exists():
            return ctypes.CDLL(path)
    raise SystemExit(
        "libusb was not found. Install Homebrew from https://brew.sh, then run:\n"
        "  brew install libusb"
    )


def require_blackmagic_install():
    """The USB and converter images stay in the official install. They are not bundled."""
    if not KEXT.is_file():
        raise SystemExit(
            "Blackmagic Multibridge Utility is not installed, so there is no USB startup image to send.\n"
            f"Expected the driver at:\n  {KEXT}\n"
            "Install that utility, leave its files where the installer put them, and run this again.\n"
            "This download does not include Blackmagic firmware."
        )
    if not CONVERTER_IMAGE.is_file():
        print(
            "The converter image was not found, so a first-time load will not be possible.\n"
            f"Expected:\n  {CONVERTER_IMAGE}\n"
            "Settings still work when the converter is already running.",
            file=sys.stderr,
        )


lib = load_libusb()


class DeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bcdUSB", ctypes.c_uint16),
        ("bDeviceClass", ctypes.c_uint8),
        ("bDeviceSubClass", ctypes.c_uint8),
        ("bDeviceProtocol", ctypes.c_uint8),
        ("bMaxPacketSize0", ctypes.c_uint8),
        ("idVendor", ctypes.c_uint16),
        ("idProduct", ctypes.c_uint16),
        ("bcdDevice", ctypes.c_uint16),
        ("iManufacturer", ctypes.c_uint8),
        ("iProduct", ctypes.c_uint8),
        ("iSerialNumber", ctypes.c_uint8),
        ("bNumConfigurations", ctypes.c_uint8),
    ]


lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
lib.libusb_init.restype = ctypes.c_int
lib.libusb_exit.argtypes = [ctypes.c_void_p]
lib.libusb_get_device_list.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
]
lib.libusb_get_device_list.restype = ctypes.c_ssize_t
lib.libusb_free_device_list.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
lib.libusb_get_device_descriptor.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(DeviceDescriptor),
]
lib.libusb_get_device_descriptor.restype = ctypes.c_int
lib.libusb_open.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
lib.libusb_open.restype = ctypes.c_int
lib.libusb_close.argtypes = [ctypes.c_void_p]
lib.libusb_control_transfer.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint8,
    ctypes.c_uint8,
    ctypes.c_uint16,
    ctypes.c_uint16,
    ctypes.c_void_p,
    ctypes.c_uint16,
    ctypes.c_uint,
]
lib.libusb_control_transfer.restype = ctypes.c_int
lib.libusb_error_name.argtypes = [ctypes.c_int]
lib.libusb_error_name.restype = ctypes.c_char_p
lib.libusb_set_auto_detach_kernel_driver.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.libusb_set_auto_detach_kernel_driver.restype = ctypes.c_int
lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.libusb_claim_interface.restype = ctypes.c_int
lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.libusb_release_interface.restype = ctypes.c_int
lib.libusb_bulk_transfer.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint8,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_uint,
]
lib.libusb_bulk_transfer.restype = ctypes.c_int
lib.libusb_clear_halt.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
lib.libusb_clear_halt.restype = ctypes.c_int


def usb_error(code):
    name = lib.libusb_error_name(code)
    text = name.decode() if name else str(code)
    return f"{text} ({code})"


class UsbError(RuntimeError):
    pass


def firmware_image():
    data = KEXT.read_bytes()
    off = CONST_FILE_OFF + (FIRMWARE_VA - CONST_VA)
    blob = data[off : off + FIRMWARE_LEN]
    if len(blob) != FIRMWARE_LEN:
        raise UsbError(f"firmware image in {KEXT} is short")
    records = []
    pos = 0
    while pos + 4 <= len(blob):
        length = blob[pos]
        if pos + length + 5 > len(blob):
            raise UsbError("firmware record runs past the image")
        record = blob[pos : pos + length + 5]
        if sum(record) & 0xFF:
            raise UsbError(f"firmware checksum failed at {pos:#x}")
        addr = (record[1] << 8) | record[2]
        flag = record[3]
        if flag:
            return records
        records.append((addr, bytes(record[4 : 4 + length])))
        pos += length + 5
    raise UsbError("firmware image has no terminator")


def analog_input_name(reg0):
    bits = reg0 & 0x07
    if bits == 0x06:
        return "s-video"
    if bits == 0x02:
        return "composite"
    return "component"


def choices(pairs, current=None):
    options = [{"value": str(value), "label": label} for value, label in pairs]
    if current is not None and not any(item["value"] == str(current) for item in options):
        options.append({"value": str(current), "label": f"Value {current}"})
    return options


def select_control(control_id, label, value, pairs):
    return {
        "id": control_id,
        "kind": "select",
        "label": label,
        "value": str(value),
        "options": choices(pairs, value),
    }


def check_control(control_id, label, value):
    return {"id": control_id, "kind": "check", "label": label, "value": bool(value)}


def embed_code(nibble):
    if nibble > 8:
        return 255
    if nibble == 8:
        return 16
    if nibble in EMBED_BLOCKED:
        return 255
    return nibble


def deembed_code(nibble):
    if nibble == 8:
        return 255
    if nibble > 8:
        return 0
    return nibble


def as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "on", "yes"):
        return True
    if text in ("0", "false", "off", "no"):
        return False
    raise UsbError("expected a yes or no value")


class Bus:
    def __init__(self):
        self.ctx = ctypes.c_void_p()
        rc = lib.libusb_init(ctypes.byref(self.ctx))
        if rc:
            raise UsbError(f"libusb_init: {usb_error(rc)}")

    def close(self):
        if self.ctx:
            lib.libusb_exit(self.ctx)
            self.ctx = None

    def find(self, bcd):
        devices = ctypes.POINTER(ctypes.c_void_p)()
        count = lib.libusb_get_device_list(self.ctx, ctypes.byref(devices))
        if count < 0:
            raise UsbError(f"device list: {usb_error(count)}")
        found = None
        try:
            for index in range(count):
                dev = devices[index]
                desc = DeviceDescriptor()
                if lib.libusb_get_device_descriptor(dev, ctypes.byref(desc)):
                    continue
                if desc.idVendor == VENDOR and desc.idProduct == PRODUCT_PRO2 and desc.bcdDevice == bcd:
                    found = desc.bcdDevice
                    handle = ctypes.c_void_p()
                    rc = lib.libusb_open(dev, ctypes.byref(handle))
                    if rc:
                        raise UsbError(f"open {desc.bcdDevice:#06x}: {usb_error(rc)}")
                    return handle
        finally:
            lib.libusb_free_device_list(devices, 1)
        return None

    def control(self, handle, bm, request, value, index, data=None, timeout=2000):
        if data is None:
            buf = None
            length = 0
        elif bm & 0x80:
            buf = ctypes.create_string_buffer(data)
            length = data
        else:
            buf = ctypes.create_string_buffer(data)
            length = len(data)
        rc = lib.libusb_control_transfer(
            handle, bm, request, value, index, buf, length, timeout
        )
        if rc < 0:
            raise UsbError(usb_error(rc))
        if bm & 0x80:
            return buf.raw[:rc]
        return rc


class Multibridge:
    def __init__(self):
        self.lock = threading.Lock()
        self.bus = Bus()
        self.handle = None
        self.records = firmware_image()

    def close_handle(self):
        if self.handle:
            lib.libusb_release_interface(self.handle, 0)
            lib.libusb_close(self.handle)
            self.handle = None

    def claim(self, handle):
        lib.libusb_set_auto_detach_kernel_driver(handle, 1)
        rc = lib.libusb_claim_interface(handle, 0)
        if rc and rc != -5:
            raise UsbError(f"claim interface: {usb_error(rc)}")

    def fx2(self, handle, address, payload):
        self.bus.control(handle, 0x40, 0xA0, address, 0, payload, timeout=1000)

    def load_firmware(self):
        handle = self.bus.find(BCD_BOOT)
        if handle is None:
            return False
        try:
            self.claim(handle)
            self.fx2(handle, 0xE600, b"\x01")
            for address, payload in self.records:
                self.fx2(handle, address, payload)
            try:
                self.fx2(handle, 0xE600, b"\x00")
            except UsbError:
                # The chip resets as soon as the CPU is released.
                pass
        finally:
            lib.libusb_close(handle)
        deadline = time.time() + 8
        while time.time() < deadline:
            time.sleep(0.25)
            try:
                handle = self.bus.find(BCD_RUN)
            except UsbError:
                handle = None
            if handle:
                lib.libusb_close(handle)
                return True
        raise UsbError("firmware was sent, but the Pro 2 did not reappear as device version 0100")

    def open_running(self):
        if self.handle:
            return
        handle = self.bus.find(BCD_RUN)
        if handle is None:
            if self.bus.find(BCD_BOOT) is None and self._any_pro2() is None:
                raise UsbError(
                    "No Multibridge Pro 2 is on USB. Power the unit, connect it with a USB 2.0 cable, and leave the PCI Express cable unplugged."
                )
            self.load_firmware()
            handle = self.bus.find(BCD_RUN)
        if handle is None:
            raise UsbError("Pro 2 is on USB but not in the running personality")
        self.claim(handle)
        self.handle = handle

    def _any_pro2(self):
        devices = ctypes.POINTER(ctypes.c_void_p)()
        count = lib.libusb_get_device_list(self.bus.ctx, ctypes.byref(devices))
        if count < 0:
            return None
        try:
            for index in range(count):
                desc = DeviceDescriptor()
                if lib.libusb_get_device_descriptor(devices[index], ctypes.byref(desc)):
                    continue
                if desc.idVendor == VENDOR and desc.idProduct == PRODUCT_PRO2:
                    return desc.bcdDevice
        finally:
            lib.libusb_free_device_list(devices, 1)
        return None

    def fpga_running(self):
        data = self.bus.control(self.handle, 0xC0, 0xD5, 0, 0, 1, timeout=1000)
        return bool(data and data[0])

    def wait_fpga(self, seconds=2):
        deadline = time.time() + seconds
        while True:
            try:
                if self.fpga_running():
                    return True
            except UsbError:
                self.close_handle()
                self.open_running()
            if time.time() >= deadline:
                return False
            time.sleep(0.2)

    def read_reg(self, reg):
        data = self.bus.control(self.handle, 0xC0, 0xD6, 0, (reg & 0xFF) << 8, 1)
        if not data:
            raise UsbError(f"empty read from register {reg:#x}")
        return data[0]

    def write_reg(self, reg, value):
        self.bus.control(
            self.handle, 0x40, 0xD7, 0, ((reg & 0xFF) << 8) | (value & 0xFF), b"", timeout=2000
        )
        time.sleep(0.005)

    def bulk_out(self, payload, timeout=5000):
        buf = ctypes.create_string_buffer(payload)
        sent = ctypes.c_int()
        rc = lib.libusb_bulk_transfer(self.handle, EP_OUT, buf, len(payload), ctypes.byref(sent), timeout)
        if rc < 0:
            raise UsbError(usb_error(rc))
        if sent.value != len(payload):
            raise UsbError(f"bulk write sent {sent.value} of {len(payload)} bytes")

    def ready(self, tries, pause):
        for _ in range(tries):
            time.sleep(pause)
            try:
                data = self.bus.control(self.handle, 0xC0, 0xDF, 0, 0, 1, timeout=1000)
            except UsbError:
                data = b"\x00"
            if data and data[0]:
                return True
        return False

    def load_converter(self):
        image = CONVERTER_IMAGE.read_bytes()
        if len(image) != 1171392:
            raise UsbError(f"{CONVERTER_IMAGE.name} is {len(image)} bytes, expected 1171392")
        with self.lock:
            self.open_running()
            # flags bit 0 selects wIndex 0xBD. Bit 1 would rewrite secondary EEPROMs and stays clear.
            self.bus.control(self.handle, 0x40, 0xDB, 0, 0xBD, b"", timeout=1000)
            for sector in range(0x12):
                command = bytes((sector, 0, 0))
                for _attempt in range(3):
                    self.bus.control(self.handle, 0x40, 0xE0, 0, 0, command, timeout=1000)
                    if self.ready(0x3B, 0.1):
                        break
                else:
                    raise UsbError(f"converter did not accept erase of sector {sector:#x}")
                print(f"erased sector {sector + 1}/18", file=sys.stderr, flush=True)
            self.bus.control(self.handle, 0x40, 0xD2, 0, 0, b"\x01\x00", timeout=1000)
            offset = 0
            packets = 0
            while offset < len(image):
                chunk = image[offset : offset + 0x100]
                packet = bytes(((offset >> 16) & 0xFF, (offset >> 8) & 0xFF, offset & 0xFF)) + chunk
                try:
                    self.bulk_out(packet)
                except UsbError:
                    lib.libusb_clear_halt(self.handle, EP_OUT)
                    self.bulk_out(packet)
                if not self.ready(0x13, 0.001):
                    raise UsbError(f"converter did not acknowledge data at {offset:#x}")
                offset += len(chunk)
                packets += 1
                if packets % 256 == 0 or offset == len(image):
                    print(f"wrote {offset}/{len(image)}", file=sys.stderr, flush=True)
            self.bus.control(self.handle, 0x40, 0xDC, 0, 0xBD, b"", timeout=1000)
            fpga = self.wait_fpga(seconds=20)
            version = self.read_reg(FPGA_VERSION_REG) if fpga else None
        return {
            "ok": bool(fpga and version == FPGA_VERSION),
            "file": CONVERTER_IMAGE.name,
            "bytes": len(image),
            "fpga": fpga,
            "version": version,
            "expectedVersion": FPGA_VERSION,
        }

    def ensure(self):
        self.open_running()
        if not self.wait_fpga():
            raise UsbError("USB firmware is running, but the converter FPGA has not come up")

    def snapshot(self):
        with self.lock:
            try:
                self.open_running()
                fpga = self.wait_fpga()
                registers = [self.read_reg(index) for index in range(0x30)] if fpga else []
            except UsbError as exc:
                self.close_handle()
                return {"ok": False, "error": str(exc)}
        if not fpga:
            return {
                "ok": True,
                "product": "Multibridge Pro 2",
                "bcdDevice": f"{BCD_RUN:04x}",
                "fpga": False,
                "analogInput": None,
                "componentSmpte": None,
                "registers": [],
                "note": "USB is connected and the interface firmware is running. The converter FPGA is not, so it is not exposing settings. The official utility stops at this same point and asks to load converter firmware.",
            }
        reg0 = registers[0]
        reg1 = registers[1]
        embed = []
        for index, label in EMBED_PORTS:
            nibble = (registers[4 + (index >> 1)] >> (4 if index & 1 else 0)) & 0x0F
            embed.append(select_control(f"embed-{index}", label, embed_code(nibble), SDI_CHANNELS))
        deembed = []
        for index, label, reg, shift in DEEMBED_PORTS:
            nibble = (registers[reg] >> shift) & 0x0F
            deembed.append(select_control(f"deembed-{index}", label, deembed_code(nibble), SDI_CHANNELS))
        return {
            "ok": True,
            "product": "Multibridge Pro 2",
            "bcdDevice": f"{BCD_RUN:04x}",
            "fpga": True,
            "analogInput": analog_input_name(reg0),
            "componentSmpte": (reg0 & 0x10) == 0,
            "registers": registers,
            "persisted": False,
            "note": "Changes apply immediately. Save power-up settings to keep them after the converter is turned off.",
            "sections": [
                {
                    "title": "Analog to digital",
                    "controls": [
                        select_control(
                            "conversion",
                            "Analog to digital conversion",
                            SDI_CONVERSION_FROM_BITS.get(reg0 & 0x07, 2),
                            SDI_CONVERSION,
                        ),
                        select_control("analog-input", "Analog video input", analog_input_name(reg0), ANALOG_INPUTS),
                        select_control("sdi-format", "SDI input format", 0 if reg0 & 0x08 else 3, SDI_FORMAT),
                        *embed,
                    ],
                },
                {
                    "title": "Digital to analog",
                    "controls": [
                        select_control("analog-output", "Digital to analog conversion", 4 if reg1 & 0x01 else 1, ANALOG_OUTPUT),
                        *deembed,
                    ],
                },
                {
                    "title": "Levels",
                    "controls": [
                        check_control("smpte", "Component uses SMPTE levels", (reg0 & 0x10) == 0),
                        check_control("setup75", "Composite uses 7.5 IRE setup", (reg0 & 0x20) != 0),
                        check_control("agc", "Analog video input AGC", (registers[0x2A] & 0x01) == 0),
                    ],
                },
            ],
        }

    def merge_bits(self, reg, mask, bits):
        current = self.read_reg(reg)
        updated = (current & ~mask) | (bits & mask)
        if updated != current:
            self.write_reg(reg, updated)

    def apply_setting(self, control_id, value):
        with self.lock:
            self.ensure()
            if control_id == "analog-input":
                if value not in ANALOG_INPUT_BITS:
                    raise UsbError("unknown analog input")
                self.merge_bits(0, 0x07, ANALOG_INPUT_BITS[value])
                return
            if control_id == "conversion":
                code = int(value)
                if code not in SDI_CONVERSION_BITS:
                    raise UsbError("unknown analog to digital conversion")
                self.merge_bits(0, 0x07, SDI_CONVERSION_BITS[code])
                return
            if control_id == "sdi-format":
                code = int(value)
                if code not in (0, 3):
                    raise UsbError("unknown SDI input format")
                self.merge_bits(0, 0x08, 0x08 if code == 0 else 0)
                return
            if control_id == "analog-output":
                code = int(value)
                if code not in (1, 4):
                    raise UsbError("unknown digital to analog conversion")
                self.merge_bits(1, 0x01, 0x01 if code == 4 else 0)
                return
            if control_id == "smpte":
                enabled = as_bool(value)
                self.merge_bits(0, 0x10, 0 if enabled else 0x10)
                self.merge_bits(1, 0x02, 0 if enabled else 0x02)
                return
            if control_id == "setup75":
                enabled = as_bool(value)
                self.merge_bits(0, 0x20, 0x20 if enabled else 0)
                self.merge_bits(1, 0x04, 0x04 if enabled else 0)
                return
            if control_id == "agc":
                enabled = as_bool(value)
                self.merge_bits(0x2A, 0x01, 0 if enabled else 0x01)
                return
            if control_id.startswith("embed-"):
                index = int(control_id.split("-", 1)[1])
                if index not in {port for port, _label in EMBED_PORTS}:
                    raise UsbError("unknown audio input")
                code = int(value)
                if code in EMBED_BLOCKED:
                    raise UsbError("This Pro 2 will not embed onto that SDI pair")
                if code == 255:
                    nibble = 9
                elif code == 16:
                    nibble = 8
                elif 0 <= code <= 7:
                    nibble = code
                else:
                    raise UsbError("unknown SDI channel")
                self.merge_bits(4 + (index >> 1), 0x0F << (4 if index & 1 else 0), nibble << (4 if index & 1 else 0))
                return
            if control_id.startswith("deembed-"):
                index = int(control_id.split("-", 1)[1])
                port = next((item for item in DEEMBED_PORTS if item[0] == index), None)
                if port is None:
                    raise UsbError("unknown audio output")
                code = int(value)
                if code == 255:
                    nibble = 8
                elif 0 <= code <= 7:
                    nibble = code
                else:
                    raise UsbError("unknown SDI channel")
                _index, _label, reg, shift = port
                self.merge_bits(reg, 0x0F << shift, nibble << shift)
                return
            raise UsbError("unknown setting")

    def erase_config_sector(self, sector):
        self.bus.control(self.handle, 0x40, 0xE0, 0, 0, bytes((sector, 0, 0)), timeout=1000)
        if not self.ready(0x1D, 0.1):
            raise UsbError("the converter did not finish preparing power-up memory")

    def save_configuration(self):
        """Write the live registers to the configuration EEPROM, not the USB identity."""
        with self.lock:
            self.ensure()
            bank = self.read_reg(0x44)
            if (bank & 0x80) == 0:
                raise UsbError("the converter is not ready to store power-up settings")
            write_sector, retire_sector = (0x1E, 0x1F) if bank & 0x01 else (0x1F, 0x1E)
            self.erase_config_sector(write_sector)
            registers = bytes(self.read_reg(index) for index in range(0x50))
            image = bytes((write_sector, 0, 0)) + registers + b"\xAA"
            self.bus.control(self.handle, 0x40, 0xD2, 0, 0, b"\x01\x00", timeout=1000)
            try:
                self.bulk_out(image)
            except UsbError:
                lib.libusb_clear_halt(self.handle, EP_OUT)
                self.bulk_out(image)
            if not self.ready(0x13, 0.001):
                raise UsbError("the converter did not accept the power-up settings")
            self.erase_config_sector(retire_sector)
            return {"bank": bank, "stored": self.read_reg(0x44)}


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Multibridge Pro 2</title>
<style>
  :root { color-scheme: light; --ink: #1c1915; --line: #d9d2c5; --paper: #f6f3ec; --card: #fffdf8; --accent: #8a3b12; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 16px/1.45 "Iowan Old Style", Palatino, Georgia, serif; background: var(--paper); color: var(--ink); }
  main { max-width: 980px; margin: 0 auto; padding: 32px 20px 64px; }
  h1 { font-weight: 600; font-size: 28px; margin: 0 0 4px; }
  p.lead { margin: 0 0 8px; color: #5c564c; }
  p.message { min-height: 1.4em; margin: 0 0 18px; color: #6b6458; }
  p.message.error { color: #8a1c12; }
  .cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  section { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 18px 18px 8px; margin-bottom: 16px; }
  h2 { font-size: 15px; letter-spacing: 0.04em; text-transform: uppercase; margin: 0 0 12px; color: #6b6458; }
  label { display: block; margin: 0 0 12px; font-size: 14px; color: #5c564c; }
  select, button { font: inherit; color: var(--ink); }
  button.save { background: var(--accent); color: white; border: 0; border-radius: 8px; padding: 8px 14px; cursor: pointer; margin: 0 0 18px; }
  button.save:disabled { opacity: 0.5; cursor: default; }
  select { width: 100%; margin-top: 4px; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--line); background: white; }
  select:disabled { opacity: 0.6; }
  .check { display: flex; gap: 8px; align-items: center; margin: 0 0 12px; font-size: 16px; color: var(--ink); }
  .check label { margin: 0; font-size: 16px; color: var(--ink); cursor: pointer; }
  .note { color: #6b6458; font-size: 14px; }
  details { margin-top: 8px; }
  summary { cursor: pointer; color: #6b6458; }
  table { width: 100%; border-collapse: collapse; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; margin-top: 8px; }
  td { padding: 3px 6px; border-top: 1px solid var(--line); }
  @media (max-width: 800px) { .cols { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<main>
  <h1>Multibridge Pro 2</h1>
  <p class="lead" id="lead">Looking for the converter on USB.</p>
  <p class="message" id="message"></p>
  <button class="save" id="save" type="button" disabled>Save power-up settings</button>
  <div class="cols" id="columns"></div>
  <section id="levels" hidden>
    <h2>Levels</h2>
    <div id="level-controls"></div>
    <p class="note">Per-channel gain is not on this page. These switches are the level flags from the utility.</p>
  </section>
  <details>
    <summary>Registers</summary>
    <table id="regs"></table>
  </details>
</main>
<script>
const columns = document.getElementById("columns");
const levels = document.getElementById("levels");
const levelControls = document.getElementById("level-controls");
const built = new Map();
let busy = false;

function fillSelect(select, control) {
  const wanted = control.options.map((item) => item.value).join("|");
  if (select.dataset.options !== wanted) {
    select.replaceChildren();
    for (const item of control.options) {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = item.label;
      select.appendChild(option);
    }
    select.dataset.options = wanted;
  }
  select.value = control.value;
  select.disabled = busy;
}

function bind(control, node) {
  node.dataset.id = control.id;
  if (control.kind === "select") {
    fillSelect(node, control);
    node.onchange = () => save(control.id, node.value);
  } else {
    node.checked = !!control.value;
    node.disabled = busy;
    node.onchange = () => save(control.id, node.checked);
  }
}

function ensure(control, parent) {
  let node = built.get(control.id);
  if (!node) {
    node = document.createElement(control.kind === "check" ? "input" : "select");
    if (control.kind === "check") {
      node.type = "checkbox";
      node.id = control.id;
      const row = document.createElement("div");
      row.className = "check";
      const label = document.createElement("label");
      label.htmlFor = control.id;
      label.textContent = control.label;
      row.append(node, label);
      parent.appendChild(row);
    } else {
      const label = document.createElement("label");
      label.append(document.createTextNode(control.label), node);
      parent.appendChild(label);
    }
    built.set(control.id, node);
  }
  bind(control, node);
}

function show(data) {
  const lead = document.getElementById("lead");
  const message = document.getElementById("message");
  const saveButton = document.getElementById("save");
  if (!data.ok) {
    lead.textContent = "The converter did not answer.";
    message.textContent = data.error || "";
    message.className = "message error";
    saveButton.disabled = true;
    return;
  }
  lead.textContent = data.fpga ? data.product + " is running. " + data.note : data.note;
  saveButton.disabled = busy || !data.fpga;
  if (!data.fpga) {
    columns.replaceChildren();
    levelControls.replaceChildren();
    levels.hidden = true;
    built.clear();
    document.getElementById("regs").innerHTML = "";
    return;
  }
  const video = (data.sections || []).filter((section) => section.title !== "Levels");
  const levelSection = (data.sections || []).find((section) => section.title === "Levels");
  if (built.size === 0) {
    for (const section of video) {
      const card = document.createElement("section");
      const heading = document.createElement("h2");
      heading.textContent = section.title;
      const body = document.createElement("div");
      card.append(heading, body);
      columns.appendChild(card);
      for (const control of section.controls) ensure(control, body);
    }
    if (levelSection) {
      levels.hidden = false;
      for (const control of levelSection.controls) ensure(control, levelControls);
    }
  } else {
    for (const section of data.sections || []) {
      for (const control of section.controls) {
        const node = built.get(control.id);
        if (node) bind(control, node);
      }
    }
  }
  const rows = data.registers.map((value, index) =>
    "<tr><td>" + index.toString(16).padStart(2, "0") + "</td><td>" + value.toString(16).padStart(2, "0") + "</td><td>" + value + "</td></tr>"
  );
  document.getElementById("regs").innerHTML = "<tr><td>reg</td><td>hex</td><td>dec</td></tr>" + rows.join("");
}

async function refresh() {
  const response = await fetch("/api/status");
  show(await response.json());
}

async function save(id, value) {
  if (busy) return;
  busy = true;
  const message = document.getElementById("message");
  message.textContent = "Applying…";
  message.className = "message";
  for (const node of built.values()) node.disabled = true;
  document.getElementById("save").disabled = true;
  try {
    const response = await fetch("/api/setting", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({id, value})
    });
    const data = await response.json();
    if (!data.ok) {
      message.textContent = data.error || "The converter refused that change.";
      message.className = "message error";
    } else {
      message.textContent = "Applied. Save power-up settings if this should remain after the converter is turned off.";
      message.className = "message";
    }
  } catch (error) {
    message.textContent = "The page could not reach the converter.";
    message.className = "message error";
  }
  busy = false;
  refresh();
}

document.getElementById("save").onclick = async () => {
  if (busy) return;
  busy = true;
  const message = document.getElementById("message");
  const saveButton = document.getElementById("save");
  message.textContent = "Saving power-up settings…";
  message.className = "message";
  saveButton.disabled = true;
  for (const node of built.values()) node.disabled = true;
  try {
    const response = await fetch("/api/save", {method: "POST"});
    const data = await response.json();
    if (!data.ok) {
      message.textContent = data.error || "The converter did not store the settings.";
      message.className = "message error";
    } else {
      message.textContent = "Saved. These settings come back after the converter is turned off.";
      message.className = "message";
    }
  } catch (error) {
    message.textContent = "The page could not reach the converter.";
    message.className = "message error";
  }
  busy = false;
  refresh();
};

refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    bridge = None

    def log_message(self, fmt, *args):
        return

    def send_json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/status":
            self.send_json(self.bridge.snapshot())
            return
        if self.path in ("/", "/index.html"):
            page = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            if self.path == "/api/save":
                saved = self.bridge.save_configuration()
                self.send_json({"ok": True, **saved})
                return
            if self.path != "/api/setting":
                self.send_response(404)
                self.end_headers()
                return
            body = json.loads(self.rfile.read(length) or b"{}")
            self.bridge.apply_setting(body.get("id"), body.get("value"))
            self.send_json({"ok": True})
        except (UsbError, json.JSONDecodeError, ValueError, TypeError) as exc:
            self.bridge.close_handle()
            self.send_json({"ok": False, "error": str(exc)}, 400)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    require_blackmagic_install()
    bridge = Multibridge()
    if command == "status":
        print(json.dumps(bridge.snapshot(), indent=2))
        bridge.close_handle()
        bridge.bus.close()
        return
    if command == "load-fpga":
        try:
            print(json.dumps(bridge.load_converter(), indent=2))
        finally:
            bridge.close_handle()
            bridge.bus.close()
        return
    if command != "serve":
        raise SystemExit("usage: mb.py [status|serve|load-fpga]")
    Handler.bridge = bridge
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        raise SystemExit(
            f"Could not listen on http://{HOST}:{PORT} ({exc.strerror or exc}).\n"
            "Another copy may already be running. Quit it, then start this one."
        )
    print(f"Multibridge Pro 2 control at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close_handle()
        bridge.bus.close()


if __name__ == "__main__":
    main()
