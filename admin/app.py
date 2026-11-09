"""Private admin app. Reads archive.db; bind to 127.0.0.1 only and never proxy it publicly."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import load_config  # noqa: E402
from webcore import create_app  # noqa: E402

app = create_app(load_config(), "admin")
