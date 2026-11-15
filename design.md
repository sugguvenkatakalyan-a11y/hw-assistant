# Design: Local Linux Hardware Q&A Assistant

Version 2. This revision merges the original implementation summary with the Problem 2 engineering guide and adds configurable options. Items marked **[Implemented]** match the current `assistant.py` as described in the README. Items marked **[Option]** are additions or alternatives that can be enabled without changing the core safety model.

## 1. Overview

A local, natural-language assistant (CLI first, optional TUI) that inspects a Linux laptop's hardware and answers questions such as "Can I upgrade my RAM, and by how much?".

The core rule: **the LLM never runs shell commands.** It can only select named tools. Each tool runs one fixed command (or reads one allow-listed file) and returns parsed JSON. Answers are written only from that JSON. Missing data is reported as:

> I cannot determine this hardware detail from system data.

### Goals

- Accurate, grounded answers with no hallucinated specs.
- No arbitrary code execution, regardless of what the model outputs.
- Python 3.8+, standard library only, for the default path.
- Fully private operation available (local model or offline router).
- Graceful degradation when binaries, permissions, or data are missing.

### Non-Goals

- Changing system state, installing packages, or making network calls from tools.
- Running the assistant as root.
- Live hardware data on Windows or macOS (`--demo` only).

### Key Engineering Challenges and How the Design Answers Them

| Challenge | Answer |
|---|---|
| Shell injection, flag manipulation, arbitrary binaries | Tool allow-list, fixed argv, `shell=False`, validated arguments |
| `dmidecode` needs root; running the agent as root is unsafe | Scoped privilege strategy (Section 6), never root |
| Distro-dependent, messy CLI output | Per-tool parsers to a stable JSON schema, `LC_ALL=C`, optional `jc` |
| Hallucination and guessing | Answers from JSON only; explicit "cannot determine" path that also names the missing tool |

## 2. Architecture

```
 User question (CLI / TUI / one-shot)
        |
        v
 +--------------------+
 | Conversation state |  history + cached hardware JSON
 +---------+----------+
           |
           v
 +--------------------+   tool call {name, args}   +---------------------+
 | Planner            | -------------------------> | Tool Registry       |
 | LLM or offline     |                            | allow-list, schema  |
 | router             |                            | validation,         |
 +---------+----------+                            | capability discovery|
           ^                                       +----------+----------+
           |                                                  |
           |                                                  v
           |                                       +---------------------+
           |                                       | Executor            |
           |                                       | fixed argv, no      |
           |                                       | shell, 5 s timeout, |
           |                                       | optional sudo -n    |
           |                                       +----------+----------+
           |                                                  |
           |        parsed JSON                               v
           |<------------------------------------+---------------------+
           |                                     | Parser / Normalizer |
           v                                     | text -> JSON        |
 +--------------------+                          +---------------------+
 | Answer writer      |
 | JSON-only grounding|
 +--------------------+
```

### Components

| Component | Responsibility |
|---|---|
| Conversation state | Holds recent turns and previously fetched JSON so follow-ups ("and the GPU?") reuse data |
| Planner | Maps the question to tool calls using an LLM or the deterministic offline router |
| Tool Registry | Defines tools and their argument schemas; registers a tool only if its binary exists |
| Executor | Runs the single fixed command; applies timeout, environment, and privilege rules |
| Parser / Normalizer | Converts raw output into validated JSON with `null` for unknown values |
| Answer writer | Writes the reply from JSON only and applies the uncertainty rule |

## 3. Request Flow

1. User asks a question.
2. Planner receives the question plus the tools **available on this machine**.
3. Planner returns tool calls, for example `{"tool": "get_memory_topology", "args": {"include_empty_slots": true}}`.
4. Registry validates the name and arguments; unknown tools or arguments are rejected.
5. Cache is checked; on a miss the executor runs the fixed command.
6. Parser produces JSON, which is cached.
7. Answer writer replies from the JSON only.
8. With `--debug`, the tools called are printed.

## 4. Tools

Tools are registered only when their binaries exist (capability discovery with `shutil.which`). `--tools` lists what is registered on this machine. Tool names below follow the reference guide's naming.

### 4.1 Core tools [Implemented]

| Tool | Source | Returns |
|---|---|---|
| `get_cpu_info` | `lscpu` | Model, architecture, cores, threads, frequency |
| `get_block_devices` | `lsblk` | Disks and partitions, size, type, rotational flag, transport, mount points |
| `get_memory_summary` | `free`, `/proc/meminfo` | Total, used, available RAM |
| `get_memory_topology` | `dmidecode -t memory` (scoped sudo) | Slots total/used/free, module sizes, type, speed, maximum capacity |
| `get_pci_devices` | `lspci` | PCI devices including GPUs |
| `get_nvidia_gpu` | `nvidia-smi` | GPU name, VRAM, driver (only if installed) |
| `get_os_info` | `/etc/os-release` | Distribution and version |
| `get_system_info` | `/sys/class/dmi/id/` | Vendor, model, BIOS version |
| `get_battery_info` | `/sys/class/power_supply/` | Presence, capacity, status, health where exposed |

