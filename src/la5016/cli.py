"""sigrok-cli compatible command line for the Kingst LA5016."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from typing import BinaryIO, List, Optional

import numpy as np

from la5016 import __version__, firmware, options, output, rle, stream
from la5016 import input as inputs
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
    p.add_argument("-g", "--channel-group", metavar="GROUP",
                   help="PWM1 or PWM2: -c then sets enabled, output_frequency, duty_cycle")
    p.add_argument("--set", action="store_true", help="apply the -g settings and exit")
    p.add_argument("-t", "--triggers", metavar="LIST", help="e.g. CH0=r,CH1=1 (0, 1, r, f)")
    p.add_argument("-w", "--wait-trigger", action="store_true", help="drop samples before the trigger")
    p.add_argument("-i", "--input-file", metavar="FILE", help="read samples from a .sr file instead of the device")
    p.add_argument("-I", "--input-format", metavar="FORMAT", help="srzip (default)")
    p.add_argument("-o", "--output-file", metavar="FILE")
    p.add_argument("-O", "--output-format", metavar="FORMAT[:OPT=VAL]",
                   help="bits (default on stdout), hex, csv, vcd, binary, srzip (default for -o)")
    p.add_argument("--scan", action="store_true", help="scan for devices")
    p.add_argument("--show", action="store_true", help="show device options")
    p.add_argument("--samples", metavar="N")
    p.add_argument("--time", metavar="MS")
    p.add_argument("-P", "--protocol-decoders", action="append", default=[], metavar="PD[:OPT=VAL...][,PD...]",
                   help="protocol decoder stack, e.g. uart:rx=CH0:baudrate=115200")
    p.add_argument("-A", "--protocol-decoder-annotations", metavar="PD[=ANN[:ANN...]][,...]")
    p.add_argument("-B", "--protocol-decoder-binary", metavar="PD[=CLASS][,...]")
    p.add_argument("-M", "--protocol-decoder-meta", metavar="PD[,PD...]",
                   help="show decoder meta output (e.g. i2c/spi bitrate) instead of annotations")
    p.add_argument("--protocol-decoder-samplenum", action="store_true", help="show sample numbers")
    p.add_argument("-L", "--list-supported", action="store_true", help="list formats and decoders")
    p.add_argument("--install-decoders", nargs="?", const="all", metavar="all|PD[,PD...]",
                   help="download sigrok protocol decoders, all or the named ones (la5016 extension)")
    p.add_argument("--decoders-commit", metavar="COMMIT",
                   help="libsigrokdecode commit for --install-decoders/--list-decoders")
    p.add_argument("--list-decoders", nargs="?", const="available", choices=("available", "installed"),
                   help="decoders that can be installed (default, needs network) or are installed (la5016 extension)")
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


def cmd_list_decoders(which: str, commit: Optional[str], loglevel: int, out) -> int:
    from la5016.srd import install
    inst = install.installed_entries()
    ver = install.installed_version()
    where = install.decoders_dir()
    if which == "installed":
        title = "Installed protocol decoders (%s, %s):" % (
            "libsigrokdecode %s" % ver[:8] if ver else "version unknown", where)
        out.write(install.list_text(inst, title, loglevel=loglevel))
        return 0
    commit = commit or install.DEFAULT_COMMIT
    entries = install.archive_entries(install.fetch(commit))
    title = "Available protocol decoders (libsigrokdecode %s; installed: %s):" % (
        commit[:8], "%d, %s" % (len(inst), ver[:8]) if inst and ver else len(inst))
    out.write(install.list_text(entries, title, {e.name for e in inst}, loglevel))
    return 0


def cmd_install_decoders(which: str, commit: Optional[str], out) -> int:
    from la5016.srd import install
    names = None if which == "all" else [n.strip() for n in which.split(",") if n.strip()]
    if names is not None and not names:
        raise UsageError("no decoder names given to --install-decoders")
    commit = commit or install.DEFAULT_COMMIT
    try:
        dest, count = install.install(commit, names=names)
    except install.InstallError as e:
        raise UsageError(str(e)) from None
    out.write("Installed %d protocol decoder%s (libsigrokdecode %s) to %s\n"
              % (count, "" if count == 1 else "s", commit[:8], dest))
    return 0


_FORMAT_DESCRIPTIONS = (
    ("binary", "Raw binary logic data"),
    ("bits", "ASCII rendering using 0/1"),
    ("csv", "Comma-separated values"),
    ("hex", "Hexadecimal digits"),
    ("srzip", "srzip session file format data"),
    ("vcd", "Value Change Dump data"),
)
_INPUT_DESCRIPTIONS = (
    ("srzip", "srzip session file format data"),
)


def cmd_list(args, out) -> int:
    from la5016.srd import annotate, loader
    out.write("Supported hardware drivers:\n  %-20s %s\n\n" % (options.DRIVER, "Kingst LA2016"))
    out.write("Supported output formats:\n")
    for name, desc in _FORMAT_DESCRIPTIONS:
        out.write("  %-20s %s\n" % (name, desc))
    out.write("\nSupported input formats:\n")
    for name, desc in _INPUT_DESCRIPTIONS:
        out.write("  %-20s %s\n" % (name, desc))
    out.write("\n")
    infos = []
    for name in loader.available():
        try:
            infos.append(loader.load(name))
        except loader.LoadError as e:
            log.warning("%s", e)
    out.write(annotate.list_text(infos, args.loglevel))
    return 0


def cmd_show_decoders(args, out) -> int:
    from la5016.srd import annotate, loader
    for text in args.protocol_decoders:
        for spec in loader.parse_pd(text):
            out.write(annotate.show_text(loader.load(spec.name)))
    return 0


def cmd_set_pwm(args, out, open_device=None) -> int:
    idx = options.parse_channel_group(args.channel_group)
    cfg = options.parse_pwm_config(args.config)
    if not cfg:
        raise UsageError("nothing to set: give -c enabled=on|off, output_frequency=, duty_cycle=")
    # Giving a frequency or duty implies "on"; the generator keeps running after exit.
    enabled = cfg.get("enabled", True)
    if open_device is None:
        from la5016.device import open_device
    from la5016.device import DeviceError
    dev = open_device(_conn(args))
    try:
        try:
            actual = dev.set_pwm(idx, enabled, cfg.get("output_frequency"), cfg.get("duty_cycle"))
        except DeviceError as e:
            raise UsageError(str(e)) from None
    finally:
        dev.close()
    name = options.PWM_GROUPS[idx]
    if actual is None:
        out.write("%s: off\n" % name)
    else:
        out.write("%s: on, %s, duty %.4g %%\n" % (name, _freq_str(actual[0]), actual[1]))
    return 0


def _freq_str(f: float) -> str:
    for div, unit in ((1e6, "MHz"), (1e3, "kHz")):
        if f >= div:
            return "%.6g %s" % (f / div, unit)
    return "%.6g Hz" % f


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
        out.write("Channel groups (PWM state as last set by la5016):\n")
        for name, (on, freq, duty) in zip(options.PWM_GROUPS, dev.pwm_state()):
            out.write("    %s: %s, output_frequency %s, duty_cycle %.4g %%\n"
                      % (name, "on" if on else "off", _freq_str(freq), duty))
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


def _stream(dev, cfg, channels: List[Channel], sink, limit: Optional[int], intr: "_Interrupts") -> None:
    """Streaming capture: samples flow to the sink while the device keeps sampling."""
    st = dev.stream(cfg)
    try:
        sink.begin(Meta(st.samplerate, channels))

        def chunks():
            for raw in st:
                yield raw
                if intr.stop:
                    st.stop()   # first Ctrl-C: halt, then drain what is in flight

        from la5016.usb import StreamError
        broken = None
        try:
            stats = stream.expand(chunks(), sink, st.channels, limit_samples=limit)
            samples = stats.samples
        except StreamError as e:
            broken = e
        st.stop()
        intr.sampling = False
        sink.end()   # what arrived before a break-off is still valid output
        if broken is not None:
            raise _Incomplete(str(broken))
        log.info("%d samples streamed (%d bytes)", samples, st.bytes)
    finally:
        st.stop()


class _Incomplete(Exception):
    """Capture ended early; the output written so far is kept."""


def _decoder_sink(args, channels: List[Channel], out: BinaryIO):
    """-P: decoder stacks replace the logic output, as in sigrok-cli."""
    import io
    from la5016.srd import annotate, loader, session
    try:
        # Only one output type is shown: -B, else -M, else annotations (sigrok-cli main.c).
        if args.protocol_decoder_binary:
            callbacks = annotate.BinaryWriter(out, {})
        else:
            text = io.TextIOWrapper(out, encoding="utf-8", write_through=True)
            text.close = lambda: None  # the caller owns `out`
            cls = annotate.MetaPrinter if args.protocol_decoder_meta else annotate.AnnotationPrinter
            callbacks = cls(text, set() if args.protocol_decoder_meta else {},
                            args.protocol_decoder_samplenum, args.loglevel)
        stacks = loader.build_stacks(args.protocol_decoders, channels, callbacks)
        infos = {}
        todo = list(stacks)
        while todo:
            inst = todo.pop()
            infos[inst.info.id] = inst.info
            todo.extend(inst.next)
        if args.protocol_decoder_binary:
            callbacks.visible = annotate.parse_binary(args.protocol_decoder_binary, infos)
        elif args.protocol_decoder_meta:
            callbacks.visible = annotate.parse_meta(args.protocol_decoder_meta, infos)
        else:
            callbacks.visible = annotate.parse_annotations(args.protocol_decoder_annotations, infos)
    except loader.LoadError as e:
        raise UsageError(str(e)) from None
    return session.DecoderSink(stacks)


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

    def abort(self) -> None:
        if hasattr(self.sink, "abort"):
            self.sink.abort()


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


def _output_format(args):
    if args.output_format:
        return options.parse_output_format(args.output_format)
    if args.output_file:
        return options.format_from_filename(args.output_file), {}
    return "bits", {}


def _open_sink(args, channels: List[Channel]):
    """Open -o (or stdout) and the -O output or -P decoders writing to it."""
    fmt, fmt_opts = _output_format(args)
    if fmt == "srzip" and not args.output_file and not args.protocol_decoders:
        raise UsageError("output format 'srzip' needs a file (-o FILE)")
    out: BinaryIO
    out = open(args.output_file, "wb") if args.output_file else sys.stdout.buffer
    try:
        if args.protocol_decoders:
            sink = _decoder_sink(args, channels, out)
        else:
            sink = output.create(fmt, out, fmt_opts)
    except (ValueError, UsageError) as e:
        if args.output_file:
            out.close()
            os.unlink(args.output_file)
        raise UsageError(str(e)) from None
    return out, sink


def _close_sink(args, out: BinaryIO, sink, ok: bool) -> None:
    if not ok and hasattr(sink, "abort"):
        sink.abort()
    if args.output_file:
        out.close()
        if not ok:
            os.unlink(args.output_file)
    else:
        out.flush()


def cmd_input(args) -> int:
    """-i: run a session file through the outputs or decoders instead of capturing."""
    device_opts = [flag for flag, given in (
        ("-d", args.driver), ("-c", args.config), ("-t", args.triggers), ("-w", args.wait_trigger),
        ("--scan", args.scan), ("--show", args.show)) if given]
    if device_opts:
        raise UsageError("%s cannot be used with -i (they configure the device)" % ", ".join(device_opts))
    if args.samples and args.time:
        raise UsageError("--samples and --time are mutually exclusive")
    if args.input_format:
        name, _, rest = args.input_format.partition(":")
        name, opts = name.strip(), options.parse_kv(rest)
        if name not in options.INPUT_FORMATS:
            raise UsageError("unsupported input format '%s': expected one of %s"
                             % (name, ", ".join(options.INPUT_FORMATS)))
        if opts:
            raise UsageError("input format '%s' has no options" % name)
    _output_format(args)
    if args.output_file and os.path.exists(args.output_file) and os.path.exists(args.input_file) \
            and os.path.samefile(args.output_file, args.input_file):
        raise UsageError("-o would overwrite the input file")
    try:
        src = inputs.open_srzip(args.input_file)
    except inputs.InputError as e:
        raise UsageError(str(e)) from None
    channels = options.select_channels(args.channels, src.channels)
    limit = options.parse_samples(args.samples) if args.samples else None
    if args.time:
        limit = max(1, src.samplerate * options.parse_time_ms(args.time) // 1000)

    out, sink = _open_sink(args, channels)
    ok = False
    try:
        n = src.read(sink, channels, limit)
        log.info("%d samples read from %s", n, args.input_file)
        ok = True
    finally:
        _close_sink(args, out, sink, ok)
    return 0


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
    _output_format(args)

    continuous = cfg_in.get("continuous", False)
    rate = cfg_in.get("samplerate", options.STREAM_MAX_SAMPLERATE if continuous else options.MAX_SAMPLERATE)
    if continuous and triggers:
        raise UsageError("streaming (continuous=on) has no hardware trigger; drop -t")
    if continuous and options.effective_samplerate(rate) > options.STREAM_MAX_SAMPLERATE:
        raise UsageError("streaming works up to %s" % _rate_str(options.STREAM_MAX_SAMPLERATE))
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
        continuous=continuous,
    )

    out, sink = _open_sink(args, channels)
    if args.wait_trigger:
        sink = _WaitTrigger(sink)

    dev = None
    ok = False
    try:
        with _Interrupts() as intr:
            dev = open_device(_conn(args))
            intr.sampling = True
            if continuous:
                _stream(dev, cfg, channels, sink, limit, intr)
            else:
                cap = dev.capture(cfg, should_stop=lambda: intr.stop)
                intr.sampling = False
                sink.begin(Meta(cap.samplerate, channels))
                stats = rle.expand(cap.chunks, sink, trigger_packets=cap.trigger_packets, limit_samples=limit)
                sink.end()
                if stats.seq_breaks:
                    log.warning("%d sequence number breaks in capture data", stats.seq_breaks)
                log.info("%d samples", stats.samples)
            ok = True
    except _Incomplete:
        ok = True
        raise
    finally:
        if dev is not None:
            dev.close()
        _close_sink(args, out, sink, ok)
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
        if args.list_decoders:
            return cmd_list_decoders(args.list_decoders, args.decoders_commit, args.loglevel, sys.stdout)
        if args.install_decoders is not None:
            return cmd_install_decoders(args.install_decoders, args.decoders_commit, sys.stdout)
        if args.list_supported:
            return cmd_list(args, sys.stdout)
        if (args.protocol_decoder_annotations or args.protocol_decoder_binary
                or args.protocol_decoder_meta) and not args.protocol_decoders:
            raise UsageError("-A/-B/-M need protocol decoders (-P)")
        if args.show and args.protocol_decoders:
            return cmd_show_decoders(args, sys.stdout)
        if args.input_file:
            if args.channel_group or args.set:
                raise UsageError("-g/--set cannot be used with -i")
            return cmd_input(args)
        if args.input_format:
            raise UsageError("-I needs an input file (-i)")
        if args.channel_group:
            if not args.set:
                raise UsageError("-g %s needs --set (configure the generator, then capture in a "
                                 "separate command)" % args.channel_group)
            return cmd_set_pwm(args, sys.stdout, open_device)
        if args.set:
            raise UsageError("--set needs -g PWM1 or -g PWM2")
        if args.driver:
            name, _ = options.parse_driver(args.driver)
        if args.scan:
            return cmd_scan(args, sys.stdout)
        if args.show:
            return cmd_show(args, sys.stdout)
        if any((args.samples, args.time, args.output_file, args.output_format, args.triggers,
                args.wait_trigger, args.protocol_decoders)):
            return cmd_capture(args, open_device)
        parser.print_help()
        return 0
    except UsageError as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        log.error("aborted")
        return 130
    except BrokenPipeError:
        # Reader went away (e.g. piped into head); stay quiet like other CLI tools.
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    except Exception as e:  # device, USB, firmware and decoder errors
        if args.loglevel >= 4:
            raise
        from la5016.srd.session import DecoderError
        if isinstance(e, DecoderError):
            logging.getLogger("srd").error("%s", e)
        else:
            log.error("%s", e)
        return 1
