import os
import imaplib
import email
import smtplib
import requests
from email.mime.text import MIMEText

# --- הגדרות קבועות ---
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"

EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


# --- פונקציה לקבלת מיילים חדשים ---
def get_unread_emails():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        mail.select("inbox")

        result, data = mail.search(None, "(UNSEEN)")
        unread_msg_nums = data[0].split()
        messages = []

        for num in unread_msg_nums:
            result, msg_data = mail.fetch(num, "(RFC822)")
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            sender = email.utils.parseaddr(msg["From"])[1]
            subject = msg["Subject"] if msg["Subject"] else "(ללא נושא)"
            body = ""

            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        charset = part.get_content_charset() or "utf-8"
                        body += part.get_payload(decode=True).decode(charset, errors="ignore")
            else:
                charset = msg.get_content_charset() or "utf-8"
                body += msg.get_payload(decode=True).decode(charset, errors="ignore")

            messages.append({"from": sender, "subject": subject, "body": body})

        mail.logout()
        return messages

    except Exception as e:
        print(f"[!] Error fetching emails: {e}")
        return []


# --- שליחת מייל עם חתימה ---
def send_email(to_email, subject, body_text):
    try:
        formatted_text = body_text.replace("\n", "<br>")

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

        with smtplib.SMTP_SSL(SMTP_SERVER, 465) as server:
            server.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ACCOUNT, to_email, msg.as_string())

        print(f"[✔] Sent reply to {to_email}")

    except Exception as e:
        print(f"[!] Error sending email: {e}")


# --- קבלת תגובה מג'מיני ---
def get_gemini_reply(prompt):
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

        headers = {
            "Content-Type": "application/json",
            "X-goog-api-key": GEMINI_API_KEY
        }

        data = {
            "contents": [
                {"parts": [{"text": prompt}]}
            ]
        }

        response = requests.post(url, headers=headers, json=data)

        if response.status_code == 200:
            result = response.json()
            return result["candidates"][0]["content"]["parts"][0]["text"]

        print("[!] Gemini API error:", response.text)
        return "אירעה שגיאה בעת יצירת התגובה."

    except Exception as e:
        print(f"[!] Error contacting Gemini API: {e}")
        return "שגיאה פנימית בתקשורת עם Gemini."


# --- הפעלת הבוט ---
def main():
    print("Starting Gemini Email Bot...")

    emails = get_unread_emails()
    if not emails:
        print("No new emails.")
        return

    for msg in emails:
        print(f"[📩] New email from {msg['from']}")
        reply = get_gemini_reply(msg["body"])
        send_email(msg["from"], f"Re: {msg['subject']}", reply)


if __name__ == "__main__":
    main()
        
