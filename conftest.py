"""Configuration commune des tests du bot."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# bot.py valide sa configuration au moment de l'import. Les tests ne doivent
# jamais dépendre d'un secret présent dans l'environnement de l'agent.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("WEB_CACHE_DIR", str(ROOT / ".test-cache"))
