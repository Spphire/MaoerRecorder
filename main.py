import os
import time
import json
import threading
import subprocess
import signal
import requests
import websocket
import brotli
import re
import sys
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError

# ================= 配置 =================
ROOM_ID = 868802213
BASE_DIR = "recordings"

FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

MAX_NO_DATA_SECONDS = 60      # ffmpeg 60s 无写入才算死
MAX_WS_SILENT_SECONDS = 120  # WS 120s 无消息不直接判死

COOKIES = {
    "MSESSID": os.getenv("MAOER_MSESSID", ""),
    "FM_SESS": os.getenv("MAOER_FM_SESS", ""),
    "FM_SESS.sig": os.getenv("MAOER_FM_SESS_SIG", ""),
    "buvid3": os.getenv("MAOER_BUVID3", ""),
}
COOKIES = {k: v for k, v in COOKIES.items() if v}
COOKIE_STR = "; ".join(f"{k}={v}" for k, v in COOKIES.items())

HEADERS = [
    "User-Agent: Mozilla/5.0",
    "Origin: https://fm.missevan.com",
    f"Referer: https://fm.missevan.com/live/{ROOM_ID}",
    f"Cookie: {COOKIE_STR}"
]

os.makedirs(BASE_DIR, exist_ok=True)

# ================= 工具 =================
def sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', name)

def is_live_http():
    try:
        r = requests.get(
            f"https://fm.missevan.com/api/v2/live/{ROOM_ID}",
            headers={h.split(": ")[0]: h.split(": ")[1] for h in HEADERS},
            cookies=COOKIES,
            timeout=10
        )
        r.raise_for_status()
        info = r.json()["info"]
        live = info["room"]["status"]["broadcasting"]
        return live, info
    except Exception:
        return False, None

def parse_ws_frame(message: bytes):
    try:
        body = message[4:]
        data = brotli.decompress(body)
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None

# ================= Session =================
class RecordSession:
    def __init__(self, room_id, info, session_dir):
        self.room_id = room_id
        self.info = info
        self.session_dir = session_dir

        self.start_wall = time.time()
        self.start_mono = time.monotonic()

        self.ffmpeg_proc = None
        self.segment_index = 0

        self.last_audio_write = None
        self.last_ws_msg = None

        self.running = True

        self.chat_file = open(
            os.path.join(session_dir, "chat.jsonl"),
            "a", encoding="utf-8"
        )

    def stop(self, reason):
        if not self.running:
            return
        print(f"\n🛑 session stop: {reason}")
        self.running = False

        if self.ffmpeg_proc and self.ffmpeg_proc.poll() is None:
            self.ffmpeg_proc.terminate()

        self.chat_file.close()

# ================= ffmpeg =================
def start_ffmpeg(session, hls_url):
    session.segment_index += 1
    out_path = os.path.join(
        session.session_dir,
        f"audio_{session.segment_index:04d}.ts"
    )

    cmd = [
        FFMPEG_PATH,
        "-y",
        "-headers", "\r\n".join(HEADERS) + "\r\n",
        "-loglevel", "error",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-rw_timeout", "30000000",
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-i", hls_url,
        "-c:a", "aac",
        "-b:a", "128k",
        "-f", "mpegts",
        out_path
    ]

    print(f"🎧 start segment {session.segment_index}")

    session.ffmpeg_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    threading.Thread(
        target=ffmpeg_watchdog,
        args=(session, out_path),
        daemon=True
    ).start()

def ffmpeg_watchdog(session, out_path):
    last_size = 0
    last_grow = time.time()

    while session.running:
        proc = session.ffmpeg_proc

        if proc.poll() is not None:
            return

        if os.path.exists(out_path):
            size = os.path.getsize(out_path)
            if size > last_size:
                last_size = size
                last_grow = time.time()
                session.last_audio_write = time.time()

        if time.time() - last_grow > MAX_NO_DATA_SECONDS:
            print("⚠️ ffmpeg stalled")
            proc.terminate()
            return

        time.sleep(1)

# ================= WebSocket =================
def handle_ws_message(session, message):
    session.last_ws_msg = time.time()

    if isinstance(message, bytes):
        data = parse_ws_frame(message)
    else:
        try:
            data = json.loads(message)
        except Exception:
            return

    if not data:
        return

    if isinstance(data, dict) and data.get("type") == "message":
        try:
            show = f"{data['user']['username']}: {data['message'].splitlines()[0]}"
            print(f"\r⏳{show}", end="", flush=True)
        except Exception:
            pass

    session.chat_file.write(json.dumps({
        "t_audio": round(time.monotonic() - session.start_mono, 3),
        "t_wall": time.time(),
        "data": data
    }, ensure_ascii=False) + "\n")
    session.chat_file.flush()

# ================= 主流程 =================
def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        ws_connected = threading.Event()

        def on_ws(ws):
            if "im.missevan.com/ws" not in ws.url:
                return

            print("[WS] connected")
            ws_connected.set()

            ws.on("framereceived", lambda frame: handle_ws_message(session, frame))
            ws.on("close", lambda: ws_connected.clear())

        page.on("websocket", on_ws)

        session = None

        while True:
            live, info = is_live_http()

            if not session:
                if not live:
                    time.sleep(1)
                    continue

                creator = sanitize(info["room"]["creator_username"])
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                session_dir = os.path.join(BASE_DIR, f"{ROOM_ID}_{creator}", ts)
                os.makedirs(session_dir, exist_ok=True)

                session = RecordSession(ROOM_ID, info, session_dir)

                page.goto(f"https://fm.missevan.com/live/{ROOM_ID}", wait_until="load")
                start_ffmpeg(session, info["room"]["channel"]["hls_pull_url"])

                print("🔴 live started")

            else:
                now = time.time()

                audio_dead = (
                    session.last_audio_write
                    and now - session.last_audio_write > 120
                )

                ws_dead = (
                    session.last_ws_msg
                    and now - session.last_ws_msg > MAX_WS_SILENT_SECONDS
                )

                if audio_dead and not live:
                    session.stop("media drained")
                    merge_and_write_meta(session)
                    session = None

                time.sleep(0.5)

def merge_and_write_meta(session):
    concat = os.path.join(session.session_dir, "concat.txt")
    with open(concat, "w") as f:
        for i in range(1, session.segment_index + 1):
            f.write(f"file 'audio_{i:04d}.ts'\n")

    subprocess.run([
        FFMPEG_PATH,
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat,
        "-c", "copy",
        os.path.join(session.session_dir, "final.ts")
    ])

    with open(os.path.join(session.session_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({
            "room_id": session.room_id,
            "start_time": session.start_wall,
            "duration": round(time.monotonic() - session.start_mono, 2),
            "segments": session.segment_index
        }, f, ensure_ascii=False, indent=2)

    print("🎬 final.ts generated")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("⛔ stopped")
        sys.exit(0)
