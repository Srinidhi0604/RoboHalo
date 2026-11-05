"""Replays site: every run in runs/ as a local web page, with its camera frames and video.

  python -m agent.replay              # rebuild every page and open runs/index.html in the browser
  python -m agent.replay --no-open    # rebuild only
  python -m agent.replay runs/<run>   # rebuild one run page (and the index)

Pages are plain files next to the run data, so they open straight from disk with no server.
Agent runs rebuild their own page and the index when they finish.
"""

import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"

OUTCOME_LABEL = {"pass": "Pass", "fail_wrong_object": "Wrong object", "fail_not_reached": "Not reached",
                 "fail_collision": "Collision", "timeout": "Timed out", "no result": "Unfinished", "unknown": "No result"}


# ---------------------------------------------------------------- data

def _started(name):
    """Run folders are named <kind->YYYYmmdd-HHMMSS-<id>; returns an ISO time or None."""
    for part_index in range(len(name)):
        chunk = name[part_index:part_index + 15]
        try:
            return datetime.strptime(chunk, "%Y%m%d-%H%M%S").isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def _kind(name, summary):
    mode = (summary or {}).get("mode", "")
    if name.startswith("speak-") or mode in ("decide_then_manual", "decide_then_scripted"):
        return "Claude decides, manual run"
    if name.startswith("quick-"):
        return "Quick (demo)"
    if name.startswith("builtin-"):
        return "Built-in (scripted)"
    if name.startswith("manual-") or mode == "manual_fixed_path":
        return "Manual (scripted)"
    if "-scout-" in name or mode == "scout":
        return "Scout"
    if "-groq-" in name:
        return "Groq listener"
    return "Camera agent"


