#!/usr/bin/env python3
"""Re-apply our local discord-plugin fixes after a plugin update clobbers them.

The official `claude-plugins-official/discord` plugin ships as a GCS-distributed
build (the running `server.ts` is newer than the public GitHub file). Every
plugin update regenerates that artifact from an internal source we don't control,
silently reverting any edits we make to the cache copy. Three fixes keep getting
wiped:

  1. Presence reconnect restore — stock fires presence only on `.once('ready')`,
     so it dies on every gateway reconnect / shardResume (laptop wake, net blip).
     Fix: `.on('ready')` + a `shardResume` handler, both calling reapplyPresence().
  2. Presence persistence — make `set_presence` write the chosen presence to
     presence.json so the handlers above can restore it. (Only applies to builds
     that HAVE a set_presence tool — the leaner public build doesn't.)
  3. Sticker-only messages deliver blank — stickers live in `msg.stickers`, not
     `msg.attachments`, so a sticker-only message has empty content and never
     starts a turn → the react/echo hooks never fire and the bot looks dead.
     Fix: surface stickers in content as `(sticker: <name>)` + meta.

This script is idempotent: each fix is skipped if already present. It patches
every discord `server.ts` under the known bot config dirs (cache + marketplace),
for whatever versions are installed. Safe to run on every SessionStart.

Run standalone (prints what it did) or as a hook (stays quiet unless verbose).
Exit code is always 0 — a patch failure must never block a session.
"""

from __future__ import annotations

import glob
import os
import sys

VERBOSE = "--verbose" in sys.argv or os.environ.get("CCDK_PLUGIN_PATCH_VERBOSE")


def out(msg: str) -> None:
    if VERBOSE:
        print(msg)


# ─── Fix 1+2: presence reconnect restore + persistence ───
# Anchor: the stock ready handler. Present in every build.
READY_ONCE = """client.once('ready', c => {
  process.stderr.write(`discord channel: gateway connected as ${c.user.tag}\\n`)
})"""

READY_PATCHED = """client.on('ready', c => {
  process.stderr.write(`discord channel: gateway connected as ${c.user.tag}\\n`)
  reapplyPresence('ready')
})
client.on('shardResume', (shardId, replayed) => {
  process.stderr.write(`discord channel: shard ${shardId} resumed (${replayed} events replayed)\\n`)
  reapplyPresence('shardResume')
})"""

# The reapplyPresence helper + PRESENCE_FILE const. Injected right after the
# INBOX_DIR line (present in every build).
INBOX_ANCHOR = "const INBOX_DIR = join(STATE_DIR, 'inbox')"
PRESENCE_HELPER = """const INBOX_DIR = join(STATE_DIR, 'inbox')
const PRESENCE_FILE = join(STATE_DIR, 'presence.json')

// --- presence persistence + reconnect restore (cc-context local patch) ---
// Re-apply persisted presence on every ready + shardResume, not just first
// ready, so it survives gateway reconnects / laptop-wake resumes.
function reapplyPresence(reason: string) {
  if (!client.user) return
  try {
    const saved = JSON.parse(readFileSync(PRESENCE_FILE, 'utf8'))
    client.user.setPresence({
      status: saved.status ?? 'online',
      ...(saved.activity ? { activities: [{ name: saved.activity, type: saved.type ?? 0 }] } : {}),
    })
    process.stderr.write(`discord channel: presence re-applied (${reason}): ${saved.activity ?? saved.status}\\n`)
  } catch { /* no saved presence yet — nothing to restore */ }
}"""

# set_presence persistence write — only present in builds that HAVE set_presence.
SETPRESENCE_ANCHOR = """        client.user?.setPresence({
          status: status as 'online' | 'idle' | 'dnd' | 'invisible',
          ...(activity ? { activities: [{ name: activity, type: typeMap[typeStr] ?? ActivityType.Playing }] } : {}),
        })
        return { content: [{ type: 'text', text: `presence set: ${status}${activity ? ` — ${typeStr} ${activity}` : ''}` }] }"""

