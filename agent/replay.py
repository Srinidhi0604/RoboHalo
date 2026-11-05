"""Run replay viewer: one self-contained replay.html per run folder.

Shows, step by step, the frame Claude saw, its one-line reasoning, the tool call and the result,
so a reviewer can trace every decision to pixels.

Usage: python -m agent.replay runs/<run folder>      (written automatically after each Claude run)
       python -m agent.replay --all                   (every run folder under runs/ that has a log)
"""

import html
import json
import sys
from pathlib import Path

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f27;--muted:#5d6675;--line:#dfe3ea;--ok:#1f8f4d;--bad:#c23b3b;--warn:#b7791f;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--text:#e8ebf1;--muted:#9aa4b5;--line:#2c3340;--ok:#4cc97f;--bad:#ff7a7a;--warn:#f0b455;--accent:#6ea0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,Segoe UI,Arial,sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:22px;margin:0 0 4px}.cmd{font-size:18px;margin:6px 0 14px}
.facts{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}
.fact{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 12px}
.fact b{display:block;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
.pass{color:var(--ok);font-weight:700}.fail{color:var(--bad);font-weight:700}
.step{display:grid;grid-template-columns:minmax(0,340px) minmax(0,1fr);gap:16px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px}
.step.bump{border-left:5px solid var(--warn)}
.step img{width:100%;border-radius:6px;display:block;background:#000}
.noimg{border:1px dashed var(--line);border-radius:6px;padding:28px 12px;text-align:center;color:var(--muted);font-size:13px}
.num{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
.why{margin:4px 0 10px}.why.none{color:var(--muted);font-style:italic}
code{font:13px/1.45 ui-monospace,Consolas,monospace;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;display:block;white-space:pre-wrap;word-break:break-word;margin-bottom:6px}
.tool{color:var(--accent);font-weight:700}.meta{font-size:12px;color:var(--muted)}
video{width:100%;max-width:640px;border-radius:8px;margin-bottom:18px;background:#000}
@media (max-width:700px){.step{grid-template-columns:1fr}}
"""


def _esc(value):
    return html.escape(str(value))


def write_replay(run_dir):
    run_dir = Path(run_dir)
    lines = [json.loads(line) for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    steps = [entry for entry in lines if "step" in entry]
    summary = next((entry["summary"] for entry in lines if "summary" in entry), {})
    outcome = summary.get("outcome", "unfinished")
    facts = [("Outcome", f'<span class="{"pass" if outcome == "pass" else "fail"}">{_esc(outcome)}</span>'),
             ("Steps", _esc(summary.get("step_count", len(steps)))),
             ("Collisions", _esc(summary.get("retries", "-"))),
             ("Model", _esc(summary.get("model", summary.get("mode", "-")))),
             ("Cost", "$" + format(summary["cost_estimate_usd"], ".3f") if summary.get("cost_estimate_usd") is not None else "-"),
             ("Images sent", str(sum(1 for s in steps if s.get("frame_path"))))]
    parts = ["<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
             f"<title>Run replay</title><style>{CSS}</style></head><body><main>",
             "<h1>Run replay</h1>", f"<div class='cmd'>Command: <b>{_esc(summary.get('command', '(not recorded)'))}</b></div>",
             "<div class='facts'>" + "".join(f"<div class='fact'><b>{name}</b>{value}</div>" for name, value in facts) + "</div>"]
    if (run_dir / "clip.mp4").exists():
        parts.append("<video controls preload='metadata' src='clip.mp4'></video>")
    for step in steps:
        result = step.get("result") or {}
        bumped = bool(result.get("collided"))
        frame = step.get("frame_path")
        image = (f"<img loading='lazy' src='{_esc(frame)}' alt='Camera frame Claude saw at step {step['step']}'>" if frame
                 else "<div class='noimg'>No new image this step<br>(follows pickup, drop or a failed action)</div>")
        why = step.get("reasoning") or ""
        args = ", ".join(f"{k}={v}" for k, v in (step.get("args") or {}).items())
        meta = []
        if "depth_m" in step:
            meta.append("depth " + ("none" if step["depth_m"] is None else f"{step['depth_m']:.2f} m"))
        state = step.get("state") or {}
        if state:
            meta.append("holding" if state.get("holding") else "not holding")
            meta.append(f"collisions {state.get('retries', 0)}/3")
        if step.get("cost_usd"):
            meta.append(f"${step['cost_usd']:.4f}")
        parts.append(
            f"<section class='step{' bump' if bumped else ''}'><div>{image}</div><div>"
            f"<div class='num'>Step {step['step']}{' · collision' if bumped else ''}</div>"
            f"<div class='why{'' if why else ' none'}'>{_esc(why) if why else 'No reasoning text recorded.'}</div>"
            f"<code><span class='tool'>{_esc(step.get('tool') or 'no tool')}</span>({_esc(args)})</code>"
            f"<code>{_esc(json.dumps(result, separators=(', ', ': ')))}</code>"
            f"<div class='meta'>{_esc(' · '.join(meta))}</div></div></section>")
    parts.append("</main></body></html>")
    out = run_dir / "replay.html"
    out.write_text("\n".join(parts), encoding="utf-8")
    return out


if __name__ == "__main__":
    if sys.argv[1:] == ["--all"]:
        for log in sorted(Path("runs").rglob("run.jsonl")):
            print(write_replay(log.parent))
    elif len(sys.argv) == 2:
        print(write_replay(sys.argv[1]))
    else:
        sys.exit(__doc__)
