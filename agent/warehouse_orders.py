"""Local Operations command bridge for the Warehouse scene.

Run from E:/ansu-hack: python -m agent.warehouse_orders
Unity POSTs to localhost:8091; credentials stay in this Python process.
"""

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from agent.run_agent import claude_client, claude_key, load_env

MODEL = "openai/gpt-oss-20b"
COLORS = ("red", "blue", "yellow", "brown")
COLOR_HINTS = {"red": r"red|crimson|scarlet|blood", "blue": r"blue|azure|sky|ocean",
               "yellow": r"yellow|gold|banana|sunshine", "brown": r"brown|chocolate|wood|earth"}
KINDS = ("crate", "flat carton", "tall carton", "box")
NUMBERS = {word: n for n, word in enumerate(
    ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"), 1)}
ACTION = re.compile(r"\b(bring|fetch|pick|collect|deliver|load|grab|get|take|retrieve|hand)\b", re.I)


def preset(text):
    """Resolve explicit orders without spending an API call; otherwise return None."""
    lower = text.lower()
    if not ACTION.search(lower):
        return None
    color = next((c for c in COLORS if re.search(rf"\b{c}\b", lower)), None)
    if re.search(r"\b(tall|tallest|large|big|high|narrow)\b", lower):
        kind = "tall carton"
    elif re.search(r"\b(flat|low|short|small|wide)\b", lower):
        kind = "flat carton"
    elif re.search(r"\b(crate|cube|square|wooden)\b", lower):
        kind = "crate"
    elif re.search(r"\b(carton|cardboard)\b", lower):
        kind = "flat carton"
    elif re.search(r"\b(box|boxes|block|blocks|package|parcel)\b", lower):
        kind = "box"
    else:
        kind = None
    if color is None and kind is None:
        return None
    number = re.search(r"\b\d+\b", lower)
    count = int(number.group()) if number else next((n for word, n in NUMBERS.items() if re.search(rf"\b{word}\b", lower)), 1)
    return {"command": f"fetch {min(max(count, 1), 20)} {color or ''} {kind or 'box'}".replace("  ", " "), "source": "preset"}


INSTRUCTIONS = "Interpret a warehouse operator's message. Reply with JSON only: {\"fetch\":boolean,\"count\":integer,\"color\":string,\"kind\":string}. Fetch is true only for a request to move cargo. Colors: red, blue, yellow, brown, or any. Kinds: crate (cube), flat carton (small/short), tall carton (large/high), box (any shape). Use count 1 if omitted. Do not invent unavailable colors or kinds."


def claude(text, client=None):
    client = client or claude_client()
    reply = client.messages.create(model=os.getenv("CLAUDE_ORDER_MODEL", "claude-haiku-5-5"), max_tokens=120,
                                   system=INSTRUCTIONS, messages=[{"role": "user", "content": text}])
    raw = next(block.text for block in reply.content if block.type == "text")  # skip thinking blocks
    return check(json.loads(raw[raw.index("{"):raw.rindex("}") + 1]), text, "claude")


def groq(text, opener=urlopen):
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("Groq key is not configured")
    body = {
        "model": os.getenv("GROQ_ORDER_MODEL", MODEL),
        "temperature": 0,
        "max_completion_tokens": 220,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": INSTRUCTIONS},
            {"role": "user", "content": text},
        ],
    }
    request = Request("https://api.groq.com/openai/v1/chat/completions",
                      data=json.dumps(body).encode(),
                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                               "User-Agent": "robohalo-agent/1.0"})
    with opener(request, timeout=12) as response:
        result = json.load(response)
    return check(json.loads(result["choices"][0]["message"]["content"]), text, "groq")


def check(item, text, source):
    """Turn the model's JSON into a Unity order, refusing anything outside the scene's stock."""
    if item.get("fetch") is not True:
        return {"error": "Please ask me to fetch a color or size of box."}
    color, kind = item.get("color"), item.get("kind")
    if color not in (*COLORS, "any") or kind not in KINDS:
        return {"error": "I could not identify a supported color and size."}
    if color != "any" and not re.search(rf"\b({COLOR_HINTS[color]})\b", text, re.I):
        color = "any"
    count = item.get("count", 1)
    if type(count) is not int or count < 1:
        return {"error": "Please give a valid quantity."}
    return {"command": f"fetch {min(count, 20)} {'' if color == 'any' else color + ' '}{kind}", "source": source}


def interpret(text, model=None):
    model = model or (claude if claude_key() else groq)
    text = (text or "").strip()
    if not text:
        return {"error": "Type a fetch request first."}
    if not ACTION.search(text):
        return {"error": "I am waiting for a fetch request; no crates were moved."}
    direct = preset(text)
    if direct:
        return direct
    try:
        return model(text)
    except Exception:
        return {"error": "AI is unavailable. Try a preset such as 'fetch a small blue box'."}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/interpret":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 1 or length > 4096:
                raise ValueError("Request is empty or too long")
            answer = interpret(json.loads(self.rfile.read(length))["text"])
            code = 200
        except (ValueError, KeyError, TypeError) as error:
            answer, code = {"error": str(error)}, 400
        data = json.dumps(answer).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


def main():
    load_env()
    with ThreadingHTTPServer(("127.0.0.1", 8091), Handler) as server:
        print("Warehouse Operations bridge listening on http://127.0.0.1:8091", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
