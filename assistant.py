#!/usr/bin/env python3
"""Local Linux Hardware Q&A Assistant (stdlib only, Python 3.8+).
Usage:  python assistant.py            interactive chat      |  python assistant.py "How much RAM can I add?"
        python assistant.py --demo     use bundled sample hardware (works on Windows/Mac)   |  --debug  show tool calls
Design: the LLM never runs shell strings. It can only pick named tools from an allow-list; each tool runs fixed argv with
shell=False, is parsed into JSON, and the answer is written ONLY from that JSON. Without an LLM an offline router answers.
LLM backends (optional): ANTHROPIC_API_KEY (cloud) or OLLAMA_MODEL (fully local, e.g. qwen2.5:7b)."""
import os, re, sys, json, time, shutil, platform, subprocess, urllib.request, glob

DEMO = "--demo" in sys.argv or os.environ.get("HWA_DEMO") == "1"
DEBUG = "--debug" in sys.argv
ANTHROPIC_KEY, OLLAMA_MODEL = os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("OLLAMA_MODEL")
MODEL = os.environ.get("HWA_MODEL", "claude-sonnet-5-5")
CANT = "I cannot determine this hardware detail from system data"

class ToolError(Exception): pass

# ---------------- sample hardware for --demo (parsed by the SAME parsers as real output) ----------------
FIX = {
 "lscpu": "Architecture: x86_64\nCPU(s): 8\nThread(s) per core: 2\nCore(s) per socket: 4\nSocket(s): 1\nModel name: 11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz\nCPU max MHz: 4200.0000\nL3 cache: 8 MiB (1 instance)\n",
 "free": "              total        used        free      shared  buff/cache   available\nMem:    16637468672  6123298816  4200000000   512000000  6314169856  9800000000\nSwap:    2147479552           0  2147479552\n",
 "dmidecode": "Physical Memory Array\n  Location: System Board Or Motherboard\n  Maximum Capacity: 64 GB\n  Number Of Devices: 2\n\nMemory Device\n  Size: 16 GB\n  Form Factor: SODIMM\n  Locator: DIMM A\n  Type: DDR4\n  Speed: 3200 MT/s\n  Manufacturer: Samsung\n\nMemory Device\n  Size: No Module Installed\n  Form Factor: SODIMM\n  Locator: DIMM B\n  Type: Unknown\n",
 "lsblk": json.dumps({"blockdevices": [{"name": "nvme0n1", "size": "476.9G", "type": "disk", "model": "Samsung SSD 970 EVO", "rota": False, "tran": "nvme"},
                                       {"name": "sda", "size": "931.5G", "type": "disk", "model": "WDC WD10SPZX", "rota": True, "tran": "sata"}]}),
 "lspci": "00:02.0 VGA compatible controller: Intel Corporation TigerLake-LP GT2 [Iris Xe Graphics] (rev 01)\n00:1f.3 Audio device: Intel Corporation Tiger Lake-LP Smart Sound\n",
 "file:/etc/os-release": 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\n', "file:/sys/class/dmi/id/sys_vendor": "Dell Inc.\n",
 "file:/sys/class/dmi/id/product_name": "Latitude 5420\n", "file:/sys/class/dmi/id/bios_version": "1.25.0\n",
 "file:/sys/class/power_supply/BAT0/capacity": "87\n", "file:/sys/class/power_supply/BAT0/status": "Discharging\n",
 "file:/sys/class/power_supply/BAT0/energy_full": "49000000\n", "file:/sys/class/power_supply/BAT0/energy_full_design": "63000000\n"}

# ---------------- safe execution layer ----------------
ALLOWED = {"lscpu", "lsblk", "free", "lspci", "nvidia-smi", "dmidecode"}
FILE_PREFIXES = ("/etc/os-release", "/sys/class/dmi/id/", "/sys/class/power_supply/", "/proc/meminfo")
DMIDECODE = shutil.which("dmidecode") or "/usr/sbin/dmidecode"

