python
import imaplib
import email
import smtplib
from email.mime.text import MIMEText
import requests
import os

IMAP_SERVER = 'imap.gmail.com'  # אם המייל שלך בגוגל, אם לא יש לשנות בהתאם
SMTP_SERVER = 'smtp.gmail.com'
EMAIL_ACCOUNT = os.getenv('EMAIL_ACCOUNT')
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD')  # סיסמת אפליקציה
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_API_URL = 'https://api.gemini.example.com/v1/chat'  # החלף לכתובת האמיתית

def get_unread_emails():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select('inbox')

    typ, data = mail.search(None, 'UNSEEN')
    mail_ids = data[0].split()

    emails = []
    for mail_id in mail_ids:
        typ, msg_data = mail.fetch(mail_id, '(RFC822)')
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])
                subject = msg['subject']
                from_ = email.utils.parseaddr(msg['from'])[1]
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == 'text/plain':
                            body = part.get_payload(decode=True).decode()
                            break
                else:
                    body = msg.get_payload(decode=True).decode()
                emails.append({'from': from_, 'subject': subject, 'body': body, 'id': mail_id})
    mail.logout()
    return emails

def send_email(to_email, subject, body):
    msg = MIMEText(body)
    msg['From'] = EMAIL_ACCOUNT
    msg['To'] = to_email
    msg['Subject'] = subject

    server = smtplib.SMTP_SSL(SMTP_SERVER, 465)
    server.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    server.sendmail(EMAIL_ACCOUNT, to_email, msg.as_string())
    server.quit()

def query_gemini_api(prompt):
    headers = {
        'Authorization': f'Bearer {GEMINI_API_KEY}',
        'Content-Type': 'application/json',
    }
    payload = {
        'prompt': prompt,
        'max_tokens': 500,
    }
    response = requests.post(GEMINI_API_URL, json=payload, headers=headers)
    if response.status_code == 200:
        response_json = response.json()
        return response_json.get('reply', 'No response from Gemini')
    else:
        return f'Error from Gemini API: {response.status_code}'

def main():
    emails = get_unread_emails()
    for mail in emails:
        print(f"Processing email from {mail['from']} with subject: {mail['subject']}")
        response = query_gemini_api(mail['body'])
        send_email(mail['from'], f"Re: {mail['subject']}", response)
        print("Response sent.")

if __name__ == '__main__':
    main()
