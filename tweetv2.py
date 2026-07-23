import sys
import time
import re
import uuid
import threading
from datetime import datetime
from collections import deque
from colorama import init, Fore, Style
import websocket
import msgpack
from curl_cffi import requests
from solders.keypair import Keypair

init(autoreset=True)

# =====================================================================
# KONFIGURASI
# =====================================================================
# (Opsional) Isi jika ingin notifikasi otomatis dikirim ke Telegram
TELEGRAM_BOT_TOKEN = "8819923714:AAEO9NL4WebuZ8Hc5s1zzwW7ovNl6NNVG8A"  # Contoh: "123456789:ABCdef..."
TELEGRAM_CHAT_ID = "-1003977092390"    # Contoh: "987654321"

def load_targets(filepath="targets.txt"):
    targets = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                username = line.strip().replace("@", "").strip()
                if username:
                    targets.append(username)
        if targets:
            print(f"{Fore.GREEN}{Style.BRIGHT}[+] {len(targets)} target dimuat dari {filepath}{Style.RESET_ALL}")
            for u in targets:
                print(f"    → @{u}")
        else:
            print(f"{Fore.YELLOW}[!] {filepath} kosong!{Style.RESET_ALL}")
    except FileNotFoundError:
        print(f"{Fore.RED}[-] File {filepath} tidak ditemukan!{Style.RESET_ALL}")
    return targets

TARGET_USERNAMES = load_targets()
SEEN_TWEETS      = set()
SEEN_TWEETS_QUEUE = deque(maxlen=5000)  # Mencegah memory leak (max 5000 ID)

CURRENT_SESSION_ID = None
FRESH_ID_TOKEN     = None
DYNAMIC_REFRESH_TOKEN = None
tweet_count = 0

# =====================================================================
# HELPER: DETEKSI CA (SOLANA & EVM) & NOTIFIKASI TELEGRAM
# =====================================================================
def find_crypto_addresses(text):
    """Mendeteksi Contract Address Solana (Base58) atau EVM (0x...) dari teks tweet."""
    solana_pattern = r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b'
    evm_pattern = r'\b0x[a-fA-F0-9]{40}\b'
    
    sol_addrs = re.findall(solana_pattern, text)
    evm_addrs = re.findall(evm_pattern, text)
    
    return list(set(sol_addrs + evm_addrs))