def run(key, argv, timeout=5):
    """Run a fixed argv (no shell). Only allow-listed binaries; sudo only for dmidecode with -n (never prompts)."""
    binary = argv[2] if argv[0] == "sudo" else argv[0]
    if os.path.basename(binary) not in ALLOWED or (argv[0] == "sudo" and os.path.basename(binary) != "dmidecode"):
        raise ToolError("command not allow-listed: " + binary)
    if DEMO:
        if key in FIX: return FIX[key]
        raise ToolError(key + " not available in demo data")
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False,
                           env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})   # LC_ALL=C: stable output across distros
    except FileNotFoundError: raise ToolError(os.path.basename(binary) + " is not installed")
    except subprocess.TimeoutExpired: raise ToolError(os.path.basename(binary) + " timed out")
    if r.returncode != 0: raise ToolError("%s failed: %s" % (os.path.basename(binary), (r.stderr or "").strip()[:120]))
    return r.stdout

def read_file(path):
    if not path.startswith(FILE_PREFIXES) or ".." in path: raise ToolError("path not allowed: " + path)
    if DEMO: return FIX.get("file:" + path, "")
    try:
        with open(path) as f: return f.read()
    except OSError: return ""

# ---------------- parsers: raw text -> normalized JSON ----------------
def to_gb(s):
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB|KiB|MiB|GiB|TiB)", s or "", re.I)
    if not m: return None
    f = {"kb": 1 / 1024 ** 2, "mb": 1 / 1024, "gb": 1, "tb": 1024}[m[2][:2].lower()]
    return round(float(m[1]) * f, 1)

def kv(text):
    d = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1); d.setdefault(k.strip(), v.strip())
    return d

def parse_lscpu(text):
    d = kv(text); sockets, cps = int(d.get("Socket(s)", 1) or 1), int(d.get("Core(s) per socket", 0) or 0)
    return {"model": d.get("Model name"), "architecture": d.get("Architecture"), "physical_cores": sockets * cps or None,
            "threads_per_core": int(d["Thread(s) per core"]) if "Thread(s) per core" in d else None,
            "logical_cpus": int(d["CPU(s)"]) if "CPU(s)" in d else None,
            "max_mhz": round(float(d["CPU max MHz"])) if "CPU max MHz" in d else None, "l3_cache": d.get("L3 cache")}

def parse_free(text):
    for line in text.splitlines():
        if line.startswith("Mem:"): return round(int(line.split()[1]) / 1024 ** 3, 1)

def parse_dmi_memory(text):
    max_gb, slots, mods = 0.0, 0, []
    for block in re.split(r"\n\s*\n", text):
        lines = [l.strip() for l in block.splitlines()]; d = kv(block)
        if "Physical Memory Array" in lines:
            max_gb += to_gb(d.get("Maximum Capacity")) or 0; slots += int(d.get("Number Of Devices", 0) or 0)
        elif "Memory Device" in lines:
            mods.append({"slot": d.get("Locator"), "size_gb": to_gb(d.get("Size")), "type": d.get("Type"),
                         "speed": d.get("Speed"), "form_factor": d.get("Form Factor"), "manufacturer": d.get("Manufacturer")})
    return {"max_gb": max_gb or None, "slots": slots or len(mods) or None, "modules": mods}

def parse_lsblk(text):
    out = []
    for d in json.loads(text).get("blockdevices", []):
        if d.get("type") != "disk" or d["name"].startswith(("zram", "loop", "ram")): continue
        rota = str(d.get("rota")).lower() in ("1", "true")
        out.append({"name": d["name"], "size": d.get("size"), "model": (d.get("model") or "").strip() or None,
                    "kind": "NVMe SSD" if d.get("tran") == "nvme" else ("HDD" if rota else "SSD"), "interface": d.get("tran")})
    return out

def parse_lspci(text):
    return [{"type": l.split(" ", 1)[1].split(":")[0], "name": l.split(": ", 1)[1].strip()} for l in text.splitlines()
            if re.search(r"VGA|3D controller|Display controller", l) and ": " in l]

# ---------------- tools ----------------
def t_cpu(): return parse_lscpu(run("lscpu", ["lscpu"]))

