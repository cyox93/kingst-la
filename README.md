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
| `-c samplerate=…` | 12.2 kHz–500 MHz, default 500 MHz; runs at `800 MHz / (800 MHz // rate)` |
| `-c captureratio=…` | pre-trigger share in %, default 50 |
| `-c voltage_threshold=x-x` | 0.4, 0.6, 0.9, 1.2, 1.4, 2.0, 2.5, 4.0 V; default 1.4 |
| `--samples N` / `--time T` | `T` in ms, or with `ms`/`s` |
| `-C` | `CH0,CH3-CH5,CH7=NAME` |
| `-t` | `CH0=r,CH1=1`: levels `0`/`1` plus one edge `r`/`f` |
| `-w` | drop samples before the trigger |
| `-O` | `bits` (default), `hex`, `csv`, `vcd`, `binary`, with `:opt=value` options as in sigrok |
| `-o FILE` | without `-O` the format follows the extension (`.vcd`, `.csv`, `.bin`) |
| `-l 0..5` | log level; 3 shows progress, 4 adds USB register traffic |

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
```

## License

GPL-3.0-or-later; see [LICENSE](LICENSE). Derived from libsigrok (GPL-3.0-or-later).
