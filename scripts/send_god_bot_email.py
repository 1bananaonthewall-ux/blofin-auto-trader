#!/usr/bin/env python3
"""Send God Bot.zip via SMTP (Gmail or custom). Reads optional env from .env."""

from __future__ import annotations

import argparse
import os
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Email God Bot.zip")
    parser.add_argument("--to", default=os.getenv("GOD_BOT_EMAIL_TO", "abcdiscjockey@gmail.com"))
    parser.add_argument(
        "--zip",
        type=Path,
        default=Path(os.getenv("GOD_BOT_ZIP", Path.home() / "Downloads" / "God Bot.zip")),
    )
    parser.add_argument("--from-addr", default=os.getenv("SMTP_FROM") or os.getenv("SMTP_USER"))
    parser.add_argument("--smtp-host", default=os.getenv("SMTP_HOST", "smtp.gmail.com"))
    parser.add_argument("--smtp-port", type=int, default=int(os.getenv("SMTP_PORT", "587")))
    parser.add_argument("--user", default=os.getenv("SMTP_USER"))
    parser.add_argument("--password", default=os.getenv("SMTP_PASSWORD") or os.getenv("SMTP_APP_PASSWORD"))
    args = parser.parse_args()

    _load_dotenv()
    user = args.user or os.getenv("SMTP_USER")
    password = args.password or os.getenv("SMTP_PASSWORD") or os.getenv("SMTP_APP_PASSWORD")
    from_addr = args.from_addr or user
    to_addr = args.to

    if not args.zip.is_file():
        print(f"Missing zip: {args.zip}")
        return 1
    if not user or not password:
        print(
            "SMTP not configured. Set in .env or environment:\n"
            "  SMTP_USER=your@gmail.com\n"
            "  SMTP_APP_PASSWORD=<Gmail app password>\n"
            "  SMTP_FROM=your@gmail.com (optional)\n"
            "Gmail: https://myaccount.google.com/apppasswords\n"
            "Then run:\n"
            f"  python scripts/send_god_bot_email.py --to {to_addr}"
        )
        return 2

    body = """Hi,

Attached is God Bot.zip — the Blofin auto-trader stack for a new Windows PC.

Inside the zip:
  - AGENT_READ_ME_FIRST.md / INSTRUCTION_MANUAL_FOR_CURSOR_AGENT.md (for Cursor on the new machine)
  - SETUP_NEW_COMPUTER.md (human install steps)
  - Full bot code, scripts, dashboard (no API keys — copy .env.example to .env)

Unzip, install Python 3.12+, pip install -r requirements.txt, fill .env, then:
  powershell -ExecutionPolicy Bypass -File ".\\God Bot.ps1" -Action ensure

Optional local LLM (~3.6 GB): scripts\\setup_local_llm.ps1 -DownloadModel 7b

— God Bot packager
"""
    msg = MIMEMultipart()
    msg["Subject"] = "God Bot — portable package + agent manual"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with args.zip.open("rb") as f:
        part = MIMEApplication(f.read(), Name=args.zip.name)
    part["Content-Disposition"] = f'attachment; filename="{args.zip.name}"'
    msg.attach(part)

    ctx = ssl.create_default_context()
    with smtplib.SMTP(args.smtp_host, args.smtp_port, timeout=120) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ctx)
        smtp.login(user, password)
        smtp.sendmail(from_addr, [to_addr], msg.as_string())

    print(f"Sent {args.zip.name} ({args.zip.stat().st_size // 1024} KB) to {to_addr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