### 4.2 Optional additional tools [Option]

All follow the same allow-list pattern and are off unless their binary or file exists and the tool is enabled in config.

| Tool | Source | Use |
|---|---|---|
| `get_network_adapters` | `lspci`, `/sys/class/net/` (names and MAC only) | "What Wi-Fi card do I have?" |
| `get_display_info` | `/sys/class/drm/` | Panel connectors and resolutions |
| `get_usb_devices` | `lsusb` | Connected USB devices |
| `get_thermal_sensors` | `/sys/class/thermal/` | Temperatures, read-only |
| `get_summary_inxi` | `inxi -F` | Broad summary when `inxi` is installed, used as a cross-check |

### 4.3 Tool definition schema

```json
{
  "name": "get_memory_topology",
  "description": "Retrieves physical RAM details including installed capacity, total slots, free slots, speed, and maximum supported capacity.",
  "parameters": {
    "type": "object",
    "properties": {
      "include_empty_slots": {
        "type": "boolean",
        "description": "Set to true to return details on unused memory slots."
      }
    },
    "required": []
  }
}
```

Schemas are shared by the planner (so the model sees accurate tool descriptions) and the validator (so rejection rules never drift from what is advertised).

### 4.4 Example: RAM upgrade question

```json
{
  "total_gb": 16.0,
  "slots_total": 2,
  "slots_used": 1,
  "slots_free": 1,
  "modules": [{"size_gb": 16, "type": "DDR4", "speed_mhz": 3200}],
  "max_capacity_gb": 64,
  "source": ["free", "dmidecode"]
}
```

From this the answer can say: 16 GB installed in 1 of 2 slots, maximum supported 64 GB, so up to 48 GB more is possible in principle. If `dmidecode` is unavailable, `slots_*` and `max_capacity_gb` are `null` and the reply reports total RAM from `free` while stating that slots and maximum cannot be determined.

## 5. Security Design

Threat model: model output is untrusted (prompt injection, hallucination, a misbehaving model), and strings reported by hardware (model names, labels) are data, not instructions.

### Controls [Implemented]

1. **Tool and binary allow-list**: `lscpu, lsblk, free, lspci, nvidia-smi, dmidecode`.
2. **No shell**: `shell=False`, fixed argv; no `;`, `&&`, or pipes are ever interpreted.
3. **Argument validation**: booleans or regex-restricted strings only; unknown tools or arguments are rejected, not ignored.
4. **File read allow-list**: `/etc/os-release`, `/sys/class/dmi/id/`, `/sys/class/power_supply/`, `/proc/meminfo`. Paths are resolved before checking to defeat `..` and symlink tricks.
5. **`sudo -n` only for `dmidecode`**: never prompts, never hangs.
6. **5 s timeout** on every command.
7. **`LC_ALL=C`** for stable, parseable output.
8. **JSON-only grounding**: the answer writer never sees raw command output.

### Additional hardening [Option]

- **Argument-pinned sudo** (see 6.1): the sudoers rule names the exact arguments, not just the binary.
- **Output size cap**: truncate stdout above a fixed size before parsing.
- **Audit log**: append-only local log of tool name, arguments, exit status, and duration (no raw output), enabled with `--audit-log PATH`.
- **Prompt-injection hygiene**: hardware strings are length-limited and stripped of control characters before they enter the model context, and always passed as JSON values.
- **Read-only mode check**: refuse to start if running as root unless `--allow-root` is explicitly set.

## 6. Privilege Strategy for `dmidecode`

The assistant must not run as root. Three strategies are supported; the default is A.

| Strategy | Mechanism | Trade-offs | Best for |
|---|---|---|---|
| **A. Scoped sudo rule** (default) [Implemented] | One sudoers line allowing exactly `dmidecode -t memory` | Simple, standard Linux pattern, no service to install | CLI use and demos |
| **B. Privileged helper daemon** [Option] | Small root service on a Unix domain socket with fixed endpoints such as `GetDmiMemory` | Strongest isolation, enforces policy centrally, needs a system service | Enterprise or desktop deployments |
| **C. Non-root fallback** [Implemented] | `free`, `/proc/meminfo`, `/sys/class/dmi/id/`, `lscpu`, `lsblk`, `lspci` only | No setup, but slot count and maximum RAM are unavailable | Zero-install use |

