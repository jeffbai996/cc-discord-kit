#!/usr/bin/env python3
"""Agent View — surface Claude Code subagent activity into Discord.

Mirrors the Claude Code terminal's bottom-row agent indicators as a
live-edited panel pinned to the bottom of the tool-trace message
(or a standalone message when no trace exists), plus an on-demand
`!agents` passthrough snapshot.

Entry modes (see main()):
  --mode pre        PreToolUse hook — register an Agent/Task spawn
  --mode post       PostToolUse hook — settle status (done/failed)
  --mode finalize   Stop hook — standalone-panel cleanup + state pruning
  --updater         detached poller that owns mid-flight panel edits
  --snapshot        render the current panel once to stdout (!agents)

Display is driven entirely by hooks + the poller — no LLM in the loop.
Spec: cc-discord-kit/docs/specs/2026-06-11-agent-view-design.md
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

_AV_STATE_DIR = os.path.expanduser("~/.local/state/cc-discord-kit")
LOG_PATH = os.environ.get(
    "CCDK_AGENT_VIEW_LOG", os.path.join(_AV_STATE_DIR, "agent_view.log"))


def log(msg: str) -> None:
    try:
        os.makedirs(_AV_STATE_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- render

_MODEL_NEEDLES = ("opus", "sonnet", "haiku", "fable")


def model_alias(model: str | None) -> str:
    """Short display alias: full model id or bare alias -> family name."""
    if not model:
        return "?"
    for needle in _MODEL_NEEDLES:
        if needle in model:
            return needle
    return model.replace("claude-", "")[:10]


def fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s" if s % 60 else f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60}m"


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


_GLYPH = {"running": "○", "done": "●", "failed": "✗", "lost": "✗"}

# Header blinker: cycles one frame per updater tick while agents run, so
# a glance at the top-left shows the panel is live-updating. Dropped on
# the final render.
_SPINNER = ("◐", "◓", "◑", "◒")


def _format_rows(rows: list[tuple[str, ...]]) -> str:
    if not rows:
        return "  (none)"
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    lines = []
    for g, label, model, elapsed, tok in rows:
        lines.append("  {}  {}  {}  {}  {}".format(
            g, label.ljust(widths[1]), model.ljust(widths[2]),
            elapsed.rjust(widths[3]), tok.rjust(widths[4])).rstrip())
    return "\n".join(lines)


def render_panel(bot_name: str, agents: list[dict], now: float,
                 max_chars: int = 900, stale: bool = False,
                 spinner_frame: int | None = None) -> str:
    """Render the agents panel as a fenced code block.

    Row: `  <glyph>  <label>  <model>  <elapsed>  <tokens>`, columns
    padded to the widest entry. When over max_chars, finished rows are
    dropped oldest-first and summarized as an `… +N more` marker —
    running agents are never dropped before finished ones.
    """
    running = [a for a in agents if a["status"] == "running"]
    finished = [a for a in agents if a["status"] != "running"]
    total_tok = sum(a.get("tokens") or 0 for a in agents)
    header = (f"agents · {bot_name} · {len(running)} running · "
              f"{len(finished)} done · {fmt_tokens(total_tok)} tok")
    if spinner_frame is not None:
        header = _SPINNER[spinner_frame % len(_SPINNER)] + " " + header
    if stale:
        header += " · stale"

    def row_cells(a: dict) -> tuple[str, str, str, str, str]:
        end = a.get("ended_at") or now
        return (_GLYPH.get(a["status"], "?"), a["label"][:24],
                model_alias(a.get("model")),
                fmt_elapsed(max(0.0, end - a["started_at"])),
                fmt_tokens(a.get("tokens") or 0))

    kept = list(agents)
    dropped = 0
    while True:
        body = _format_rows([row_cells(a) for a in kept])
        out = "```\n" + header + "\n\n" + body
        if dropped:
            out += f"\n  … +{dropped} more"
        out += "\n```"
        if len(out) <= max_chars or len(kept) <= 1:
            return out
        # drop the oldest finished row first; running rows only as a
        # last resort
        for i, a in enumerate(kept):
            if a["status"] != "running":
                del kept[i]
                break
        else:
            del kept[0]
        dropped += 1


# ------------------------------------------------------------- registry

def _av_state_path() -> str:
    return os.environ.get(
        "CCDK_AGENT_VIEW_STATE",
        os.path.join(_AV_STATE_DIR, "agent_view_state.json"))


def _load_av_state() -> dict:
    try:
        with open(_av_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_av_state(state: dict) -> None:
    path = _av_state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# -------------------------------------------------------------- recorder

_AGENT_TOOLS = ("Agent", "Task")


def _bot_key() -> str:
    """Per-bot namespace for the shared state file. Bots co-located on
    one machine share one agent_view_state.json — without
    this prefix, one bot's !agents snapshot serves another bot's
    registry. Reuses react_hook's _bot_id partitioning via narrate."""
    try:
        from narrate import _bot_id
        return _bot_id()
    except Exception:  # noqa: BLE001
        return "bot"