def t_memory(include_empty_slots=False):
    out = {"os_visible_total_gb": parse_free(run("free", ["free", "-b"]))}
    argv = ["sudo", "-n", DMIDECODE, "-t", "memory"] if (not DEMO and os.geteuid() != 0) else [DMIDECODE, "-t", "memory"]
    try: dmi = parse_dmi_memory(run("dmidecode", argv))
    except ToolError as e:
        out.update({"max_supported_gb": None, "total_slots": None, "note": "Slot/maximum data unavailable (%s). Needs the scoped sudo rule in the README." % e})
        return out
    mods = dmi["modules"]; installed = [m for m in mods if m["size_gb"]]; used = sum(m["size_gb"] for m in installed)
    out.update({"max_supported_gb": dmi["max_gb"], "total_slots": dmi["slots"], "used_slots": len(installed),
                "free_slots": (dmi["slots"] - len(installed)) if dmi["slots"] else None, "installed_modules_gb": used,
                "modules": mods if include_empty_slots else installed})
    out["upgrade_headroom_gb"] = round(dmi["max_gb"] - used, 1) if dmi["max_gb"] else None
    if any(m["form_factor"] not in ("DIMM", "SODIMM") for m in installed): out["note"] = "Some memory may be soldered; headroom may not apply."
    return out

def t_storage(device=None):
    disks = parse_lsblk(run("lsblk", ["lsblk", "-J", "-d", "-o", "NAME,SIZE,TYPE,MODEL,ROTA,TRAN"]))
    return [d for d in disks if d["name"] == device] if device else disks

def t_gpu():
    gpus = parse_lspci(run("lspci", ["lspci"])); out = {"gpus": gpus}
    if not DEMO and shutil.which("nvidia-smi"):
        try: out["nvidia"] = run("nvidia-smi", ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]).strip().splitlines()
        except ToolError: pass
    return out

def t_system():
    g = lambda p: read_file(p).strip() or None
    m = re.search(r'PRETTY_NAME="?([^"\n]+)', read_file("/etc/os-release"))
    return {"os": m[1] if m else None, "kernel": "demo" if DEMO else platform.release(), "vendor": g("/sys/class/dmi/id/sys_vendor"),
            "model": g("/sys/class/dmi/id/product_name"), "bios_version": g("/sys/class/dmi/id/bios_version")}

def t_battery():
    names = ["BAT0"] if DEMO else [os.path.basename(p) for p in glob.glob("/sys/class/power_supply/BAT*")]
    out = []
    for n in names:
        g = lambda f: read_file("/sys/class/power_supply/%s/%s" % (n, f)).strip()
        full, design = g("energy_full") or g("charge_full"), g("energy_full_design") or g("charge_full_design")
        out.append({"name": n, "charge_percent": int(g("capacity") or 0), "status": g("status"),
                    "health_percent": round(int(full) / int(design) * 100) if full and design else None})
    if not out: raise ToolError("no battery found")
    return out

TOOLS = {
 "get_cpu_info": {"fn": t_cpu, "bins": ["lscpu"], "desc": "CPU model, architecture, physical cores, threads, max clock, cache.", "schema": {"type": "object", "properties": {}, "required": []}},
 "get_memory_topology": {"fn": t_memory, "bins": ["free"], "desc": "RAM: installed capacity, slots used/free, speed, type, maximum supported capacity and upgrade headroom.",
   "schema": {"type": "object", "properties": {"include_empty_slots": {"type": "boolean", "description": "Also return empty slots."}}, "required": []}},
 "get_storage_info": {"fn": t_storage, "bins": ["lsblk"], "desc": "Physical disks: size, model, SSD/HDD/NVMe, interface.",
   "schema": {"type": "object", "properties": {"device": {"type": "string", "pattern": "^[a-z0-9]+$", "description": "Optional device name such as nvme0n1."}}, "required": []}},
 "get_gpu_info": {"fn": t_gpu, "bins": ["lspci"], "desc": "Graphics adapters (integrated and discrete).", "schema": {"type": "object", "properties": {}, "required": []}},
 "get_system_info": {"fn": t_system, "bins": [], "desc": "OS, kernel, laptop vendor and model, BIOS version.", "schema": {"type": "object", "properties": {}, "required": []}},
 "get_battery_info": {"fn": t_battery, "bins": [], "desc": "Battery charge, status and health.", "schema": {"type": "object", "properties": {}, "required": []}}}

