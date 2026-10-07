import smtplib
import secrets
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv

load_dotenv()

# Email configuration from .env
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL")
FROM_NAME = os.getenv("FROM_NAME", "NIRIKHYANA PURI")

# OTP settings
OTP_EXPIRY_MINUTES = int(os.getenv("OTP_EXPIRY_MINUTES", "1"))

def generate_otp() -> str:
    """Generate 6-digit OTP"""
    return str(secrets.randbelow(900000) + 100000)

def get_otp_expiry() -> datetime:
    """Get OTP expiry datetime"""
    return datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)

def create_otp_email_body(otp: str, user_name: str) -> str:
    """Create HTML email body for OTP"""
    return f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #2c5aa0;">NIRIKHYANA PURI - Password Reset</h2>
                
                <p>Dear {user_name},</p>
                
                <p>You have requested to reset your password for your NIRIKHYANA PURI account.</p>
                
                <div style="background-color: #f8f9fa; padding: 20px; border-radius: 5px; text-align: center; margin: 20px 0;">
                    <h3 style="color: #2c5aa0; margin: 0;">Your OTP Code</h3>
                    <h1 style="color: #dc3545; font-size: 36px; margin: 10px 0; letter-spacing: 5px;">{otp}</h1>
                    <p style="color: #6c757d; margin: 0;">Valid for {OTP_EXPIRY_MINUTES} minutes only</p>
                </div>
                
                <p><strong>Important:</strong></p>
                <ul>
                    <li>Do not share this OTP with anyone</li>
                    <li>This OTP will expire in {OTP_EXPIRY_MINUTES} minutes</li>
                    <li>If you didn't request this, please ignore this email</li>
                </ul>
                
                <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
                
                <p style="color: #6c757d; font-size: 12px;">
                    This is an automated email from NIRIKHYANA PURI System. Please do not reply to this email.
                </p>
            </div>
        </body>
    </html>
    """

async def send_otp_email(email: str, otp: str, user_name: str) -> bool:
    """Send OTP via email"""
    try:
        # Create message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = "NIRIKHYANA PURI - Password Reset OTP"
        msg['From'] = f"{FROM_NAME} <{FROM_EMAIL}>"
        msg['To'] = email
        
        # Create HTML content
        html_body = create_otp_email_body(otp, user_name)
        html_part = MIMEText(html_body, 'html')
        msg.attach(html_part)
        
        # Send email
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        
        return True
    except Exception as e:
        print(f"Email sending failed: {e}")
        return False