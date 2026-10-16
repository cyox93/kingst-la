import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _private_data_dir(request, tmp_path, monkeypatch):
    """Keep tests away from the user's firmware dir and PWM state, except on hardware."""
    if request.node.get_closest_marker("hw") is None:
        from la5016.srd import loader
        # Installed decoders stay where they are (tests read them, never write).
        monkeypatch.setenv("LA5016_DECODERS_DIR", str(loader.decoders_dir()))
        monkeypatch.setenv("LA5016_FIRMWARE_DIR", str(tmp_path / "la5016" / "firmware"))
