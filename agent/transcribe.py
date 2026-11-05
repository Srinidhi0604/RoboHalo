"""Speech to text for the Unity panel's Speak button.

Unity records the microphone and hands the WAV to the agent process; this turns it into text.
1. Whisper on Groq (whisper-large-v3-turbo), if GROQ_API_KEY is set: accurate and free-tier.
2. Otherwise the offline Windows speech engine (System.Speech) reading the WAV file.

  python -m agent.transcribe recording.wav     # test on a file
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-large-v3-turbo"
# A short hint of the kind of sentence to expect. It describes the robot, not the scene.
HINT = "A spoken command to a fetch robot, for example: Bring me the round thing. Fetch the tallest object."


def whisper(wav, key, opener=urllib.request.urlopen):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("model", WHISPER_MODEL), ("language", "en"), ("response_format", "json"), ("temperature", "0"), ("prompt", HINT)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="command.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode() + wav + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(WHISPER_URL, data=b"".join(parts), headers={
        "Authorization": "Bearer " + key, "Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "robohalo-agent/1.0"})
    for attempt in range(3):
        try:
            with opener(request, timeout=60) as response:
                return json.loads(response.read()).get("text", "").strip()
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 2:
                time.sleep(min(float(error.headers.get("retry-after") or 5), 20))
                continue
            raise RuntimeError(f"Whisper HTTP {error.code}: {error.read()[:200].decode('utf-8', 'replace')}") from None
    return ""


WINDOWS_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$r = New-Object System.Speech.Recognition.SpeechRecognitionEngine
$r.SetInputToWaveFile('__FILE__')
$r.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$r.EndSilenceTimeout = [TimeSpan]::FromSeconds(0.8)
$res = $r.Recognize()   # a command is one phrase; looping past the end of the file can hang
Write-Output ('TEXT|' + $(if ($res) { $res.Text } else { '' }))
$r.Dispose()
"""


def windows_offline(wav):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        handle.write(wav)
        path = handle.name
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_SCRIPT.replace("__FILE__", path.replace("'", "''"))],
                              capture_output=True, text=True, timeout=30)
        lines = [l for l in done.stdout.splitlines() if l.startswith("TEXT|")]
        return lines[-1][5:].strip() if lines else ""
    except subprocess.TimeoutExpired:
        return ""
    finally:
        os.unlink(path)


def transcribe(wav, say=print):
    """WAV bytes -> text ('' if no words could be made out)."""
    from agent.run_agent import load_env
    load_env()
    key = os.getenv("GROQ_API_KEY")
    if key:
        try:
            return whisper(wav, key)
        except Exception as error:
            say(f"Whisper failed ({error}); using the offline Windows recogniser.")
    return windows_offline(wav)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(transcribe(Path(sys.argv[1]).read_bytes()))
