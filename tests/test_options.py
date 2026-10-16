from __future__ import annotations

import pytest

from la5016.options import (
    BASECLOCK,
    CONFIG_KEYS,
    MAX_SAMPLERATE,
    MIN_SAMPLERATE,
    SAMPLERATE_LIST,
    THRESHOLDS,
    UsageError,
    effective_samplerate,
    format_from_filename,
    parse_captureratio,
    parse_channels,
    parse_config,
    parse_conn,
    parse_driver,
    parse_kv,
    parse_output_format,
    parse_samples,
    parse_si,
    parse_threshold,
    parse_time_ms,
    parse_triggers,
    samplerate_divider,
)
from la5016.pipeline import Channel


def test_usage_error_is_value_error():
    assert issubclass(UsageError, ValueError)


# --- parse_si ---------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("100", 100),
    ("0", 0),
    ("1k", 1_000),
    ("1K", 1_000),
    ("100m", 100_000_000),
    ("1M", 1_000_000),
    ("2g", 2_000_000_000),
    ("2G", 2_000_000_000),
    ("1.5m", 1_500_000),
    ("1 MHz", 1_000_000),
    ("100mhz", 100_000_000),
    ("20kHz", 20_000),
    ("500HZ", 500),
    ("  10k  ", 10_000),
    ("0.5k", 500),
])
def test_parse_si_valid(text, expected):
    assert parse_si(text) == expected
    assert isinstance(parse_si(text), int)


@pytest.mark.parametrize("text", [
    "", " ", "abc", "-1", "-1k", "1x", "k", "1.5", "1.0001k", "1kk",
    "1e3", "1.2.3", "hz", "1 k m", "nan", "inf", "+-1",
])
def test_parse_si_invalid(text):
    with pytest.raises(UsageError):
        parse_si(text)


# --- parse_time_ms ----------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("100", 100),
    ("100ms", 100),
    ("1s", 1000),
    ("1.5s", 1500),
    ("0.001s", 1),
    (" 250 ms ", 250),
])
def test_parse_time_ms_valid(text, expected):
    assert parse_time_ms(text) == expected


@pytest.mark.parametrize("text", [
    "", "0", "0ms", "0s", "-1", "2m", "1h", "1.5", "1.5ms", "0.0001s", "abc", "s", "ms",
])
def test_parse_time_ms_invalid(text):
    with pytest.raises(UsageError):
        parse_time_ms(text)


# --- parse_kv ---------------------------------------------------------------

def test_parse_kv():
    assert parse_kv("samplerate=100m:captureratio=10") == {
        "samplerate": "100m", "captureratio": "10"}


def test_parse_kv_empty():
    assert parse_kv("") == {}


def test_parse_kv_value_may_contain_equals_and_be_empty():
    assert parse_kv("a=b=c") == {"a": "b=c"}
    assert parse_kv("a=") == {"a": ""}


@pytest.mark.parametrize("text", ["flag", "a=1:flag", "=1", "a=1::b=2", "a=1:"])
def test_parse_kv_invalid(text):
    with pytest.raises(UsageError):
        parse_kv(text)


def test_parse_kv_duplicate_key_rejected():
    with pytest.raises(UsageError):
        parse_kv("a=1:a=2")


# --- parse_driver / parse_conn ---------------------------------------------

def test_parse_driver_plain():
    assert parse_driver("kingst-la2016") == ("kingst-la2016", {})


def test_parse_driver_with_conn():
    assert parse_driver("kingst-la2016:conn=1.5") == ("kingst-la2016", {"conn": "1.5"})


def test_parse_driver_unsupported():
    with pytest.raises(UsageError, match="kingst-la2016"):
        parse_driver("fx2lafw")


@pytest.mark.parametrize("text", ["", "kingst-la2016:conn", "Kingst-LA2016"])
def test_parse_driver_invalid(text):
    with pytest.raises(UsageError):
        parse_driver(text)


@pytest.mark.parametrize("text, expected", [("1.5", (1, 5)), ("20.127", (20, 127)), ("0.0", (0, 0))])
def test_parse_conn_valid(text, expected):
    assert parse_conn(text) == expected


@pytest.mark.parametrize("text", ["", "1", "1.", ".5", "1.5.6", "a.b", "-1.5", "1,5", "1.x"])
def test_parse_conn_invalid(text):
    with pytest.raises(UsageError):
        parse_conn(text)


# --- parse_channels ---------------------------------------------------------

def test_parse_channels_default_all():
    chans = parse_channels(None)
    assert chans == [Channel(i, f"CH{i}") for i in range(16)]


def test_parse_channels_default_total():
    assert parse_channels(None, total=4) == [Channel(i, f"CH{i}") for i in range(4)]


def test_parse_channels_list_and_range():
    assert parse_channels("CH0,CH3-CH5") == [
        Channel(0, "CH0"), Channel(3, "CH3"), Channel(4, "CH4"), Channel(5, "CH5")]


def test_parse_channels_single_range_endpoint():
    assert parse_channels("CH7-CH7") == [Channel(7, "CH7")]


