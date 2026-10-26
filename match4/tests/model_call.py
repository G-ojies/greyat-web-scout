"""CI check: the HF backend loads and generates on the installed torch/transformers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from agent import llm  # noqa: E402

m = llm.load()
print('reply:', m.chat('Reply with JSON only.', 'Give {"ok": true}', 30))
