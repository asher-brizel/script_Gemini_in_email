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

# --- הגדרות קבועות ---
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
THREADS_FILE = "threads.json"

EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# מודל: אפשר לשנות לפי מה שיש לך פעיל
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")


# ---------- עזר: פענוח כותרות מייל ----------
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


# --- טעינה ושמירה של השרשורים מקובץ JSON ---
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


# --- ניקוי חתימות והודעות חוזרות ---
def clean_email_body(body: str) -> str:
    patterns_to_remove = [
        r"--\s*\n.*",                 # קו חתימה --
        r"Sent from my .*",           # Sent from my iPhone וכו'
        r"שלח:.*",                    # שורות של מייל קודם בעברית
        r"נשלח:.*",
        r"From:.*",
        r"To:.*",
        r"Cc:.*",
        r"Subject:.*",
        r"-----Original Message-----",
        r"^>+.*$",                    # ציטוטים
    ]
    pattern = "|".join(patterns_to_remove)
    body = re.split(pattern, body, flags=re.IGNORECASE | re.MULTILINE)[0]
    return body.strip()


# --- חילוץ גוף טקסט מתוך הודעה (תומך גם HTML fallback) ---
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

    # עדיפות לטקסט רגיל
    body = "\n".join(text_parts).strip()
    if body:
        return body

    # fallback ל-HTML (מינימלי: הסרת תגיות בסיסית)
    if html_parts:
        html = "\n".join(html_parts)
        html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"</p\s*>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"<[^>]+>", "", html)
        return html.strip()

    return ""


# --- קבלת מיילים חדשים ---
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
            message_id = msg.get("Message-ID")
            in_reply_to = msg.get("In-Reply-To")

            body = extract_body(msg)
            body = clean_email_body(body)

            if not message_id:
                # בלי message-id קשה לשרשר; נייצר אחד מקומי
                message_id = f"<local-{num.decode('utf-8', 'ignore')}@bot>"

            messages.append({
                "from": sender,
                "subject": subject,
                "body": body,
                "message_id": message_id,
                "in_reply_to": in_reply_to
            })

        mail.logout()
        return messages

    except Exception as e:
        print(f"[!] Error fetching emails: {e}")
        return []


# --- בניית השרשור עבור ג'מיני ---
def build_thread_for_gemini(message: Dict[str, Any], threads: Dict[str, Any]) -> Tuple[str, str]:
    thread_id = message["in_reply_to"] or message["message_id"]

    if thread_id not in threads:
        threads[thread_id] = []

    # הוספת הודעת המשתמש החדשה
    threads[thread_id].append({
        "from": "user",
        "body": message["body"]
    })

    # בניית טקסט לג'מיני
    gemini_prompt = (
        "אתה עוזר במייל. כתוב תשובה מקצועית, קצרה וברורה בעברית.\n"
        "אם חסר מידע – שאל שאלה אחת-שתיים בלבד.\n\n"
    )

    for msg in threads[thread_id]:
        if msg["from"] == "user":
            gemini_prompt += f"[משתמש כתב]:\n{msg['body']}\n\n"
        elif msg["from"] == "gemini":
            gemini_prompt += f"[הבוט כתב]:\n{msg['body']}\n\n"

    return gemini_prompt, thread_id


# --- שליחת מייל כולל שרשור ---
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


# --- קבלת תגובה מג'מיני (מתוקן: key ב-URL + פירוט שגיאות מלא) ---
def get_gemini_reply(prompt: str) -> str:
    if not GEMINI_API_KEY:
        return "שגיאה: GEMINI_API_KEY לא מוגדר (Secret חסר ב-GitHub Actions או משתנה סביבה חסר)."

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )

    data = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]}
        ]
    }

    try:
        response = requests.post(url, json=data, timeout=60)

        if response.status_code != 200:
            # כאן תראה את השגיאה האמיתית מהשרת
            return f"Gemini API error ({response.status_code}): {response.text}"

        result = response.json()

        candidates = result.get("candidates", [])
        if not candidates:
            return f"Gemini API: אין candidates. Raw: {result}"

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts or "text" not in parts[0]:
            finish_reason = candidates[0].get("finishReason")
            safety = candidates[0].get("safetyRatings")
            return f"Gemini API: אין טקסט בתשובה (finishReason={finish_reason}). safetyRatings={safety}. Raw: {result}"

        return parts[0]["text"]

    except Exception as e:
        return f"שגיאה פנימית בתקשורת עם Gemini: {e}"


# --- הפעלת הבוט ---
def main() -> None:
    print("Starting Gemini Email Bot...")

    # בדיקות מהירות (בלי לחשוף סודות)
    print("[DBG] EMAIL_ACCOUNT exists:", bool(EMAIL_ACCOUNT))
    print("[DBG] EMAIL_PASSWORD exists:", bool(EMAIL_PASSWORD))
    print("[DBG] GEMINI_API_KEY exists:", bool(GEMINI_API_KEY), "len:", (len(GEMINI_API_KEY) if GEMINI_API_KEY else 0))
    print("[DBG] GEMINI_MODEL:", GEMINI_MODEL)

    threads = load_threads()
    emails = get_unread_emails()

    if not emails:
        print("No new emails.")
        return

    for msg in emails:
        print(f"[📩] New email from {msg['from']} | subject: {msg['subject']}")

        gemini_prompt, thread_id = build_thread_for_gemini(msg, threads)
        gemini_reply = get_gemini_reply(gemini_prompt)

        threads[thread_id].append({
            "from": "gemini",
            "body": gemini_reply
        })

        send_email(
            msg["from"],
            f"Re: {msg['subject']}",
            gemini_reply,
            msg["message_id"]
        )

    save_threads(threads)


if __name__ == "__main__":
    main()
