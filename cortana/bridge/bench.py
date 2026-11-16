"""Time each bridge stage with a synthesized request (no speaker or mic needed)."""
import time

import httpx

http = httpx.Client(base_url="http://127.0.0.1:8765", timeout=120)
q = http.post("/v1/speak", json={"text": "Hey Cortana, what is the tallest mountain in the world?"}).content
for run in range(3):
    t0 = time.perf_counter()
    text = http.post("/v1/transcribe", params={"hotwords": "Cortana"}, content=q).json()["text"]
    t1 = time.perf_counter()
    reply = http.post("/v1/ask", json={"text": text, "session": "bench"}).json()["reply"]
    t2 = time.perf_counter()
    wav = http.post("/v1/speak", json={"text": reply}).content
    t3 = time.perf_counter()
    print(f"run {run + 1}: transcribe {t1 - t0:.2f}s  qwen {t2 - t1:.2f}s  voice {t3 - t2:.2f}s  "
          f"total {t3 - t0:.2f}s | heard: {text!r} | reply: {reply!r}")
http.post("/v1/reset", json={"session": "bench"})