def load_run(folder):
    """One run folder -> a dict the pages use, or None if the folder holds nothing to show."""
    folder = Path(folder)
    name = folder.name
    steps, summary, plan = [], {}, None
    log = folder / "run.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if "summary" in record:
                summary = record["summary"]
            elif "step" in record:
                steps.append(record)
    if (folder / "plan.json").exists():
        plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
    clip = folder / "clip.mp4"
    clip_meta = {}
    if (folder / "clip.json").exists():
        clip_meta = json.loads((folder / "clip.json").read_text(encoding="utf-8"))
    if not steps and plan is None and not clip.exists():
        return None

    frames = [s["frame_path"] for s in steps if s.get("frame_path") and (folder / s["frame_path"]).exists()]
    if not frames:
        frames = sorted(p.name for p in folder.glob("frame-*.*"))
    thumb = None
    picks = [s for s in steps if s.get("tool") == "pickup" and (s.get("result") or {}).get("ok")]
    if picks:   # the moment the robot reached the item tells the story best
        before = [s.get("frame_path") for s in steps if s["step"] <= picks[0]["step"] and s.get("frame_path")]
        thumb = before[-1] if before else None
    if thumb is None and frames:
        thumb = frames[-1]
    if thumb is None and (folder / "clip_frames").exists():
        clip_frames = sorted((folder / "clip_frames").glob("frame_*.jpg"))
        if clip_frames:
            thumb = "clip_frames/" + clip_frames[len(clip_frames) // 2].name

    if plan is not None:
        results = [r["result"] for r in plan.get("results", [])]
        outcome = "pass" if results and all(r.startswith(("pass", "done")) for r in results) else ("no steps" if not results else "; ".join(results))
        command, model = plan.get("request", ""), plan.get("model", "")
    else:
        outcome = summary.get("outcome") or ("no result" if steps else "")
        command = summary.get("command") or clip_meta.get("command", "")
        model = summary.get("model") or summary.get("mode", "")
    collisions = sum(1 for s in steps if (s.get("result") or {}).get("collided"))
    return {
        "id": name,
        "kind": _kind(name, summary),
        "command": command,
        "model": model or "-",
        "outcome": outcome or "unknown",
        "steps": summary.get("step_count", len(steps)) if plan is None else len(plan.get("steps", [])),
        "cost": summary.get("cost_estimate_usd"),
        "collisions": collisions,
        "started": _started(name),
        "thumb": thumb,
        "clip": "clip.mp4" if clip.exists() else None,
        "problem": summary.get("problem") or (plan or {}).get("note", ""),
        "log": steps,
        "plan": plan,
        "frames": len(frames),
    }


def collect(runs_dir=RUNS):
    runs = [load_run(folder) for folder in sorted(Path(runs_dir).iterdir()) if folder.is_dir()] if Path(runs_dir).exists() else []
    runs = [r for r in runs if r]
    runs.sort(key=lambda r: r["started"] or "", reverse=True)
    return runs


# ---------------------------------------------------------------- pages

STYLE = """
:root{--bg:#f3f4f7;--panel:#ffffff;--panel-2:#eef0f4;--text:#161a22;--muted:#5d6676;--line:#dde1e8;--accent:#2f6fec;
--pass:#1f8f4d;--pass-bg:#e3f5ea;--fail:#c2362f;--fail-bg:#fbe7e5;--warn:#a96a00;--warn-bg:#fdf1dc;--neutral-bg:#e9ecf1;--shadow:0 1px 2px rgba(16,24,40,.06),0 4px 14px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--panel:#181c25;--panel-2:#1f2430;--text:#e8ebf1;--muted:#98a2b3;--line:#2a303d;--accent:#6e9cff;
--pass:#4cc97f;--pass-bg:#173527;--fail:#ff7a72;--fail-bg:#3a1d1c;--warn:#f0b455;--warn-bg:#3a2c14;--neutral-bg:#262c38;--shadow:0 1px 2px rgba(0,0,0,.4)}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 "Segoe UI",system-ui,-apple-system,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1280px;margin:0 auto;padding:24px 20px 64px}
header.top{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:18px}
h1{font-size:24px;letter-spacing:-.01em;margin:0}h2{font-size:15px;margin:0 0 10px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--muted);font-size:13px;margin-top:2px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:0 0 18px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;box-shadow:var(--shadow)}
.stat b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.stat span{font-size:12px;color:var(--muted)}
.models{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:18px;box-shadow:var(--shadow);overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:6px;border-radius:3px;background:var(--neutral-bg);min-width:80px;overflow:hidden}.bar i{display:block;height:100%;background:var(--pass)}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px;position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0}
.filters input,.filters select{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:9px 12px}
.filters input{flex:1;min-width:200px}.count{color:var(--muted);font-size:13px;align-self:center;margin-left:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px}
.card{display:flex;flex-direction:column;background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:var(--shadow);color:inherit;transition:transform .12s,border-color .12s}
.card:hover{transform:translateY(-2px);border-color:var(--accent);text-decoration:none}
.thumb{position:relative;aspect-ratio:4/3;background:#0b0d12;display:flex;align-items:center;justify-content:center;color:#7d8696;font-size:13px}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.thumb .tag{position:absolute;top:8px;left:8px}.thumb .vid{position:absolute;bottom:8px;right:8px;background:rgba(0,0,0,.65);color:#fff;font-size:12px;padding:2px 8px;border-radius:999px}
.body{padding:12px 14px 14px;display:flex;flex-direction:column;gap:6px;flex:1}
.cmd{font-weight:600;line-height:1.35}.meta{color:var(--muted);font-size:12.5px;display:flex;flex-wrap:wrap;gap:4px 12px}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;padding:3px 9px;border-radius:999px;background:var(--neutral-bg);color:var(--muted);white-space:nowrap}
.badge.pass{background:var(--pass-bg);color:var(--pass)}.badge.fail{background:var(--fail-bg);color:var(--fail)}.badge.warn{background:var(--warn-bg);color:var(--warn)}
.empty{padding:40px;text-align:center;color:var(--muted);background:var(--panel);border:1px dashed var(--line);border-radius:14px}
.back{display:inline-block;margin-bottom:12px;font-size:14px}
.facts{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 18px}
.fact{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:6px 12px;font-size:13px}.fact b{font-variant-numeric:tabular-nums}
.layout{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(300px,1fr);gap:16px;align-items:start}
@media (max-width:900px){.layout{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:hidden}
.viewer img{width:100%;aspect-ratio:4/3;object-fit:contain;background:#0b0d12;display:block}
.noframe{aspect-ratio:4/3;display:flex;align-items:center;justify-content:center;background:#0b0d12;color:#7d8696;text-align:center;padding:20px}
.controls{display:flex;align-items:center;gap:8px;padding:10px 12px;border-top:1px solid var(--line)}
.controls button{font:inherit;font-weight:600;border:1px solid var(--line);background:var(--panel-2);color:var(--text);border-radius:9px;padding:6px 12px;cursor:pointer}
.controls button:hover{border-color:var(--accent)}.controls input[type=range]{flex:1;accent-color:var(--accent)}
.controls .pos{font-variant-numeric:tabular-nums;color:var(--muted);font-size:13px;min-width:70px;text-align:right}
.detail{padding:14px 16px}.detail .tool{font-family:ui-monospace,Consolas,monospace;font-size:14px;font-weight:600;color:var(--accent)}
.detail .why{margin:8px 0 10px;font-size:15px}.detail .why.none{color:var(--muted);font-style:italic}
pre{margin:0;font:12.5px/1.45 ui-monospace,Consolas,monospace;background:var(--panel-2);border:1px solid var(--line);border-radius:9px;padding:9px 10px;white-space:pre-wrap;word-break:break-word}
.kv{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.strip{display:flex;gap:6px;overflow-x:auto;padding:10px 12px;border-top:1px solid var(--line);scrollbar-width:thin}
.strip button{flex:0 0 auto;width:74px;border:2px solid transparent;border-radius:8px;padding:0;background:var(--panel-2);cursor:pointer;overflow:hidden;color:var(--muted);font:600 11px/1 "Segoe UI",sans-serif}
.strip button img{width:100%;aspect-ratio:4/3;object-fit:cover;display:block}.strip button .n{display:block;padding:3px 0}
.strip button.on{border-color:var(--accent)}.strip button.bump{box-shadow:inset 0 -3px 0 var(--warn)}.strip button .blank{display:flex;aspect-ratio:4/3;align-items:center;justify-content:center;font-size:18px}
video{width:100%;display:block;background:#000}
.steps td{white-space:normal;vertical-align:top}.steps tr{cursor:pointer}.steps tr:hover td{background:var(--panel-2)}.steps tr.on td{background:var(--panel-2)}
.steps .t{font-family:ui-monospace,Consolas,monospace;font-weight:600}
.kbd{font:11px ui-monospace,Consolas,monospace;border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:0 4px;color:var(--muted)}
@media (max-width:600px){.wrap{padding:16px 16px 48px}h1{font-size:20px}.grid{grid-template-columns:1fr}}
"""


def _json_for_html(value):
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def _page(title, body, script):
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{STYLE}</style></head><body>{body}<script>{script}</script></body></html>")


INDEX_SCRIPT = r"""
const RUNS = JSON.parse(document.getElementById('data').textContent);
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const LABEL = %LABELS%;
const cls = o => o === 'pass' ? 'pass' : (o || '').startsWith('fail') ? 'fail' : o === 'timeout' ? 'warn' : '';
const when = iso => iso ? new Date(iso).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : '';
function options(sel, values, all) {
  sel.innerHTML = `<option value="">${all}</option>` + [...new Set(values)].filter(Boolean).sort().map(v => `<option>${esc(v)}</option>`).join('');
}
options($('#model'), RUNS.map(r => r.model), 'All models');
options($('#kind'), RUNS.map(r => r.kind), 'All run types');
function stats(list) {
  const graded = list.filter(r => r.outcome !== 'unknown' && r.outcome !== 'no result');
  const passed = graded.filter(r => r.outcome === 'pass').length;
  const cost = list.reduce((s, r) => s + (r.cost || 0), 0);
  $('#stats').innerHTML = [
    [list.length, 'runs shown'], [passed + ' / ' + graded.length, 'passed'],
    [graded.length ? Math.round(100 * passed / graded.length) + '%' : '-', 'pass rate'],
    [list.filter(r => r.clip).length, 'with video'], ['$' + cost.toFixed(2), 'model cost']
  ].map(([b, s]) => `<div class="stat"><b>${b}</b><span>${s}</span></div>`).join('');
  const by = {};
  graded.forEach(r => { const k = r.model + '\u0001' + r.kind; (by[k] = by[k] || {model: r.model, kind: r.kind, n: 0, p: 0, steps: 0, cost: 0}); by[k].n++; by[k].p += r.outcome === 'pass'; by[k].steps += r.steps || 0; by[k].cost += r.cost || 0; });
  const rows = Object.values(by).sort((a, b) => b.n - a.n);
  $('#models').innerHTML = rows.length ? `<h2>Pass rate by model</h2><table><thead><tr><th>Model</th><th>Run type</th><th class="num">Runs</th><th class="num">Passed</th><th></th><th class="num">Avg steps</th><th class="num">Avg cost</th></tr></thead><tbody>` +
    rows.map(m => `<tr><td>${esc(m.model)}</td><td>${esc(m.kind)}</td><td class="num">${m.n}</td><td class="num">${m.p} (${Math.round(100 * m.p / m.n)}%)</td><td><div class="bar"><i style="width:${100 * m.p / m.n}%"></i></div></td><td class="num">${(m.steps / m.n).toFixed(0)}</td><td class="num">${m.cost ? '$' + (m.cost / m.n).toFixed(3) : '-'}</td></tr>`).join('') + `</tbody></table>` : '';
}
function render() {
  const q = $('#q').value.trim().toLowerCase(), o = $('#outcome').value, m = $('#model').value, k = $('#kind').value;
  const list = RUNS.filter(r => (!q || (r.command + ' ' + r.id + ' ' + r.problem).toLowerCase().includes(q))
    && (!o || (o === 'fail' ? r.outcome.startsWith('fail') : r.outcome === o)) && (!m || r.model === m) && (!k || r.kind === k));
  stats(list);
  $('#count').textContent = list.length + ' of ' + RUNS.length;
  $('#grid').innerHTML = list.length ? list.map(r => `
    <a class="card" href="${encodeURIComponent(r.id)}/replay.html">
      <div class="thumb">${r.thumb ? `<img loading="lazy" src="${encodeURIComponent(r.id)}/${r.thumb.split('/').map(encodeURIComponent).join('/')}" alt="">` : 'No camera frames'}
        <span class="badge tag ${cls(r.outcome)}">${esc(LABEL[r.outcome] || r.outcome)}</span>${r.clip ? '<span class="vid">&#9654; video</span>' : ''}</div>
      <div class="body"><div class="cmd">${esc(r.command || '(no command recorded)')}</div>
        <div class="meta"><span>${esc(r.model)}</span><span>${esc(r.kind)}</span></div>
        <div class="meta"><span>${when(r.started)}</span><span>${r.steps} steps</span>${r.collisions ? `<span>${r.collisions} bump${r.collisions > 1 ? 's' : ''}</span>` : ''}${r.cost ? `<span>$${r.cost.toFixed(3)}</span>` : ''}</div></div>
    </a>`).join('') : '<div class="empty">No runs match these filters.</div>';
}
['#q', '#outcome', '#model', '#kind'].forEach(s => $(s).addEventListener('input', render));
render();
"""

RUN_SCRIPT = r"""
const RUN = JSON.parse(document.getElementById('data').textContent);
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const steps = RUN.log;
let at = 0, timer = null;
const frameFor = i => { for (let j = i; j >= 0; j--) if (steps[j].frame_path) return [steps[j].frame_path, j === i]; return [null, false]; };
const fmtArgs = a => Object.entries(a || {}).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(', ');
function show(i) {
  if (!steps.length) return;
  at = Math.max(0, Math.min(steps.length - 1, i));
  const s = steps[at], [frame, fresh] = frameFor(at), r = s.result || {};
  $('#frame').innerHTML = frame ? `<img src="${frame.split('/').map(encodeURIComponent).join('/')}" alt="Camera frame at step ${s.step}">` : '<div class="noframe">No camera image yet</div>';
  const why = (s.reasoning || '').trim();
  const kv = [];
  if (s.depth_m !== undefined) kv.push(`depth ${s.depth_m === null ? 'none' : (+s.depth_m).toFixed(2) + ' m'}`);
  if (s.state) { kv.push(s.state.holding ? 'holding' : 'not holding'); kv.push(`collisions ${s.state.retries ?? 0}/3`); }
  if (!fresh && frame) kv.push('no new image this step');
  if (r.collided) kv.push('collided');
  if (s.cost_usd) kv.push('$' + s.cost_usd.toFixed(4));
  $('#detail').innerHTML = `<div class="sub">Step ${s.step} of ${steps.length}</div>
    <div class="tool">${esc(s.tool || 'no tool')}(${esc(fmtArgs(s.args))})</div>
    <div class="why ${why ? '' : 'none'}">${why ? esc(why) : 'No reasoning recorded for this step.'}</div>
    <pre>${esc(JSON.stringify(r, null, 1))}</pre>
    <div class="kv">${kv.map(k => `<span class="badge ${k === 'collided' ? 'warn' : ''}">${esc(k)}</span>`).join('')}</div>`;
  $('#slider').value = at;
  $('#pos').textContent = `${at + 1} / ${steps.length}`;
  document.querySelectorAll('.strip button').forEach((b, j) => b.classList.toggle('on', j === at));
  document.querySelectorAll('.steps tbody tr').forEach((t, j) => t.classList.toggle('on', j === at));
  const on = document.querySelector('.strip button.on'); if (on) on.scrollIntoView({block: 'nearest', inline: 'center'});
}
function play() {
  if (timer) { clearInterval(timer); timer = null; $('#play').textContent = 'Play'; return; }
  if (at >= steps.length - 1) show(0);
  $('#play').textContent = 'Pause';
  timer = setInterval(() => { if (at >= steps.length - 1) play(); else show(at + 1); }, 900);
}
if (steps.length) {
  $('#slider').max = steps.length - 1;
  $('#strip').innerHTML = steps.map((s, i) => `<button title="Step ${s.step}: ${esc(s.tool || '')}" class="${(s.result || {}).collided ? 'bump' : ''}">${s.frame_path ? `<img loading="lazy" src="${s.frame_path.split('/').map(encodeURIComponent).join('/')}" alt="">` : `<span class="blank">${s.tool === 'pickup' ? '&#9995;' : s.tool === 'drop' ? '&#11015;' : '&middot;'}</span>`}<span class="n">${s.step} ${esc(s.tool || '')}</span></button>`).join('');
  document.querySelectorAll('.strip button').forEach((b, i) => b.addEventListener('click', () => show(i)));
  $('#steps').innerHTML = steps.map(s => `<tr><td class="num">${s.step}</td><td class="t">${esc(s.tool || '-')}</td><td>${esc(fmtArgs(s.args))}</td><td>${esc((s.reasoning || '').slice(0, 220))}</td><td>${(s.result || {}).collided ? '<span class="badge warn">collided</span>' : (s.result || {}).error ? '<span class="badge fail">error</span>' : ''}</td></tr>`).join('');
  document.querySelectorAll('.steps tbody tr').forEach((t, i) => t.addEventListener('click', () => { show(i); window.scrollTo({top: 0, behavior: 'smooth'}); }));
  $('#prev').onclick = () => show(at - 1); $('#next').onclick = () => show(at + 1); $('#play').onclick = play;
  $('#slider').oninput = e => show(+e.target.value);
  document.addEventListener('keydown', e => { if (e.target.tagName === 'INPUT') return;
    if (e.key === 'ArrowRight') show(at + 1); else if (e.key === 'ArrowLeft') show(at - 1); else if (e.key === ' ') { e.preventDefault(); play(); } });
  show(0);
}
"""


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _badge_class(outcome):
    return "pass" if outcome == "pass" else "fail" if str(outcome).startswith("fail") else "warn" if outcome == "timeout" else ""


def write_run_page(run, folder):
    outcome = run["outcome"]
    when = datetime.fromisoformat(run["started"]).strftime("%d %b %Y, %H:%M") if run["started"] else ""
    facts = [("Steps", run["steps"]), ("Collisions", run["collisions"]), ("Images", run["frames"])]
    if run["cost"] is not None:
        facts.append(("Model cost", f"${run['cost']:.3f}"))
    if when:
        facts.append(("Started", when))
    head = (f"<a class='back' href='../index.html'>&larr; All runs</a>"
            f"<header class='top'><div><h1>{_esc(run['command'] or '(no command recorded)')}</h1>"
            f"<div class='sub'>{_esc(run['id'])}</div></div>"
            f"<div><span class='badge {_badge_class(outcome)}'>{_esc(OUTCOME_LABEL.get(outcome, outcome))}</span> "
            f"<span class='badge'>{_esc(run['model'])}</span> <span class='badge'>{_esc(run['kind'])}</span></div></header>"
            "<div class='facts'>" + "".join(f"<div class='fact'>{k}: <b>{_esc(v)}</b></div>" for k, v in facts) + "</div>")
    if run["problem"]:
        head += f"<div class='fact' style='margin:-6px 0 16px'>Note: {_esc(run['problem'])}</div>"
    video = (f"<div class='panel' style='margin-bottom:16px'><video controls preload='metadata' src='{run['clip']}'></video>"
             f"<div class='detail'><div class='sub'>Robot camera clip (Phase 2 format)</div></div></div>") if run["clip"] else ""
    if run["log"]:
        main = ("<div class='layout'><div class='panel viewer'><div id='frame'></div>"
                "<div class='controls'><button id='prev' aria-label='Previous step'>&larr;</button><button id='play'>Play</button>"
                "<button id='next' aria-label='Next step'>&rarr;</button><input id='slider' type='range' min='0' value='0' aria-label='Step'>"
                "<span class='pos' id='pos'></span></div><div class='strip' id='strip'></div></div>"
                f"<div>{video}<div class='panel'><div class='detail' id='detail'></div></div>"
                "<p class='sub' style='margin:10px 2px'><span class='kbd'>&larr;</span> <span class='kbd'>&rarr;</span> step, "
                "<span class='kbd'>space</span> play</p></div></div>"
                "<div class='panel' style='margin-top:16px;overflow-x:auto'><table class='steps'><thead><tr><th class='num'>#</th><th>Tool</th>"
                "<th>Arguments</th><th>Reasoning</th><th></th></tr></thead><tbody id='steps'></tbody></table></div>")
    elif run["plan"]:
        plan = run["plan"]
        rows = "".join(f"<tr><td class='t'>{_esc(r['step'])}</td><td>{_esc(r['result'])}</td></tr>" for r in plan.get("results", []))
        main = (f"<div class='panel'><div class='detail'><div class='sub'>Quick mode: one model call planned these steps; the engine drove.</div>"
                f"<div class='why'>{_esc(plan.get('note', ''))}</div></div>"
                f"<table class='steps'><thead><tr><th>Step</th><th>Result</th></tr></thead><tbody>{rows}</tbody></table></div>")
    else:
        main = video or "<div class='empty'>Nothing recorded for this run.</div>"
        video = ""
    data = dict(run)
    data.pop("plan", None)
    body = (f"<div class='wrap'>{head}{main}</div>"
            f"<script type='application/json' id='data'>{_json_for_html(data)}</script>")
    (Path(folder) / "replay.html").write_text(_page("Run replay", body, RUN_SCRIPT), encoding="utf-8")


def write_index(runs, runs_dir=RUNS):
    listing = [{k: v for k, v in r.items() if k not in ("log", "plan")} for r in runs]
    body = ("<div class='wrap'><header class='top'><div><h1>RoboHalo replays</h1>"
            f"<div class='sub'>Every run in this folder, newest first. Built {datetime.now():%d %b %Y, %H:%M}.</div></div></header>"
            "<div class='stats' id='stats'></div><div class='models' id='models'></div>"
            "<div class='filters'><input id='q' type='search' placeholder='Search commands' aria-label='Search commands'>"
            "<select id='outcome' aria-label='Outcome'><option value=''>All outcomes</option><option value='pass'>Pass</option>"
            "<option value='fail'>Any failure</option><option value='fail_wrong_object'>Wrong object</option>"
            "<option value='fail_not_reached'>Not reached</option><option value='fail_collision'>Collision</option></select>"
            "<select id='model' aria-label='Model'></select><select id='kind' aria-label='Run type'></select>"
            "<span class='count' id='count'></span></div><div class='grid' id='grid'></div></div>"
            f"<script type='application/json' id='data'>{_json_for_html(listing)}</script>")
    out = Path(runs_dir) / "index.html"
    out.write_text(_page("RoboHalo replays", body, INDEX_SCRIPT.replace("%LABELS%", json.dumps(OUTCOME_LABEL))), encoding="utf-8")
    return out


def write_replay(run_dir):
    """Rebuild one run's page, then the index. Called by the agents when a run ends."""
    run_dir = Path(run_dir)
    run = load_run(run_dir)
    if run:
        write_run_page(run, run_dir)
    write_index(collect(run_dir.parent), run_dir.parent)
    return run_dir / "replay.html"


def build_all(runs_dir=RUNS):
    runs = collect(runs_dir)
    for run in runs:
        write_run_page(run, Path(runs_dir) / run["id"])
    return write_index(runs, runs_dir), len(runs)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        print(write_replay(args[0]))
    else:
        index, count = build_all()
        print(f"Built {count} run pages and {index}")
        if "--no-open" not in sys.argv:
            webbrowser.open(index.resolve().as_uri())
