import os, time, json, sys, traceback, re
import requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID    = os.getenv("CHANNEL_ID", "").strip()

# mindestens WEBHOOK_1 erforderlich; WEBHOOK_2 optional
WEBHOOK_1 = os.getenv("WEBHOOK_1", "").strip()
WEBHOOK_2 = os.getenv("WEBHOOK_2", "").strip()

# Notional (Margin ohne Hebel) an den Trading-Server mitgeben
DEFAULT_NOTIONAL = float(os.getenv("NOTIONAL", os.getenv("DEFAULT_NOTIONAL", "50")))

# Polling-Takt
POLL_BASE   = int(os.getenv("POLL_BASE_SECONDS", "300"))   # 5 min
POLL_OFFSET = int(os.getenv("POLL_OFFSET_SECONDS", "5"))   # +5 sec

STATE_FILE = Path("state.json")

if not DISCORD_TOKEN or not CHANNEL_ID or not WEBHOOK_1:
    print("Bitte ENV Variablen setzen: DISCORD_TOKEN, CHANNEL_ID, WEBHOOK_1.")
    sys.exit(1)

HEADERS = {
    "Authorization": DISCORD_TOKEN,
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
}

# ---------- Utils ----------

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_id": None}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

def fetch_latest_messages(channel_id, limit=5):
    url = f"https://discord.com/api/v9/channels/{channel_id}/messages?limit={limit}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    if r.status_code == 429:
        retry = 5
        try:
            retry = r.json().get("retry_after", 5)
        except Exception:
            pass
        time.sleep(retry + 1)
        r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    data_sorted = sorted(data, key=lambda m: int(m["id"]))  # älteste zuerst
    return data_sorted

def _first_block(text: str) -> str:
    """
    Nimmt nur den ersten Signal-Block. Trennkriterium: 1+ Leerzeile(n).
    """
    parts = re.split(r"\n\s*\n", text.strip())
    return parts[0].strip() if parts else text.strip()

def _extract_timeframe_from_text(t: str) -> str | None:
    m = re.search(r"Timeframe:\s*([A-Za-z0-9]+)", t, re.I)
    return m.group(0).strip() if m else None  # komplette "Timeframe: XYZ"-Zeile

def build_signal_text_from_msg(msg: dict) -> str:
    """
    Baut den finalen Signaltext:
      - Embed: nimmt embeds[0].description (nur erster Block) + embeds[0].footer.text (Timeframe)
      - Fallback: content (nur erster Block) + evtl. TF aus content oder embed-footer
      - Garantiert, dass am Ende genau EINE Timeframe-Zeile steht
    """
    content = (msg.get("content") or "").strip()
    embeds  = msg.get("embeds") or []

    desc = ""
    footer_tf_line = None

    if embeds and isinstance(embeds, list):
        e0 = embeds[0] or {}
        desc = (e0.get("description") or "").strip()
        # Discord embed footer kann als obj oder dict vorliegen
        footer = e0.get("footer") or {}
        if isinstance(footer, dict):
            footer_txt = (footer.get("text") or "").strip()
        else:
            footer_txt = ""
        # komplette "Timeframe: XYZ"-Zeile extrahieren, nicht nur Wert
        if footer_txt:
            ft = _extract_timeframe_from_text(footer_txt)
            if ft:
                footer_tf_line = ft

    # Quelltext für den ersten Block: bevorzugt embed.description, sonst content
    base_text = desc if desc else content
    base_text = _first_block(base_text)

    # Hat der Base-Block schon eine TF-Zeile?
    tf_inline = _extract_timeframe_from_text(base_text)

    # Falls nicht, TF aus content/footer suchen
    if not tf_inline:
        tf_from_content = _extract_timeframe_from_text(content) if content else None
        tf_line = tf_from_content or footer_tf_line
        if tf_line:
            # TF ans Ende hängen
            final_text = base_text.rstrip() + "\n" + tf_line
        else:
            # keine TF gefunden -> trotzdem base_text zurück (Server filtert ggf. raus)
            final_text = base_text
    else:
        # Es gibt bereits eine TF-Zeile im Block – sicherstellen, dass sie am Ende steht.
        # Schneide evtl. nachfolgende Zeilen ab und hänge nur die TF als letzte Zeile an.
        block_ohne_tf = re.sub(r"\n?Timeframe:\s*[A-Za-z0-9]+\s*$", "", base_text, flags=re.I).rstrip()
        final_text = block_ohne_tf + "\n" + tf_inline

    return final_text.strip()

def forward_to_webhooks(msg):
    """
    Sendet NICHT mehr die Roh-Payload, sondern genau den Signaltext + notional,
    so wie es der FastAPI-Server erwartet.
    """
    text = build_signal_text_from_msg(msg)
    if not text:
        print("[skip] Keine verwertbare Message (leer).")
        return

    payload = {
        "text": text,
        "notional": DEFAULT_NOTIONAL
    }

    urls = [WEBHOOK_1] + ([WEBHOOK_2] if WEBHOOK_2 else [])
    for idx, url in enumerate(urls, start=1):
        try:
            r = requests.post(url, json=payload, timeout=20)
            r.raise_for_status()
            print(f"[→ Webhook{idx}] OK | text[:80]={text[:80]!r}")
        except Exception as ex:
            print(f"[→ Webhook{idx}] FAIL: {ex}")

def sleep_until_next_tick():
    """
    Schläft exakt bis zum nächsten (n*POLL_BASE + POLL_OFFSET)-Zeitpunkt,
    basierend auf Unix-Zeit (Serverzeit).
    """
    now = time.time()
    period_start = (now // POLL_BASE) * POLL_BASE
    next_tick = period_start + POLL_BASE + POLL_OFFSET
    if now < period_start + POLL_OFFSET:
        next_tick = period_start + POLL_OFFSET
    sleep_s = max(0, next_tick - now)
    time.sleep(sleep_s)

# ---------- Main Loop ----------

def main():
    print(f"Getaktet: alle {POLL_BASE}s, jeweils +{POLL_OFFSET}s Offset (z. B. 10:00:05, 10:05:05, …)")
    print(f"➡️  Notional (pro Trade, ohne Hebel): {DEFAULT_NOTIONAL}")
    state = load_state()
    last_id = state.get("last_id")

    # Auf ersten exakten Tick ausrichten
    sleep_until_next_tick()

    while True:
        try:
            msgs = fetch_latest_messages(CHANNEL_ID, limit=5)
            new_msgs = []
            for m in msgs:
                mid = m.get("id")
                if last_id is None or int(mid) > int(last_id):
                    new_msgs.append(m)

            if new_msgs:
                for m in new_msgs:  # älteste zuerst
                    forward_to_webhooks(m)
                last_id = new_msgs[-1]["id"]
                state["last_id"] = last_id
                save_state(state)
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] {len(new_msgs)} neue Nachricht(en) verarbeitet. last_id={last_id}")
            else:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] Keine neuen Nachrichten.")

        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except requests.HTTPError as http_err:
            print("[HTTP ERROR]", http_err.response.status_code, http_err.response.text[:200])
        except Exception:
            print("[ERROR]")
            traceback.print_exc()

        sleep_until_next_tick()

if __name__ == "__main__":
    main()