### 6.1 Strategy A: scoped sudo rule

```
sudo visudo -f /etc/sudoers.d/hw-assistant
```

```
yourusername ALL=(root) NOPASSWD: /usr/sbin/dmidecode -t memory
```

Notes:

- Allowing bare `/usr/sbin/dmidecode` would permit any `dmidecode` argument, including options that write files. Pinning `-t memory` avoids that.
- Always edit with `visudo` so a syntax error cannot lock out sudo.
- The binary path can differ by distro (`/usr/sbin` vs `/usr/bin`). Discovery resolves the absolute path and the setup helper prints the matching rule.

### 6.2 Strategy B: helper daemon [Option]

- Runs as a systemd service under root, listening on a socket owned by a dedicated group.
- Exposes only named endpoints (for example `GetDmiMemory`), never a generic "run command".
- Returns already-parsed JSON, so the unprivileged client does no parsing of privileged output.
- Selected with `HW_PRIVILEGE=daemon`.

### 6.3 Selecting a strategy [Option]

`--privilege {sudo,daemon,none}` or `HW_PRIVILEGE`. `none` forces Strategy C and makes the "cannot determine" message explicit about what root-only data is being skipped.

## 7. Parsing and Normalization

### Principles

- Every tool returns a fixed schema; unknown values are `null`, never guessed.
- Parsers run on `LC_ALL=C` output only.
- Units are normalized (bytes or GB as numbers, not strings like "16 GB").

### Parser options

| Source | Default parser | Optional |
|---|---|---|
| `lsblk` | `lsblk -J` native JSON | none needed |
| `lscpu` | key/value split on first `:` | `jc --lscpu` |
| `free` | column parse of `free -b` | `jc --free` |
| `dmidecode -t memory` | block-based regex parser | `jc --dmidecode` |
| `lspci` | line parser | `jc --lspci` |

**[Option]** If the `jc` package is installed and `--parser jc` is set, it is used for the commands it supports, with the built-in parser as fallback. The default path stays dependency-free.

### dmidecode parsing note

Parse per **Memory Device** block rather than counting every `Size:` line across the whole output. Slots with "No Module Installed" are counted as free; the **Physical Memory Array** block supplies `Maximum Capacity` and `Number Of Devices`. Block-based parsing avoids miscounting on machines that report extra sections.

## 8. LLM Backends and Planner Options

Selected in this order unless overridden by `--backend`:

| Condition | Backend | Privacy |
|---|---|---|
| `ANTHROPIC_API_KEY` set | Anthropic API | Question and tool JSON go to the cloud |
| `OLLAMA_MODEL` set (e.g. `qwen2.5:7b`) | Local Ollama model | Fully local |
| Neither | Deterministic offline router | Fully local, no model |

### Additional backend options [Option]

- `OPENAI_API_KEY`: OpenAI-compatible endpoint for cloud function calling.
- `OPENAI_BASE_URL`: any OpenAI-compatible local server (vLLM, llama.cpp server) for a private setup.
- `--backend {auto,anthropic,openai,ollama,offline}`: force a backend.
- **Hybrid mode**: LLM plans the tool calls, offline templates write the answer (`--answer-mode template`). This makes the answer text fully deterministic and removes the model from the answering step entirely.

All backends share one interface: question plus tool list in, validated tool calls out; then JSON in, text out. Swapping backends cannot expand what the system is able to do.

### System prompt requirement

> Base your response solely on the provided JSON tool output. If the JSON does not contain the answer, state "I cannot determine this hardware detail from system data" and list which tools or data were unavailable.

## 9. Offline Router

The deterministic router maps keywords and intents to tools (for example "ram", "memory", "upgrade" to `get_memory_summary` and `get_memory_topology`). It also computes simple derived answers (such as maximum capacity minus installed capacity) so that the no-LLM mode remains useful. It is the backend used by the test suite.

## 10. Caching and Multi-Turn Behavior

- Tool results are cached per tool and argument set in memory for **60 s** **[Implemented]**.
- Follow-ups reuse cached JSON before running commands again.
- **[Option]** `--cache-ttl SECONDS` (0 disables caching, useful for battery or thermal readings).
- **[Option]** Volatile tools (battery, thermal) default to a shorter TTL than static ones (CPU model, DMI).
- The cache is in memory only and is never written to disk.

## 11. Error Handling and Missing Data

| Situation | Behavior |
|---|---|
| Binary not installed | Tool not registered; the planner is not offered it |
| `dmidecode` lacks permission | `sudo -n` fails fast; fall back to `free`; slot and max fields are `null` |
| Command times out or exits non-zero | Tool returns an error object; answer reports the data as unavailable |
| Parser cannot find a field | Field is `null` |
| Model requests an unknown tool or argument | Rejected; the model is told and may retry once |
| No relevant data | "I cannot determine this hardware detail from system data" plus the list of unavailable tools |

