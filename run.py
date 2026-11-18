import webbrowser

import uvicorn

if __name__ == "__main__":
    port = 8765
    webbrowser.open(f"http://127.0.0.1:{port}")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")
