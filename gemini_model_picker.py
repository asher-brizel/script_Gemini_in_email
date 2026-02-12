import os
import requests

# ================= CONFIG =================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
API_BASE = "https://generativelanguage.googleapis.com/v1beta"

if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY environment variable")

# סדר עדיפות – מהכי טוב לפחות טוב (בחינם)
MODEL_PRIORITY = [
    "gemini-3-flash",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.1-flash",
    "gemini-2.0-flash",
]

# ================= API =================
def list_models():
    url = f"{API_BASE}/models?key={GEMINI_API_KEY}"
    r = requests.get(url, timeout=30)

    if r.status_code != 200:
        raise RuntimeError(
            f"Failed to list models: {r.status_code} {r.text[:300]}"
        )

    return r.json().get("models", [])

def pick_best_free_model():
    models = list_models()

    available = set()
    for m in models:
        name = m.get("name", "")
        methods = m.get("supportedGenerationMethods", []) or []
        if name.startswith("models/") and "generateContent" in methods:
            short = name.split("/", 1)[1]
            available.add(short)

    print("Available models for this API key:")
    for m in sorted(available):
        print(" -", m)

    for preferred in MODEL_PRIORITY:
        if preferred in available:
            print(f"\nSelected best model: {preferred}")
            return preferred

    raise RuntimeError("No usable Gemini text-generation model found")

# ================= TEST CALL =================
def test_generation(model_id: str):
    url = f"{API_BASE}/models/{model_id}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": "כתוב משפט אחד ברור בעברית."}]
        }],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 200,
        }
    }

    r = requests.post(url, json=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Generation failed: {r.status_code} {r.text}")

    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    print("\nTest generation output:")
    print(text.strip())

# ================= MAIN =================
if __name__ == "__main__":
    model = pick_best_free_model()
    test_generation(model)