def send_telegram_alert(author, text, url, cas, tweet_type):
    """Mengirim alert otomatis ke Telegram jika TOKEN dan CHAT_ID diisi."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    ca_info = f"\n\n🔑 **CA Detected:** `{cas[0]}`" if cas else ""
    message = (
        f"🚀 **{tweet_type} BARU DARI @{author}**\n\n"
        f"{text}{ca_info}\n\n"
        f"🔗 [Buka Tweet]({url})"
    )
    
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False
            },
            timeout=5
        )
    except Exception as e:
        print(f"{Fore.RED}[-] Gagal kirim Telegram: {e}{Style.RESET_ALL}")

# =====================================================================
# AUTH PADRE.GG
# =====================================================================
def create_wallet_and_login():
    keypair = Keypair()
    headers = {'accept': 'application/json', 'origin': 'https://trade.padre.gg'}
    try:
        msg = requests.get(
            'https://backend.padre.gg/auth/get-solana-sign-in-input',
            headers=headers, impersonate="chrome120"
        ).json().get("message")
        if not msg:
            return None

        sig = str(keypair.sign_message(msg.encode('utf-8')))
        ct  = requests.post(
            'https://backend.padre.gg/auth/sign-in-with-phantom',
            json={
                "walletAddress": str(keypair.pubkey()),
                "signature": sig,
                "message": msg,
                "kolName": None
            },
            headers=headers, impersonate="chrome120"
        ).json().get('token')

        return requests.post(
            'https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken'
            '?key=AIzaSyDytD3neNMfkCmjm7Ll24bJuAzZIaERw8Q',
            json={"token": ct, "returnSecureToken": True},
            impersonate="chrome120"
        ).json().get('refreshToken')

    except Exception as e:
        print(f"{Fore.RED}[-] Gagal login Padre: {type(e).__name__} - {e}{Style.RESET_ALL}")
        return None


def refresh_firebase_token(rt):
    try:
        return requests.post(
            'https://securetoken.googleapis.com/v1/token'
            '?key=AIzaSyDytD3neNMfkCmjm7Ll24bJuAzZIaERw8Q',
            data={'grant_type': 'refresh_token', 'refresh_token': rt},
            impersonate="chrome120"
        ).json().get('id_token')
    except Exception as e:
        print(f"{Fore.RED}[-] Gagal refresh token: {type(e).__name__}{Style.RESET_ALL}")
        return None

# =====================================================================
# PARSER TWEET — sesuai skema nyata Padre.gg
# =====================================================================
def extract_text(obj):
    if not obj:
        return ""
    if isinstance(obj, str):
        return obj.strip()
    if not isinstance(obj, dict):
        return ""

    body = obj.get('body')
    if isinstance(body, str) and body.strip():
        return body.strip()
    if isinstance(body, dict):
        for k in ('text', 'full_text', 'content'):
            v = body.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()

    for key in ('full_text', 'text', 'content'):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    legacy = obj.get('legacy') or obj.get('Legacy')
    if isinstance(legacy, dict):
        for key in ('full_text', 'text', 'content'):
            val = legacy.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

    note = obj.get('note_tweet') or {}
    if isinstance(note, dict):
        note_result = note.get('note_tweet_results', {}).get('result', {})
        if isinstance(note_result, dict):
            for key in ('text', 'full_text', 'content'):
                val = note_result.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()

    for wrapper_key in ('tweet_results', 'tweetResults'):
        result = (obj.get(wrapper_key) or {}).get('result', {})
        if isinstance(result, dict):
            t = extract_text(result)
            if t:
                return t

    return ""


def extract_images(obj):
    if not obj or not isinstance(obj, dict):
        return []

    media = obj.get('media')
    if isinstance(media, dict):
        for key in ('images', 'photos', 'photo'):
            imgs = media.get(key)
            if isinstance(imgs, list) and imgs:
                urls = []
                for img in imgs:
                    if isinstance(img, str):
                        urls.append(img)
                    elif isinstance(img, dict):
                        url = (
                            img.get('url')
                            or img.get('media_url_https')
                            or img.get('media_url')
                            or img.get('originalUrl')
                        )
                        if url:
                            urls.append(url)
                if urls:
                    return urls

    legacy = obj.get('legacy') or {}
    if isinstance(legacy, dict):
        entities = legacy.get('extended_entities') or legacy.get('entities') or {}
        if isinstance(entities, dict):
            media_list = entities.get('media', [])
            urls = [
                m.get('media_url_https') or m.get('media_url')
                for m in media_list
                if isinstance(m, dict) and m.get('type') in (None, 'photo')
            ]
            return [u for u in urls if u]

    return []

# =====================================================================
# DISPLAY TWEET
# =====================================================================
def display_tweet(author, tweet_id, tweet_text, images, tweet_type="TWEET"):
    global tweet_count
    tweet_count += 1

    now  = datetime.now().strftime("%H:%M:%S")
    url  = f"https://x.com/{author}/status/{tweet_id}" if tweet_id else f"https://x.com/{author}"
    text_clean = re.sub(r'https?://t\.co/\S+', '', tweet_text).strip() or tweet_text

    # Deteksi Contract Address (Solana / EVM)
    detected_cas = find_crypto_addresses(text_clean)

    # ── Header ────────────────────────────────────────────────────
    print(f"\n{Fore.MAGENTA}{Style.BRIGHT}{'─' * 60}")
    print(f"{Fore.YELLOW}{Style.BRIGHT}  🐦 [{now}] #{tweet_count} — {tweet_type}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}{'─' * 60}{Style.RESET_ALL}")

    # ── Author ────────────────────────────────────────────────────
    print(f"  {Fore.CYAN}{Style.BRIGHT}AUTHOR  {Style.RESET_ALL}: @{author}")

    # ── Teks ──────────────────────────────────────────────────────
    words   = text_clean.split()
    lines   = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > 55:
            if current:
                lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)

    if lines:
        print(f"  {Fore.WHITE}{Style.BRIGHT}CAPTION {Style.RESET_ALL}: {lines[0]}")
        for line in lines[1:]:
            print(f"            {line}")
    else:
        print(f"  {Fore.WHITE}{Style.BRIGHT}CAPTION {Style.RESET_ALL}: (kosong)")

    # ── Deteksi CA (jika ditemukan) ──────────────────────────────
    if detected_cas:
        print(f"  {Fore.YELLOW}{Style.BRIGHT}DETECTED CA{Style.RESET_ALL}: {', '.join(detected_cas)}")

    # ── Gambar ────────────────────────────────────────────────────
    if images:
        print(f"  {Fore.GREEN}{Style.BRIGHT}GAMBAR  {Style.RESET_ALL}: {len(images)} file")
        for i, img_url in enumerate(images, 1):
            print(f"    [{i}] {Fore.BLUE}{img_url}{Style.RESET_ALL}")
    else:
        print(f"  {Fore.RED}GAMBAR  {Style.RESET_ALL}: tidak ada")

    # ── URL Tweet ─────────────────────────────────────────────────
    print(f"  {Fore.CYAN}URL     {Style.RESET_ALL}: {Fore.BLUE}{url}{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}{'─' * 60}{Style.RESET_ALL}")

    # Kirim Alert ke Telegram (jika diisi)
    send_telegram_alert(author, text_clean, url, detected_cas, tweet_type)

# =====================================================================
# WEBSOCKET HANDLER
# =====================================================================
def on_message(ws, message):
    global CURRENT_SESSION_ID, SEEN_TWEETS, SEEN_TWEETS_QUEUE

    if not isinstance(message, bytes):
        return

    try:
        data = msgpack.unpackb(message, strict_map_key=False)

        # ── Heartbeat ────────────────────────────────────────────
        if data == [3]:
            return ws.send(msgpack.packb([3]), opcode=websocket.ABNF.OPCODE_BINARY)

        # ── Session ID → subscribe ────────────────────────────────
        if len(data) >= 2 and data[0] == 2:
            CURRENT_SESSION_ID = data[1]
            ws.send(
                msgpack.packb([
                    8, 125,
                    "/twitter/watched/v2/subscribed",
                    str(uuid.uuid4()),
                    {
                        "uid": CURRENT_SESSION_ID,
                        "accounts": [{"refType": "handle", "ref": u} for u in TARGET_USERNAMES]
                    }
                ]),
                opcode=websocket.ABNF.OPCODE_BINARY
            )

        # ── Subscribe konfirmasi → aktifkan feed ─────────────────
        elif len(data) >= 4 and data[0] == 9 and data[1] == 125 and data[3].get("accounts"):
            ws.send(
                msgpack.packb([
                    4, 33,
                    f"/twitter/tweet/subscribe-feed/v3/{CURRENT_SESSION_ID}"
                    f"?encodedCategoryFilters=&onlySubscribedAccounts=1"
                ]),
                opcode=websocket.ABNF.OPCODE_BINARY
            )
            print(f"\n{Fore.GREEN}{Style.BRIGHT}[+] Padre.gg siap! Radar Twitter aktif. "
                  f"Memantau {len(TARGET_USERNAMES)} akun...{Style.RESET_ALL}\n")

        # ── Tweet masuk ──────────────────────────────────────────
        elif len(data) >= 3 and data[0] == 5:
            updates = (
                data[2].get('update', {}).get('updates', [])
                if isinstance(data[2], dict) else []
            )

            for item in updates:
                tweet = item.get('tweet', {})
                if not tweet.get('id') or tweet['id'] in SEEN_TWEETS:
                    continue

                # Cek & Batasi ukuran SEEN_TWEETS agar hemat RAM
                SEEN_TWEETS.add(tweet['id'])
                SEEN_TWEETS_QUEUE.append(tweet['id'])
                if len(SEEN_TWEETS) > 5000:
                    SEEN_TWEETS = set(SEEN_TWEETS_QUEUE)

                original = (
                    tweet.get('subtweet')
                    or tweet.get('retweeted_tweet')
                    or tweet.get('retweeted_status')
                    or tweet.get('retweetedTweet')
                )

                if original and isinstance(original, dict):
                    src_obj    = original
                    author     = (original.get('author') or {}).get('handle', 'Unknown')
                    tweet_id   = original.get('id') or tweet.get('id', '')
                    tweet_type = "RETWEET"
                else:
                    src_obj    = tweet
                    author     = (tweet.get('author') or {}).get('handle', 'Unknown')
                    tweet_id   = tweet.get('id', '')
                    tweet_type = "TWEET"

                tweet_text = extract_text(src_obj) or extract_text(tweet)
                images     = extract_images(src_obj) or extract_images(tweet)

                if tweet.get('quoted') and not original:
                    tweet_type = "QUOTE TWEET"

                if not tweet_text or not tweet_text.strip():
                    raw_keys = list(tweet.keys()) if isinstance(tweet, dict) else []
                    body_raw = str(tweet.get('body', 'NONE'))[:80]
                    print(f"{Fore.RED}⚠️ [DEBUG] Tweet dari @{author} tidak bisa di-parse! "
                          f"Keys: {raw_keys} | body: {body_raw}{Style.RESET_ALL}")
                    continue

                display_tweet(author, tweet_id, tweet_text, images, tweet_type)

    except Exception as e:
        print(f"{Fore.RED}[-] Error on_message: {type(e).__name__} - {str(e)[:100]}{Style.RESET_ALL}")


def on_error(ws, error):
    print(f"{Fore.RED}[-] WebSocket error: {error}{Style.RESET_ALL}")


def on_close(ws, close_status_code, close_msg):
    global FRESH_ID_TOKEN
    FRESH_ID_TOKEN = None  # Paksa refresh token saat reconnect
    print(f"{Fore.YELLOW}[!] WebSocket ditutup. Reconnecting & menyegarkan token...{Style.RESET_ALL}")

# =====================================================================
# MAIN LOOP
# =====================================================================
def run():
    global FRESH_ID_TOKEN, DYNAMIC_REFRESH_TOKEN

    last_refresh    = 0
    REFRESH_INTERVAL = 50 * 60  # 50 menit

    while True:
        now = time.time()

        if not DYNAMIC_REFRESH_TOKEN:
            print(f"{Fore.CYAN}[*] Login ke Padre.gg...{Style.RESET_ALL}")
            DYNAMIC_REFRESH_TOKEN = create_wallet_and_login()
            if not DYNAMIC_REFRESH_TOKEN:
                print(f"{Fore.RED}[-] Login gagal. Coba lagi 10 detik...{Style.RESET_ALL}")
                time.sleep(10)
                continue
            print(f"{Fore.GREEN}[+] Login berhasil!{Style.RESET_ALL}")

        if not FRESH_ID_TOKEN or (now - last_refresh > REFRESH_INTERVAL):
            print(f"{Fore.CYAN}[*] Refresh Firebase ID Token...{Style.RESET_ALL}")
            new_token = refresh_firebase_token(DYNAMIC_REFRESH_TOKEN)
            if new_token:
                FRESH_ID_TOKEN = new_token
                last_refresh   = now
                print(f"{Fore.GREEN}[+] Token berhasil di-refresh.{Style.RESET_ALL}")
            else:
                print(f"{Fore.RED}[-] Gagal refresh token. Coba lagi 10 detik...{Style.RESET_ALL}")
                time.sleep(10)
                continue

        # Buka WebSocket
        def on_open(w):
            def heartbeat():
                # Menggunakan parameter 'w' yang aktif untuk mengecek status koneksi
                while w.sock and w.sock.connected:
                    try:
                        time.sleep(15)  # Ping per 15 detik agar koneksi awet
                        w.send(msgpack.packb([3]), opcode=websocket.ABNF.OPCODE_BINARY)
                    except Exception:
                        break

            threading.Thread(target=heartbeat, daemon=True).start()

            try:
                w.send(
                    msgpack.packb([1, FRESH_ID_TOKEN, "d-def66a-85f9"]),
                    opcode=websocket.ABNF.OPCODE_BINARY
                )
            except Exception as e:
                print(f"{Fore.RED}[-] Gagal kirim auth: {e}{Style.RESET_ALL}")

        ws = websocket.WebSocketApp(
            "wss://backend3.padre.gg/_heavy_multiplex?desc=%2Ftracker",
            header=[
                "Origin: https://trade.padre.gg",
                "Cache-Control: no-cache",
                "User-Agent: Mozilla/5.0"
            ],
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        ws.run_forever()
        time.sleep(5)


if __name__ == "__main__":
    print(f"{Fore.MAGENTA}{Style.BRIGHT}{'=' * 60}")
    print(f"         TWITTER SCRAP - WEBSOCKETS BY BENY (OPTIMIZED)")
    print(f"{'=' * 60}{Style.RESET_ALL}\n")

    if not TARGET_USERNAMES:
        print(f"{Fore.RED}[-] Tidak ada target! Isi targets.txt dulu lalu jalankan ulang.{Style.RESET_ALL}")
        exit(1)

    try:
        run()
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}[!] Bot dihentikan. Total tweet: {tweet_count}{Style.RESET_ALL}")