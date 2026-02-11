import os
import imaplib
import email
import smtplib
import requests
import markdown
from email.mime.text import MIMEText
import json
import re
from email.header import decode_header
from typing import Dict, Any, List, Optional, Tuple

# ----------------- קבועים -----------------
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
THREADS_FILE = "threads.json"

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

PREFERRED_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")  # אופציונלי

# יעד אורך: מיילים ארוכים ומפורטים
TARGET_STYLE = (
    "ענה תשובה ארוכה מאוד, מפורטת, ומעשית. "
    "תן פירוט, דוגמאות, צעדים, ורשימת אפשרויות. "
    "אל תחסוך במילים. "
    "אם המשתמש מבקש 'בדיחה' — תן 3 בדיחות שונות + אחת קצרה ואחת ארוכה. "
    "אם יש כמה אפשרויות — הצג אותן עם יתרונות/חסרונות."
)

# ----------------- JSON threads -----------------
def load_threads() -> Dict[str, Any]:
    try:
        with open(THREADS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[!] Error loading threads: {e}")
        return {}

def save_threads(threads: Dict[str, Any]) -> None:
    try:
        with open(THREADS_FILE, "w", encoding="utf-8") as f:
            json.dump(threads, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[!] Error saving threads: {e}")


# ----------------- עזרי Email -----------------
def decode_mime_header(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="ignore"))
        else:
            out.append(text)
    return "".join(out).strip()

def clean_email_body(body: str) -> str:
    patterns_to_remove = [
        r"--\s*\n.*",
        r"Sent from my .*",
        r"שלח:.*",
        r"נשלח:.*",
        r"From:.*",
        r"To:.*",
        r"Cc:.*",
        r"Subject:.*",
        r"-----Original Message-----",
        r"^>+.*$",
        r"^On .*wrote:.*$",
    ]
    pattern = "|".join(patterns_to_remove)
    body = re.split(pattern, body, flags=re.IGNORECASE | re.MULTILINE)[0]
    return body.strip()

def extract_body(msg: email.message.Message) -> str:
    text_parts = []
    html_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue

            payload = part.get_payload(decode=True)
            if payload is None:
                continue

            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="ignore")

            if ctype == "text/plain":
                text_parts.append(decoded)
            elif ctype == "text/html":
                html_parts.append(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="ignore")
            if msg.get_content_type() == "text/plain":
                text_parts.append(decoded)
            elif msg.get_content_type() == "text/html":
                html_parts.append(decoded)

    body = "\n".join(text_parts).strip()
    if body:
        return body

    if html_parts:
        html = "\n".join(html_parts)
        html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"</p\s*>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"<[^>]+>", "", html)
        return html.strip()

    return ""


# ----------------- IMAP: unread emails -----------------
def get_unread_emails() -> List[Dict[str, Any]]:
    try:
        if not EMAIL_ACCOUNT or not EMAIL_PASSWORD:
            print("[!] Missing EMAIL_ACCOUNT / EMAIL_PASSWORD env vars.")
            return []

        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        mail.select("inbox")

        result, data = mail.search(None, "(UNSEEN)")
        if result != "OK":
            print("[!] IMAP search failed:", result, data)
            mail.logout()
            return []

        unread_msg_nums = data[0].split()
        messages = []

        for num in unread_msg_nums:
            result, msg_data = mail.fetch(num, "(RFC822)")
            if result != "OK":
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            sender = email.utils.parseaddr(msg.get("From", ""))[1]
            subject = decode_mime_header(msg.get("Subject")) or "(ללא נושא)"
            message_id = msg.get("Message-ID") or f"<local-{num.decode('utf-8','ignore')}@bot>"
            in_reply_to = msg.get("In-Reply-To")

            body = clean_email_body(extract_body(msg))

            messages.append({
                "from": sender,
                "subject": subject,
                "body": body,
                "message_id": message_id,
                "in_reply_to": in_reply_to,
            })

        mail.logout()
        return messages

    except Exception as e:
        print(f"[!] Error fetching emails: {e}")
        return []


# ----------------- Thread prompt (מפורט מאוד) -----------------
def build_thread_for_gemini(message: Dict[str, Any], threads: Dict[str, Any]) -> Tuple[str, str]:
    thread_id = message["in_reply_to"] or message["message_id"]

    if thread_id not in threads:
        threads[thread_id] = []

    threads[thread_id].append({"from": "user", "body": message["body"]})

    # 🔥 זה מה שגורם לאריכות: חוקי כתיבה + פורמט קבוע
    system_instructions = (
        "אתה בוט אימייל אוטומטי.\n"
        "אתה הוא זה שכתב את התשובות הקודמות בשרשור זה.\n"
        "אתה חייב לענות תשובות ארוכות מאוד ומפורטות.\n"
        f"{TARGET_STYLE}\n\n"
        "כל תשובה תיכתב בפורמט הזה בדיוק:\n"
        "1) תקציר קצר (2-3 שורות)\n"
        "2) תשובה מפורטת מאוד (לפחות 12-20 שורות)\n"
        "3) דוגמאות/נוסחים/אפשרויות (לפחות 3 פריטים)\n"
        "4) שאלות המשך (עד 3 שאלות, רק אם באמת חסר מידע)\n\n"
        "אסור לכתוב תשובת פתיחה כללית כמו: 'קיבלתי את פנייתך'.\n\n"
        "=== היסטוריית השרשור ===\n"
    )

    history = ""
    for msg in threads[thread_id][-12:]:
        who = "משתמש" if msg["from"] == "user" else "אתה"
        history += f"{who}:\n{msg['body']}\n\n"

    prompt = system_instructions + history + "ענה עכשיו כהמשך ישיר, לפי הפורמט:\n"
    return prompt, thread_id


