"""USB access for the Kingst LA5016: enumeration, FX2 firmware load, transport.

This is the only module that imports pyusb. Everything above it talks to a
`Transport`, so device logic can be tested against a fake.
"""

from __future__ import annotations

import ctypes.util
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterator, List, Optional, Protocol, Tuple

import usb.core
import usb.util
import usb.backend.libusb1

log = logging.getLogger(__name__)

VID = 0x77A1
PID = 0x01A2
IPRODUCT_INDEX = 2          # iProduct string index once the FX2 firmware runs
CONFIGURATION = 1
INTERFACE = 0
EP_BITSTREAM = 0x02
EP_CAPTURE = 0x86

DEFAULT_TIMEOUT_MS = 200
BULK_TIMEOUT_MS = 1000
FW_CHUNKSIZE = 4096         # ezusb.c

RENUM_GONE_DELAY_MS = 1800
RENUM_POLL_INTERVAL_MS = 200
RENUM_CHECK_PERIOD_MS = 3000

_REQ_VENDOR_IN = usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_IN | usb.util.CTRL_RECIPIENT_DEVICE
_REQ_VENDOR_OUT = usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_OUT | usb.util.CTRL_RECIPIENT_DEVICE


class UsbError(Exception):
    pass


class StreamError(UsbError):
    """The streaming data flow broke off; data received until then is valid."""


class Transport(Protocol):
    def ctrl_in(self, request: int, value: int, index: int, length: int) -> bytes: ...
    def ctrl_out(self, request: int, value: int, index: int, data: bytes = b"") -> None: ...
    def bulk_write(self, ep: int, data: bytes) -> None: ...
    def bulk_read_stream(self, ep: int, nbytes: int, chunk: int) -> Iterator[bytes]: ...
    def bulk_reader(self, ep: int, chunk: int, timeout_ms: int, depth: int = 64) -> "BulkReader": ...
    def close(self) -> None: ...


def _backend():
    def find(name):
        # The first hit may be for the wrong architecture (an Intel Homebrew
        # copy in /usr/local on Apple Silicon), so return the first that loads.
        paths = [ctypes.util.find_library(name),
                 "/opt/homebrew/lib/libusb-1.0.0.dylib", "/usr/local/lib/libusb-1.0.0.dylib"]
        if sys.platform == "win32":
            try:
                import libusb_package  # optional, ships libusb-1.0.dll
                paths.append(libusb_package.get_library_path())
            except ImportError:
                pass
        for path in paths:
            if path and (os.path.exists(path) or "/" not in path):
                try:
                    ctypes.CDLL(path)
                    return path
                except OSError:
                    continue
        return None

    be = usb.backend.libusb1.get_backend(find_library=find)
    if be is None:
        raise UsbError("libusb-1.0 not found (macOS: brew install libusb)")
    return be


@dataclass
class Candidate:
    dev: "usb.core.Device"
    bus: int
    address: int
    port_path: Tuple[int, ...]
    pid: int
    has_firmware: bool

    @property
    def conn(self) -> str:
        return "%d.%d" % (self.bus, self.address)


def find_devices() -> List[Candidate]:
    out = []
    for dev in usb.core.find(find_all=True, idVendor=VID, idProduct=PID, backend=_backend()):
        out.append(Candidate(dev, dev.bus, dev.address, tuple(dev.port_numbers or ()),
                             dev.idProduct, dev.iProduct == IPRODUCT_INDEX))
    return out


def select(conn: Optional[Tuple[int, int]] = None) -> Candidate:
    devices = find_devices()
    if conn is not None:
        devices = [d for d in devices if (d.bus, d.address) == conn]
    if not devices:
        raise UsbError("no Kingst LA5016 found" + (" at conn=%d.%d" % conn if conn else ""))
    return devices[0]


def upload_fx2_firmware(cand: Candidate, image: bytes) -> None:
    """Load firmware into FX2 RAM (vendor request 0xA0, CPUCS reset), as ezusb.c does."""
    if len(image) > 0x10000:
        raise UsbError("FX2 firmware image larger than 64 KiB")
    dev = cand.dev
    try:
        dev.set_configuration(CONFIGURATION)
        dev.ctrl_transfer(_REQ_VENDOR_OUT, 0xA0, 0xE600, 0, b"\x01", DEFAULT_TIMEOUT_MS)
        for off in range(0, len(image), FW_CHUNKSIZE):
            dev.ctrl_transfer(_REQ_VENDOR_OUT, 0xA0, off, 0, image[off:off + FW_CHUNKSIZE],
                              DEFAULT_TIMEOUT_MS)
        dev.ctrl_transfer(_REQ_VENDOR_OUT, 0xA0, 0xE600, 0, b"\x00", DEFAULT_TIMEOUT_MS)
    except usb.core.USBError as e:
        raise UsbError("FX2 firmware upload failed: %s" % e) from None
    finally:
        usb.util.dispose_resources(dev)


def wait_renumerated(bus: int, port_path: Tuple[int, ...], uploaded_at: float) -> Candidate:
    """Find the device again on the same physical port after the FX2 reset."""
    gone = uploaded_at + RENUM_GONE_DELAY_MS / 1000
    now = time.monotonic()
    if now < gone:
        time.sleep(gone - now)
    while True:
        for d in find_devices():
            if d.bus == bus and d.port_path == port_path and d.has_firmware:
                log.info("device came back after %d ms", (time.monotonic() - uploaded_at) * 1000)
                return d
        if (time.monotonic() - uploaded_at) * 1000 >= RENUM_CHECK_PERIOD_MS:
            raise UsbError("device did not re-enumerate after FX2 firmware upload")
        time.sleep(RENUM_POLL_INTERVAL_MS / 1000)