SETPRESENCE_PATCHED = """        const presenceType = typeMap[typeStr] ?? ActivityType.Playing
        client.user?.setPresence({
          status: status as 'online' | 'idle' | 'dnd' | 'invisible',
          ...(activity ? { activities: [{ name: activity, type: presenceType }] } : {}),
        })
        // Persist so ready/shardResume can restore it after a reconnect (cc-context patch).
        try { writeFileSync(PRESENCE_FILE, JSON.stringify({ status, activity: activity ?? null, type: presenceType }), { mode: 0o600 }) } catch {}
        return { content: [{ type: 'text', text: `presence set: ${status}${activity ? ` — ${typeStr} ${activity}` : ''}` }] }"""

# ─── Fix 3: sticker-only messages ───
STICKER_ANCHOR = """  const atts: string[] = []
  for (const att of msg.attachments.values()) {
    const kb = (att.size / 1024).toFixed(0)
    atts.push(`${safeAttName(att)} (${att.contentType ?? 'unknown'}, ${kb}KB)`)
  }

  // Attachment listing goes in meta only — an in-content annotation is
  // forgeable by any allowlisted sender typing that string.
  const content = msg.content || (atts.length > 0 ? '(attachment)' : '')

  mcp.notification({
    method: 'notifications/claude/channel',
    params: {
      content,
      meta: {
        chat_id,
        message_id: msg.id,
        user: msg.author.username,
        user_id: msg.author.id,
        ts: msg.createdAt.toISOString(),
        ...(atts.length > 0 ? { attachment_count: String(atts.length), attachments: atts.join('; ') } : {}),
      },
    },
  }).catch(err => {"""

STICKER_PATCHED = """  const atts: string[] = []
  for (const att of msg.attachments.values()) {
    const kb = (att.size / 1024).toFixed(0)
    atts.push(`${safeAttName(att)} (${att.contentType ?? 'unknown'}, ${kb}KB)`)
  }

  // Stickers are NOT attachments in discord.js — they live in msg.stickers.
  // A sticker-only message therefore has empty content AND no attachments,
  // which would deliver a blank-bodied notification: the model can't see
  // anything was sent, and an empty body doesn't reliably start a turn, so
  // the react/echo hooks never fire (the bot looks dead to the sender).
  // Surface stickers like attachments — a non-empty body that names them.
  const stickers: string[] = []
  for (const st of msg.stickers.values()) {
    stickers.push(st.name)
  }

  // Attachment / sticker listing goes in meta only — an in-content
  // annotation is forgeable by any allowlisted sender typing that string.
  const placeholder = atts.length > 0
    ? (stickers.length > 0 ? '(attachment + sticker)' : '(attachment)')
    : (stickers.length > 0 ? `(sticker: ${stickers.join(', ')})` : '')
  const content = msg.content || placeholder

  mcp.notification({
    method: 'notifications/claude/channel',
    params: {
      content,
      meta: {
        chat_id,
        message_id: msg.id,
        user: msg.author.username,
        user_id: msg.author.id,
        ts: msg.createdAt.toISOString(),
        ...(atts.length > 0 ? { attachment_count: String(atts.length), attachments: atts.join('; ') } : {}),
        ...(stickers.length > 0 ? { sticker_count: String(stickers.length), stickers: stickers.join('; ') } : {}),
      },
    },
  }).catch(err => {"""


# ─── Fix 4: trusted-relay inbound (lets the squad helper deliver choice-tap
# results back to the agent session) ───
# The stock filter `if (msg.author.bot) return` drops EVERY bot message, so a
# reaction the helper hears can never reach the model. This narrowly opens it:
# a message from the helper bot id that carries a VALID HMAC marker
# `⟦vc-relay:<hex>⟧` (signing channelId+payload with a shared secret) is let
# through; the marker is stripped and the payload delivered as a normal prompt.
# A random bot, or the helper without a valid signature, is still dropped — so
# this can't be used to inject arbitrary prompts.
RELAY_ANCHOR = "  if (msg.author.bot) return\n"