# ----------------- SMTP send -----------------
def send_email(to_email: str, subject: str, body_text: str, original_message_id: Optional[str] = None) -> None:
    try:
        if not EMAIL_ACCOUNT or not EMAIL_PASSWORD:
            print("[!] Missing EMAIL_ACCOUNT / EMAIL_PASSWORD env vars.")
            return

        formatted_text = markdown.markdown(body_text)

        signature = """
        <hr>
        <div style="color:#666; font-size:14px; margin-top:10px;">
        בינה מלאכותית ג'מיני באימייל נבנה ע"י @טשיקאוור ניוז
        </div>
        """

        html_body = f"""
        <html>
            <body style="direction: rtl; text-align: right; font-family: Arial, sans-serif;">
                {formatted_text}
                {signature}
            </body>
        </html>
        """

        msg = MIMEText(html_body, "html", "utf-8")
        msg["From"] = EMAIL_ACCOUNT
        msg["To"] = to_email
        msg["Subject"] = subject

        if original_message_id:
            msg["In-Reply-To"] = original_message_id
            msg["References"] = original_message_id

        with smtplib.SMTP_SSL(SMTP_SERVER, 465) as server:
            server.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ACCOUNT, to_email, msg.as_string())

        print(f"[✔] Sent reply to {to_email}")

    except Exception as e:
        print(f"[!] Error sending email: {e}")


# ----------------- Gemini: models.list + בחירת מודל קיים -----------------
def gemini_list_models() -> List[Dict[str, Any]]:
    if not GEMINI_API_KEY:
        return []
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            print(f"[!] models.list failed ({r.status_code}): {r.text}")
            return []
        return r.json().get("models", [])
    except Exception as e:
        print(f"[!] models.list exception: {e}")
        return []

def pick_generate_content_model(preferred: str) -> str:
    models = gemini_list_models()
    if not models:
        return preferred

    candidates = []
    for m in models:
        name = m.get("name", "")
        methods = m.get("supportedGenerationMethods", []) or []
        if name.startswith("models/") and "generateContent" in methods:
            candidates.append(name.split("/", 1)[1])

    if preferred in candidates:
        return preferred
    if candidates:
        print(f"[DBG] Preferred model '{preferred}' not available -> fallback '{candidates[0]}'")
        return candidates[0]
    return preferred


def call_gemini(prompt: str, model_id: str) -> Tuple[bool, str]:
    if not GEMINI_API_KEY:
        return False, "Missing GEMINI_API_KEY"

    url = f"{API_BASE}/models/{model_id}:generateContent?key={GEMINI_API_KEY}"

    # 🔥 generationConfig כדי לדחוף אורך
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.8,
            "topP": 0.95,
            # ערך גבוה = מרחיב. אם תראה שזה נחתך עדיין, תעלה ל-4096/8192 (תלוי מודל)
            "maxOutputTokens": 2048
        }
    }

    try:
        r = requests.post(url, json=payload, timeout=90)
        if r.status_code != 200:
            return False, f"Gemini API error ({r.status_code}) model={model_id}: {r.text}"

        data = r.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return False, f"No candidates. Raw: {data}"

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts or "text" not in parts[0]:
            finish_reason = candidates[0].get("finishReason")
            safety = candidates[0].get("safetyRatings")
            return False, f"No text. finishReason={finish_reason} safetyRatings={safety} Raw: {data}"

        return True, parts[0]["text"]

    except Exception as e:
        return False, f"Exception calling Gemini: {e}"


# ----------------- Main -----------------
def main() -> None:
    print("Starting Gemini Email Bot...")
    print("[DBG] EMAIL_ACCOUNT exists:", bool(EMAIL_ACCOUNT))
    print("[DBG] EMAIL_PASSWORD exists:", bool(EMAIL_PASSWORD))
    print("[DBG] GEMINI_API_KEY exists:", bool(GEMINI_API_KEY), "len:", (len(GEMINI_API_KEY) if GEMINI_API_KEY else 0))
    print("[DBG] Preferred model:", PREFERRED_GEMINI_MODEL)

    active_model = pick_generate_content_model(PREFERRED_GEMINI_MODEL)
    print("[DBG] Active model:", active_model)

    threads = load_threads()
    emails = get_unread_emails()

    if not emails:
        print("No new emails.")
        return

    for msg in emails:
        print(f"[📩] From: {msg['from']} | Subject: {msg['subject']}")

        prompt, thread_id = build_thread_for_gemini(msg, threads)
        ok, reply_or_error = call_gemini(prompt, active_model)

        if ok:
            gemini_reply = reply_or_error
            threads[thread_id].append({"from": "gemini", "body": gemini_reply})
            send_email(
                msg["from"],
                f"Re: {msg['subject']}",
                gemini_reply,
                msg["message_id"]
            )
        else:
            err = reply_or_error
            print("[!] Gemini failed:", err)

            safe_reply = (
                "יש תקלה זמנית במנוע התשובות ולכן לא הצלחתי לייצר תשובה מפורטת עכשיו.\n"
                "נסה שוב בעוד כמה דקות 🙂"
            )
            send_email(
                msg["from"],
                f"Re: {msg['subject']}",
                safe_reply,
                msg["message_id"]
            )

            if ADMIN_EMAIL and ADMIN_EMAIL != msg["from"]:
                send_email(
                    ADMIN_EMAIL,
                    "Gemini Email Bot - Error report",
                    f"Sender: {msg['from']}\nSubject: {msg['subject']}\n\nError:\n{err}\n\nPrompt (first 1200 chars):\n{prompt[:1200]}",
                    None
                )

    save_threads(threads)


if __name__ == "__main__":
    main()
