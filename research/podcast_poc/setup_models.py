"""Download the two pinned public models; never invokes hosted inference."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["HF_HOME"] = str(ROOT / ".podcast-models")
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

from huggingface_hub import snapshot_download

MODELS = {
    "mlx-community/Qwen3-4B-Instruct-2507-4bit": "50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b",
    "mlx-community/whisper-small-mlx": "45f3915923c7a79a5a5b5a7d909d39aeb0e5630e",
}
for model, revision in MODELS.items():
    path = snapshot_download(model, revision=revision, token=False,
                             allow_patterns=["*.json", "*.safetensors", "*.npz", "*.model", "*.txt"])
    print(model, path, flush=True)
