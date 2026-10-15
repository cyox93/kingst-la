"""sigrok-cli compatible command line for the Kingst LA5016."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from typing import BinaryIO, List, Optional

import numpy as np

from la5016 import __version__, firmware, options, output, rle
from la5016.options import UsageError
from la5016.pipeline import Channel, Meta, Sink

log = logging.getLogger("la5016")

MODEL = "Kingst LA5016"
CHANNELS = 16

_LOGLEVELS = {0: logging.CRITICAL + 10, 1: logging.ERROR, 2: logging.WARNING,
              3: logging.INFO, 4: logging.DEBUG, 5: logging.DEBUG}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="la5016",
        description="Kingst LA5016 logic analyzer capture, sigrok-cli compatible.",
        epilog="Example: la5016 -c samplerate=1m --samples 1m -C CH0 -O vcd -o cap.vcd",
    )
    p.add_argument("-V", "--version", action="store_true", help="show version")
    p.add_argument("-l", "--loglevel", type=int, default=2, metavar="LEVEL", help="0-5 (default 2)")
    p.add_argument("-d", "--driver", metavar="DRIVER", help="kingst-la2016[:conn=BUS.ADDR]")
    p.add_argument("-c", "--config", action="append", default=[], metavar="KEY=VAL[:...]",
                   help="samplerate, captureratio, voltage_threshold")
    p.add_argument("-C", "--channels", metavar="LIST", help="e.g. CH0,CH3-CH5,CH7=SDA")
    p.add_argument("-t", "--triggers", metavar="LIST", help="e.g. CH0=r,CH1=1 (0, 1, r, f)")
    p.add_argument("-w", "--wait-trigger", action="store_true", help="drop samples before the trigger")
    p.add_argument("-o", "--output-file", metavar="FILE")
    p.add_argument("-O", "--output-format", metavar="FORMAT[:OPT=VAL]",
                   help="bits (default), hex, csv, vcd, binary")
    p.add_argument("--scan", action="store_true", help="scan for devices")
    p.add_argument("--show", action="store_true", help="show device options")
    p.add_argument("--samples", metavar="N")
    p.add_argument("--time", metavar="MS")
    p.add_argument("--extract-firmware", nargs="?", const=firmware.DEFAULT_KINGSTVIS, metavar="KINGSTVIS",
                   help="extract FX2 firmware and FPGA bitstreams from KingstVIS (la5016 extension)")
    return p


def _setup_logging(level: int) -> None:
    logging.basicConfig(level=_LOGLEVELS.get(max(0, min(5, level))), stream=sys.stderr,
                        format="%(name)s: %(message)s")
    if level < 5:
        logging.getLogger("la5016.usb").setLevel(max(logging.INFO, _LOGLEVELS[min(level, 5)]))


def _conn(args) -> Optional[tuple]:
    if not args.driver:
        return None
    _, opts = options.parse_driver(args.driver)
    unknown = set(opts) - {"conn"}
    if unknown:
        raise UsageError("unknown driver option(s): %s" % ", ".join(sorted(unknown)))
    return options.parse_conn(opts["conn"]) if "conn" in opts else None


def _device_line(dev) -> str:
    chans = " ".join("CH%d" % i for i in range(CHANNELS))
    return "%s:conn=%s - %s with %d channels: %s" % (
        options.DRIVER, dev.t.candidate.conn, MODEL, CHANNELS, chans)


def cmd_extract(app: str, out) -> int:
    files = firmware.install(app)
    out.write("Extracted to %s:\n" % firmware.firmware_dir())
    for path, size, digest in files:
        out.write("  %-34s %8d  sha256 %s\n" % (path.name, size, digest))
    return 0


def cmd_scan(args, out) -> int:
    from la5016.device import open_device
    dev = open_device(_conn(args))
    try:
        out.write("The following devices were found:\n")
        out.write(_device_line(dev) + "\n")
    finally:
        dev.close()
    return 0


def cmd_show(args, out) -> int:
    from la5016.device import open_device
    dev = open_device(_conn(args))
    try:
        ident = dev.identity
        out.write("Driver functions:\n    Logic analyzer\n")
        out.write("Scan options:\n    conn\n")
        out.write(_device_line(dev) + "\n")
        out.write("Hardware revision: %s (EEPROM magic %#04x/%#04x)" % (ident.stem, ident.magic, ident.magic2))
        out.write(", manufactured %s\n" % ident.manufactured if ident.manufactured else "\n")
        out.write("Supported configuration options:\n")
        out.write("    Supported triggers: 0 1 r f\n")
        out.write("    limit_samples: 0 (current)\n")
        out.write("    limit_time: 0 (current)\n")
        out.write("    samplerate - supported samplerates:\n")
        for rate in options.SAMPLERATE_LIST:
            out.write("      %s%s\n" % (_rate_str(rate), " (current)" if rate == options.MAX_SAMPLERATE else ""))
        out.write("      (any rate from %s to %s is accepted)\n"
                  % (_rate_str(options.MIN_SAMPLERATE), _rate_str(options.MAX_SAMPLERATE)))
        out.write("    captureratio: 50 (current)\n")
        out.write("    voltage_threshold: %s\n" % ", ".join(
            "%.1f-%.1f%s" % (v, v, " (current)" if v == options.DEFAULT_THRESHOLD else "")
            for v in options.THRESHOLDS))
    finally:
        dev.close()
    return 0


def _rate_str(rate: int) -> str:
    for div, unit in ((1_000_000_000, "GHz"), (1_000_000, "MHz"), (1_000, "kHz")):
        if rate >= div and rate % div == 0:
            return "%d %s" % (rate // div, unit)
    for div, unit in ((1_000_000, "MHz"), (1_000, "kHz")):
        if rate >= div:
            return "%g %s" % (rate / div, unit)
    return "%d Hz" % rate


class _WaitTrigger:
    """-w: pass samples only from the trigger on."""

    def __init__(self, sink: Sink):
        self.sink = sink
        self.armed = False

    def begin(self, meta: Meta) -> None:
        self.sink.begin(meta)

    def feed(self, samples: np.ndarray) -> None:
        if self.armed:
            self.sink.feed(samples)

    def trigger(self) -> None:
        self.armed = True

    def end(self) -> None:
        self.sink.end()


class _Interrupts:
    """First Ctrl-C while sampling stops the capture and keeps the data; any other aborts."""

    def __init__(self):
        self.stop = False
        self.sampling = False
        self._prev = None

    def __enter__(self):
        self._prev = signal.signal(signal.SIGINT, self._handler)
        return self

    def __exit__(self, *exc):
        signal.signal(signal.SIGINT, self._prev)

    def _handler(self, signum, frame):
        if self.sampling and not self.stop:
            self.stop = True
            log.warning("stopping capture; press Ctrl-C again to abort")
            return
        raise KeyboardInterrupt


def cmd_capture(args, open_device=None) -> int:
    from la5016.device import CaptureConfig
    if open_device is None:
        from la5016.device import open_device

    cfg_in = options.parse_config(args.config)
    channels = options.parse_channels(args.channels, CHANNELS)
    triggers = options.parse_triggers(args.triggers, channels) if args.triggers else {}
    if args.wait_trigger and not triggers:
        raise UsageError("-w/--wait-trigger needs a trigger (-t)")
    if args.samples and args.time:
        raise UsageError("--samples and --time are mutually exclusive")
    if args.output_format:
        fmt, fmt_opts = options.parse_output_format(args.output_format)
    elif args.output_file:
        fmt, fmt_opts = options.format_from_filename(args.output_file), {}
    else:
        fmt, fmt_opts = "bits", {}

    rate = cfg_in.get("samplerate", options.MAX_SAMPLERATE)
    eff = options.effective_samplerate(rate)
    if eff != rate:
        log.warning("samplerate %s runs at %s", _rate_str(rate), _rate_str(eff))
    limit = options.parse_samples(args.samples) if args.samples else None
    if args.time:
        # Convert to a sample count, so the hardware stops on its own at the exact length.
        limit = max(1, eff * options.parse_time_ms(args.time) // 1000)

    cfg = CaptureConfig(
        samplerate=rate,
        limit_samples=limit,
        captureratio=cfg_in.get("captureratio", 50),
        threshold=cfg_in.get("voltage_threshold", options.DEFAULT_THRESHOLD),
        channel_mask=sum(1 << c.index for c in channels),
        triggers=triggers,
    )

    out: BinaryIO
    out = open(args.output_file, "wb") if args.output_file else sys.stdout.buffer
    try:
        sink = output.create(fmt, out, fmt_opts)
    except ValueError as e:
        if args.output_file:
            out.close()
            os.unlink(args.output_file)
        raise UsageError(str(e)) from None
    if args.wait_trigger:
        sink = _WaitTrigger(sink)

    dev = None
    ok = False
    try:
        with _Interrupts() as intr:
            dev = open_device(_conn(args))
            intr.sampling = True
            cap = dev.capture(cfg, should_stop=lambda: intr.stop)
            intr.sampling = False
            sink.begin(Meta(cap.samplerate, channels))
            stats = rle.expand(cap.chunks, sink, trigger_packets=cap.trigger_packets, limit_samples=limit)
            sink.end()
            if stats.seq_breaks:
                log.warning("%d sequence number breaks in capture data", stats.seq_breaks)
            log.info("%d samples", stats.samples)
            ok = True
    finally:
        if dev is not None:
            dev.close()
        if args.output_file:
            out.close()
            if not ok:
                os.unlink(args.output_file)
        else:
            out.flush()
    return 0


def main(argv: Optional[List[str]] = None, open_device=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.loglevel)
    try:
        if args.version:
            print("la5016 %s" % __version__)
            return 0
        if args.extract_firmware:
            return cmd_extract(args.extract_firmware, sys.stdout)
        if args.driver:
            name, _ = options.parse_driver(args.driver)
        if args.scan:
            return cmd_scan(args, sys.stdout)
        if args.show:
            return cmd_show(args, sys.stdout)
        if any((args.samples, args.time, args.output_file, args.output_format, args.triggers,
                args.wait_trigger)):
            return cmd_capture(args, open_device)
        parser.print_help()
        return 0
    except UsageError as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        log.error("aborted")
        return 130
    except Exception as e:  # device, USB and firmware errors
        if args.loglevel >= 4:
            raise
        log.error("%s", e)
        return 1