def test_parse_channels_rename():
    assert parse_channels("CH3=SDA,CH2=SCL") == [Channel(3, "SDA"), Channel(2, "SCL")]


def test_parse_channels_preserves_order():
    assert [c.index for c in parse_channels("CH15,CH0,CH8-CH9")] == [15, 0, 8, 9]


def test_parse_channels_spaces_tolerated():
    assert parse_channels(" CH1 , CH2 ") == [Channel(1, "CH1"), Channel(2, "CH2")]


@pytest.mark.parametrize("text", [
    "",                 # empty
    "CH0,,CH1",         # empty item
    "CH16",             # out of range
    "CH5-CH3",          # reversed range
    "CH0-CH16",         # range out of range
    "CH0,CH0",          # duplicate
    "CH0-CH3,CH2",      # duplicate through range
    "0",                # bare number
    "ch0",              # wrong case prefix
    "Ch0",
    "D0",
    "CH",
    "CHx",
    "CH0-CH3=BUS",      # rename on a range
    "CH0=",             # empty new name
    "CH0=A,CH1=A",      # duplicate names
    "CH0=CH1,CH1",      # rename clashing with another channel's name
    "CH-1",
    "CH01x",
])
def test_parse_channels_invalid(text):
    with pytest.raises(UsageError):
        parse_channels(text)


def test_parse_channels_out_of_range_respects_total():
    with pytest.raises(UsageError):
        parse_channels("CH4", total=4)


# --- parse_triggers ---------------------------------------------------------

ALL = parse_channels(None)


def test_parse_triggers_basic():
    assert parse_triggers("CH0=r,CH1=1", ALL) == {0: "r", 1: "1"}


def test_parse_triggers_levels_only():
    assert parse_triggers("CH0=0,CH1=1,CH2=0", ALL) == {0: "0", 1: "1", 2: "0"}


def test_parse_triggers_falling():
    assert parse_triggers("CH15=f", ALL) == {15: "f"}


def test_parse_triggers_by_renamed_name_or_original():
    chans = parse_channels("CH3=SDA,CH4")
    assert parse_triggers("SDA=f,CH4=1", chans) == {3: "f", 4: "1"}
    assert parse_triggers("CH3=r", chans) == {3: "r"}


def test_parse_triggers_any_edge_rejected():
    with pytest.raises(UsageError, match="any-edge"):
        parse_triggers("CH0=e", ALL)


def test_parse_triggers_two_edges_rejected():
    with pytest.raises(UsageError, match="only one edge trigger"):
        parse_triggers("CH0=r,CH1=f", ALL)


def test_parse_triggers_disabled_channel_rejected():
    with pytest.raises(UsageError):
        parse_triggers("CH5=1", parse_channels("CH0,CH1"))


@pytest.mark.parametrize("text", [
    "", "CH0", "CH0=", "CH0=x", "CH0=rr", "CH0=R", "CH99=1", "FOO=1",
    "CH0=1,CH0=0", "CH0=1,,CH1=0",
])
def test_parse_triggers_invalid(text):
    with pytest.raises(UsageError):
        parse_triggers(text, ALL)


def test_parse_triggers_duplicate_via_rename_rejected():
    chans = parse_channels("CH3=SDA")
    with pytest.raises(UsageError):
        parse_triggers("SDA=1,CH3=0", chans)


# --- output format ----------------------------------------------------------

@pytest.mark.parametrize("name", ["bits", "hex", "csv", "vcd", "binary"])
def test_parse_output_format_names(name):
    assert parse_output_format(name) == (name, {})


def test_parse_output_format_options():
    assert parse_output_format("csv:time=true:header=false") == (
        "csv", {"time": "true", "header": "false"})


@pytest.mark.parametrize("text", ["", "sr", "CSV", "csv:time", "bits:width"])
def test_parse_output_format_invalid(text):
    with pytest.raises(UsageError):
        parse_output_format(text)


@pytest.mark.parametrize("path, expected", [
    ("cap.vcd", "vcd"), ("a/b/cap.csv", "csv"), ("x.bin", "binary"), ("CAP.VCD", "vcd"),
    ("cap.sr", "srzip"), ("cap", "srzip"), ("cap.txt", "srzip"),
])
def test_format_from_filename(path, expected):
    assert format_from_filename(path) == expected




# --- threshold --------------------------------------------------------------

def test_thresholds_constant():
    assert THRESHOLDS == (0.4, 0.6, 0.9, 1.2, 1.4, 2.0, 2.5, 4.0)


@pytest.mark.parametrize("text, expected", [
    ("1.4-1.4", 1.4), ("1.4", 1.4), ("0.4-0.4", 0.4), ("4.0", 4.0), ("4", 4.0),
    ("2-2", 2.0), ("2.50-2.5", 2.5),
])
def test_parse_threshold_valid(text, expected):
    assert parse_threshold(text) == pytest.approx(expected)
    assert parse_threshold(text) in THRESHOLDS


def test_parse_threshold_lo_ne_hi():
    with pytest.raises(UsageError):
        parse_threshold("1.2-1.4")


