import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _private_data_dir(request, tmp_path, monkeypatch):
    """Keep tests away from the user's firmware dir and PWM state, except on hardware."""
    if request.node.get_closest_marker("hw") is None:
        from kingst_la.srd import loader
        # Installed decoders stay where they are (tests read them, never write).
        monkeypatch.setenv("KINGST_LA_DECODERS_DIR", str(loader.decoders_dir()))
        monkeypatch.setenv("KINGST_LA_FIRMWARE_DIR", str(tmp_path / "kingst-la" / "firmware"))
