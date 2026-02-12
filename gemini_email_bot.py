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

# מודל "מועדף" בלבד - בפועל נבחר מודל קיים דרך models.list
PREFERRED_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# אורך יצירה: תעלה אם צריך (8192 זה גבוה יחסית; אם המודל לא תומך - הוא יתעלם/יחזיר שגיאה מפורטת)
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "8192"))

# כמה פעמים מותר "להמשיך" אוטומטית כדי למנוע חיתוך
MAX_CONTINUATIONS = int(os.getenv("MAX_CONTINUATIONS", "6"))

# כמה מהסוף להדביק כדי שימשיך בדיוק מאיפה שנקטע
CONTINUATION_TAIL_CHARS = int(os.getenv("CONTINUATION_TAIL_CHARS", "900"))

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")  # אופציונלי: מייל שלך לדוחות שגיאה בלבד

TARGET_STYLE = (
    "ענה תשובה ארוכה מאוד, מפורטת, מעשית ומוסברת היטב. "
    "תן צעדים, דוגמאות, הרחבות, ורשימות. "
    "אל תקצר. "
    "אם המשתמש מבקש 'בדיחה' — תן 3 בדיחות שונות: קצרה, בינונית, ארוכה."
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

    # fallback בסיסי מ-HTML לטקסט (לא שולחים את ה-HTML המקורי הלא-מנוקה)
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

# ----------------- Thread prompt (מפורט מאוד + זהות קשיחה) -----------------
def build_thread_for_gemini(message: Dict[str, Any], threads: Dict[str, Any]) -> Tuple[str, str]:
    thread_id = message["in_reply_to"] or message["message_id"]

    if thread_id not in threads:
        threads[thread_id] = []

    threads[thread_id].append({"from": "user", "body": message["body"]})

    system_instructions = (
        "אתה בוט אימייל אוטומטי.\n"
        "אתה הוא זה שכתב את התשובות הקודמות בשרשור זה.\n"
        "אסור לכתוב תשובות פתיחה כלליות כמו 'קיבלתי את פנייתך'.\n"
        "ענה בעברית, מפורט מאוד, עם הרבה הרחבות.\n"
        f"{TARGET_STYLE}\n\n"
        "פורמט חובה:\n"
        "1) תקציר (2-4 שורות)\n"
        "2) תשובה מפורטת מאוד (לפחות 25-50 שורות)\n"
        "3) דוגמאות/אפשרויות/נוסחים (לפחות 5 סעיפים)\n"
        "4) אם צריך — שאלות המשך ממוקדות (עד 3)\n\n"
        "=== היסטוריית השרשור ===\n"
    )

    # לא להגביל יותר מדי היסטוריה, כדי שלא יאבד הקשר
    history = ""
    for msg in threads[thread_id][-25:]:
        who = "משתמש" if msg["from"] == "user" else "אתה"
        history += f"{who}:\n{msg['body']}\n\n"

    prompt = system_instructions + history + "ענה עכשיו כהמשך ישיר, לפי הפורמט:\n"
    return prompt, thread_id

# ----------------- SMTP send (HTML קל, לא כבד) -----------------
def send_email(to_email: str, subject: str, body_text: str, original_message_id: Optional[str] = None) -> None:
    try:
        if not EMAIL_ACCOUNT or not EMAIL_PASSWORD:
            print("[!] Missing EMAIL_ACCOUNT / EMAIL_PASSWORD env vars.")
            return

        # HTML מינימלי: פחות “כבד” מלהדביק המון עיצוב
        formatted_text = markdown.markdown(body_text)

        signature = """
        <hr>
        <div style="color:#666; font-size:14px; margin-top:10px;">
        בינה מלאכותית ג'מיני באימייל נבנה ע"י @טשיקאוור ניוז
        </div>
        """

        html_body = f"""
        <html>
            <body style="direction: rtl; text-align: right; font-family: Arial, sans-serif; font-size: 15px;">
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
    url = f"{API_BASE}/models?key={GEMINI_API_KEY}"
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

def call_gemini_once(prompt: str, model_id: str) -> Tuple[bool, str, Optional[str]]:
    """
    מחזיר: (ok, text_or_error, finish_reason)
    """
    if not GEMINI_API_KEY:
        return False, "Missing GEMINI_API_KEY", None

    url = f"{API_BASE}/models/{model_id}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.8,
            "topP": 0.95,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
        },
    }

    try:
        r = requests.post(url, json=payload, timeout=120)
        if r.status_code != 200:
            return False, f"Gemini API error ({r.status_code}) model={model_id}: {r.text}", None

        data = r.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return False, f"No candidates. Raw: {data}", None

        finish_reason = candidates[0].get("finishReason")

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts or "text" not in parts[0]:
            safety = candidates[0].get("safetyRatings")
            return False, f"No text. finishReason={finish_reason} safetyRatings={safety} Raw: {data}", finish_reason

        return True, parts[0]["text"], finish_reason

    except Exception as e:
        return False, f"Exception calling Gemini: {e}", None

def call_gemini_no_cut(prompt: str, model_id: str) -> Tuple[bool, str]:
    """
    ✅ לא מפצל מיילים.
    אם Gemini עוצר בגלל טוקנים, ממשיך אוטומטית ומחבר לטקסט אחד.
    """
    ok, text_or_error, finish_reason = call_gemini_once(prompt, model_id)
    if not ok:
        return False, text_or_error

    full_text = text_or_error

    # אם הוא נעצר בגלל מגבלת אורך, נמשיך עד שמסיים.
    for _ in range(MAX_CONTINUATIONS):
        # הרבה מודלים מחזירים MAX_TOKENS כשהם נעצרים בגלל אורך
        if finish_reason not in ("MAX_TOKENS", "MAX_OUTPUT_TOKENS"):
            break

        tail = full_text[-CONTINUATION_TAIL_CHARS:]
        continuation_prompt = (
            "הטקסט שלך נקטע באמצע בגלל מגבלת אורך.\n"
            "המשך בדיוק מאיפה שנעצרת, בלי לחזור על מה שכבר כתבת.\n"
            "אל תכתוב כותרת כמו 'המשך:' ואל תסכם.\n\n"
            "סוף הטקסט האחרון (לייחוס בלבד):\n"
            f"{tail}\n\n"
            "המשך עכשיו:\n"
        )

        ok2, next_text, finish_reason2 = call_gemini_once(continuation_prompt, model_id)
        if not ok2:
            # במקרה קיצון: נחזיר את מה שיש + שורה שמציינת תקלה (לא חיתוך פנימי)
            return True, full_text + "\n\n(הערה: הייתה תקלה בזמן ניסיון להמשיך את התשובה.)"

        # חיבור נקי
        full_text = full_text.rstrip() + "\n\n" + next_text.lstrip()
        finish_reason = finish_reason2

    return True, full_text

# ----------------- Main -----------------
def main() -> None:
    print("Starting Gemini Email Bot...")
    print("[DBG] EMAIL_ACCOUNT exists:", bool(EMAIL_ACCOUNT))
    print("[DBG] EMAIL_PASSWORD exists:", bool(EMAIL_PASSWORD))
    print("[DBG] GEMINI_API_KEY exists:", bool(GEMINI_API_KEY), "len:", (len(GEMINI_API_KEY) if GEMINI_API_KEY else 0))
    print("[DBG] Preferred model:", PREFERRED_GEMINI_MODEL)
    print("[DBG] MAX_OUTPUT_TOKENS:", MAX_OUTPUT_TOKENS)
    print("[DBG] MAX_CONTINUATIONS:", MAX_CONTINUATIONS)

    active_model = pick_generate_content_model(PREFERRED_GEMINI_MODEL)
    print("[DBG] Active model:", active_model)

    threads = load_threads()
    emails = get_unread_emails()

    if not emails:
        print("No new emails.")
        return

    for msg in emails:
        print(f"[📩] From: {msg['from']} | Subject: {msg['subject']} | Body: {msg['body'][:80]}...")

        prompt, thread_id = build_thread_for_gemini(msg, threads)

        ok, reply_or_error = call_gemini_no_cut(prompt, active_model)

        if ok:
            gemini_reply = reply_or_error
            threads[thread_id].append({"from": "gemini", "body": gemini_reply})

            send_email(
                msg["from"],
                f"Re: {msg['subject']}",
                gemini_reply,
                msg["message_id"],
            )
        else:
            err = reply_or_error
            print("[!] Gemini failed:", err)

            safe_reply = "יש תקלה זמנית במנוע התשובות. נסה שוב בעוד כמה דקות 🙂"
            send_email(
                msg["from"],
                f"Re: {msg['subject']}",
                safe_reply,
                msg["message_id"],
            )

            # אופציונלי: דוח שגיאה רק אליך
            if ADMIN_EMAIL and ADMIN_EMAIL != msg["from"]:
                send_email(
                    ADMIN_EMAIL,
                    "Gemini Email Bot - Error report",
                    f"Sender: {msg['from']}\nSubject: {msg['subject']}\n\nError:\n{err}\n\nPrompt(first 1500):\n{prompt[:1500]}",
                    None,
                )

    save_threads(threads)

if __name__ == "__main__":
    main()
