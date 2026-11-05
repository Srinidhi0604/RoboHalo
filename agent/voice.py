"""Voice commands: say what to fetch, Claude drives the robot.

  python -m agent.voice                 # listen once, confirm the heard text, run
  python -m agent.voice --yes           # fully hands-free: run as soon as a command is heard clearly
  python -m agent.voice --loop          # keep taking commands (scene resets between them)
  python -m agent.voice --text "Bring the red cube"   # skip the microphone (testing)

Speech is recognised offline by the Windows speech engine (System.Speech), so no audio leaves the
machine and nothing extra is installed. The recognised sentence is passed to the Claude agent as the
run's command, exactly as if it had been typed.
"""

import argparse
import json
import subprocess
import sys

from agent.run_agent import Engine, add_provider_args, make_client, run_from_args

MIN_CONFIDENCE = 0.45
# ponytail: System.Speech free dictation is rough on unusual words; swap listen() for a Whisper
# model if commands are misheard too often.
_LISTEN_PS = r"""
Add-Type -AssemblyName System.Speech
$r = New-Object System.Speech.Recognition.SpeechRecognitionEngine
try { $r.SetInputToDefaultAudioDevice() } catch { Write-Output 'ERROR|no microphone found'; exit }
$r.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
Register-ObjectEvent -InputObject $r -EventName SpeechRecognized -SourceIdentifier heard | Out-Null
$r.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Single)
$e = $null; $level = 0
for ($i = 0; $i -lt (__WAIT__ * 4) -and -not $e; $i++) {
  $e = Wait-Event -SourceIdentifier heard -Timeout 0.25
  if ($r.AudioLevel -gt $level) { $level = $r.AudioLevel }
}
$r.RecognizeAsyncCancel()
if ($e) {
  $res = $e.SourceEventArgs.Result
  Write-Output ("{0}|{1}" -f $res.Confidence.ToString([Globalization.CultureInfo]::InvariantCulture), $res.Text)
} elseif ($level -eq 0) { Write-Output 'ERROR|no audio from the microphone (check it is plugged in, unmuted, and allowed for desktop apps in Windows privacy settings)' }
else { Write-Output 'NONE|' }
"""


def parse_heard(line):
    """'0.83|bring the red cube' -> (text, confidence); nothing heard -> (None, 0.0)."""
    head, _, text = line.strip().partition("|")
    if head == "ERROR":
        raise RuntimeError(text or "speech recognition failed")
    if head == "NONE" or not text.strip():
        return None, 0.0
    return text.strip(), float(head)


def listen(wait_seconds=8):
    """Block until one sentence is spoken (or the wait runs out). Returns (text or None, confidence)."""
    script = _LISTEN_PS.replace("__WAIT__", str(int(wait_seconds)))
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=wait_seconds + 20)
    lines = [l for l in done.stdout.splitlines() if "|" in l]
    if not lines:
        raise RuntimeError((done.stderr or "speech recognition produced no output").strip()[:300])
    return parse_heard(lines[-1])


def hear_command(listen_fn=listen, confirm=input, auto_yes=False, attempts=3, say=print):
    """Listen until a usable command is heard. Returns the command, or None if the user gives up."""
    for _ in range(attempts):
        say("Listening... say the command.")
        try:
            text, confidence = listen_fn()
        except RuntimeError as error:   # no microphone audio: fall back to typing
            say(f"Voice input unavailable: {error}")
            typed = "" if auto_yes else confirm("Type the command instead (or Enter to stop): ").strip()
            return typed or None
        if not text:
            say("Heard nothing.")
            continue
        say(f'Heard: "{text}"  (confidence {confidence:.2f})')
        if auto_yes:
            if confidence >= MIN_CONFIDENCE:
                return text
            say("Not clear enough, please repeat.")
            continue
        answer = confirm("Enter = run it, r = say it again, or type the correct command: ").strip()
        if answer == "":
            return text
        if answer.lower() != "r":
            return answer  # typed correction
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Speak a fetch command; the agent drives the robot.")
    parser.add_argument("--yes", action="store_true", help="run without asking for confirmation")
    parser.add_argument("--loop", action="store_true", help="keep taking commands until Ctrl+C")
    parser.add_argument("--text", help="use this text instead of the microphone")
    add_provider_args(parser)
    args = parser.parse_args(argv)
    make_client(args.provider, args.model)  # fail early if no key is configured
    engine = Engine()
    while True:
        command = args.text or hear_command(auto_yes=args.yes)
        if not command:
            print("No command heard.")
            return 1
        if args.loop:
            engine.call("/reset", {})
        print(f'Running: "{command}"')
        summary = run_from_args(command, args, engine)
        print(json.dumps(summary, indent=2))
        if not args.loop or args.text:
            return 0 if summary["outcome"] == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())