RELAY_PATCHED = """  if (msg.author.bot) {
    const relayed = tryTrustedRelay(msg)   // cc-context local patch (Fix 4)
    if (!relayed) return
  }
"""

# The helper + the relay verifier, injected right after the bot-filter block.
# Anchored on the handleInbound signature so it lands at top-level scope.
RELAY_HELPER_ANCHOR = "async function handleInbound(msg: Message): Promise<void> {\n"

RELAY_HELPER = """// ─── cc-context local patch (Fix 4): trusted-relay verifier ───
// Reads the helper bot id + HMAC secret from a state file (written by the squad
// helper). A relay message is `⟦vc-relay:<hexhmac>⟧ <payload>`; the hmac signs
// `<chat_id>\\n<payload>` so a marker can't be replayed into another channel.
// On success, strips the marker and delivers <payload> as a normal channel
// notification (same shape gate()'s deliver path uses), then returns true so
// the stock handler stops (we've already delivered).
function tryTrustedRelay(msg: Message): boolean {
  try {
    const cfgPath = join(STATE_DIR, 'relay.json')
    if (!existsSync(cfgPath)) return false
    const cfg = JSON.parse(readFileSync(cfgPath, 'utf8'))
    if (!cfg.helper_id || !cfg.secret) return false
    if (msg.author.id !== cfg.helper_id) return false
    // Require the FULL 64-hex (256-bit) digest — a shorter prefix used to be
    // accepted ({16,64}) and compared only up to its own length, so a 64-bit
    // prefix forgery passed. Now the whole 256-bit HMAC must match.
    const m = /^\\u27e6vc-relay:([^:\\u27e7]*):([0-9a-f]{64})\\u27e7\\s*([\\s\\S]*)$/.exec(msg.content)
    if (!m) return false
    const [, target, sig, payload] = m
    const crypto = require('crypto')
    const want = crypto.createHmac('sha256', cfg.secret)
      .update(`${msg.channelId}\\n${target}\\n${payload}`).digest('hex')
    // constant-time compare on the FULL digest (both are 64 hex chars)
    const a = Buffer.from(sig, 'utf8')
    const b = Buffer.from(want, 'utf8')
    if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return false
    // Over-broadcast fix (2026-07-01): a valid HMAC only proves the HELPER
    // signed this marker for this channel — with N bots co-present in one
    // channel, every one of them would otherwise deliver the SAME tap. Drop
    // it here unless it's addressed to THIS bot. Empty target (old-format
    // marker / unknown asker) or empty self_id (this bot's relay.json isn't
    // registry-backed yet) falls back to the old broadcast-to-all behavior.
    if (target && cfg.self_id && target !== cfg.self_id) return false
    mcp.notification({
      method: 'notifications/claude/channel',
      params: {
        content: payload,
        meta: {
          chat_id: msg.channelId,
          message_id: msg.id,
          user: cfg.relay_user ?? 'choice-relay',
          user_id: cfg.relay_user_id ?? msg.author.id,
          ts: msg.createdAt.toISOString(),
          relayed: 'true',
        },
      },
    }).catch((err: unknown) => process.stderr.write(`discord relay deliver failed: ${err}\\n`))
    // The signed marker is machine plumbing — once delivered, delete it so the
    // raw ⟦vc-relay:…⟧ blob doesn't linger as visible channel noise. The
    // human-readable "✅ chose N" message (posted separately by the helper) is
    // what stays as the visible confirmation.
    msg.delete().catch(() => {})
    return true
  } catch (e) {
    process.stderr.write(`discord relay verify error: ${e}\\n`)
    return false
  }
}

async function handleInbound(msg: Message): Promise<void> {
"""

