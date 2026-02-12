import os
import time
import json
import re
import requests

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY environment variable")

# עדיפות: הכי “שווה” וחינמי/נגיש לרוב חשבונות, ואז יורדים.
MODEL_PRIORITY = [
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-2.0-flash",
    "gemma-3-27b-it",
    "gemma-3-12b-it",
    "gemma-3-4b-it",
    "gemma-3-1b-it",
]

def list_models():
    url = f"{API_BASE}/models?key={GEMINI_API_KEY}"
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to list models: {r.status_code} {r.text[:300]}")
    return r.json().get("models", [])

def available_text_models():
    models = list_models()
    available = []
    for m in models:
        name = m.get("name", "")
        methods = m.get("supportedGenerationMethods", []) or []
        if name.startswith("models/") and "generateContent" in methods:
            available.append(name.split("/", 1)[1])
    return sorted(set(available))

def try_generate(model_id: str) -> tuple[bool, str]:
    url = f"{API_BASE}/models/{model_id}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": "כתוב משפט אחד ברור בעברית, בלי לקצר באמצע."}]
        }],
        "generationConfig": {
            "temperature": 0.6,
            "maxOutputTokens": 128,
            "candidateCount": 1
        }
    }
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code == 200:
        data = r.json()
        try:
            parts = data["candidates"][0]["content"].get("parts", [])
            txt = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
            return True, txt or "(empty)"
        except Exception:
            return True, "(parsed empty)"
    else:
        # Return error message (short)
        return False, f"{r.status_code} {r.text[:200]}"

def looks_like_quota_429(err: str) -> bool:
    return "429" in err or "RESOURCE_EXHAUSTED" in err or "Quota exceeded" in err

def pick_best_working_model():
    avail = available_text_models()

    print("Available models for this API key:")
    for m in avail:
        print(" -", m)

    # סדר בדיקה: קודם עדיפות, אבל רק כאלה שבאמת קיימים ברשימה
    candidates = [m for m in MODEL_PRIORITY if m in avail]
    # ואם אין התאמה (שמות שונים) – ננסה את מה שיש
    if not candidates:
        candidates = avail

    print("\nTrying models (until one works):")
    last_err = ""

    for m in candidates:
        ok, info = try_generate(m)
        if ok:
            print(f"✅ OK: {m}")
            print("\nTest generation output:")
            print(info)
            return m
        else:
            print(f"❌ FAIL: {m} -> {info}")
            last_err = info

            # אם זה 429, לא נתקעים—עוברים למודל הבא
            if looks_like_quota_429(info):
                continue

            # אם זה 403/404—גם ממשיכים
            if info.startswith("403") or info.startswith("404"):
                continue

            # שגיאות אחרות—ממשיכים בכל מקרה
            continue

    print("\nNo model passed the live test (likely quota limits).")
    print("Pick one of these (they exist for your key):")
    for m in candidates[:8]:
        print(" -", m)

    # לא נכשל – רק מציג מסקנה כדי שה-workflow לא ייפול
    return candidates[0] if candidates else "gemini-2.0-flash"

if __name__ == "__main__":
    chosen = pick_best_working_model()
    print(f"\nSelected model to use in your bot: {chosen}")