def test_parse_threshold_not_in_list():
    with pytest.raises(UsageError, match="1.4"):
        parse_threshold("1.5")


@pytest.mark.parametrize("text", ["", "-", "a-a", "1.4-", "-1.4", "1.4-1.4-1.4", "abc"])
def test_parse_threshold_invalid(text):
    with pytest.raises(UsageError):
        parse_threshold(text)


# --- samplerate -------------------------------------------------------------

def test_samplerate_constants():
    assert BASECLOCK == 800_000_000
    assert MAX_SAMPLERATE == 500_000_000
    assert MIN_SAMPLERATE == 12_208


@pytest.mark.parametrize("requested, divider, eff", [
    # Hardware rule measured on an LA5016: 1, 4, or a multiple of 8.
    (500_000_000, 1, 500_000_000),
    (400_000_000, 4, 200_000_000),     # divider 2 runs at 200 MHz
    (300_000_000, 4, 200_000_000),
    (200_000_000, 4, 200_000_000),
    (160_000_000, 4, 200_000_000),     # divider 5
    (100_000_000, 8, 100_000_000),
    (80_000_000, 8, 100_000_000),      # divider 10
    (40_000_000, 16, 50_000_000),      # divider 20
    (8_000_000, 96, 8_333_333),        # divider 100
    (1_000_000, 800, 1_000_000),
    (20_000, 40_000, 20_000),
    (12_208, 65_528, 12_208),
    (3_000_000, 264, 3_030_303),
])
def test_effective_samplerate(requested, divider, eff):
    assert samplerate_divider(requested) == divider
    assert effective_samplerate(requested) == eff


@pytest.mark.parametrize("requested", [500_000_001, 1_000_000_000, 12_206, 0, -1])
def test_samplerate_out_of_range(requested):
    with pytest.raises(UsageError):
        effective_samplerate(requested)
    with pytest.raises(UsageError):
        samplerate_divider(requested)


def test_samplerate_list():
    assert SAMPLERATE_LIST == (   # sigrok rates_500mhz
        20_000, 50_000, 100_000, 200_000, 500_000,
        1_000_000, 2_000_000, 5_000_000, 10_000_000, 20_000_000, 50_000_000,
        100_000_000, 200_000_000, 500_000_000)
    assert all(isinstance(r, int) for r in SAMPLERATE_LIST)
    # Every listed rate is exactly reachable.
    assert all(effective_samplerate(r) == r for r in SAMPLERATE_LIST)


# --- captureratio / samples -------------------------------------------------

@pytest.mark.parametrize("text, expected", [("0", 0), ("50", 50), ("100", 100), (" 10 ", 10)])
def test_parse_captureratio_valid(text, expected):
    assert parse_captureratio(text) == expected


@pytest.mark.parametrize("text", ["", "-1", "101", "1k", "10.5", "abc"])
def test_parse_captureratio_invalid(text):
    with pytest.raises(UsageError):
        parse_captureratio(text)


@pytest.mark.parametrize("text, expected", [("1", 1), ("1m", 1_000_000), ("1.5k", 1500)])
def test_parse_samples_valid(text, expected):
    assert parse_samples(text) == expected


@pytest.mark.parametrize("text", ["0", "", "-5", "abc", "0.5"])
def test_parse_samples_invalid(text):
    with pytest.raises(UsageError):
        parse_samples(text)


# --- parse_config -----------------------------------------------------------

def test_config_keys():
    assert CONFIG_KEYS == {"samplerate", "captureratio", "voltage_threshold", "continuous"}


@pytest.mark.parametrize("text, value", [("on", True), ("off", False), ("TRUE", True), ("0", False)])
def test_continuous(text, value):
    assert parse_config(["continuous=" + text]) == {"continuous": value}


def test_continuous_invalid():
    with pytest.raises(UsageError):
        parse_config(["continuous=maybe"])


def test_parse_config_empty():
    assert parse_config([]) == {}


def test_parse_config_single():
    assert parse_config(["samplerate=100m:captureratio=10:voltage_threshold=1.4-1.4"]) == {
        "samplerate": 100_000_000, "captureratio": 10, "voltage_threshold": 1.4}


def test_parse_config_keeps_requested_samplerate():
    assert parse_config(["samplerate=300m"]) == {"samplerate": 300_000_000}


def test_parse_config_merges_multiple_options():
    assert parse_config(["samplerate=1m", "captureratio=20"]) == {
        "samplerate": 1_000_000, "captureratio": 20}


def test_parse_config_later_option_wins():
    assert parse_config(["captureratio=20", "captureratio=30"]) == {"captureratio": 30}


def test_parse_config_unknown_key():
    with pytest.raises(UsageError, match="samplerate"):
        parse_config(["foo=1"])


@pytest.mark.parametrize("opts", [
    ["samplerate=1g"],
    ["samplerate=abc"],
    ["captureratio=200"],
    ["voltage_threshold=1.2-1.4"],
    ["samplerate"],
    [""],
])
def test_parse_config_invalid_values(opts):
    with pytest.raises(UsageError):
        parse_config(opts)
