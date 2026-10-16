# la5016

Kingst LA5016 logic analyzer capture for macOS (and, untested, Linux/Windows),
in Python over libusb. Options and output formats follow
[sigrok-cli](https://sigrok.org/wiki/Sigrok-cli); the USB protocol is ported from
libsigrok's `kingst-la2016` driver. Neither sigrok nor KingstVIS is needed at run time.

## Install

```sh
brew install libusb
pip install -e '.[dev]'
la5016 --extract-firmware          # once; reads /Applications/KingstVIS.app
```

The FX2 firmware and FPGA bitstreams belong to Kingst and are not part of this
repository. `--extract-firmware` copies them out of your own KingstVIS into
`~/Library/Application Support/la5016/firmware/` (Linux: `~/.local/share/la5016/firmware/`,
Windows: `%LOCALAPPDATA%\la5016\firmware\`; override with `LA5016_FIRMWARE_DIR`).
Do not commit or redistribute them.

## Use

```sh
la5016 --scan
la5016 --show
la5016 -c samplerate=1m --samples 100k -C CH0,CH1=SDA -O vcd -o cap.vcd
la5016 -c samplerate=100m:captureratio=20 -t CH0=r --time 10ms -O csv
la5016 -c voltage_threshold=0.6-0.6 --samples 1m -C CH0 -O bits
la5016 -c samplerate=500m -o cap.vcd     # no limit: stops when memory is full or on Ctrl-C
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
| `-O` | `bits` (default), `hex`, `csv`, `vcd`, `binary`, with `:opt=value` options as in sigrok |
| `-o FILE` | without `-O` the format follows the extension (`.vcd`, `.csv`, `.bin`) |
| `-l 0..5` | log level; 3 shows progress, 4 adds USB register traffic |

### Protocol decoders

```sh
la5016 --list-decoders                         # what can be installed (needs network), marks installed ones
la5016 --install-decoders                       # all of them; fetches sigrok's decoders (GPL) at a pinned commit
la5016 --install-decoders uart,i2c              # or just some (common/ always comes along)
la5016 --list-decoders installed               # offline: what is installed, and from which commit
la5016 -L                                       # formats and decoders
la5016 --show -P uart                           # channels, options, annotation classes
la5016 -c samplerate=10m --samples 1m -P uart:rx=CH0:baudrate=115200 -A uart=rx-data
la5016 -c samplerate=4m --time 50ms -P i2c:scl=CH0:sda=CH1,eeprom24xx
la5016 -c samplerate=10m --samples 1m -P uart:rx=CH0:baudrate=115200 -B uart=rx -o rx.bin
```

The decoders are libsigrokdecode's own Python files, run unmodified by a pure Python
`sigrokdecode` engine (no libsigrokdecode build needed). With `-P` the logic output is
replaced by decoder output, as in sigrok-cli. `--protocol-decoder-samplenum` prefixes each
annotation with its sample range; `-l 3`/`-l 4` add quotes, class ids and short texts.

### PWM outputs

```sh
la5016 -g PWM1 -c enabled=on:output_frequency=1k:duty_cycle=25 --set   # prints the actual frequency/duty
la5016 -g PWM2 -c output_frequency=123.4k --set                       # giving a value turns it on
la5016 -g PWM1 -c enabled=off --set
la5016 --show                                                         # includes the last PWM settings
```

The two generators run at 200 MHz / N (up to 20 MHz) and keep running after the command exits,
so a later command can capture them, e.g. wire PWM2 to CH0 as a known test signal. The PWM
registers cannot be read back; la5016 remembers what it last set in its data directory.

### Streaming

```sh
la5016 -c continuous=on:samplerate=10m -C CH0-CH3 -o long.vcd        # until Ctrl-C
la5016 -c continuous=on:samplerate=1m --time 60s -P uart:rx=CH0:baudrate=9600
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
LA5016_TEST_FREQ=1000 LA5016_TEST_THRESHOLD=0.6 pytest -m hw  # LA5016 with a 1 kHz signal on CH0
pytest -m decoders                                        # needs --install-decoders
LA5016_TEST_PWM=2 pytest -m hw                            # PWM2 wired to CH0: loopback tests
```

## License

GPL-3.0-or-later; see [LICENSE](LICENSE). Derived from libsigrok (GPL-3.0-or-later).
