import os
import imaplib
import email
import smtplib
import requests
import markdown
from email.mime.text import MIMEText
import re
from email.header import decode_header

IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"

EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# ------------------ helpers ------------------
def decode_mime_header(value):
    if not value:
        return ""
    parts = decode_header(value)
    return "".join(
        t.decode(enc or "utf-8", errors="ignore") if isinstance(t, bytes) else t
        for t, enc in parts
    )

def clean_email_body(text):
    patterns = [
        r"--\s*\n.*",
        r"Sent from my .*",
        r"^>+.*$",
        r"-----Original Message-----",
    ]
    for p in patterns:
        text = re.split(p, text, flags=re.IGNORECASE | re.MULTILINE)[0]
    return text.strip()

def extract_length_instruction(text):
    """
    מזהה בקשות כמו:
    - 100 מילים
    - 2 שורות
    - קצר
    - ארוך
    """
    m = re.search(r"(\d+)\s*מילים", text)
    if m:
        return f"כתוב בדיוק {m.group(1)} מילים."

    if "קצר" in text:
        return "כתוב תשובה קצרה מאוד (שורה–שתיים)."
    if "ארוך" in text:
        return "כתוב תשובה ארוכה ומפורטת."
    return None

# ------------------ email ------------------
def get_unread_emails():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("inbox")

    _, data = mail.search(None, "(UNSEEN)")
    messages = []

    for num in data[0].split():
        _, msg_data = mail.fetch(num, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])

        sender = email.utils.parseaddr(msg["From"])[1]
        subject = decode_mime_header(msg.get("Subject")) or "(ללא נושא)"
        message_id = msg.get("Message-ID")

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body += part.get_payload(decode=True).decode("utf-8", "ignore")
        else:
            body = msg.get_payload(decode=True).decode("utf-8", "ignore")

        messages.append({
            "from": sender,
            "subject": subject,
            "body": clean_email_body(body),
            "message_id": message_id
        })

    mail.logout()
    return messages

def send_email(to, subject, body, reply_to=None):
    html = markdown.markdown(body)
    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = EMAIL_ACCOUNT
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["In-Reply-To"] = reply_to
        msg["References"] = reply_to

    with smtplib.SMTP_SSL(SMTP_SERVER, 465) as s:
        s.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        s.sendmail(EMAIL_ACCOUNT, to, msg.as_string())

# ------------------ gemini ------------------
def call_gemini(user_text):
    length_rule = extract_length_instruction(user_text)

    prompt = (
        "ענה בעברית, בטון טבעי ולא רשמי.\n"
        "אל תחפור, אל תסביר עקרונות, אל תכתוב מבנים.\n"
        "ענה רק למה שהתבקש.\n"
    )

    if length_rule:
        prompt += length_rule + "\n"

    prompt += "\nהבקשה:\n" + user_text

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.9,
            "maxOutputTokens": 512
        }
    }

    url = f"{API_BASE}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    r = requests.post(url, json=payload, timeout=60)
    if r.status_code != 200:
        return None

    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

# ------------------ main ------------------
def main():
    emails = get_unread_emails()
    for m in emails:
        reply = call_gemini(m["body"])
        if reply:
            send_email(
                m["from"],
                f"Re: {m['subject']}",
                reply,
                m["message_id"]
            )

if __name__ == "__main__":
    main()
