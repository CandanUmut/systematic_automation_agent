#!/usr/bin/env python3
"""
Agent‑Framework backend  (MongoDB + APScheduler + code‑exec)
─────────────────────────────────────────────────────────────
* Serves dashboard static files
* Proxies Ollama (:11434)
* Talks to Operator‑Web (:5000)
* Runs/schedules workflows **and** individual Operator‑Web tests
* Executes short Python / Node snippets in a sandbox
"""
from automation import pru_db
from __future__ import annotations
import os, json, time, uuid, shlex, tempfile, subprocess, pathlib, textwrap
from datetime import datetime
from threading import Thread
from typing import Iterable

import requests
from flask import (Flask, request, jsonify, Response,
                   send_from_directory, abort, stream_with_context)
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from pymongo import MongoClient
from bson import ObjectId

# ───────────────────────── configuration ──────────────────────
OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://localhost:11434")
OPERATOR_URL = os.getenv("OPERATOR_URL", "http://10.0.0.211:5000")

MONGO_URI    = os.getenv("MONGO_URI",    "mongodb://localhost:27017")
DB_NAME      = os.getenv("MONGO_DB",     "automation")

STATIC_DIR = pathlib.Path(__file__).with_name("static")
PORT         = int(os.getenv("PORT",     "5000"))
HOST         = os.getenv("HOST",         "0.0.0.0")

# ───────────────────── flask / socket.io / scheduler ───────────
app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")
CORS(app, origins="*", supports_credentials=True)
sio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

scheduler = BackgroundScheduler(); scheduler.start()

# ─────────────────────── database collections ─────────────────
client = MongoClient(MONGO_URI)
db     = client[DB_NAME]
workflows_col      = db["workflows"]       # {_id,name,nodes,created}
workflow_runs_col  = db["workflow_runs"]   # {_id,wf_id?,test_id?,status,log[],started,done}
caps_col           = db["capabilities"]    # single doc: {"enabled":[...], "updated":…}
caps_col.update_one({}, {"$setOnInsert": {"enabled": []}}, upsert=True)

# ───────────────────── helper / infra funcs ───────────────────
def oid(x): return ObjectId(x) if not isinstance(x, ObjectId) else x

def stream_sse(lines: Iterable[str], event="message"):
    for line in lines:
        yield f"event: {event}\ndata: {line}\n\n"

def proxy_req(base, path, **kw):
    r = requests.request(url=f"{base}{path}", timeout=30, **kw)
    r.raise_for_status()
    return r

def ws_log(msg:str, exec_id:str|None=None):
    payload = {"msg": msg}
    if exec_id: payload["exec"] = exec_id
    sio.emit("log", payload)

def db_log(exec_id:str, msg:str):
    workflow_runs_col.update_one({"_id": oid(exec_id)},
                                 {"$push": {"log": {"ts": datetime.utcnow(),
                                                    "msg": msg}}})

def log(exec_id:str|None, message:str):
    ws_log(message, exec_id)
    if exec_id: db_log(exec_id, message)

# ───────────────────────── static assets ──────────────────────
@app.get("/")
def index(): return send_from_directory(STATIC_DIR, "index.html")

@app.get("/<path:path>")
def assets(path): return send_from_directory(STATIC_DIR, path)

# ───────────────────────── Ollama proxy ───────────────────────
@app.post("/ollama/chat")
def chat():
    data   = request.get_json(force=True)
    stream = bool(data.get("stream"))
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json=data,
        stream=stream,
        headers={"Accept": "text/event-stream"},
        timeout=3600
    )

    if not stream:
        return (r.json(), r.status_code)

    # --- inside /ollama/chat (leave the rest of the handler unchanged) ----
    def gen():
        # Ollama sends newline–delimited JSON.  Re‑wrap every line as SSE.
        for raw in r.iter_lines():
            if not raw:  # skip keep‑alives
                continue
            yield f"data: {raw.decode()}\n\n"  # ← ONE‑LINE FIX
    return Response(stream_with_context(gen()),
                    content_type="text/event-stream")

