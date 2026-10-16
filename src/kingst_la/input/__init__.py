"""Input formats: sample sources read from files instead of the device."""

from .srzip import InputError, SrzipFile, open_srzip

__all__ = ["InputError", "SrzipFile", "open_srzip"]