# Fix 4c upgrade anchors: turn the bare "deliver" tail of an already-injected
# relay helper into deliver + delete-the-marker-message. The anchor is the exact
# deliver-failed catch line followed by `return true`.
RELAY_DELIVERED_ANCHOR = """    }).catch((err: unknown) => process.stderr.write(`discord relay deliver failed: ${err}\\n`))
    return true"""

RELAY_DELIVERED_PATCHED = """    }).catch((err: unknown) => process.stderr.write(`discord relay deliver failed: ${err}\\n`))
    // The signed marker is machine plumbing — once delivered, delete it so the
    // raw blob doesn't linger as visible channel noise. The "✅ chose N" message
    // (posted separately by the helper) stays as the visible confirmation.
    msg.delete().catch(() => {})
    return true"""

# Fix 4d upgrade anchors (2026-07-01): turn an already-injected relay verifier
# that only binds channelId+payload into one that ALSO binds a target-bot and
# drops the relay unless it's addressed to THIS bot (the choice-tap
# over-broadcast fix). Idempotent — skipped once `const [, target, ...` present.
RELAY_TARGET_ANCHOR = """    const m = /^\\u27e6vc-relay:([0-9a-f]{64})\\u27e7\\s*([\\s\\S]*)$/.exec(msg.content)
    if (!m) return false
    const [, sig, payload] = m
    const crypto = require('crypto')
    const want = crypto.createHmac('sha256', cfg.secret)
      .update(`${msg.channelId}\\n${payload}`).digest('hex')
    // constant-time compare on the FULL digest (both are 64 hex chars)
    const a = Buffer.from(sig, 'utf8')
    const b = Buffer.from(want, 'utf8')
    if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return false
    mcp.notification({"""

RELAY_TARGET_PATCHED = """    const m = /^\\u27e6vc-relay:([^:\\u27e7]*):([0-9a-f]{64})\\u27e7\\s*([\\s\\S]*)$/.exec(msg.content)
    if (!m) return false
    const [, target, sig, payload] = m
    const crypto = require('crypto')
    const want = crypto.createHmac('sha256', cfg.secret)
      .update(`${msg.channelId}\\n${target}\\n${payload}`).digest('hex')
    // constant-time compare on the FULL digest (both are 64 hex chars)
    const a = Buffer.from(sig, 'utf8')
    const b = Buffer.from(want, 'utf8')
    if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return false
    // Over-broadcast fix (2026-07-01): a valid HMAC only proves the HELPER
    // signed this marker for this channel — with N bots co-present in one
    // channel, every one would otherwise deliver the SAME tap. Drop it unless
    // it's addressed to THIS bot. Empty target (old-format marker / unknown
    // asker) or empty self_id (this bot not in the registry) → old broadcast.
    if (target && cfg.self_id && target !== cfg.self_id) return false
    mcp.notification({"""


# ─── Fix 6: lone-❌ message interrupts the current turn ───
# A message that is JUST ❌ (from an already-gate()-approved sender) writes the
# .stop_signal flag the WIRED cc-stop-check PreToolUse hook reads to halt the
# turn at the next tool boundary — then is swallowed (NOT delivered as a new
# prompt) and deleted. Scroll-proof interrupt: type ❌, the turn stops. (Replaces
# the old reaction-based interrupt, which scrolled out of view and didn't clear.)
# Anchored right before the permission-reply intercept, so the sender is already
# allowlisted and we're on the deliver path.
STOP_INTERCEPT_ANCHOR = """  // Permission-reply intercept: if this looks like \"yes xxxxx\" for a"""

STOP_INTERCEPT_PATCHED = """  // cc-context local patch (Fix 6): a lone ❌ message STOPS the current turn.
  // Sender is already gate()-approved here. Write the .stop_signal flag that the
  // wired cc-stop-check PreToolUse hook reads (halt at next tool boundary), then
  // swallow + delete the message so it never starts a new turn or lingers.
  if (msg.content.trim().replace(/\\ufe0f/g, '') === '\\u274c') {
    try {
      writeFileSync(join(STATE_DIR, '.stop_signal'), JSON.stringify({
        ts: Date.now(), channel_id: msg.channelId, message_id: msg.id,
      }), { mode: 0o600 })
    } catch (e) { process.stderr.write(`stop-signal write failed: ${e}\\n`) }
    msg.delete().catch(() => {})
    return
  }

  // Permission-reply intercept: if this looks like \"yes xxxxx\" for a"""


