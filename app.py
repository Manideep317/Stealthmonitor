"""
app.py
Webhook listener that turns a raw change-diff into a formatted alert and
dispatches it via Caspian.

Kept decoupled from watcher.py on purpose: this is a separate process reachable
over HTTP, so you could swap the watcher for a completely different detector
(a browser-extension ping, a cron job, another team's service) without
touching this file, as long as it POSTs the same JSON shape.

Run:
    python app.py
Then in another terminal:
    python watcher.py
"""
import atexit
import json

from flask import Flask, jsonify, request
from openai import OpenAI

import alerts
from db import DB
from config import FEATHERLESS_API_KEY, FEATHERLESS_BASE_URL, FEATHERLESS_MODEL

app = Flask(__name__)
db = DB()

# --- Featherless AI client (OpenAI-compatible) ---
llm_client = OpenAI(base_url=FEATHERLESS_BASE_URL, api_key=FEATHERLESS_API_KEY)

# Caspian channel setup (email + optional Telegram) lives in alerts.py —
# importing it above already connected the channels and registered the
# inbound handler. Start the listener on a background thread so the bot
# actually receives (and replies to) messages while Flask serves HTTP.
alerts.start_listen_in_background()


def format_alert_with_llm(payload: dict) -> str:
    """Ask Featherless (Llama 3) to turn the raw diff into a short, readable
    alert message. Falls back to a plain-text summary if the call fails —
    an LLM outage should never block delivery of the underlying alert."""
    prompt = (
        "You are an alerting assistant. Turn this website-change payload into "
        "a concise (2-3 sentence) alert message for a human, in plain text, "
        "no markdown. Mention the URL and what specifically changed "
        "(e.g. price, stock, or general content).\n\n"
        f"Payload:\n{json.dumps(payload, indent=2, default=str)}"
    )
    try:
        response = llm_client.chat.completions.create(
            model=FEATHERLESS_MODEL,
            messages=[
                {"role": "system", "content": "You write short, clear change alerts."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=200,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:  # noqa: BLE001 - alerting path must degrade gracefully
        print(f"[app] Featherless formatting failed, using fallback: {e}")
        return (
            f"Change detected on {payload.get('url')}. "
            f"Fingerprint went from {payload.get('old_value')} to {payload.get('new_value')}."
        )


def send_caspian_alert(message: str) -> None:
    alerts.send_alert(message)


@app.route("/webhook/change-detected", methods=["POST"])
def change_detected():
    payload = request.get_json(force=True, silent=True)
    if not payload or "url" not in payload:
        return jsonify({"error": "expected JSON body with at least a 'url' field"}), 400

    alert_message = format_alert_with_llm(payload)
    send_caspian_alert(alert_message)

    return jsonify({"status": "alert dispatched", "message": alert_message}), 200


@app.route("/targets", methods=["GET", "POST"])
def targets():
    """Convenience endpoint for the demo: list or register watch targets
    without touching sqlite directly."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        url = body.get("url")
        if not url:
            return jsonify({"error": "'url' is required"}), 400
        db.add_target(url)
        return jsonify({"status": "added", "url": url}), 201

    rows = db.get_all_targets()
    return jsonify([dict(r) for r in rows])


@app.route("/changes", methods=["GET"])
def changes():
    rows = db.get_recent_changes(limit=int(request.args.get("limit", 20)))
    return jsonify([dict(r) for r in rows])


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@atexit.register
def _shutdown():
    print("[app] shutting down")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)