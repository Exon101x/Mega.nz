"""Optional: create a USER_SESSION string (for 4 GB uploads on Premium, or posting where a bot cannot).
Run once on a machine you trust:  python gen_session.py   — then paste the printed string into .env"""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API ID: ").strip())
api_hash = input("API hash: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nUSER_SESSION=" + client.session.save())
    print("\nKeep this string secret: anyone holding it can use your Telegram account.")
