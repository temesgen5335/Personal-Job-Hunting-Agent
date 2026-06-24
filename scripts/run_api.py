"""Run the FastAPI orchestrator. Usage: .venv/bin/python scripts/run_api.py
Needs the [api] extra: uv pip install -e ".[api,llm]"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import uvicorn  # noqa: E402

from jobagent.api import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    import os

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