### Fallback hierarchy

1. Primary tool (for example `dmidecode`).
2. Unprivileged source (`/proc/meminfo`, `/sys/class/dmi/id/`).
3. Report as undeterminable. The answer writer never fills gaps from general knowledge about the laptop model.

## 12. User Interface Options

| Mode | Command | Status |
|---|---|---|
| Interactive chat | `python assistant.py` | [Implemented] |
| One-shot question | `python assistant.py "How much RAM can I add?"` | [Implemented] |
| Demo data | `python assistant.py --demo` | [Implemented] |
| List tools | `python assistant.py --tools` | [Implemented] |
| Debug tool trace | `python assistant.py --debug` | [Implemented] |
| Machine-readable output | `--json` returns `{answer, tools_called, data}` | [Option] |
| Show evidence | `--explain` appends the JSON fields each statement came from | [Option] |
| Full-screen UI | `--tui` using Textual | [Option] |
| Typer-based CLI | Subcommands (`ask`, `tools`, `setup-sudo`) | [Option] |
| Setup helper | `--setup-sudo` prints the exact sudoers rule for this machine | [Option] |

## 13. Configuration [Option]

Optional `~/.config/hw-assistant/config.toml` (Python 3.11+ `tomllib`, otherwise environment variables and flags only):

```toml
backend = "auto"
privilege = "sudo"        # sudo | daemon | none
cache_ttl = 60
parser = "builtin"        # builtin | jc
answer_mode = "llm"       # llm | template
audit_log = ""            # path, empty disables
enabled_tools = ["cpu", "memory", "storage", "gpu", "battery", "os", "system"]
```

Precedence: command-line flags, then environment variables, then config file, then defaults.

## 14. Demo Mode

`--demo` replaces the executor with a fixture provider returning sample laptop JSON. Planner, validation, cache, and answer logic are unchanged, so demo mode exercises the real path and works on Windows and macOS. **[Option]** `--demo-profile {thinkpad,macbook-like,desktop,low-ram}` loads different fixtures to demonstrate full-slot, empty-slot, and missing-data scenarios.

## 15. Testing

`test_assistant.py` (9 tests, `python -m unittest test_assistant -v`) runs without Linux hardware or network, using fixtures and the offline router.

Coverage targets:

- Parsers produce expected JSON from sample outputs.
- Unknown tools and arguments are rejected.
- Argument validation rejects wrong types and regex violations.
- File reads outside the allow-list are refused.
- Tools are registered only when binaries exist.
- Missing data produces the standard message.
- Cache serves repeats within 60 s and expires afterward.
- Offline router maps representative questions to correct tools.
- `dmidecode` fallback reports slots and maximum capacity as unavailable.

**[Option]** Extra tests worth adding: path traversal and symlink attempts on file reads, injection strings in hardware fields, `dmidecode` output with empty slots and multiple arrays, and a root-start refusal check.

## 16. Distro Portability

- `LC_ALL=C` everywhere.
- Absolute binary paths resolved at startup via `shutil.which` and checked against the allow-list.
- Tools missing on a distro are simply not offered.
- Tested output samples should cover at least Debian/Ubuntu, Fedora, and Arch variations of `lscpu`, `lsblk`, and `dmidecode`.

## 17. Privacy Modes

| Mode | What leaves the machine |
|---|---|
| Offline router | Nothing |
| Ollama (local model) | Nothing |
| Cloud backend | The question and the tool JSON returned for it |

**[Option]** `--redact` strips serial numbers, UUIDs, and MAC addresses from tool JSON before it goes to any cloud backend.

## 18. File Layout

```
assistant.py        Planner, registry, executor, parsers, CLI
test_assistant.py   Test suite (9 tests)
README.md           Usage and setup
design.md           This document
```

**[Option]** If the code grows, split into `tools/`, `parsers/`, `backends/`, and `cli.py` while keeping the single-file entry point working.

## 19. Interview Talking Points

- **Why tool-calling instead of free shell?** The model's authority is limited to choosing a name from a short list; compromise of the model does not mean compromise of the machine.
- **Why not run as root?** Least privilege. A scoped sudo rule or a helper daemon grants one capability instead of everything.
- **How is hallucination controlled?** Answers come only from parsed JSON, with an explicit "cannot determine" path and no filling from general knowledge.
- **How does it handle different distros?** Capability discovery, `LC_ALL=C`, and fixed schemas with `null` for unknown values.
- **How is privacy handled?** Offline router and local models mean no hardware data leaves the laptop; `--redact` limits exposure when using cloud models.
