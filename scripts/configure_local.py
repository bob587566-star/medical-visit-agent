"""Reuse only the already configured DeepSeek connection; never print credentials."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.model import read_project_env

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / ".env"
source = ROOT.parent / "ecommerce-service-agent" / ".env"
if target.exists():
    print("Existing private configuration preserved.")
else:
    old = read_project_env(source)
    key = old.get("DEEPSEEK_API_KEY", "") or ""
    model = old.get("DEEPSEEK_MODEL") or old.get("MODEL_NAME") or "deepseek-chat"
    # Never copy user data, unrelated tokens, provider endpoints, or session secrets.
    values = {"APP_HOST": "127.0.0.1", "APP_PORT": "8270", "DEEPSEEK_API_KEY": key,
              "DEEPSEEK_BASE_URL": "https://api.deepseek.com", "DEEPSEEK_MODEL": model,
              "MODEL_TIMEOUT_SECONDS": "12", "DATA_RETENTION_DAYS": "7"}
    lines = ["# Private local configuration. Excluded from Git and Docker build."]
    for name, value in values.items():
        # Escaping is dotenv encoding, never shell interpolation.
        encoded = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")
        lines.append(f'{name}="{encoded}"')
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Local configuration ready; configured model service:", bool(key))
