"""Groq adapter tests. Nothing here calls Groq: the network is replaced, so no free-tier tokens are used."""

import io
import json
import tempfile
import unittest
import urllib.error

from agent.groq_client import GroqClient, from_openai, parse_duration, to_openai
from agent.run_agent import build_messages, request_kwargs, run, step_cost
from tests.test_agent import FakeEngine, tiny_jpeg, usage


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload, self.headers = payload, headers or {}

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def reply(tool="turn", args=None, text="I see a shape ahead."):
    message = {"role": "assistant", "content": text}
    if tool:
        message["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": tool, "arguments": json.dumps(args or {"degrees": 45})}}]
    return {"choices": [{"message": message, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 3200, "completion_tokens": 40}}


def http_error(code, headers=None, body=b'{"error":"slow down"}'):
    return urllib.error.HTTPError("https://api.groq.com", code, "error", headers or {}, io.BytesIO(body))


class Recorder:
    """Stands in for urlopen: replays queued outcomes and records requests."""

    def __init__(self, outcomes):
        self.outcomes, self.requests = list(outcomes), []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class GroqAdapterTests(unittest.TestCase):
    def test_request_translation_keeps_one_image_and_pairs_tool_results(self):
        previous = {"id": "toolu_1", "name": "turn", "input": {"degrees": 45}, "result": {"ok": True, "heading": 45}}
        messages = build_messages("Fetch it", tiny_jpeg(), 1.2, {"holding": False, "retry_count": 0}, ["1. turn(degrees=45) -> {}"], 2, previous)
        body = to_openai(request_kwargs("qwen/qwen3.8-27b", messages))
        roles = [m["role"] for m in body["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "user"])
        self.assertEqual(body["messages"][2]["tool_calls"][0]["function"]["name"], "turn")
        self.assertEqual(body["messages"][3]["tool_call_id"], "toolu_1")
        self.assertIn('"heading":45', body["messages"][3]["content"])
        images = [p for p in body["messages"][4]["content"] if p["type"] == "image_url"]
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertNotIn("betas", body)
        self.assertNotIn("output_config", body)

    def test_reply_translation_and_bad_arguments(self):
        parsed = from_openai(reply(text="<think>hidden</think>Turning to search."))
        self.assertEqual([b.type for b in parsed.content], ["text", "tool_use"])
        self.assertEqual(parsed.content[0].text, "Turning to search.")
        self.assertEqual(parsed.content[1].input, {"degrees": 45})
        self.assertEqual(parsed.usage.input_tokens, 3200)
        broken = reply()
        broken["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
        self.assertEqual(from_openai(broken).content[-1].input, {})
        self.assertEqual(step_cost("qwen/qwen3.8-27b", usage(input_tokens=10 ** 6)), 0.0)

    def test_durations(self):
        self.assertAlmostEqual(parse_duration("2m59.5s"), 179.5)
        self.assertAlmostEqual(parse_duration("120ms"), 0.12)
        self.assertAlmostEqual(parse_duration("7"), 7.0)
        self.assertEqual(parse_duration(None), 0.0)


class GroqPacingTests(unittest.TestCase):
    def make(self, outcomes):
        self.now, self.sleeps = 100.0, []

        def sleep(seconds):
            self.sleeps.append(round(seconds, 2))
            self.now += seconds

        opener = Recorder(outcomes)
        client = GroqClient("test-key", opener=opener, sleep=sleep, clock=lambda: self.now, say=lambda line: None)
        return client, opener

    def kwargs(self):
        return request_kwargs("qwen/qwen3.8-27b", [{"role": "user", "content": [{"type": "text", "text": "x"}]}])

    def test_minimum_gap_between_calls(self):
        client, _ = self.make([FakeResponse(reply()), FakeResponse(reply())])
        client.create(**self.kwargs())
        client.create(**self.kwargs())
        self.assertEqual(self.sleeps, [2.5])

    def test_waits_for_the_token_window_before_a_call_that_would_not_fit(self):
        low = {"x-ratelimit-remaining-tokens": "1200", "x-ratelimit-reset-tokens": "22s", "x-ratelimit-remaining-requests": "900"}
        client, _ = self.make([FakeResponse(reply(), low), FakeResponse(reply())])
        client.create(**self.kwargs())
        client.create(**self.kwargs())
        self.assertIn(22.5, self.sleeps)

    def test_429_waits_retry_after_then_succeeds(self):
        client, opener = self.make([http_error(429, {"retry-after": "12"}), FakeResponse(reply())])
        result = client.create(**self.kwargs())
        self.assertEqual(result.content[-1].name, "turn")
        self.assertIn(12.0, self.sleeps)
        self.assertEqual(len(opener.requests), 2)

    def test_gives_up_after_three_retries_and_other_errors_fail_at_once(self):
        client, opener = self.make([http_error(429, {"retry-after": "1"})] * 4)
        with self.assertRaises(RuntimeError):
            client.create(**self.kwargs())
        self.assertEqual(len(opener.requests), 4)
        client, opener = self.make([http_error(400, body=b'{"error":"bad request"}')])
        with self.assertRaisesRegex(RuntimeError, "Groq HTTP 400"):
            client.create(**self.kwargs())
        self.assertEqual(len(opener.requests), 1)

    def test_invalid_tool_call_from_the_model_is_a_wasted_step_not_a_crash(self):
        body = json.dumps({"error": {"message": "tool call validation failed: missing properties: 'degrees'", "code": "tool_use_failed",
                                     "failed_generation": "I will turn to search."}}).encode()
        client, opener = self.make([http_error(400, body=body), FakeResponse(reply("drop", {}))])
        with tempfile.TemporaryDirectory() as directory:
            summary = run("Fetch it", client, "qwen/qwen3.8-27b", FakeEngine(), directory, 5)
        self.assertEqual((summary["outcome"], summary["step_count"]), ("pass", 2))
        second = json.loads(opener.requests[1].data)
        self.assertIn("required arguments", json.dumps(second["messages"]))

    def test_daily_allowance_exhausted_stops_without_calling(self):
        spent = {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "3h"}
        client, opener = self.make([FakeResponse(reply(), spent)])
        client.create(**self.kwargs())
        with self.assertRaisesRegex(RuntimeError, "daily request allowance"):
            client.create(**self.kwargs())
        self.assertEqual(len(opener.requests), 1)

    def test_key_is_sent_as_bearer_and_agent_loop_runs_end_to_end(self):
        client, opener = self.make([FakeResponse(reply("turn", {"degrees": 45})), FakeResponse(reply("drop", {}))])
        with tempfile.TemporaryDirectory() as directory:
            summary = run("Fetch it", client, "qwen/qwen3.8-27b", FakeEngine(), directory, 5)
        self.assertEqual(summary["outcome"], "pass")
        self.assertEqual(summary["cost_estimate_usd"], 0)
        self.assertEqual(summary["run_number"], 0)          # Groq runs do not count toward the Claude budget
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer test-key")


if __name__ == "__main__":
    unittest.main()
