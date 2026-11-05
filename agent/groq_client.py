"""Groq adapter for plumbing tests (free tier). Judged runs must use Claude.

It exposes the same `client.beta.messages.create(**kwargs)` call the agent makes for Claude,
translates the request to Groq's OpenAI-compatible chat format, and translates the reply back,
so the agent loop is identical for both providers.

Staying inside the free limits:
- at least MIN_INTERVAL seconds between calls
- reads Groq's rate-limit headers after every call and waits for the per-minute token window to
  refill before a call that would not fit (an image alone costs 2,048 input tokens)
- on HTTP 429, waits the time Groq asks for (retry-after) and retries, at most MAX_RETRIES times
- stops with a clear error when the daily request allowance is used up
"""

import json
import re
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "qwen/qwen3.8-27b"   # vision + tool use on Groq (checked against the live model list)
MIN_INTERVAL = 2.5                # seconds between calls
STEP_TOKENS = 4000                # one step measured at ~3,560 tokens (image 2,048 + prompt + reply), plus history growth
MAX_RETRIES = 3
MAX_WAIT = 90.0


def parse_duration(text):
    """Groq reset headers look like '7.66s', '2m59.56s' or '120ms'. Returns seconds."""
    if not text:
        return 0.0
    total = 0.0
    for value, unit in re.findall(r"([\d.]+)(ms|h|m|s)", str(text)):
        total += float(value) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
    if total == 0.0:
        try:
            total = float(text)
        except ValueError:
            pass
    return total


def to_openai(kwargs):
    """Anthropic-style request -> OpenAI-style chat request."""
    system = "".join(block["text"] for block in kwargs["system"])
    system += " Every tool call must include all of its required arguments."
    messages = [{"role": "system", "content": system}]
    for message in kwargs["messages"]:
        blocks = message["content"] if isinstance(message["content"], list) else [{"type": "text", "text": message["content"]}]
        if message["role"] == "assistant":
            calls = [b for b in blocks if b["type"] == "tool_use"]
            messages.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}} for b in calls]})
            continue
        parts = []
        for block in blocks:
            if block["type"] == "tool_result":
                inner = block["content"] if isinstance(block["content"], list) else [{"type": "text", "text": str(block["content"])}]
                text = "\n".join(b["text"] for b in inner if b["type"] == "text")
                messages.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": text})
                images = [b for b in inner if b["type"] == "image"]
                if images:  # tool messages cannot carry images in this format: send the frame right after
                    parts.append({"type": "text", "text": "Camera image after that action:"})
                    parts.extend(_image(b) for b in images)
            elif block["type"] == "image":
                parts.append(_image(block))
            else:
                parts.append({"type": "text", "text": block["text"]})
        if parts:
            messages.append({"role": "user", "content": parts})
    body = {"model": kwargs["model"], "messages": messages, "max_tokens": min(int(kwargs.get("max_tokens", 800)), 800), "temperature": 0.2}
    if kwargs.get("tools"):
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
                         for t in kwargs["tools"]]
        body["tool_choice"] = "auto"
    return body


def _image(block):
    source = block["source"]
    return {"type": "image_url", "image_url": {"url": f"data:{source['media_type']};base64,{source['data']}"}}


def from_openai(data):
    """OpenAI-style reply -> the block/usage shape the agent reads from Claude."""
    choice = data["choices"][0]
    message = choice["message"]
    content = []
    text = re.sub(r"<think>.*?</think>", "", message.get("content") or "", flags=re.S).strip()
    if text:
        content.append(SimpleNamespace(type="text", text=text[:1500]))
    for call in (message.get("tool_calls") or [])[:1]:  # one tool call per step
        try:
            arguments = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        content.append(SimpleNamespace(type="tool_use", id=call["id"], name=call["function"]["name"],
                                       input=arguments if isinstance(arguments, dict) else {}))
    usage = data.get("usage") or {}
    return SimpleNamespace(
        content=content,
        stop_reason="tool_use" if any(b.type == "tool_use" for b in content) else choice.get("finish_reason"),
        usage=SimpleNamespace(input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                              cache_creation_input_tokens=0, cache_read_input_tokens=0))