def _session_key(session_id: str) -> str:
    return f"{_bot_key()}:{session_id}"


def _agent_key(tool_input: dict) -> str:
    raw = (tool_input.get("description", "") + "\x00"
           + tool_input.get("prompt", ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _latest_discord_origin(transcript_path: str) -> tuple[str, str] | None:
    """Most recent Discord (chat_id, message_id) anywhere in the
    transcript, newest-first.

    The newest user entry alone isn't enough — by the time an Agent
    spawns it's often skill-injection content or a tool result, not the
    channel-tagged message. The panel belongs to whichever channel
    spoke most recently, so walk back."""
    from narrate import _extract_user_text, parse_discord_origins
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "user":
            continue
        txt = _extract_user_text(obj)
        if not txt:
            continue
        origins = parse_discord_origins(txt)
        if origins:
            return origins[-1]
    return None


def _resolve_chat_id(payload: dict) -> str | None:
    """Per-turn inbound-channel detection via transcript walk-back."""
    try:
        origin = _latest_discord_origin(
            payload.get("transcript_path", "") or "")
        return origin[0] if origin else None
    except Exception as exc:  # noqa: BLE001 — hook must never crash the turn
        log(f"chat_id resolution failed: {exc}")
        return None


def handle_pre(payload: dict) -> int:
    """PreToolUse on Agent|Task: register the spawn, ensure the updater."""
    if payload.get("tool_name") not in _AGENT_TOOLS:
        return 0
    tool_input = payload.get("tool_input") or {}
    session = payload.get("session_id") or ""
    if not session:
        return 0
    skey = _session_key(session)
    state = _load_av_state()
    sess = state.setdefault(skey, {
        "chat_id": None,
        "transcript_path": payload.get("transcript_path"),
        "updater_pid": None,
        "standalone_msg_id": None,
        "agents": {},
    })
    if sess.get("chat_id") is None:
        sess["chat_id"] = _resolve_chat_id(payload)
    base = _agent_key(tool_input)
    key, n = base, 1
    while key in sess["agents"]:
        n += 1
        key = f"{base}:{n}"
    sess["agents"][key] = {
        "label": (tool_input.get("description") or "agent")[:48],
        "model": tool_input.get("model"),
        "status": "running",
        "started_at": time.time(),
        "ended_at": None,
        "tokens": 0,
        "prompt_sha": base,
        "agent_id": None,
        "transcript": None,
        # verbatim prompt head — the updater matches registrations to
        # subagent transcript files by prefix until agentId arrives
        "_prompt": (tool_input.get("prompt") or "")[:200],
    }
    _save_av_state(state)
    log(f"registered {key} ({sess['agents'][key]['label']}) in {skey}")
    _ensure_updater(skey)
    return 0


def handle_post(payload: dict, failed: bool) -> int:
    """PostToolUse / PostToolUseFailure on Agent|Task: settle status."""
    if payload.get("tool_name") not in _AGENT_TOOLS:
        return 0
    tool_input = payload.get("tool_input") or {}
    resp = payload.get("tool_response") or {}
    session = payload.get("session_id") or ""
    state = _load_av_state()
    sess = state.get(_session_key(session))
    if not sess:
        return 0
    base = _agent_key(tool_input)
    target = None
    for want in ("running", "lost"):
        for agent in sess["agents"].values():
            if agent["prompt_sha"] == base and agent["status"] == want:
                target = agent
                break
        if target:
            break
    if target is None:
        return 0
    if not failed and isinstance(resp, dict) and (
            resp.get("isAsync") or resp.get("status") == "async_launched"):
        # run_in_background: this PostToolUse only means "launched" — the
        # agent is still working. Record the definitive agentId, flag it
        # async, and let the updater settle it when the harness drops a
        # <task-notification> into the parent transcript.
        target["async"] = True
        if resp.get("agentId"):
            target["agent_id"] = resp["agentId"]
        _save_av_state(state)
        log(f"async launch {target['label']} in {_session_key(session)}")
        return 0
    # the harness's completion event is ground truth — it overrides a
    # provisional lost-marking from the updater
    target["status"] = "failed" if failed else "done"
    target["ended_at"] = max(time.time(), target.get("started_at") or 0)
    if isinstance(resp, dict) and resp.get("agentId"):
        target["agent_id"] = resp["agentId"]
    _save_av_state(state)
    log(f"settled {target['label']} -> {target['status']} in {_session_key(session)}")
    return 0


# --------------------------------------------------------------- updater

_LOST_AFTER = 15 * 60       # running + transcript silent this long -> lost
_HARD_TIMEOUT = 2 * 3600    # updater self-destructs after this


def all_terminal(agents: dict) -> bool:
    return all(a["status"] != "running" for a in agents.values())


def tick_agents(agents: dict, subagents_dir: str, now: float) -> None:
    """One poll pass: link new transcripts, refresh tokens/model, detect
    lost agents. Mutates the agent records in place."""
    match_transcripts(subagents_dir, agents)
    for a in agents.values():
        if not a.get("transcript"):
            continue
        stats = read_transcript_stats(a["transcript"])
        if stats["tokens"]:
            a["tokens"] = stats["tokens"]
        if stats["model"] and not a.get("model"):
            a["model"] = stats["model"]
        if (a["status"] == "running" and not a.get("async")
                and stats["last_ts"]
                and stats["last_ts"] >= (a.get("started_at") or 0)
                and now - stats["last_ts"] > _LOST_AFTER):
            # last_ts < started_at would mean we linked a stale file —
            # that's a matching bug, not a silent agent; never "lose" it
            a["status"] = "lost"
            a["ended_at"] = stats["last_ts"]


def _async_completions(transcript_path: str, agent_ids: list,
                       tail_bytes: int = 262144) -> dict:
    """Scan the parent transcript's tail for <task-notification> records.
    Background agents have no completion hook — the harness instead
    injects a notification with the task-id (== agentId) and status into
    the parent session. Returns {agent_id: "done"|"failed"}."""
    out = {}
    if not agent_ids:
        return out
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "r", encoding="utf-8",
                  errors="replace") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # drop the partial first line
            text = f.read()
    except OSError:
        return out
    for aid in agent_ids:
        idx = text.find(f"<task-id>{aid}</task-id>")
        if idx == -1:
            continue
        m = re.search(r"<status>(\w+)</status>", text[idx:idx + 2000])
        status = m.group(1) if m else "completed"
        out[aid] = "done" if status in ("completed", "done",
                                        "success") else "failed"
    return out


def settle_async(sess: dict, now: float) -> bool:
    """Settle async agents whose completion notification has landed in
    the parent transcript. Returns True if anything changed."""
    pending = {a["agent_id"]: a for a in sess.get("agents", {}).values()
               if a.get("async") and a["status"] == "running"
               and a.get("agent_id")}
    if not pending:
        return False
    changed = False
    found = _async_completions(sess.get("transcript_path") or "",
                               list(pending))
    for aid, status in found.items():
        pending[aid]["status"] = status
        pending[aid]["ended_at"] = now
        changed = True
    return changed


def _subagents_dir(transcript_path: str) -> str:
    base, _ = os.path.splitext(transcript_path)
    return os.path.join(base, "subagents")


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


_SPAWN_CLAIM_TTL = 30


def _updater_claimed(value, now: float) -> bool:
    """Is the updater slot taken — either a live pid or a fresh
    'spawning:<ts>' claim from a fork that hasn't written its pid yet?"""
    if isinstance(value, str) and value.startswith("spawning:"):
        try:
            return now - float(value.split(":", 1)[1]) < _SPAWN_CLAIM_TTL
        except (ValueError, IndexError):
            return False
    return _pid_alive(value)


def _ensure_updater(session: str) -> None:
    """Spawn the detached poller for this session if not already alive.

    Hook processes must exit promptly (the harness waits on them), so
    the updater is double-forked + setsid'd to detach fully. The
    grandchild re-execs this script with --updater so it gets a clean
    interpreter state instead of inheriting the hook's.

    The claim is written under the shared state lock BEFORE forking —
    two near-simultaneous registrations otherwise both pass a naive
    liveness check, spawn two updaters, and double-post the panel."""
    try:
        from narrate import _state_lock
        with _state_lock():
            state = _load_av_state()
            sess = state.get(session) or {}
            if _updater_claimed(sess.get("updater_pid"), time.time()):
                return
            if session in state:
                state[session]["updater_pid"] = f"spawning:{time.time()}"
                _save_av_state(state)
    except Exception as exc:  # noqa: BLE001 — fall back to unserialized
        log(f"spawn claim failed ({exc}); proceeding unlocked")
        if _pid_alive((_load_av_state().get(session) or {}).get("updater_pid")):
            return
    try:
        pid = os.fork()
    except OSError as exc:
        log(f"fork failed: {exc}")
        return
    if pid != 0:
        os.waitpid(pid, 0)  # reap the intermediate child
        return
    # child: detach and exec the updater
    try:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        os.execv(sys.executable, [
            sys.executable, os.path.abspath(__file__),
            "--updater", "--session", session])
    except Exception:  # noqa: BLE001 — never let the fork path escape
        os._exit(1)


def run_updater(session: str) -> None:
    """Detached poller: refresh stats and publish the panel every tick
    until every agent is terminal (or the hard timeout trips)."""
    tick = float(os.environ.get("CCDK_AGENT_VIEW_TICK", "2"))
    deadline = time.time() + _HARD_TIMEOUT
    from narrate import _state_lock
    with _state_lock():
        state = _load_av_state()
        if session in state:
            # second line of defense: if a DIFFERENT live updater wrote
            # its pid first, yield instead of double-driving the panel
            existing = state[session].get("updater_pid")
            if (existing and existing != os.getpid()
                    and _pid_alive(existing)):
                log(f"updater for {session}: {existing} already live, yielding")
                return
            state[session]["updater_pid"] = os.getpid()
            _save_av_state(state)
    log(f"updater started for {session[:8]} (pid {os.getpid()})")
    frame = 0
    while True:
        state = _load_av_state()
        sess = state.get(session)
        if not sess or not sess.get("agents"):
            log(f"updater for {session[:8]}: nothing to track, exiting")
            return
        now = time.time()
        tick_agents(sess["agents"],
                    _subagents_dir(sess.get("transcript_path") or ""), now)
        settle_async(sess, now)
        _save_av_state(state)
        stale = now > deadline
        final = all_terminal(sess["agents"])
        try:
            publish_panel(session, sess, final=final, stale=stale,
                          spinner_frame=None if final else frame)
        except Exception as exc:  # noqa: BLE001 — keep polling on error
            log(f"publish failed: {exc}")
        frame += 1
        if final or stale:
            log(f"updater for {session[:8]} done (final={final} stale={stale})")
            return
        time.sleep(tick)


# ------------------------------------------------------------- publisher
#
# Thin seams over narrate/tool_watcher so tests can monkeypatch Discord
# I/O and turn lookup without importing their full machinery.

def _bot_name() -> str:
    """Panel-header identity. Precedence: SQUAD_STORE_BOT env override;
    the squad bot registry (bot_config.detect_bot resolves co-located
    bots by config/state dir — turns ~/.claude into 'fraggy' instead of
    the cwd basename); session cwd basename as the last resort."""
    name = os.environ.get("SQUAD_STORE_BOT")
    if name:
        return name
    try:
        store_dir = os.path.dirname(SCRIPT_DIR)  # bot_config.py at repo root
        if store_dir not in sys.path:
            sys.path.insert(0, store_dir)
        import bot_config
        from narrate import detect_discord_state_dir
        detected = bot_config.detect_bot(
            config_dir=os.environ.get("CLAUDE_CONFIG_DIR", ""),
            discord_state_dir=detect_discord_state_dir())
        if detected:
            return detected
    except Exception:  # noqa: BLE001 — cosmetic, never block the panel
        pass
    cwd = os.path.basename(os.getcwd().rstrip("/"))
    return cwd or "bot"


def _tools_mode_for(chat_id: str) -> str:
    from narrate import _channel_tools_mode, detect_discord_state_dir
    return _channel_tools_mode(detect_discord_state_dir(), chat_id)


def _bot_token() -> str | None:
    from narrate import detect_discord_state_dir, read_bot_token
    return read_bot_token(detect_discord_state_dir())


def _discord_send(token: str, chat_id: str, content: str) -> str | None:
    from narrate import discord_send_message
    return discord_send_message(token, chat_id, content)


def _discord_edit(token: str, chat_id: str, msg_id: str,
                  content: str) -> bool:
    from narrate import discord_edit_message
    return discord_edit_message(token, chat_id, msg_id, content)


def _discord_delete(token: str, chat_id: str, msg_id: str) -> bool:
    from narrate import discord_delete_message
    return discord_delete_message(token, chat_id, msg_id)


def _reply_count(transcript_path: str) -> int:
    try:
        from narrate import count_discord_replies
        return count_discord_replies(transcript_path)
    except Exception:  # noqa: BLE001
        return 0


def _narrate_turn_for(sess: dict) -> tuple[str | None, dict | None]:
    """Resolve the narrate turn record for this session's current turn,
    or (None, None) when there's no live tool-trace to ride."""
    try:
        from narrate import _load_state, _turn_key
        path = sess.get("transcript_path") or ""
        origin = _latest_discord_origin(path)
        if not origin or origin[0] != sess.get("chat_id"):
            return None, None
        key = _turn_key(path)
        if not key:
            return None, None
        return key, _load_state().get(key)
    except Exception as exc:  # noqa: BLE001
        log(f"turn lookup failed: {exc}")
        return None, None


def _save_narrate_turn(turn_key: str, turn: dict) -> None:
    from narrate import _load_state, _save_state
    state = _load_state()
    state[turn_key] = turn
    _save_state(state)


def _update_sess_fields(session: str, sess: dict, **fields) -> None:
    """Persist standalone-panel bookkeeping without clobbering agent
    records other processes may have updated since our load."""
    sess.update(fields)
    state = _load_av_state()
    if session in state:
        state[session].update(fields)
        _save_av_state(state)


_DISCORD_LIMIT = 2000


def publish_panel(session: str, sess: dict, final: bool,
                  stale: bool, spinner_frame: int | None = None) -> None:
    """Render the panel and push it: as the tool-trace footer when a
    live trace message exists for this turn, standalone otherwise."""
    chat_id = sess.get("chat_id")
    if not chat_id or not sess.get("agents"):
        return
    mode = _tools_mode_for(chat_id)
    if mode == "off":
        return  # no live panel; !agents still reads the registry
    token = _bot_token()
    if not token:
        return
    agents = list(sess["agents"].values())
    now = time.time()
    panel = render_panel(_bot_name(), agents, now, stale=stale,
                         spinner_frame=spinner_frame)

    turn_key, turn = _narrate_turn_for(sess)
    if turn is not None and turn.get("tool_msg_id") \
            and not turn.get("finalized"):
        # Footer mode. Lock so we don't interleave with a concurrent
        # tool_watcher edit of the same turn record.
        from narrate import _state_lock
        with _state_lock():
            from narrate import _load_state
            turn = _load_state().get(turn_key) or turn
            import tool_watcher as tw
            body_len = len(tw._tool_message_content(
                turn.get("tool_buffer", "")))
            budget = max(200, _DISCORD_LIMIT - body_len - 1)
            if len(panel) > budget:
                panel = render_panel(_bot_name(), agents, now,
                                     max_chars=budget, stale=stale,
                                     spinner_frame=spinner_frame)
            turn["agent_panel"] = panel
            content = tw._tool_message_content(
                turn.get("tool_buffer", ""), panel=panel)
            _discord_edit(token, chat_id, turn["tool_msg_id"], content)
            _save_narrate_turn(turn_key, turn)
        if sess.get("standalone_msg_id"):
            # a trace appeared mid-burst — the footer supersedes the
            # standalone panel
            _discord_delete(token, chat_id, sess["standalone_msg_id"])
            _update_sess_fields(session, sess, standalone_msg_id=None)
        return

    # Standalone fallback — no trace message to ride.
    #
    # Card lifecycle is self-healing: every card we ever posted is
    # tracked in sess["panel_cards"]; everything except the current
    # card gets delete-retried every tick. A single failed delete
    # (rate limit, transient 5xx) therefore can't orphan a frozen
    # duplicate — seen live 2026-06-11 when each owner reply triggered
    # a delete+repost cycle and one delete silently failed.
    replies = _reply_count(sess.get("transcript_path") or "")
    # last-moment re-read: pick up a card another process just created
    fresh = _load_av_state().get(session) or {}
    if fresh.get("standalone_msg_id") and not sess.get("standalone_msg_id"):
        sess["standalone_msg_id"] = fresh["standalone_msg_id"]
        sess["standalone_replies_at_create"] = fresh.get(
            "standalone_replies_at_create", 0)
    sess.setdefault("panel_cards", fresh.get("panel_cards", []))

    displaced = (sess.get("standalone_msg_id")
                 and replies > sess.get("standalone_replies_at_create", 0)
                 and now - sess.get("standalone_reposted_at", 0)
                 > _REPOST_COOLDOWN)
    if displaced or not sess.get("standalone_msg_id"):
        # post the replacement FIRST (never a zero-card window), then
        # retire the old card via the sweep list
        msg_id = _discord_send(token, chat_id, panel)
        if msg_id:
            old_id = sess.get("standalone_msg_id")
            cards = [c for c in sess.get("panel_cards", []) if c != msg_id]
            cards.append(msg_id)
            if old_id and old_id not in cards:
                cards.append(old_id)
            _update_sess_fields(
                session, sess, standalone_msg_id=msg_id,
                standalone_replies_at_create=replies,
                standalone_reposted_at=now, panel_cards=cards)
    else:
        _discord_edit(token, chat_id, sess["standalone_msg_id"], panel)
    _sweep_cards(session, sess, token, chat_id)


_REPOST_COOLDOWN = 10  # min seconds between displacement reposts


def _sweep_cards(session: str, sess: dict, token: str,
                 chat_id: str) -> None:
    """Delete every tracked card except the current one; failures stay
    on the list and retry next tick."""
    current = sess.get("standalone_msg_id")
    cards = sess.get("panel_cards") or []
    keep = []
    changed = False
    for mid in cards:
        if mid == current:
            keep.append(mid)
            continue
        if _discord_delete(token, chat_id, mid):
            log(f"swept orphan panel card {mid}")
            changed = True
        else:
            log(f"orphan card delete FAILED for {mid}; will retry")
            keep.append(mid)
    if changed or len(keep) != len(cards):
        _update_sess_fields(session, sess, panel_cards=keep)


# ----------------------------------------------------- finalize/snapshot

_PRUNE_AFTER = 24 * 3600


def _sess_age_anchor(sess: dict) -> float:
    starts = [a.get("started_at") or 0 for a in sess.get("agents", {}).values()]
    return max(starts) if starts else 0.0


def handle_finalize(payload: dict) -> int:
    """Stop hook: collapse-mode cleanup for the standalone panel and
    registry pruning. The footer variant needs nothing here — the trace
    finalizer (narrate) already deletes/freezes the host message."""
    session = payload.get("session_id") or ""
    state = _load_av_state()
    changed = False
    sess = state.get(_session_key(session))
    if sess:
        if not _pid_alive(sess.get("updater_pid")):
            sess["updater_pid"] = None
            changed = True
        chat_id = sess.get("chat_id")
        cards = list(sess.get("panel_cards") or [])
        if sess.get("standalone_msg_id") and \
                sess["standalone_msg_id"] not in cards:
            cards.append(sess["standalone_msg_id"])
        if cards and chat_id and _tools_mode_for(chat_id) == "collapse":
            token = _bot_token()
            if token:
                for mid in cards:
                    if _discord_delete(token, chat_id, mid):
                        log(f"deleted standalone panel {mid} (collapse)")
            sess["standalone_msg_id"] = None
            sess["panel_cards"] = []
            changed = True
    now = time.time()
    for key in [k for k, s in state.items()
                if now - _sess_age_anchor(s) > _PRUNE_AFTER]:
        del state[key]
        changed = True
    if changed:
        _save_av_state(state)
    return 0


def render_snapshot(session: str | None = None) -> str:
    """One-shot panel render for !agents. Picks the given session, or
    the one with the most recent agent activity."""
    state = _load_av_state()
    prefix = _bot_key() + ":"
    mine = {k: v for k, v in state.items() if k.startswith(prefix)}
    if session:
        sess = mine.get(session) or mine.get(prefix + session)
    else:
        sess = max(mine.values(), key=_sess_age_anchor, default=None)
    if not sess or not sess.get("agents"):
        return "no agents running this session"
    return render_panel(_bot_name(), list(sess["agents"].values()),
                        now=time.time())


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["pre", "post", "finalize"])
    ap.add_argument("--failure", action="store_true")
    ap.add_argument("--updater", action="store_true")
    ap.add_argument("--session")
    ap.add_argument("--snapshot", action="store_true")
    args = ap.parse_args()
    if args.updater:
        if args.session:
            try:
                run_updater(args.session)
            except BaseException:  # noqa: BLE001 — stderr is /dev/null here
                import traceback
                log("updater crashed:\n" + traceback.format_exc())
                raise
        return 0
    if args.snapshot:
        print(render_snapshot(args.session))
        return 0
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log("bad JSON on stdin")
        return 0
    if args.mode == "pre":
        return handle_pre(payload)
    if args.mode == "post":
        return handle_post(payload, failed=args.failure)
    if args.mode == "finalize":
        return handle_finalize(payload)
    return 0


