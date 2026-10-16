# kingst-la

Capture for Kingst LA series logic analyzers on macOS (and, untested, Linux/Windows),
in Python over libusb. The command is `kla`. Only the LA5016 is supported so far; the
code is laid out so other models of the family can be added. Options and output formats follow
[sigrok-cli](https://sigrok.org/wiki/Sigrok-cli); the USB protocol is ported from
libsigrok's `kingst-la2016` driver. Neither sigrok nor KingstVIS is needed at run time.

This is an unofficial project, not affiliated with or endorsed by Kingst or sigrok.

## Install

```sh
brew install libusb
pip install -e '.[dev]'
kla --extract-firmware          # once; reads /Applications/KingstVIS.app
```

The FX2 firmware and FPGA bitstreams belong to Kingst and are not part of this
repository. `--extract-firmware` copies them out of your own KingstVIS into
`~/Library/Application Support/kingst-la/firmware/` (Linux: `~/.local/share/kingst-la/firmware/`,
Windows: `%LOCALAPPDATA%\kingst-la\firmware\`; override with `KINGST_LA_FIRMWARE_DIR`).
Do not commit or redistribute them.

## Use

```sh
kla --scan
kla --show
kla -c samplerate=1m --samples 100k -C CH0,CH1=SDA -O vcd -o cap.vcd
kla -c samplerate=100m:captureratio=20 -t CH0=r --time 10ms -O csv
kla -c voltage_threshold=0.6-0.6 --samples 1m -C CH0 -O bits
kla -c samplerate=500m -o cap.vcd     # no limit: stops when memory is full or on Ctrl-C
kla -c samplerate=10m --samples 10m -o cap.sr   # sigrok session file, opens in PulseView
```

| Option | |
|---|---|
| `-d kingst-la2016[:conn=BUS.ADDR]` | device; optional with one analyzer attached |
| `-c samplerate=…` | 12.2 kHz–500 MHz, default 500 MHz. The hardware divides 800 MHz by 1, 4 or a multiple of 8, so other rates are rounded down (a warning tells the actual rate) |
| `-c captureratio=…` | pre-trigger share in %, default 50 |
| `-c voltage_threshold=x-x` | 0.4, 0.6, 0.9, 1.2, 1.4, 2.0, 2.5, 4.0 V; default 1.4 |
| `--samples N` / `--time T` | `T` in ms, or with `ms`/`s` |
| `-C` | `CH0,CH3-CH5,CH7=NAME` |
| `-t` | `CH0=r,CH1=1`: levels `0`/`1` plus one edge `r`/`f` |
| `-w` | drop samples before the trigger |
| `-O` | `bits` (default on stdout), `hex`, `csv`, `vcd`, `binary`, `srzip`, with `:opt=value` options as in sigrok |
| `-o FILE` | without `-O` the format follows the extension (`.vcd`, `.csv`, `.bin`); anything else, `.sr` included, is srzip as in sigrok-cli |
| `-i FILE` | read a `.sr` file instead of the device (see below) |
| `-l 0..5` | log level; 3 shows progress, 4 adds USB register traffic |

### Protocol decoders

```sh
kla --list-decoders                         # what can be installed (needs network), marks installed ones
kla --install-decoders                       # all of them; fetches sigrok's decoders (GPL) at a pinned commit
kla --install-decoders uart,i2c              # or just some (common/ always comes along)
kla --list-decoders installed               # offline: what is installed, and from which commit
kla -L                                       # formats and decoders
kla --show -P uart                           # channels, options, annotation classes
kla -c samplerate=10m --samples 1m -P uart:rx=CH0:baudrate=115200 -A uart=rx-data
kla -c samplerate=4m --time 50ms -P i2c:scl=CH0:sda=CH1,eeprom24xx
kla -c samplerate=10m --samples 1m -P uart:rx=CH0:baudrate=115200 -B uart=rx -o rx.bin
kla -c samplerate=4m --time 50ms -P i2c:scl=CH0:sda=CH1 -M i2c     # meta output: "i2c-1: Bitrate: <bit/s>"
```

The decoders are libsigrokdecode's own Python files, run unmodified by a pure Python
`sigrokdecode` engine (no libsigrokdecode build needed). With `-P` the logic output is
replaced by decoder output, as in sigrok-cli. Only one kind is shown: `-B` binary, else `-M`
meta values (i2c/spi bitrate, jitter counts), else `-A` annotations. `--protocol-decoder-samplenum` prefixes each
annotation with its sample range; `-l 3`/`-l 4` add quotes, class ids and short texts.

### Session files

```sh
kla -i cap.sr -O vcd -o cap.vcd                     # convert
kla -i cap.sr -P uart:rx=CH0:baudrate=115200        # decode again without the device
kla -i pulseview.sr -C D0=CLK,D1 --samples 100k -O csv
```

`-o x.sr` writes sigrok's srzip format, the same files PulseView and libsigrok's `kingst-la2016`
driver save, and `-i` reads them back, also files from other analyzers (up to their first 16
channels). With `-i`, `-C` names the file's channels (or `CHn` for bit n), and `--samples`/`--time`
take the first part. The trigger position is not stored in srzip.

### PWM outputs

```sh
kla -g PWM1 -c enabled=on:output_frequency=1k:duty_cycle=25 --set   # prints the actual frequency/duty
kla -g PWM2 -c output_frequency=123.4k --set                       # giving a value turns it on
kla -g PWM1 -c enabled=off --set
kla --show                                                         # includes the last PWM settings
```

The two generators run at 200 MHz / N (up to 20 MHz) and keep running after the command exits,
so a later command can capture them, e.g. wire PWM2 to CH0 as a known test signal. The PWM
registers cannot be read back; kingst-la remembers what it last set in its data directory.

### Streaming

```sh
kla -c continuous=on:samplerate=10m -C CH0-CH3 -o long.vcd        # until Ctrl-C
kla -c continuous=on:samplerate=1m --time 60s -P uart:rx=CH0:baudrate=9600
```

Streaming sends samples while the device keeps sampling, so a capture can be longer than the
device memory. It has no hardware trigger, runs up to 100 MHz, and samplerate × channels should
stay at or below about 160 Mbps (200 Mbps mostly works but sometimes breaks off; the output up
to that point is kept and the exit code is 1).

Ctrl-C while sampling stops the capture and still writes what was sampled.
A second Ctrl-C, or one during the download, aborts (exit 130) and removes the `-o` file.

Differences from sigrok-cli are listed in [docs/design.md](docs/design.md) §5.4.

## Notes

- The inputs have no hysteresis. A slow signal near the threshold produces short glitches on each crossing.
- Linux needs a udev rule for `77a1:01a2`; Windows needs the WinUSB driver bound to the device (Zadig).
  Neither has been tried yet.

## Tests

```sh
pytest                                                    # no hardware
KINGST_LA_TEST_FREQ=1000 KINGST_LA_TEST_THRESHOLD=0.6 pytest -m hw  # LA5016 with a 1 kHz signal on CH0
pytest -m decoders                                        # needs --install-decoders
KINGST_LA_TEST_PWM=2 pytest -m hw                            # PWM2 wired to CH0: loopback tests
```

## License

GPL-3.0-or-later; see [LICENSE](LICENSE). Derived from libsigrok (GPL-3.0-or-later).
