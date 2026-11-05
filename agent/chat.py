"""Agent chat box: type a request, the robot does it.

  python -m agent.chat            # opens http://127.0.0.1:8090 in your browser

Two modes, chosen in the page:
- Fast (default): one small model call turns the request into basic commands and the robot's own
  planner drives. Works in seconds on the free tier. The model is told what is in the scene, so this
  is a demo mode, not a judged Phase 1 run.
- Camera only: the model drives step by step from camera images alone (the judged way). On the free
  tier this takes minutes and the free model often fails; it is the mode to use once Claude credits arrive.

The Unity panel's command box sends its text here too (POST /ask), so typing in Unity works the same.
"""

import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent import quick
from agent.run_agent import Engine, make_client, run

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RoboHalo agent</title><style>
:root{--bg:#f4f5f7;--card:#fff;--text:#1a1e26;--muted:#5f6878;--line:#dde1e8;--accent:#2563eb;--me:#2563eb;--ok:#1f8f4d;--bad:#c23b3b}
@media (prefers-color-scheme:dark){:root{--bg:#11141a;--card:#1a1f28;--text:#e8ebf1;--muted:#98a2b3;--line:#2b323f;--accent:#6ea0ff;--me:#3b6fe0;--ok:#4cc97f;--bad:#ff7a7a}}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,Segoe UI,Arial,sans-serif;display:flex;flex-direction:column}
header{padding:14px 18px;border-bottom:1px solid var(--line);background:var(--card)}
h1{font-size:17px;margin:0}header p{margin:2px 0 0;color:var(--muted);font-size:13px}
#log{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:8px;max-width:860px;width:100%;margin:0 auto}
.msg{padding:9px 13px;border-radius:12px;max-width:82%;white-space:pre-wrap;word-break:break-word}
.user{align-self:flex-end;background:var(--me);color:#fff;border-bottom-right-radius:4px}
.agent{align-self:flex-start;background:var(--card);border:1px solid var(--line);border-bottom-left-radius:4px}
.step{align-self:flex-start;color:var(--muted);font:13px/1.45 ui-monospace,Consolas,monospace;padding:0 13px}
.error{align-self:flex-start;color:var(--bad);padding:0 13px}.good{color:var(--ok);font-weight:600}
form{display:flex;gap:8px;padding:12px 18px 16px;border-top:1px solid var(--line);background:var(--card);flex-wrap:wrap;justify-content:center}
#text{flex:1;min-width:220px;max-width:640px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text);font:inherit}
select,button{padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--text);font:inherit}
button{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600;cursor:pointer}button:disabled{opacity:.5;cursor:default}
.hints{display:flex;gap:6px;flex-wrap:wrap;justify-content:center;padding:0 18px 8px;background:var(--card)}
.hints button{background:transparent;color:var(--accent);font-weight:500;padding:5px 10px;font-size:13px}
</style></head><body>
<header><h1>RoboHalo agent</h1><p id="status">Connecting...</p></header>
<div id="log" aria-live="polite"></div>
<div class="hints" id="hints"></div>
<form id="form"><input id="text" autocomplete="off" placeholder="Ask the robot, for example: bring me something to drink" aria-label="Request">
<select id="mode" aria-label="Mode"><option value="fast">Fast (demo)</option><option value="camera">Camera only (slow)</option></select>
<button id="send">Send</button></form>
<script>
const log=document.getElementById('log'),text=document.getElementById('text'),statusEl=document.getElementById('status'),send=document.getElementById('send');
let since=0;
function add(e){const d=document.createElement('div');d.className='msg '+e.role;if(e.role==='step'||e.role==='error')d.className=e.role;
 d.textContent=e.text;if(e.good)d.classList.add('good');log.appendChild(d);log.scrollTop=log.scrollHeight}
async function poll(){try{const r=await fetch('/events?since='+since);const j=await r.json();j.events.forEach(e=>{add(e);since=e.id});
 statusEl.textContent=j.status;send.disabled=j.busy}catch(e){statusEl.textContent='Agent server not reachable'}setTimeout(poll,700)}
async function ask(t){if(!t.trim())return;text.value='';await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({text:t,mode:document.getElementById('mode').value})})}
document.getElementById('form').addEventListener('submit',e=>{e.preventDefault();ask(text.value)});
['Bring the red cube','Bring me something to drink','Bring all the round things','Reset the scene','Turn off the lights and fetch the tallest object'].forEach(h=>{
 const b=document.createElement('button');b.type='button';b.textContent=h;b.onclick=()=>ask(h);document.getElementById('hints').appendChild(b)});
poll();text.focus();
</script></body></html>"""


class Agent:
    """Holds the chat log and runs one request at a time on a worker thread."""

    def __init__(self, client=None, model=None, engine=None, runs_dir="runs"):
        self.client, self.model, self.engine, self.runs_dir = client, model, engine or Engine(), runs_dir
        self.events, self.lock, self.busy = [], threading.Lock(), False
        self.jobs = []
        self.wake = threading.Event()
        threading.Thread(target=self._work, daemon=True).start()

    def emit(self, role, text, good=False):
        with self.lock:
            self.events.append({"id": len(self.events) + 1, "role": role, "text": str(text), "good": good})

    def ask(self, text, mode="fast"):
        text = (text or "").strip()
        if not text:
            return False
        self.emit("user", text)
        with self.lock:
            self.jobs.append((text, mode if mode in ("fast", "camera") else "fast"))
        self.wake.set()
        return True

    def status(self):
        try:
            state = self.engine.call("/state")
            robot = f"Robot: {state.get('name', '?')}" + (", holding something" if state.get("holding") else "")
        except Exception:
            robot = "Robot engine not reachable: press Play in Unity"
        return ("Working... " if self.busy else "Ready. ") + robot + f". Model: {self.model or 'not connected'}"

    def _connect(self):
        if self.client is None:
            self.client, self.model, _ = make_client()

    def _work(self):
        while True:
            self.wake.wait()
            while True:
                with self.lock:
                    if not self.jobs:
                        self.wake.clear()
                        break
                    text, mode = self.jobs.pop(0)
                self.busy = True
                try:
                    self._connect()
                    self.engine.call("/state")
                    if mode == "camera":
                        self._camera(text)
                    else:
                        self._fast(text)
                except SystemExit as error:      # missing API key
                    self.emit("error", str(error))
                except Exception as error:
                    refused = "refused" in str(error).lower() or "10061" in str(error)
                    self.emit("error", "The robot engine is not running. Press Play in Unity and try again." if refused else f"Something went wrong: {error}")
                finally:
                    self.busy = False

    def _fast(self, text):
        record = quick.handle(text, self.client, self.model, self.engine, runs_dir=self.runs_dir,
                              say=lambda line: self.emit("step", line.strip()))
        results = [r["result"] for r in record["results"]]
        if not record["steps"]:
            self.emit("agent", record["note"] or "I can't do that with this robot.")
        elif all(r.startswith(("pass", "done")) for r in results):
            self.emit("agent", "Done. " + (record["note"] or ""), good=True)
        else:
            self.emit("agent", "Finished, but not everything worked: " + "; ".join(f"{r['step']}: {r['result']}" for r in record["results"]))

    def _camera(self, text):
        self.emit("agent", "Camera-only run started. The model sees only camera images, one step at a time. On the free tier expect about two steps a minute.")

        def on_step(step):
            why = (step.get("reasoning") or "").strip()
            args = ", ".join(f"{k}={v}" for k, v in (step.get("args") or {}).items())
            self.emit("step", f"{step['step']}. {step.get('tool') or 'no tool'}({args})" + (f"  - {why[:140]}" if why else ""))

        summary = run(text, self.client, self.model, self.engine, runs_dir=self.runs_dir, max_steps=40, on_step=on_step)
        good = summary["outcome"] == "pass"
        self.emit("agent", f"{'Done' if good else 'Not completed'}: {summary['outcome']} after {summary['step_count']} steps. Replay: {summary['run_dir']}/replay.html", good=good)


def make_handler(agent):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path.startswith("/events"):
                try:
                    since = int(self.path.split("since=")[1]) if "since=" in self.path else 0
                except ValueError:
                    since = 0
                with agent.lock:
                    events = [e for e in agent.events if e["id"] > since]
                self._send(200, {"events": events, "busy": agent.busy, "status": agent.status()})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/ask":
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._send(400, {"error": "bad JSON"})
                return
            accepted = agent.ask(body.get("text"), body.get("mode", "fast"))
            self._send(200 if accepted else 400, {"ok": accepted})

    return Handler


def serve(agent, port=8090):
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(agent))


def main(argv=None):
    port = 8090
    agent = Agent()
    try:
        agent._connect()
    except SystemExit as error:
        print(error)
        return 1
    server = serve(agent, port)
    url = f"http://127.0.0.1:{port}"
    print(f"Agent chat box running at {url}  (Ctrl+C to stop)")
    agent.emit("agent", 'Hi. Tell me what to fetch or do, for example "bring me something to drink". I can also reset the scene, switch the lights, add the detour wall and change the camera.')
    if "--no-browser" not in (argv or sys.argv[1:]):
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