def _failed_tool_call(raw):
    """Text of the model's attempt when Groq rejects its tool call, else None."""
    try:
        error = json.loads(raw).get("error", {})
    except (ValueError, AttributeError):
        return None
    if not isinstance(error, dict) or error.get("code") != "tool_use_failed":
        return None
    return "Invalid tool call: " + str(error.get("message", ""))[:160] + " | " + str(error.get("failed_generation", ""))


class GroqClient:
    def __init__(self, api_key, opener=urllib.request.urlopen, sleep=time.sleep, clock=time.time,
                 min_interval=MIN_INTERVAL, say=print, url=GROQ_URL):
        self.url = url   # any OpenAI-compatible chat endpoint (Groq by default)
        self.api_key, self.opener, self.sleep, self.clock, self.min_interval, self.say = api_key, opener, sleep, clock, min_interval, say
        self.last_call = None
        self.limits = {}     # latest rate-limit headers, for the run summary
        self.calls = 0
        self.step_tokens = STEP_TOKENS   # tokens the next call is expected to need
        self.beta = SimpleNamespace(messages=self)
        self.messages = self

    def _pace(self):
        if self.last_call is not None:
            gap = self.min_interval - (self.clock() - self.last_call)
            if gap > 0:
                self.sleep(gap)
        if self.limits.get("remaining_requests") == 0:
            raise RuntimeError("Groq daily request allowance is used up; try again after it resets "
                               f"({self.limits.get('reset_requests', 'see Groq console')}).")
        remaining = self.limits.get("remaining_tokens")
        if remaining is not None and remaining < self.step_tokens:
            wait = min(parse_duration(self.limits.get("reset_tokens")) + 0.5, MAX_WAIT)
            self.say(f"Groq token window low ({remaining} left): waiting {wait:.0f}s for it to refill.")
            self.sleep(wait)

    def _remember(self, headers):
        def number(name):
            value = headers.get(name)
            return int(float(value)) if value not in (None, "") else None
        self.limits = {"remaining_requests": number("x-ratelimit-remaining-requests"),
                       "remaining_tokens": number("x-ratelimit-remaining-tokens"),
                       "limit_tokens": number("x-ratelimit-limit-tokens"),
                       "reset_tokens": headers.get("x-ratelimit-reset-tokens"),
                       "reset_requests": headers.get("x-ratelimit-reset-requests")}

    def create(self, **kwargs):
        body = json.dumps(to_openai(kwargs)).encode()
        for attempt in range(MAX_RETRIES + 1):
            self._pace()
            request = urllib.request.Request(self.url, data=body, headers={
                "Authorization": "Bearer " + self.api_key, "Content-Type": "application/json", "User-Agent": "robohalo-agent/1.0"})
            self.last_call = self.clock()
            self.calls += 1
            try:
                with self.opener(request, timeout=60) as response:
                    self._remember(response.headers)
                    return from_openai(json.loads(response.read()))
            except urllib.error.HTTPError as error:
                raw = error.read()
                detail = raw[:300].decode("utf-8", "replace")
                if error.headers:
                    self._remember(error.headers)
                failed = _failed_tool_call(raw) if error.code == 400 else None
                if failed is not None:
                    # The model wrote an invalid tool call (e.g. a missing argument). That is a wasted
                    # step, not a crash: hand the agent a reply with no tool call so it asks again.
                    return SimpleNamespace(content=[SimpleNamespace(type="text", text=failed[:300])], stop_reason="tool_use_failed",
                                           usage=SimpleNamespace(input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0))
                if error.code == 429 and attempt < MAX_RETRIES:
                    wait = min(parse_duration(error.headers.get("retry-after")) or 20.0, MAX_WAIT)
                    self.say(f"Groq rate limit hit: waiting {wait:.0f}s (retry {attempt + 1} of {MAX_RETRIES}).")
                    self.sleep(wait)
                    continue
                raise RuntimeError(f"Groq HTTP {error.code}: {detail}") from None
        raise RuntimeError("Groq rate limit: gave up after retries")
