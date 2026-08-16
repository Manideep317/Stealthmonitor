"""
alerts.py
Caspian channel setup for Stealth Monitor. Connects email (always, needs
no signup) and Telegram (if a bot token is configured), then exposes
send_alert() for app.py to call when a price change is detected.

Docs: https://www.trycaspianai.com/docs/quickstart.html

Per the quickstart, the handler shape (message.text/.sender/.reply()) is
identical across every channel, and adding a channel is just one more
connect_*() call against the same client — no separate integration code
per channel.
"""
import config
from caspian_sdk import CommClient

client = CommClient()  # reads CASPIAN_API_KEY / CASPIAN_BASE_URL from env

# ---------------------------------------------------------------------
# Connect channels at import time, once. Email needs no signup or token.
# Telegram is optional — only connects if a bot token is configured, so
# the rest of the app works fine with just email during early testing.
# ---------------------------------------------------------------------
email_inbox = client.connect_email(display_name="Stealth Monitor")
print(f"[alerts] Email agent address: {email_inbox['address']}")

telegram_inbox = None
if config.CASPIAN_TELEGRAM_BOT_TOKEN:
    telegram_inbox = client.connect_telegram(bot_token=config.CASPIAN_TELEGRAM_BOT_TOKEN)
    print(f"[alerts] Telegram bot connected: {telegram_inbox.get('username', telegram_inbox)}")
else:
    print("[alerts] CASPIAN_TELEGRAM_BOT_TOKEN not set — Telegram alerts disabled.")


# ---------------------------------------------------------------------
# Inbound messages — same handler fires for every connected channel.
# Not required for alert delivery, but means someone can email or message
# the agent's address/bot and get an acknowledgement, and gives you a
# ready hook if you later want inbound commands (e.g. "pause alerts").
# ---------------------------------------------------------------------
def start_listen_in_background() -> None:
    """Run client.listen() on a daemon thread so the inbound handler keeps
    working inside another long-running process (e.g. Flask in app.py),
    where the main thread is blocked serving HTTP."""
    import threading
    t = threading.Thread(target=client.listen, daemon=True)
    t.start()
    return t


@client.on_message
def handle_inbound(message):
    """Route inbound messages to the agent loop.

    The agent is async and long-running (Exa search, Featherless LLM calls,
    optional Playwright crawls), so we spin it up in a daemon thread with its
    own event loop.  message.reply is passed as the callback so every piece
    of agent output goes straight back to the user's Telegram / email thread.
    """
    import asyncio
    import threading

    from agent import run_agent_for_message

    print(f"[alerts] inbound from {message.sender}: {message.text}")

    def _run():
        try:
            asyncio.run(run_agent_for_message(message.text, message.reply))
        except Exception as e:
            print(f"[alerts] agent thread crashed: {e}")
            try:
                message.reply(f"⚠️ Something went wrong while processing your request: {e}")
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------------------------------------------------------------------
# Outbound — this is what app.py's webhook calls after Featherless
# formats the alert text.
# ---------------------------------------------------------------------
def send_alert(message_text: str) -> bool:
    """Sends to every configured channel. Returns True if at least one
    channel actually succeeded — a failure on one channel shouldn't be
    treated as total delivery failure if another got through."""
    sent = False

    if config.ALERT_RECIPIENT:
        try:
            client.initiate(email_inbox["id"], config.ALERT_RECIPIENT, message_text)
            print(f"[alerts] email sent to {config.ALERT_RECIPIENT}")
            sent = True
        except Exception as e:
            print(f"[alerts] email delivery failed: {e}")
    else:
        print("[alerts] ALERT_RECIPIENT not set — skipping email.")

    if telegram_inbox and config.ALERT_TELEGRAM_CHAT_ID:
        try:
            client.initiate(telegram_inbox["id"], config.ALERT_TELEGRAM_CHAT_ID, message_text)
            print(f"[alerts] telegram sent to {config.ALERT_TELEGRAM_CHAT_ID}")
            sent = True
        except Exception as e:
            print(f"[alerts] telegram delivery failed: {e}")
    elif telegram_inbox and not config.ALERT_TELEGRAM_CHAT_ID:
        print("[alerts] Telegram connected but ALERT_TELEGRAM_CHAT_ID not set — skipping.")

    if not sent:
        print(f"[alerts] no channel delivered — message was:\n{message_text}")

    return sent


if __name__ == "__main__":
    # Manual verification per the quickstart's "no mail client handy" note:
    # this injects a test message into the email connection so you can
    # watch handle_inbound() actually fire, without leaving this terminal.
    print("\nSending a test email through the gateway...")
    client.test_email()
    print("Check the console above for '[alerts] inbound from ...' — that")
    print("confirms handle_inbound() fired and the reply went out.")

    print("\nListening for real inbound messages (Ctrl+C to stop)...")
    print(f"Email this agent at: {email_inbox['address']}")
    if telegram_inbox:
        print(f"Or message the Telegram bot: {telegram_inbox.get('username', '(see dashboard)')}")
        print("First message you send will reveal your chat_id in the")
        print("'[alerts] inbound from ...' log line — use that for ALERT_TELEGRAM_CHAT_ID.")
    client.listen()