# ─── Fix 5: re-inject the Discord pass-through block (/effort, /model, …) ───
# The big pass-through handler (PASSTHROUGH_SLASH_RE + handleSlash, ~180 lines)
# is a cc-context mod that a plugin update also wipes — but unlike Fixes 1-3 it
# was never in this script, so it was NOT restored. Reinforce it: the canonical
# block lives in discord_plugin_patches/passthrough_block.ts; we inject it at
# module scope after a stable anchor, and wire its call-site into handleInbound.
_HERE = os.path.dirname(os.path.abspath(__file__))
PASSTHROUGH_BLOCK_FILE = os.path.join(_HERE, "discord_plugin_patches", "passthrough_block.ts")

# Anchor for the module-scope block: a stable stock line near where it belongs.
PASSTHROUGH_MOD_ANCHOR = "if (!STATIC) setInterval(checkApprovals, 5000).unref()\n"

# Call-site: inserted right after the permission-reply intercept in handleInbound
# (stock code present in every build), so a `/effort`-style message is handled
# server-side instead of delivered to the model.
PASSTHROUGH_CALL_ANCHOR = """    const emoji = permMatch[1]!.toLowerCase().startsWith('y') ? '✅' : '❌'
    void msg.react(emoji).catch(() => {})
    return
  }
"""

PASSTHROUGH_CALL_PATCHED = PASSTHROUGH_CALL_ANCHOR + """
  // cc-context local patch (Fix 5): pass-through slash command intercept.
  const slashMatch = PASSTHROUGH_SLASH_RE.exec(msg.content)
  if (slashMatch) {
    void handleSlash(msg, slashMatch[1]!, slashMatch[2] ?? '').catch(e => {
      process.stderr.write(`passthrough handleSlash crashed: ${e}\\n`)
    })
    return
  }
"""


def _load_passthrough_block() -> str:
    try:
        return open(PASSTHROUGH_BLOCK_FILE, encoding="utf-8").read()
    except OSError:
        return ""


