import os
import uvicorn

if __name__ == "__main__":
    reload = os.environ.get("DEV", "").lower() in ("1", "true")
    port = int(os.environ.get("PORT", "7345"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=reload)
