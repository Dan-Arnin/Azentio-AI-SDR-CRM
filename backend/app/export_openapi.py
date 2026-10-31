"""Write the generated OpenAPI contract to backend/openapi.json (run after changing routes or contracts)."""
import json
from pathlib import Path

from .main import app

if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "openapi.json"
    out.write_text(json.dumps(app.openapi(), indent=2) + "\n")
    print(f"wrote {out}")