def patch_file(path: str) -> list[str]:
    """Apply any missing fixes to one server.ts. Returns list of fix names applied."""
    try:
        txt = open(path, encoding="utf-8").read()
    except OSError as e:
        out(f"  skip (unreadable): {path}: {e}")
        return []
    applied: list[str] = []
    orig = txt

    # Fix 1+2a: presence helper (only if not already present and anchor exists)
    if "reapplyPresence" not in txt and INBOX_ANCHOR in txt:
        txt = txt.replace(INBOX_ANCHOR, PRESENCE_HELPER, 1)
        applied.append("presence-helper")

    # Fix 1b: ready/shardResume handlers. Guard on the actual handler code, not
    # the bare word "shardResume" — the presence-helper comment above also
    # contains that word, which would falsely mark this fix as already applied.
    if "client.on('shardResume'" not in txt and READY_ONCE in txt:
        txt = txt.replace(READY_ONCE, READY_PATCHED, 1)
        applied.append("ready+shardResume")

    # Fix 2b: set_presence persistence write (only builds that have set_presence)
    if "presence.json" not in txt.split("set_presence")[-1] and SETPRESENCE_ANCHOR in txt:
        txt = txt.replace(SETPRESENCE_ANCHOR, SETPRESENCE_PATCHED, 1)
        applied.append("set_presence-persist")

    # Fix 3: sticker content
    if "msg.stickers" not in txt and STICKER_ANCHOR in txt:
        txt = txt.replace(STICKER_ANCHOR, STICKER_PATCHED, 1)
        applied.append("stickers")

    # Fix 4a: inject the trusted-relay verifier just before handleInbound.
    if "tryTrustedRelay" not in txt and RELAY_HELPER_ANCHOR in txt:
        txt = txt.replace(RELAY_HELPER_ANCHOR, RELAY_HELPER, 1)
        applied.append("relay-helper")
    # Fix 4b: open the bot-filter for a verified relay. Guard on the patched
    # call-site marker so we don't double-apply; the unpatched anchor still
    # being present means it hasn't been done yet.
    if "const relayed = tryTrustedRelay(msg)" not in txt and RELAY_ANCHOR in txt:
        txt = txt.replace(RELAY_ANCHOR, RELAY_PATCHED, 1)
        applied.append("relay-filter")

    # Fix 4c: upgrade an already-injected relay helper to delete the marker
    # message after delivery (so the raw ⟦vc-relay:…⟧ blob doesn't linger). Only
    # fires when the helper exists but the delete doesn't yet — idempotent.
    if "tryTrustedRelay" in txt and "msg.delete().catch(() => {})" not in txt \
            and RELAY_DELIVERED_ANCHOR in txt:
        txt = txt.replace(RELAY_DELIVERED_ANCHOR, RELAY_DELIVERED_PATCHED, 1)
        applied.append("relay-delete-cleanup")

    # Fix 4d: upgrade an already-injected relay verifier to bind + enforce the
    # target bot (over-broadcast fix). Fires only when the old block is present
    # and the new target-aware one isn't yet — idempotent.
    if "const [, target, sig, payload] = m" not in txt and RELAY_TARGET_ANCHOR in txt:
        txt = txt.replace(RELAY_TARGET_ANCHOR, RELAY_TARGET_PATCHED, 1)
        applied.append("relay-target-scope")

    # Fix 5a: re-inject the pass-through module block if missing.
    if "PASSTHROUGH_SLASH_RE" not in txt and PASSTHROUGH_MOD_ANCHOR in txt:
        block = _load_passthrough_block()
        if block:
            txt = txt.replace(
                PASSTHROUGH_MOD_ANCHOR,
                PASSTHROUGH_MOD_ANCHOR + "\n" + block + "\n", 1)
            applied.append("passthrough-block")
    # Fix 5b: wire the pass-through call-site into handleInbound if missing.
    if "PASSTHROUGH_SLASH_RE.exec" not in txt and PASSTHROUGH_CALL_ANCHOR in txt:
        txt = txt.replace(PASSTHROUGH_CALL_ANCHOR, PASSTHROUGH_CALL_PATCHED, 1)
        applied.append("passthrough-callsite")

    # Fix 6: lone-❌ message → write .stop_signal + swallow (interrupt the turn).
    if "Fix 6): a lone" not in txt and STOP_INTERCEPT_ANCHOR in txt:
        txt = txt.replace(STOP_INTERCEPT_ANCHOR, STOP_INTERCEPT_PATCHED, 1)
        applied.append("stop-intercept")

    if txt != orig:
        # Sanity: brace/paren balance must be unchanged from a clean apply.
        if txt.count("{") - txt.count("}") != orig.count("{") - orig.count("}"):
            out(f"  ABORT (brace imbalance): {path}")
            return []
        try:
            open(path, "w", encoding="utf-8").write(txt)
        except OSError as e:
            out(f"  skip (unwritable): {path}: {e}")
            return []
    return applied


# Every mod that MUST be present after patching, by marker. Keyed by the
# substring that proves the mod landed. `optional` markers only apply to builds
# that have the corresponding stock feature (e.g. set_presence) — their absence
# isn't a failure if the anchor was never there.
REQUIRED_MARKERS = {
    "presence-restore": "reapplyPresence",
    "stickers": "msg.stickers",
    "relay-verifier": "tryTrustedRelay",
    "relay-filter-open": "const relayed = tryTrustedRelay(msg)",
    "passthrough": "PASSTHROUGH_SLASH_RE.exec",
    "stop-intercept": "Fix 6): a lone",
}