@app.get("/ollama/models")
def models(): return proxy_req(OLLAMA_URL, "/api/tags", method="GET").json()

# ───────────────────────── Operator‑Web bridge ────────────────
@app.get("/ow/tests")
def ow_tests():
    tests = proxy_req(OPERATOR_URL, "/tests", method="GET").json()
    return jsonify([
        {
            "id"  : t["_id"],
            "name": t.get("name") or t["_id"][-6:],
            "ts"  : t.get("created_at")
        } for t in tests])

@app.post("/ow/run")
def ow_run():
    body = request.get_json(force=True)
    return proxy_req(OPERATOR_URL, "/run", method="POST", json=body).json()

# ───────────────────────── capabilities toggle ────────────────
@app.get("/workflow/capabilities")
def get_caps():
    caps_cur = caps_col.find_one() or {}
    enabled  = set(caps_cur.get("enabled", []))
    default  = {
        "web-search"    : "DuckDuckGo search",
        "system-control": "Shell commands",
        "web-automation": "Playwright via Operator‑Web",
        "email"         : "SMTP / IMAP send/receive",
        "api-integration":"Generic REST calls"
    }
    return jsonify({k: {"enabled": k in enabled, "desc": v}
                    for k, v in default.items()})

@app.patch("/workflow/capabilities")
def patch_caps():
    enabled = request.json.get("enabled", [])
    caps_col.update_one({}, {"$set": {"enabled": enabled,
                                      "updated": datetime.utcnow()}},
                        upsert=True)
    ws_log("Capabilities updated → "+", ".join(enabled))
    return {"status": "ok"}

# ───────────────────────── scheduler API ──────────────────────
def run_target(target:str):
    if target.startswith("test:"):
        test_id = target.split(":",1)[1]
        proxy_req(OPERATOR_URL, "/run", method="POST", json={"id":test_id})
    elif target.startswith("workflow:"):
        wf_id = target.split(":",1)[1]
        run_workflow(wf_id)          # re‑use below view
    else:
        ws_log(f"[sched] Unknown target {target}")

@app.post("/schedule")
def schedule():
    data   = request.get_json(force=True)
    target = data["target"]          # test:<id> | workflow:<id>
    cron   = data["cron"]            # */5 * * * *
    trig   = CronTrigger.from_crontab(cron)
    jid    = f"{target}_{uuid.uuid4().hex[:6]}"
    scheduler.add_job(run_target, trigger=trig, id=jid, args=[target])
    ws_log(f"⏰ Scheduled {target} ({cron}) — job id {jid}")
    return {"job_id": jid}, 202

# ───────────────────────── workflow CRUD ──────────────────────
@app.post("/workflow")
def create_workflow():
    wf = request.get_json(force=True)
    wf.update({"created": datetime.utcnow(),
               "name"   : wf.get("name") or f"Workflow {uuid.uuid4().hex[:6]}"})
    _id = workflows_col.insert_one(wf).inserted_id
    return {"id": str(_id)}, 201

@app.get("/workflow/<wf_id>")
def get_workflow(wf_id):
    wf = workflows_col.find_one({"_id": oid(wf_id)}) or abort(404)
    wf["_id"] = str(wf["_id"]); return wf

@app.delete("/workflow/<wf_id>")
def delete_workflow(wf_id):
    workflows_col.delete_one({"_id": oid(wf_id)})
    return {"deleted": wf_id}

# ── proxy Operator‑Web artefacts so UI stays same‑origin ─────────
@app.get("/ow/file/<path:fname>")
def ow_file(fname):
    r = requests.get(f"{OPERATOR_URL}/run-files/{fname}",
                     stream=True, timeout=60)
    return Response(r.iter_content(chunk_size=32_768),
                    status=r.status_code,
                    content_type=r.headers.get("Content-Type",
                                               "application/octet-stream"))