def avail(name):   # capability discovery: only offer tools whose binaries exist
    return DEMO or all(shutil.which(b) for b in TOOLS[name]["bins"])
def active_tools(): return {n: t for n, t in TOOLS.items() if avail(n)}

def validate(schema, args):
    for k, v in args.items():
        p = schema["properties"].get(k)
        if not p: return "unexpected argument: " + k
        if p["type"] == "boolean" and not isinstance(v, bool): return k + " must be boolean"
        if p["type"] == "string" and not (isinstance(v, str) and re.fullmatch(p.get("pattern", ".*"), v)): return k + " is invalid"

CACHE = {}   # session memory of tool results (60 s TTL) so follow-ups don't re-run commands
def call_tool(name, args=None):
    args = args or {}
    if name not in TOOLS or not avail(name): return {"error": "unknown or unavailable tool: %s" % name}
    err = validate(TOOLS[name]["schema"], args)
    if err: return {"error": err}
    key = name + json.dumps(args, sort_keys=True); hit = CACHE.get(key)
    if hit and time.time() - hit[0] < 60: return hit[1]
    try: res = TOOLS[name]["fn"](**args)
    except ToolError as e: return {"error": str(e)}
    except Exception as e: return {"error": "tool failed: %s" % e}
    CACHE[key] = (time.time(), res); return res

# ---------------- LLM orchestration (tool calling) ----------------
SYSTEM = ("You are a local Linux hardware assistant. ALWAYS call the provided tools to get facts. Base your answer solely on the tool JSON. "
          "If the JSON lacks the answer or has an error, say '%s' and name what is missing. Never guess specs. Be concise." % CANT)

def post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=dict(headers, **{"content-type": "application/json"}), method="POST")
    return json.load(urllib.request.urlopen(req, timeout=60))

def show(name, args):
    if DEBUG: print("  [tool] %s(%s)" % (name, json.dumps(args)), file=sys.stderr)