SELFTEST_SENTINEL = os.path.expanduser(
    os.environ.get("CCDK_PLUGIN_PATCH_SENTINEL",
                   "~/.local/state/cc-discord-kit/discord_patch_status.json"))


def verify_file(path: str) -> list[str]:
    """Return the list of REQUIRED markers MISSING from a patched file. Empty
    list = all mods present. This is the scream-test: a non-empty result means
    a plugin update moved an anchor and a fix silently didn't apply."""
    try:
        txt = open(path, encoding="utf-8").read()
    except OSError:
        return list(REQUIRED_MARKERS)  # unreadable counts as all-missing
    return [name for name, marker in REQUIRED_MARKERS.items() if marker not in txt]


def main() -> int:
    home = os.path.expanduser("~")
    # Which config dirs hold a plugin copy to patch. By default: this agent's
    # CLAUDE_CONFIG_DIR (if set) plus ~/.claude. A multi-agent host where each
    # bot has its OWN config dir + plugin copy can list them all (relative to
    # $HOME, comma-separated) via CCDK_PLUGIN_DIRS — e.g. ".claude,.claude-bot2".
    # Dirs without a plugin copy are harmless (glob finds nothing).
    bot_dirs: list[str] = []
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if cfg:
        # store the path relative to $HOME if under it, else absolute
        bot_dirs.append(os.path.relpath(cfg, home) if cfg.startswith(home) else cfg)
    bot_dirs.append(".claude")
    extra = os.environ.get("CCDK_PLUGIN_DIRS", "")
    bot_dirs += [d.strip() for d in extra.split(",") if d.strip()]
    targets: list[str] = []
    for base in dict.fromkeys(bot_dirs):  # dedupe, preserve order
        root = base if os.path.isabs(base) else f"{home}/{base}"
        targets += glob.glob(
            f"{root}/plugins/cache/claude-plugins-official/discord/*/server.ts"
        )
        targets += glob.glob(
            f"{root}/plugins/marketplaces/claude-plugins-official"
            "/external_plugins/discord/server.ts"
        )
    targets = sorted(set(targets))
    total = 0
    failures: dict[str, list[str]] = {}
    for f in targets:
        applied = patch_file(f)
        if applied:
            total += 1
            out(f"  PATCHED {f}: {', '.join(applied)}")
        else:
            out(f"  ok (no-op) {f}")
        # Self-test: after patching, every required mod MUST be present.
        missing = verify_file(f)
        if missing:
            failures[f] = missing
    out(f"discord_plugin_patch: {total} file(s) changed of {len(targets)} scanned")

    # SCREAM if any required mod is missing — a plugin update broke an anchor and
    # a fix silently didn't apply. Write a sentinel a SessionStart check surfaces,
    # and shout on stderr (always visible in the hook log) regardless of verbose.
    if failures:
        lines = ["⚠️  DISCORD PLUGIN PATCH INCOMPLETE — fixes did NOT apply:"]
        for f, miss in failures.items():
            lines.append(f"    {f}: MISSING {', '.join(miss)}")
        lines.append("    A plugin update likely moved an anchor. Re-fit the patch")
        lines.append("    (scripts/discord_plugin_patch.py). Until then, affected")
        lines.append("    features (choice-tap relay, pass-through, presence) are DOWN.")
        msg = "\n".join(lines)
        sys.stderr.write(msg + "\n")
        try:
            os.makedirs(os.path.dirname(SELFTEST_SENTINEL), exist_ok=True)
            import json
            open(SELFTEST_SENTINEL, "w").write(json.dumps(
                {"ok": False, "failures": failures}, indent=2))
        except OSError:
            pass
    else:
        # Clear a stale sentinel once everything's healthy again.
        try:
            if os.path.exists(SELFTEST_SENTINEL):
                import json
                open(SELFTEST_SENTINEL, "w").write(json.dumps({"ok": True}))
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