# ----------------------------------------------------- transcript stats

def _parse_ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, TypeError):
        return 0.0


def read_transcript_stats(path: str) -> dict:
    """One pass over a subagent jsonl: cumulative output tokens, model,
    last-activity timestamp. Tolerates partial trailing lines — the file
    is being appended to while we read."""
    tokens, model, last_ts = 0, None, 0.0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue  # mid-write tail line
                last_ts = max(last_ts, _parse_ts(d.get("timestamp", "")))
                msg = d.get("message")
                if d.get("type") == "assistant" and isinstance(msg, dict):
                    model = msg.get("model") or model
                    usage = msg.get("usage") or {}
                    try:
                        tokens += int(usage.get("output_tokens") or 0)
                    except (TypeError, ValueError):
                        pass
    except OSError:
        pass
    return {"tokens": tokens, "model": model, "last_ts": last_ts}


def _first_prompt(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.loads(f.readline())
        content = (d.get("message") or {}).get("content", "")
        return content if isinstance(content, str) else ""
    except (OSError, json.JSONDecodeError):
        return ""


_MATCH_SLACK = 120  # seconds a transcript may predate its registration


def match_transcripts(subagents_dir: str, agents: dict) -> None:
    """Link registered-but-unlinked agents to their transcript files.

    agentId (from PostToolUse) wins when present; otherwise match by
    verbatim prompt prefix — but ONLY against files created around or
    after the registration. Stale files from earlier (pre-hook) bursts
    in the same session can share a prompt prefix; matching one poisons
    tokens/elapsed and triggers instant bogus lost-marking (seen live
    2026-06-11). Each file is claimed at most once."""
    import glob as _glob
    if not any(not a.get("transcript") for a in agents.values()):
        return
    claimed = {a.get("agent_id") for a in agents.values()
               if a.get("agent_id") and a.get("transcript")}
    for path in sorted(_glob.glob(
            os.path.join(subagents_dir, "agent-*.jsonl"))):
        aid = os.path.basename(path)[len("agent-"):-len(".jsonl")]
        if aid in claimed:
            continue
        try:
            f_mtime = os.path.getmtime(path)
        except OSError:
            continue
        prompt = None  # lazy — only read when a prefix match is needed
        for agent in agents.values():
            if agent.get("transcript"):
                continue
            if agent.get("agent_id"):
                if agent["agent_id"] == aid:
                    agent["transcript"] = path
                    claimed.add(aid)
                    break
                continue
            # prefix matching only considers files still being written
            # at (or created after) registration time
            if f_mtime < (agent.get("started_at") or 0) - _MATCH_SLACK:
                continue
            if prompt is None:
                prompt = _first_prompt(path)
            if agent.get("_prompt") and prompt.startswith(agent["_prompt"]):
                agent["agent_id"] = aid
                agent["transcript"] = path
                claimed.add(aid)
                break


# The __main__ guard MUST be the last statement: exec'd entry points
# (notably --updater via _ensure_updater's exec) run main() the moment
# this line executes, so every module-level def has to exist by then.
# (Learned the hard way 2026-06-11: a mid-file guard made the updater
# crash with NameError on functions defined further down.)
if __name__ == "__main__":
    sys.exit(main())