def chat_anthropic(history):
    tools = [{"name": n, "description": t["desc"], "input_schema": t["schema"]} for n, t in active_tools().items()]
    for _ in range(5):
        r = post("https://api.anthropic.com/v1/messages", {"model": MODEL, "max_tokens": 700, "system": SYSTEM, "tools": tools, "messages": history},
                 {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"})
        history.append({"role": "assistant", "content": r["content"]})
        calls = [b for b in r["content"] if b["type"] == "tool_use"]
        if not calls: return "".join(b.get("text", "") for b in r["content"])
        results = []
        for c in calls:
            show(c["name"], c["input"]); results.append({"type": "tool_result", "tool_use_id": c["id"], "content": json.dumps(call_tool(c["name"], c["input"]))})
        history.append({"role": "user", "content": results})
    return CANT + " (too many tool rounds)."

def chat_ollama(history):
    tools = [{"type": "function", "function": {"name": n, "description": t["desc"], "parameters": t["schema"]}} for n, t in active_tools().items()]
    for _ in range(5):
        m = post("http://localhost:11434/api/chat", {"model": OLLAMA_MODEL, "messages": [{"role": "system", "content": SYSTEM}] + history, "tools": tools, "stream": False}, {})["message"]
        history.append(m)
        if not m.get("tool_calls"): return m.get("content", "")
        for c in m["tool_calls"]:
            f = c["function"]; show(f["name"], f.get("arguments", {}))
            history.append({"role": "tool", "content": json.dumps(call_tool(f["name"], f.get("arguments") or {}))})
    return CANT + " (too many tool rounds)."

# ---------------- offline router (no LLM): deterministic answers from tool JSON ----------------
INTENTS = [("get_cpu_info", "cpu processor core thread ghz clock"), ("get_memory_topology", "ram memory slot upgrade ddr dimm"),
           ("get_storage_info", "disk storage ssd hdd nvme drive"), ("get_gpu_info", "gpu graphics video nvidia"),
           ("get_system_info", "os kernel model bios laptop distro vendor linux"), ("get_battery_info", "battery")]

def say(name, d):
    if isinstance(d, dict) and "error" in d: return "%s (%s)." % (CANT, d["error"])
    if name == "get_cpu_info": return "CPU: %s, %s physical cores / %s threads%s%s." % (d["model"], d["physical_cores"], d["logical_cpus"], ", up to %s MHz" % d["max_mhz"] if d["max_mhz"] else "", ", L3 %s" % d["l3_cache"] if d["l3_cache"] else "")
    if name == "get_memory_topology":
        if d.get("max_supported_gb") is None: return "RAM: about %s GB visible. %s" % (d["os_visible_total_gb"], d.get("note", ""))
        m = d["modules"][0] if d["modules"] else {}
        s = "RAM: %s GB installed in %s of %s slots (%s %s). Maximum supported: %s GB." % (d["installed_modules_gb"], d["used_slots"], d["total_slots"], m.get("type"), m.get("speed"), d["max_supported_gb"])
        if d["free_slots"] == 0: s += " All slots are full, so you can only upgrade by replacing modules (up to %s GB total)." % d["max_supported_gb"]
        elif d["upgrade_headroom_gb"]: s += " You have %s free slot(s) and can add up to %s GB more." % (d["free_slots"], d["upgrade_headroom_gb"])
        return s + (" " + d["note"] if d.get("note") else "")
    if name == "get_storage_info": return "Storage: " + "; ".join("%s %s %s (%s)" % (x["name"], x["size"], x["kind"], x["model"]) for x in d) if d else CANT + "."
    if name == "get_gpu_info": return "Graphics: " + "; ".join(g["name"] for g in d["gpus"]) if d["gpus"] else CANT + " (no GPU found)."
    if name == "get_system_info": return "System: %s %s running %s (kernel %s), BIOS %s." % (d["vendor"], d["model"], d["os"], d["kernel"], d["bios_version"])
    if name == "get_battery_info": return "Battery: " + "; ".join("%s at %s%% (%s), health %s%%" % (b["name"], b["charge_percent"], b["status"], b["health_percent"]) for b in d)

def chat_offline(question, last=[]):
    words = set(re.findall(r"[a-z0-9]+", question.lower())); words |= {w[:-1] for w in words if len(w) > 3 and w.endswith("s")}
    names = [n for n, kw in INTENTS if words & set(kw.split())]
    if words & {"summary", "specs", "everything", "overview"}: names = [n for n, _ in INTENTS]
    if not names: return "I can answer questions about CPU, RAM (and upgrades), storage, GPU, system/OS and battery."
    last[:] = names
    return "\n".join(say(n, call_tool(n)) if avail(n) else "%s (%s is not available on this system)." % (CANT, n) for n in names)

# ---------------- main ----------------
def ask(history, q):
    if ANTHROPIC_KEY or OLLAMA_MODEL:
        history.append({"role": "user", "content": q})
        try: return (chat_anthropic if ANTHROPIC_KEY else chat_ollama)(history)
        except Exception as e: return "LLM error (%s). Falling back to offline answers.\n%s" % (e, chat_offline(q))
    return chat_offline(q)

if __name__ == "__main__":
    qs = [a for a in sys.argv[1:] if not a.startswith("--")]
    mode = "Claude" if ANTHROPIC_KEY else ("Ollama " + OLLAMA_MODEL if OLLAMA_MODEL else "offline router (no LLM)")
    if "--tools" in sys.argv: print("\n".join("%-22s %s" % (n, t["desc"]) for n, t in active_tools().items())); sys.exit()
    if not DEMO and not sys.platform.startswith("linux"): print("Not Linux: run with --demo for sample hardware, or use WSL/Linux.")
    hist = []
    if qs: print(ask(hist, " ".join(qs))); sys.exit()
    print("Hardware assistant [%s%s]. Ask about CPU, RAM, storage, GPU, battery. 'exit' to quit." % (mode, ", DEMO data" if DEMO else ""))
    while True:
        try: q = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt): break
        if q.lower() in ("exit", "quit"): break
        if q: print(ask(hist, q))