# ───────────────────────── workflow execution ─────────────────
@app.post("/workflow/run/<wf_id>")
def run_workflow(wf_id):
    wf = workflows_col.find_one({"_id": oid(wf_id)}) or abort(404)
    exec_id = workflow_runs_col.insert_one({
        "wf_id"  : wf_id,
        "status" : "running",
        "started": datetime.utcnow(),
        "log"    : []
    }).inserted_id
    Thread(target=execute_workflow, args=(wf, str(exec_id)), daemon=True).start()
    return {"exec_id": str(exec_id)}, 202

def execute_workflow(wf, exec_id:str):
    log(exec_id, f"▶ Workflow “{wf.get('name')}”")
    for node in wf.get("nodes", []):
        typ = node.get("type")
        if typ == "ow-test":
            run_operator_node(node, exec_id)
        else:
            time.sleep(1)
            log(exec_id, f"Executed node: {node.get('text')}")
    workflow_runs_col.update_one({"_id": oid(exec_id)},
                                 {"$set": {"status":"completed",
                                           "completed": datetime.utcnow()}})
    log(exec_id, "Workflow completed ✅")

def run_operator_node(node, exec_id):
    try:
        res = proxy_req(OPERATOR_URL, "/run", method="POST", json=node).json()
        for step in res.get("results", []):
            log(exec_id, f"» Step {step['step']} [{step['action']}] – {step['status']}")
        log(exec_id, "Operator‑Web finished: "+res.get("status","ok"))
    except Exception as e:
        log(exec_id, f"Operator‑Web error: {e}")
        workflow_runs_col.update_one({"_id": oid(exec_id)},
                                     {"$set": {"status":"error"}})

# ───────────────────────── code‑exec sandbox ──────────────────
SANDBOX_TIMEOUT = int(os.getenv("SANDBOX_TIMEOUT", "15"))   # seconds
LANG_CMDS = {
    "python": ["python", "-u", "-"],
    "node"  : ["node", "--input-type=module", "-"]
}

def sandbox_run(lang:str, code:str) -> Iterable[str]:
    if lang not in LANG_CMDS:
        yield f"Unsupported language {lang}\n"; return
    cmd = LANG_CMDS[lang]
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                text=True, cwd=td)
        proc.stdin.write(code)
        proc.stdin.close()
        start = time.time()
        for line in iter(proc.stdout.readline, ''):
            yield line.rstrip()
            if time.time() - start > SANDBOX_TIMEOUT:
                proc.kill(); yield f"[terminated after {SANDBOX_TIMEOUT}s]"
                break
        proc.wait()

@app.post("/exec")
def exec_code():
    payload = request.get_json(force=True)
    lang = payload.get("lang","python")
    code = payload.get("code","")
    stream = bool(payload.get("stream", True))
    if not stream:
        output = "\n".join(sandbox_run(lang, code))
        return {"out": output}
    # SSE / Socket‑IO dual stream
    def gen():
        for ln in sandbox_run(lang, code):
            yield f"data: {json.dumps({'line':ln})}\n\n"
    return Response(stream_with_context(gen()),
                    content_type="text/event-stream")

# ───────────────────────── socket‑io helpers ──────────────────
@sio.on("connect")
def sio_connect(): emit("log", {"msg": "WebSocket connected 👍"})

@sio.on("llm")          # optional live LLM from the browser
def sio_llm(data):
    model  = data.get("model","llama3")
    prompt = data["prompt"]
    r = requests.post(f"{OLLAMA_URL}/api/chat",
                      json={"model":model,"stream":True,
                            "messages":[{"role":"user","content":prompt}]},
                      stream=True, timeout=3600)
    for ln in r.iter_lines():
        token = (json.loads(ln or b"{}").get("message") or {}).get("content")
        if token: emit("token", {"token": token})

# ───────────────────────── launch ─────────────────────────────
if __name__ == "__main__":
    print(f"✓ static → {STATIC_DIR.resolve()}")
    print(f"✓ Ollama → {OLLAMA_URL}")
    print(f"✓ Operator‑Web → {OPERATOR_URL}")
    print(f"✓ MongoDB → {MONGO_URI}/{DB_NAME}")
    sio.run(app, host=HOST, port=PORT, debug=True, allow_unsafe_werkzeug=True)
