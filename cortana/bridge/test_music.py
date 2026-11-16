"""Checks for music.py that need no Apple account: phrase parsing, token
signing (with a throwaway key) and the command relay to the player page."""
import asyncio
import tempfile
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import music

CASES = {
    "Play Abbey Road by The Beatles.": ("play", "abbey road", "the beatles", None),
    "play the album Rumours": ("play", "rumours", None, "album"),
    "Can you play some Miles Davis please?": ("play", "miles davis", None, None),
    "play songs by Taylor Swift": ("play", "taylor swift", None, "artist"),
    "Put on the playlist chill vibes on Apple Music": ("play", "chill vibes", None, "playlist"),
    "play Bohemian Rhapsody": ("play", "bohemian rhapsody", None, None),
    "Pause the music.": ("pause",), "stop": ("pause",), "Resume": ("resume",), "play": ("resume",),
    "Next song": ("next",), "skip this one": ("next",), "previous song": ("previous",),
    "What's playing?": ("nowplaying",), "what song is this": ("nowplaying",),
    "volume to 40 percent": ("volume",), "turn it up": ("volume",), "quieter": ("volume",),
    "What's the capital of France?": None, "tell me a joke": None, "how do you play chess": None,
    "what is two plus two": None, "stop listening": None,
}
bad = 0
for phrase, want in CASES.items():
    got = music.parse(phrase)
    if want is None:
        ok = got is None
    elif want[0] == "play":
        ok = bool(got) and (got["action"], got["query"], got["artist"], got["kind"]) == want
    else:
        ok = bool(got) and got["action"] == want[0]
    bad += not ok
    if not ok:
        print(f"PARSE MISMATCH {phrase!r}: {got}")
print(f"parse: {len(CASES) - bad}/{len(CASES)} phrases correct")

# token signing with a throwaway key
with tempfile.TemporaryDirectory() as tmp:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    (Path(tmp) / "AuthKey_TESTKEY123.p8").write_bytes(pem)
    (Path(tmp) / "team.txt").write_text("TEAMID1234\n")
    music.APPLE_DIR = Path(tmp)
    token = music.developer_token("http://localhost:8765")
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, key.public_key(), algorithms=["ES256"])
    assert header == {"alg": "ES256", "kid": "TESTKEY123", "typ": "JWT"}, header
    assert claims["iss"] == "TEAMID1234" and claims["origin"] == ["http://localhost:8765"], claims
    print(f"token: signed and verified (ES256, kid {header['kid']}, valid {(claims['exp'] - claims['iat']) // 3600}h)")


    async def relay() -> None:
        print("no player:", await music.try_handle("play abbey road"))
        gen = music.hub.events()
        await gen.__anext__()                      # a fake player page connects

        async def page() -> None:
            async for line in gen:
                if line.startswith("data:"):
                    import json
                    cmd = json.loads(line[5:])
                    fut = music.hub.pending[cmd["id"]]
                    fut.set_result({"ok": True, "text": f"Playing {cmd.get('query', cmd['action'])}."})

        task = asyncio.create_task(page())
        print("with player:", await music.try_handle("play abbey road by the beatles"))
        print("playing flag:", music.hub.playing)
        print("pause:", await music.try_handle("pause"), "| playing flag:", music.hub.playing)
        print("not music:", await music.try_handle("what's the weather"))
        task.cancel()

    asyncio.run(relay())
