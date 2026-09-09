#!/usr/bin/env python3
import json, os, sys
from dotenv import load_dotenv
load_dotenv()

from effectgate.providers.ollama import OllamaProvider

p = OllamaProvider()
print("Ollama:", p.base_url)
try:
    tags = p.tags()
except Exception as e:
    print("FAILED to reach Ollama:", repr(e))
    sys.exit(2)

models = [m.get("name") or m.get("model") for m in tags.get("models",[])]
print("Available models:")
for m in models:
    print(" -", m)

configured = ["gemma3:27b","phi4:latest","mistral:latest","deepseek-r1:7b"]
print("\nConfigured model presence:")
for m in configured:
    exact = m in models
    prefix = any((x or "").startswith(m.split(":")[0] + ":") for x in models)
    print(f" {m}: {'exact' if exact else 'family-found' if prefix else 'missing'}")

model = os.getenv("OLLAMA_DEFAULT_MODEL","gemma3:27b")
print(f"\nSmoke chat with {model}...")
r = p.chat(model,[{"role":"user","content":"Return only the JSON object {\"ok\": true}."}],num_predict=64,temperature=0.0)
print(r.text)
print(json.dumps(r.metrics,indent=2))