class PyUsbTransport:
    def __init__(self, cand: Candidate):
        self._dev = cand.dev
        self.candidate = cand
        try:
            self._dev.set_configuration(CONFIGURATION)
        except usb.core.USBError as e:
            # Already configured is fine; macOS may refuse a redundant SET_CONFIGURATION.
            log.debug("set_configuration: %s", e)
        try:
            usb.util.claim_interface(self._dev, INTERFACE)
        except usb.core.USBError as e:
            raise UsbError("cannot claim USB interface (is KingstVIS running?): %s" % e) from None

    def ctrl_in(self, request: int, value: int, index: int, length: int) -> bytes:
        try:
            data = bytes(self._dev.ctrl_transfer(_REQ_VENDOR_IN, request, value, index, length,
                                                 DEFAULT_TIMEOUT_MS))
        except usb.core.USBError as e:
            raise UsbError("ctrl in req %#x val %#x: %s" % (request, value, e)) from None
        if len(data) != length:
            raise UsbError("ctrl in req %#x val %#x: short read %d/%d" % (request, value, len(data), length))
        log.debug("ctrl_in  %#04x %#06x %d -> %s", request, value, index, data.hex())
        return data

    def ctrl_out(self, request: int, value: int, index: int, data: bytes = b"") -> None:
        log.debug("ctrl_out %#04x %#06x %d <- %s", request, value, index, data.hex())
        try:
            n = self._dev.ctrl_transfer(_REQ_VENDOR_OUT, request, value, index, data, DEFAULT_TIMEOUT_MS)
        except usb.core.USBError as e:
            raise UsbError("ctrl out req %#x val %#x: %s" % (request, value, e)) from None
        if n != len(data):
            raise UsbError("ctrl out req %#x val %#x: short write" % (request, value))

    def bulk_write(self, ep: int, data: bytes) -> None:
        try:
            n = self._dev.write(ep, data, DEFAULT_TIMEOUT_MS)
        except usb.core.USBError as e:
            raise UsbError("bulk write ep %#x: %s" % (ep, e)) from None
        if n != len(data):
            raise UsbError("bulk write ep %#x: short write %d/%d" % (ep, n, len(data)))

    def bulk_read_stream(self, ep: int, nbytes: int, chunk: int) -> Iterator[bytes]:
        """Synchronous reads until `nbytes` arrived. Swap for async transfers when streaming."""
        remaining = nbytes
        while remaining > 0:
            want = min(chunk, (remaining + 511) // 512 * 512)
            try:
                data = self._dev.read(ep, want, BULK_TIMEOUT_MS)
            except usb.core.USBTimeoutError:
                raise UsbError("bulk read timed out with %d of %d bytes left" % (remaining, nbytes)) from None
            except usb.core.USBError as e:
                raise UsbError("bulk read ep %#x: %s" % (ep, e)) from None
            n = min(len(data), remaining)
            remaining -= n
            yield data[:n].tobytes()

    def bulk_reader(self, ep: int, chunk: int, timeout_ms: int, depth: int = 64) -> "BulkReader":
        return BulkReader(self._dev, ep, chunk, timeout_ms, depth)

    def close(self) -> None:
        try:
            usb.util.release_interface(self._dev, INTERFACE)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(self._dev)


class BulkReader:
    """Continuous bulk reads in a background thread, for streaming mode.

    The device does not wait for the host in streaming mode, so reading must
    not pause while samples are processed. libusb calls release the GIL, so a
    plain thread keeps reading while the main thread decodes. Iterating yields
    the received chunks in order; it ends after stop() once the queue is
    drained, and raises UsbError on a read error, a stall or a queue overrun.
    """

    def __init__(self, dev, ep: int, chunk: int, timeout_ms: int, depth: int = 64):
        self._dev = dev
        self.ep = ep
        self.chunk = chunk
        self.timeout_ms = timeout_ms
        self._q: "queue.Queue" = queue.Queue(maxsize=depth)
        self._stop = threading.Event()
        self._error: Optional[str] = None
        self._thread = threading.Thread(target=self._run, name="usb-reader", daemon=True)
        self.bytes = 0

    def start(self) -> "BulkReader":
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    data = self._dev.read(self.ep, self.chunk, self.timeout_ms)
                except usb.core.USBTimeoutError:
                    if self._stop.is_set():
                        break
                    self._error = ("stream stopped: no data for %d ms (the device buffer probably "
                                   "overflowed; use fewer channels or a lower samplerate)" % self.timeout_ms)
                    break
                except usb.core.USBError as e:
                    if not self._stop.is_set():
                        self._error = "bulk read ep %#x: %s" % (self.ep, e)
                    break
                self.bytes += len(data)
                try:
                    self._q.put_nowait(data.tobytes())
                except queue.Full:
                    self._error = "stream overrun: processing fell behind the device"
                    break
        finally:
            self._q.put(None)  # wake the consumer; may block briefly when full

    def __iter__(self) -> Iterator[bytes]:
        while True:
            item = self._q.get()
            if item is None:
                if self._error:
                    raise StreamError(self._error)
                return
            yield item

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout)
