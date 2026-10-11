"""启动入口：起 FastAPI 服务并自动打开浏览器。"""
import os
import sys
import threading
import time
import webbrowser

import uvicorn

HOST = "127.0.0.1"
PORT = 8001
URL = f"http://{HOST}:{PORT}"


def _open_browser():
    time.sleep(1.2)
    webbrowser.open(URL)


if __name__ == "__main__":
    no_browser = os.environ.get("BILI_NOTE_NO_BROWSER") == "1" or "--no-browser" in sys.argv
    if not no_browser:
        threading.Thread(target=_open_browser, daemon=True).start()
    print(f"bili-note 已启动 → {URL}")
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning")
