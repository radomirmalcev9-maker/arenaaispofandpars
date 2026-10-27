"""Internal Direct Chat page automation, JS snippets, cookie seeding, and UI helpers.

Houses the DOM composer submission flow, in-page reCAPTCHA v3 / Supabase
refresh scripts, post-submit challenge detection, and trust-cookie seeding used
by :class:`~arena_api.auto.DirectVerificationToken`. Kept free of eager heavy
imports so ``import arena_api`` remains fast and dependency-light.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .auth.captcha import RecaptchaToolkitSolver
from .auth.firefox_session import FirefoxSessionManager
from .auth.login import DEFAULT_STATE_PATH

# --------------------------------------------------------------------------- #
# Constants & cookies / JWT helpers
# --------------------------------------------------------------------------- #

ARENA_RECAPTCHA_SITE_KEY_ENV = "ARENA_RECAPTCHA_SITE_KEY"
_DEFAULT_ARENA_RECAPTCHA_SITE_KEY = "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0"

_BROWSER_BOUND_CF_COOKIES: frozenset[str] = frozenset(
    {"cf_clearance", "__cf_bm", "_cfuvid"}
)

_AUTH_REJECT_MARKERS: tuple[str, ...] = (
    "user not found",
    "unauthorized",
    "unauthenticated",
    "not signed in",
    "login required",
    "invalid jwt",
)

_EVAL_STREAM_URL_MARKERS: tuple[str, ...] = (
    "/nextjs-api/stream/create-evaluation",
    "/nextjs-api/stream/post-to-evaluation/",
    "/nextjs-api/stream/create-chat",
)

_PRESERVED_TRUST_COOKIE_PREFIXES: tuple[str, ...] = (
    "_GRECAPTCHA",
    "cf_clearance",
    "__cf_bm",
    "_cfuvid",
    "NID",
    "AEC",
)


class _RetryInHeadedBrowser(Exception):
    """Internal signal to retry a Direct Chat browser request in headed mode."""


def _token_solver(solver: Any) -> Any:
    """Unwrap the token-producing member of a composite solver."""
    members = getattr(solver, "solvers", None)
    if not members:
        return solver
    for member in members:
        if isinstance(member, RecaptchaToolkitSolver):
            return member
    return solver


def _default_recaptcha_site_key(page_url: str) -> str:
    explicit = os.environ.get(ARENA_RECAPTCHA_SITE_KEY_ENV, "").strip()
    if explicit:
        return explicit
    host = (urlparse(page_url).hostname or "").lower()
    if host == "arena.ai" or host.endswith(".arena.ai"):
        return _DEFAULT_ARENA_RECAPTCHA_SITE_KEY
    return ""


def _cookie_source_for_manager(manager: Any) -> str | None:
    if isinstance(manager, FirefoxSessionManager):
        return "firefox"
    if isinstance(getattr(manager, "fallback", None), FirefoxSessionManager):
        return "firefox"
    return None


def _seedable_browser_cookies(
    cookies: Mapping[str, str], *, cookie_source: str | None
) -> dict[str, str]:
    if cookie_source == "firefox":
        return {
            str(k): str(v)
            for k, v in cookies.items()
            if str(k) not in _BROWSER_BOUND_CF_COOKIES
        }
    return {str(k): str(v) for k, v in cookies.items()}


def _reconstruct_supabase_cookie(cookies: Mapping[str, str]) -> str:
    whole = cookies.get("arena-auth-prod-v1")
    if isinstance(whole, str) and whole.strip():
        return whole.strip()
    parts: list[str] = []
    index = 0
    while True:
        piece = cookies.get(f"arena-auth-prod-v1.{index}")
        if not isinstance(piece, str) or not piece:
            break
        parts.append(piece)
        index += 1
    return "".join(parts)


def _arena_auth_jwt_expired(
    cookies: Mapping[str, str], *, leeway: float = 60.0
) -> bool:
    raw = _reconstruct_supabase_cookie(cookies)
    if not raw.startswith("base64-"):
        return False
    encoded = raw[len("base64-") :]
    if not encoded:
        return False
    padded = encoded + "=" * ((4 - (len(encoded) % 4)) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(payload, Mapping):
        return False
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return False
    return float(expires_at) <= (time.time() + leeway)


def _is_eval_stream_url(raw_url: str) -> bool:
    return any(marker in raw_url for marker in _EVAL_STREAM_URL_MARKERS)


def _direct_browser_profile_dir(options: Any) -> Path:
    storage_path = getattr(options, "storage_path", None)
    base_state = (
        Path(storage_path).expanduser()
        if storage_path is not None
        else Path(DEFAULT_STATE_PATH)
    )
    return base_state.parent / "direct_browser_profile"


async def _seed_direct_context_cookies(
    context: Any,
    seed_cookies: Mapping[str, str],
    base_url: str,
) -> None:
    if not seed_cookies or context is None:
        return
    add_cookies_fn = getattr(context, "add_cookies", None)
    if not callable(add_cookies_fn):
        return
    preserved_existing: set[str] = set()
    cookies_fn = getattr(context, "cookies", None)
    if callable(cookies_fn):
        try:
            existing = await cookies_fn()
        except Exception:  # noqa: BLE001
            existing = None
        if isinstance(existing, Sequence):
            for item in existing:
                if not isinstance(item, Mapping):
                    continue
                name = str(item.get("name") or "")
                value = str(item.get("value") or "")
                if value and name.startswith(_PRESERVED_TRUST_COOKIE_PREFIXES):
                    preserved_existing.add(name)
    payload = [
        {
            "name": str(name),
            "value": str(value),
            "url": base_url,
        }
        for name, value in seed_cookies.items()
        if name not in preserved_existing
    ]
    if payload:
        await add_cookies_fn(payload)


# --------------------------------------------------------------------------- #
# In-page JavaScript builders & DOM inspection snippets
# --------------------------------------------------------------------------- #


def _build_execute_recaptcha_v3_js(fallback_site_key: str) -> str:
    encoded_fallback = json.dumps(fallback_site_key)
    return f"""
async () => {{
    const fallbackKey = {encoded_fallback};
    const findDomSiteKey = () => {{
        for (const el of document.querySelectorAll("script[src*='recaptcha']")) {{
            try {{
                const u = new URL(el.src, location.href);
                const k = u.searchParams.get("render");
                if (k && k !== "explicit") return k;
            }} catch (_) {{}}
        }}
        for (const el of document.querySelectorAll("iframe[src*='recaptcha']")) {{
            try {{
                const u = new URL(el.src, location.href);
                const k = u.searchParams.get("k");
                if (k) return k;
            }} catch (_) {{}}
        }}
        const el = document.querySelector("[data-sitekey]");
        if (el) {{
            const k = el.getAttribute("data-sitekey");
            if (k) return k;
        }}
        if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {{
            const seen = new Set();
            const stack = [window.___grecaptcha_cfg.clients];
            while (stack.length) {{
                const cur = stack.pop();
                if (!cur || typeof cur !== "object" || seen.has(cur)) continue;
                seen.add(cur);
                for (const v of Object.values(cur)) {{
                    if (typeof v === "string" && /^6L[A-Za-z0-9_-]{{38}}$/.test(v)) {{
                        return v;
                    }}
                    if (v && typeof v === "object") stack.push(v);
                }}
            }}
        }}
        return null;
    }};

    let siteKey = findDomSiteKey();
    if (!siteKey) {{
        const scripts = Array.from(
            document.querySelectorAll("script[src*='/_next/static/chunks/']")
        ).slice(0, 20);
        for (const s of scripts) {{
            try {{
                const txt = await (await fetch(s.src, {{ cache: "force-cache" }})).text();
                const m =
                    txt.match(/(?:recaptcha|siteKey|SITE_KEY)[^"']{{0,80}}["'](6L[A-Za-z0-9_-]{{38}})["']/i) ||
                    txt.match(/["'](6Le[A-Za-z0-9_-]{{37}})["']/);
                if (m && m[1]) {{
                    siteKey = m[1];
                    break;
                }}
            }} catch (_) {{}}
        }}
    }}
    if (!siteKey && fallbackKey) {{
        siteKey = fallbackKey;
    }}
    if (!siteKey) return null;

    const getExecutor = () => {{
        if (window.grecaptcha && window.grecaptcha.enterprise && typeof window.grecaptcha.enterprise.execute === "function") {{
            return window.grecaptcha.enterprise;
        }}
        if (window.grecaptcha && typeof window.grecaptcha.execute === "function") {{
            return window.grecaptcha;
        }}
        return null;
    }};

    if (!getExecutor()) {{
        const existing = document.querySelector("script[src*='recaptcha/enterprise.js'], script[src*='recaptcha/api.js']");
        if (!existing && document.head) {{
            const tag = document.createElement("script");
            tag.src = "https://www.google.com/recaptcha/enterprise.js?render=" + encodeURIComponent(siteKey);
            tag.async = true;
            tag.defer = true;
            document.head.appendChild(tag);
        }}
    }}

    const deadline = Date.now() + 20000;
    let g = getExecutor();
    while (!g && Date.now() < deadline) {{
        await new Promise((r) => setTimeout(r, 150));
        g = getExecutor();
    }}
    if (!g) return null;

    return await new Promise((resolve) => {{
        const timer = setTimeout(() => resolve(null), 20000);
        const run = () => {{
            Promise.resolve(g.execute(siteKey, {{ action: "chat_submit" }}))
                .then((tok) => {{ clearTimeout(timer); resolve(tok || null); }})
                .catch(() => {{ clearTimeout(timer); resolve(null); }});
        }};
        if (typeof g.ready === "function") g.ready(run);
        else run();
    }});
}}
"""


def _build_wait_supabase_refresh_js(stale_raw: str) -> str:
    stale_literal = json.dumps(stale_raw)
    return f"""
async () => {{
    const stale = {stale_literal};
    const readJoined = () => {{
        const map = new Map();
        for (const entry of (document.cookie || "").split(";")) {{
            const eq = entry.indexOf("=");
            if (eq < 0) continue;
            map.set(entry.slice(0, eq).trim(), entry.slice(eq + 1).trim());
        }}
        const whole = map.get("arena-auth-prod-v1");
        if (whole) return whole;
        let out = "";
        for (let i = 0; map.has("arena-auth-prod-v1." + i); i += 1) {{
            out += map.get("arena-auth-prod-v1." + i) || "";
        }}
        return out;
    }};
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {{
        const current = readJoined();
        if (current && current !== stale) return true;
        await new Promise((r) => setTimeout(r, 200));
    }}
    return false;
}}
"""


def _build_browser_fetch_stream_js(
    endpoint_path: str, serialized_body: str
) -> str:
    url_literal = json.dumps(endpoint_path)
    body_literal = json.dumps(serialized_body)
    return f"""
async () => {{
    const resp = await window.fetch({url_literal}, {{
        method: "POST",
        credentials: "include",
        headers: {{
            "Content-Type": "application/json",
            "Accept": "text/event-stream, text/plain, */*"
        }},
        body: {body_literal}
    }});
    const contentType = (resp.headers && resp.headers.get("content-type")) || "";
    const cfMitigated = (resp.headers && resp.headers.get("cf-mitigated")) || "";
    if (!resp.ok) {{
        let errBody = "";
        try {{ errBody = await resp.text(); }} catch (_) {{}}
        return {{
            ok: false,
            status: resp.status,
            body: errBody,
            contentType: contentType,
            cfMitigated: cfMitigated
        }};
    }}
    if (typeof window.__arenaDirectMeta === "function") {{
        await window.__arenaDirectMeta(contentType);
    }}
    if (!resp.body || typeof resp.body.getReader !== "function") {{
        const full = await resp.text();
        if (full && typeof window.__arenaDirectChunk === "function") {{
            await window.__arenaDirectChunk(full);
        }}
        return {{ ok: true, status: resp.status, contentType: contentType }};
    }}
    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    while (true) {{
        const {{ value, done }} = await reader.read();
        if (done) break;
        if (value && value.length) {{
            const piece = decoder.decode(value, {{ stream: true }});
            if (piece && typeof window.__arenaDirectChunk === "function") {{
                await window.__arenaDirectChunk(piece);
            }}
        }}
    }}
    const tail = decoder.decode();
    if (tail && typeof window.__arenaDirectChunk === "function") {{
        await window.__arenaDirectChunk(tail);
    }}
    return {{ ok: true, status: resp.status, contentType: contentType }};
}}
"""


def _build_main_frame_body_patch_js(patch_obj: Mapping[str, Any]) -> str:
    patch_literal = json.dumps(dict(patch_obj), separators=(",", ":"))
    return f"""
() => {{
  const patch = {patch_literal};
  const origFetch = window.fetch;
  if (typeof origFetch !== "function" || window.__arenaPatchInstalled) return;
  window.__arenaPatchInstalled = true;
  window.fetch = function (input, init) {{
    let urlStr = "";
    try {{
      urlStr = typeof input === "string" ? input : (input && input.url) || "";
    }} catch (_) {{}}
    if (
      init &&
      typeof init.body === "string" &&
      (urlStr.indexOf("/nextjs-api/stream/create-evaluation") !== -1 ||
        urlStr.indexOf("/nextjs-api/stream/post-to-evaluation/") !== -1 ||
        urlStr.indexOf("/nextjs-api/stream/create-chat") !== -1)
    ) {{
      try {{
        const parsed = JSON.parse(init.body);
        if (parsed && typeof parsed === "object") {{
          if (patch.modelAId) parsed.modelAId = patch.modelAId;
          if (patch.generationConfig) parsed.generationConfig = patch.generationConfig;
          const nextInit = Object.assign({{}}, init, {{ body: JSON.stringify(parsed) }});
          return origFetch.call(this, input, nextInit);
        }}
      }} catch (_) {{}}
    }}
    return origFetch.apply(this, arguments);
  }};
}}
""".strip()


_FIND_COMPOSER_JS = """
async () => {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    for (const btn of document.querySelectorAll("button")) {
      const label = ((btn.textContent || "") + " " + (btn.getAttribute("aria-label") || "")).trim().toLowerCase();
      if (label === "accept cookies" || label === "accept all") {
        try { btn.click(); } catch (_) {}
      }
    }
    for (const old of document.querySelectorAll("[data-arena-composer]")) {
      old.removeAttribute("data-arena-composer");
    }
    const candidates = Array.from(
      document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"][contenteditable="true"]')
    );
    let best = null;
    let bestScore = -1;
    for (const el of candidates) {
      const id = (el.id || "").toLowerCase();
      const name = (el.getAttribute("name") || "").toLowerCase();
      if (id.includes("recaptcha") || name.includes("recaptcha") || id.includes("turnstile")) {
        continue;
      }
      if (el.disabled || el.readOnly || el.getAttribute("aria-hidden") === "true") {
        continue;
      }
      const style = window.getComputedStyle(el);
      if (!style || style.display === "none" || style.visibility === "hidden" || parseFloat(style.opacity || "1") === 0) {
        continue;
      }
      const rect = el.getBoundingClientRect();
      if (!rect || rect.width < 100 || rect.height < 18) {
        continue;
      }
      const score = rect.width + (el.closest("form") ? 500 : 0) + (el.tagName === "TEXTAREA" ? 200 : 0);
      if (score > bestScore) {
        best = { el, rect };
        bestScore = score;
      }
    }
    if (best) {
      best.el.setAttribute("data-arena-composer", "1");
      return {
        found: true,
        tag: best.el.tagName.toLowerCase(),
        x: Math.round(best.rect.left + best.rect.width / 2),
        y: Math.round(best.rect.top + best.rect.height / 2)
      };
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  return { found: false };
}
""".strip()

_MARK_SEND_BUTTON_JS = """
() => {
  for (const old of document.querySelectorAll("[data-arena-send]")) {
    old.removeAttribute("data-arena-send");
  }
  const composer = document.querySelector('[data-arena-composer="1"]') ||
    document.querySelector('textarea:not([id*="recaptcha"]):not([name*="recaptcha"]), [contenteditable="true"]');
  if (!composer) return { found: false };
  const skipRe = /attach|upload|file|image|photo|plus|add|model|mode|search|web|globe|mic|voice|audio|new|clear|copy|history|menu|sidebar|close|cancel|stop/i;
  const sendRe = /send|submit|ask|отправ/i;

  let scope = composer.closest("form");
  if (!scope) {
    let cur = composer.parentElement;
    for (let depth = 0; cur && depth < 5; depth += 1) {
      if (cur.querySelectorAll("button").length > 0) {
        scope = cur;
      }
      cur = cur.parentElement;
    }
  }
  if (!scope) scope = document;

  const buttons = Array.from(scope.querySelectorAll("button")).filter((btn) => {
    if (btn.disabled || btn.getAttribute("aria-disabled") === "true") return false;
    const style = window.getComputedStyle(btn);
    if (!style || style.display === "none" || style.visibility === "hidden") return false;
    const rect = btn.getBoundingClientRect();
    return rect && rect.width >= 16 && rect.height >= 16;
  });

  let confidence = "explicit";
  let chosen = buttons.find((btn) => (btn.getAttribute("type") || "").toLowerCase() === "submit");
  if (!chosen) {
    chosen = buttons.find((btn) => {
      const meta = [btn.getAttribute("aria-label"), btn.getAttribute("title"), btn.getAttribute("data-testid"), btn.textContent].filter(Boolean).join(" ");
      return sendRe.test(meta);
    });
  }
  if (!chosen && composer.closest("form")) {
    const filtered = buttons.filter((btn) => {
      const meta = [btn.getAttribute("aria-label"), btn.getAttribute("title"), btn.getAttribute("data-testid"), btn.textContent].filter(Boolean).join(" ");
      return !skipRe.test(meta);
    });
    if (filtered.length > 0) {
      chosen = filtered[filtered.length - 1];
      confidence = "fallback";
    }
  }
  if (!chosen) return { found: false };
  chosen.setAttribute("data-arena-send", "1");
  const rect = chosen.getBoundingClientRect();
  return {
    found: true,
    confidence: confidence,
    x: Math.round(rect.left + rect.width / 2),
    y: Math.round(rect.top + rect.height / 2)
  };
}
""".strip()

_DETECT_POST_SUBMIT_CHALLENGE_JS = """
() => {
  const vw = Math.max(window.innerWidth || 0, 320);
  const vh = Math.max(window.innerHeight || 0, 240);
  function isFrameVisible(frame, minW, minH) {
    const style = window.getComputedStyle(frame);
    if (
      !style ||
      style.display === "none" ||
      style.visibility === "hidden" ||
      parseFloat(style.opacity || "1") <= 0.1
    ) {
      return null;
    }
    const rect = frame.getBoundingClientRect();
    if (!rect || rect.width < minW || rect.height < minH) {
      return null;
    }
    if (rect.bottom <= 16 || rect.right <= 16 || rect.top >= vh - 16 || rect.left >= vw - 16) {
      return null;
    }
    return rect;
  }

  for (const frame of document.querySelectorAll('iframe[src*="recaptcha"][src*="bframe"]')) {
    const rect = isFrameVisible(frame, 180, 180);
    if (rect) {
      return {
        kind: "recaptcha_bframe",
        x: Math.round(rect.left + rect.width / 2),
        y: Math.round(rect.top + rect.height / 2),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      };
    }
  }

  for (const frame of document.querySelectorAll('iframe[src*="recaptcha"][src*="anchor"]')) {
    const src = String(frame.getAttribute("src") || "");
    if (frame.closest(".grecaptcha-badge") || src.indexOf("size=invisible") !== -1) {
      continue;
    }
    const rect = isFrameVisible(frame, 140, 45);
    if (rect && rect.left >= -10 && rect.right <= vw + 24) {
      return {
        kind: "recaptcha_anchor",
        x: Math.round(rect.left + 28),
        y: Math.round(rect.top + Math.min(38, rect.height / 2)),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      };
    }
  }

  for (const frame of document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]')) {
    const rect = isFrameVisible(frame, 140, 40);
    if (rect && rect.left >= -10) {
      return {
        kind: "turnstile",
        x: Math.round(rect.left + 32),
        y: Math.round(rect.top + rect.height / 2),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      };
    }
  }

  for (const frame of document.querySelectorAll('iframe[src*="hcaptcha"]')) {
    const rect = isFrameVisible(frame, 140, 45);
    if (rect && rect.left >= -10) {
      return {
        kind: "hcaptcha",
        x: Math.round(rect.left + 30),
        y: Math.round(rect.top + rect.height / 2),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      };
    }
  }

  return { kind: null };
}
""".strip()

_COMPOSER_NONEMPTY_JS = (
    "/* composer_nonempty */ () => { "
    "const el = document.querySelector('[data-arena-composer=\"1\"]') || "
    "document.querySelector('textarea:not([id*=\"recaptcha\"]):not([name*=\"recaptcha\"]), [contenteditable=\"true\"]'); "
    "return !!(el && ((el.value || el.textContent || '').trim().length > 0)); }"
)

# --------------------------------------------------------------------------- #
# Archiving the finished conversation ("... -> Archive")
# --------------------------------------------------------------------------- #

#: Marks every candidate trigger for the conversation menu.  The sidebar row of
#: the chat that is open has a "menu" button (``aria-haspopup="menu"``); which
#: element it is exactly differs between Arena builds, so the candidates are
#: ranked, tried in order, and each is marked with its index for Playwright to
#: click with real pointer events (Radix opens on pointerdown, not on click).
_ARCHIVE_MARK_TRIGGERS_JS = r"""
/* arena_archive_triggers */ () => {
  for (const old of document.querySelectorAll('[data-arena-archive-trigger]')) {
    old.removeAttribute('data-arena-archive-trigger');
  }
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 2 && rect.height > 2 &&
      style.visibility !== 'hidden' && style.display !== 'none';
  };
  const activeRow = () => {
    const marked = document.querySelector(
      '[aria-current="page"], [data-state="active"], [data-active="true"]'
    );
    if (marked) return marked;
    const url = window.location.pathname || '';
    const match = url.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    if (match) {
      const link = document.querySelector('a[href*="' + match[0] + '"]');
      if (link) return link;
    }
    return null;
  };

  const ranked = [];
  const push = (el, score) => {
    if (!el || !visible(el) || ranked.some((item) => item.el === el)) return;
    ranked.push({ el, score });
  };

  const row = activeRow();
  // 1. A menu button inside the active conversation row (or its container).
  if (row) {
    for (const el of row.querySelectorAll('[aria-haspopup="menu"]')) push(el, 100);
    let parent = row.parentElement;
    for (let depth = 0; parent && depth < 3; depth += 1, parent = parent.parentElement) {
      for (const el of parent.querySelectorAll('[aria-haspopup="menu"]')) push(el, 90 - depth);
    }
  }
  // 2. Any menu button that sits next to a link to a conversation.
  for (const link of document.querySelectorAll('a[href*="/c/"], a[href*="/chat"]')) {
    const container = link.closest('li, [role="listitem"], div');
    if (!container) continue;
    for (const el of container.querySelectorAll('[aria-haspopup="menu"]')) push(el, 70);
  }
  // 3. Last resort: a visible menu button in a sidebar-ish container.
  for (const el of document.querySelectorAll('[aria-haspopup="menu"]')) {
    const inNav = el.closest('aside, nav, [role="navigation"]');
    push(el, inNav ? 50 : 10);
  }

  ranked.sort((left, right) => right.score - left.score);
  const chosen = ranked.slice(0, 4);
  chosen.forEach((item, index) => {
    item.el.setAttribute('data-arena-archive-trigger', String(index));
  });
  return { found: chosen.length, strategies: chosen.map((item) => item.score) };
}
""".strip()

_ARCHIVE_FIND_ITEM_JS = r"""
/* arena_archive_item */ () => {
  for (const old of document.querySelectorAll('[data-arena-archive-item]')) {
    old.removeAttribute('data-arena-archive-item');
  }
  const label = /(^|\s)(archive|архив|в архив)(\s|$)/i;
  const items = [];
  for (const el of document.querySelectorAll(
    '[role="menuitem"], [data-radix-collection-item], [role="menu"] [role="option"]'
  )) {
    const text = (el.textContent || '').trim();
    if (!text || !label.test(text)) continue;
    if (el.getAttribute('data-disabled') !== null) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) continue;
    items.push(el);
  }
  if (!items.length) return { found: 0 };
  items[0].setAttribute('data-arena-archive-item', '1');
  return { found: items.length, label: (items[0].textContent || '').trim() };
}
""".strip()

_ARCHIVE_CLOSE_MENU_JS = (
    "/* arena_archive_close */ () => { document.body.dispatchEvent("
    "new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); return true; }"
)

_ARCHIVE_CONFIRM_JS = r"""
/* arena_archive_confirm */ () => {
  const menuOpen = document.querySelector('[role="menu"]');
  const active = document.querySelector(
    '[aria-current="page"], [data-state="active"], [data-active="true"]'
  );
  return { menu_open: Boolean(menuOpen), active_row: Boolean(active) && active.offsetParent !== null };
}
""".strip()

_ARCHIVE_CLEANUP_JS = (
    "/* arena_archive_cleanup */ () => { "
    "for (const el of document.querySelectorAll("
    "'[data-arena-archive-trigger],[data-arena-archive-item]')) { "
    "el.removeAttribute('data-arena-archive-trigger'); "
    "el.removeAttribute('data-arena-archive-item'); } }"
)


async def _click_marked(page: Any, selector: str) -> bool:
    """Click a marked element with real pointer events, then by coordinates."""
    locator_fn = getattr(page, "locator", None)
    if callable(locator_fn):
        try:
            target = locator_fn(selector).first
            await target.click(timeout=2_000)
            return True
        except Exception:  # noqa: BLE001 - fall back to a synthetic click
            pass
        try:
            target = locator_fn(selector).first
            await target.click(timeout=1_500, force=True)
            return True
        except Exception:  # noqa: BLE001
            pass
    eval_fn = getattr(page, "evaluate", None)
    if callable(eval_fn):
        try:
            await eval_fn(
                "/* arena_archive_dom_click */ () => {"
                f" const el = document.querySelector({selector!r});"
                " if (!el) return false;"
                " for (const type of ['pointerdown','mousedown','pointerup','mouseup','click']) {"
                "  el.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)"
                "   (type, { bubbles: true, cancelable: true, view: window, button: 0 }));"
                " } return true; }"
            )
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


async def archive_conversation(
    page: Any,
    *,
    timeout: float = 8.0,
    confirm_timeout: float = 0.6,
    wait_fn: Any = None,
) -> bool:
    """Move the open conversation to the archive.

    The UI action is "conversation row -> menu -> Archive"; the row's menu
    button is found by :data:`_ARCHIVE_MARK_TRIGGERS_JS`, the ``Archive`` entry
    by :data:`_ARCHIVE_FIND_ITEM_JS` (that is the ``role="menuitem"`` element
    with a lucide ``archive`` icon in Arena's markup).  Returns ``True`` only
    when the menu item was actually clicked, so a caller can log a miss instead
    of pretending the chat was archived.
    """
    eval_fn = getattr(page, "evaluate", None)
    if not callable(eval_fn):
        return False
    sleep = wait_fn or getattr(page, "wait_for_timeout", None)

    async def _wait() -> None:
        if callable(sleep):
            try:
                await sleep(60)
            except Exception:  # noqa: BLE001 - a fake page may not implement it
                pass

    try:
        marked = await eval_fn(_ARCHIVE_MARK_TRIGGERS_JS)
        found = int(marked.get("found") or 0) if isinstance(marked, Mapping) else 0
        if not found:
            return False
        deadline = time.monotonic() + max(1.0, timeout)
        for index in range(found):
            selector = f'[data-arena-archive-trigger="{index}"]'
            if not await _click_marked(page, selector):
                continue
            # Radix renders the menu into a portal on the next frame.
            for _ in range(12):
                item = await eval_fn(_ARCHIVE_FIND_ITEM_JS)
                if isinstance(item, Mapping) and int(item.get("found") or 0):
                    break
                if time.monotonic() >= deadline:
                    break
                await _wait()
            else:
                item = {"found": 0}
            if isinstance(item, Mapping) and int(item.get("found") or 0):
                clicked = await _click_marked(page, '[data-arena-archive-item="1"]')
                if not clicked:
                    return False
                # Give the app a moment to send its request: closing the browser
                # right after the click would abort the archival.  The menu
                # closing is the first sign it went through.
                deadline_confirm = time.monotonic() + max(0.0, confirm_timeout)
                while time.monotonic() < deadline_confirm:
                    state = await eval_fn(_ARCHIVE_CONFIRM_JS)
                    if isinstance(state, Mapping) and not state.get("menu_open"):
                        break
                    await _wait()
                return True
            # Wrong trigger: close whatever it opened and try the next one.
            try:
                await eval_fn(_ARCHIVE_CLOSE_MENU_JS)
            except Exception:  # noqa: BLE001
                pass
            await _wait()
        return False
    finally:
        try:
            await eval_fn(_ARCHIVE_CLEANUP_JS)
        except Exception:  # noqa: BLE001
            pass


_CLEANUP_ARENA_DOM_MARKS_JS = (
    "/* arena_cleanup_marks */ () => { "
    "for (const el of document.querySelectorAll('[data-arena-composer],[data-arena-send]')) { "
    "el.removeAttribute('data-arena-composer'); el.removeAttribute('data-arena-send'); } }"
)


# --------------------------------------------------------------------------- #
# Page challenge detection, checkbox clicker & composer submission
# --------------------------------------------------------------------------- #


async def _detect_post_submit_challenge(page: Any) -> dict[str, Any]:
    eval_fn = getattr(page, "evaluate", None)
    if not callable(eval_fn):
        return {"kind": None}
    try:
        raw = await eval_fn(_DETECT_POST_SUBMIT_CHALLENGE_JS)
    except Exception:  # noqa: BLE001
        return {"kind": None}
    if isinstance(raw, bool):
        return {"kind": "recaptcha_bframe" if raw else None}
    if isinstance(raw, Mapping):
        kind = raw.get("kind")
        if kind == "recaptcha_anchor":
            for frame in getattr(page, "frames", ()) or ():
                f_url = str(getattr(frame, "url", "") or "")
                if (
                    "recaptcha" in f_url
                    and "anchor" in f_url
                    and "size=invisible" not in f_url
                ):
                    loc_fn = getattr(frame, "locator", None)
                    if callable(loc_fn):
                        try:
                            if (
                                await loc_fn(
                                    '#recaptcha-anchor[aria-checked="true"]'
                                ).count()
                                > 0
                            ):
                                return {"kind": None}
                        except Exception:  # noqa: BLE001
                            pass
        return dict(raw) if isinstance(kind, str) and kind else {"kind": None}
    return {"kind": None}


async def _safe_bring_to_front(page: Any) -> None:
    bring_fn = getattr(page, "bring_to_front", None)
    if callable(bring_fn):
        try:
            await bring_fn()
        except Exception:  # noqa: BLE001
            pass


async def _click_challenge_checkbox(
    page: Any,
    obs: Mapping[str, Any],
    *,
    bring_to_front: bool = False,
) -> bool:
    kind = str(obs.get("kind") or "")
    if kind == "recaptcha_anchor":
        for frame in getattr(page, "frames", ()) or ():
            f_url = str(getattr(frame, "url", "") or "")
            if (
                "recaptcha" in f_url
                and "anchor" in f_url
                and "size=invisible" not in f_url
            ):
                loc_fn = getattr(frame, "locator", None)
                if callable(loc_fn):
                    try:
                        checked = await loc_fn(
                            '#recaptcha-anchor[aria-checked="true"]'
                        ).count()
                        if checked > 0:
                            return False
                    except Exception:  # noqa: BLE001
                        pass

    if bring_to_front:
        await _safe_bring_to_front(page)
    wait_fn = getattr(page, "wait_for_timeout", None)
    if callable(wait_fn):
        # Let the widget finish animating in, but do not pay a third of a
        # second for it: the mouse path below already takes a few frames.
        await wait_fn(120)

    x = float(obs.get("x") or 0)
    y = float(obs.get("y") or 0)
    mouse = getattr(page, "mouse", None)
    if x > 0 and y > 0 and mouse is not None:
        move_fn = getattr(mouse, "move", None)
        click_fn = getattr(mouse, "click", None)
        if callable(move_fn):
            try:
                await move_fn(max(20.0, x - 90.0), max(20.0, y - 50.0), steps=8)
                await move_fn(x, y, steps=14)
            except Exception:  # noqa: BLE001
                pass
        if callable(wait_fn):
            await wait_fn(90)
        if callable(click_fn):
            try:
                await click_fn(x, y, delay=70)
                return True
            except Exception:  # noqa: BLE001
                pass

    if kind == "recaptcha_anchor":
        for frame in getattr(page, "frames", ()) or ():
            f_url = str(getattr(frame, "url", "") or "")
            if (
                "recaptcha" in f_url
                and "anchor" in f_url
                and "size=invisible" not in f_url
            ):
                loc_fn = getattr(frame, "locator", None)
                if callable(loc_fn):
                    try:
                        await loc_fn("#recaptcha-anchor").first.click()
                        return True
                    except Exception:  # noqa: BLE001
                        pass
    return False


async def _gentle_page_warmup(page: Any) -> None:
    mouse = getattr(page, "mouse", None)
    move_fn = getattr(mouse, "move", None) if mouse is not None else None
    if callable(move_fn):
        try:
            await move_fn(360.0, 340.0, steps=3)
        except Exception:  # noqa: BLE001
            pass


async def _reclick_send_button_if_unsent(page: Any) -> bool:
    eval_fn = getattr(page, "evaluate", None)
    locator_fn = getattr(page, "locator", None)
    if not (callable(eval_fn) and callable(locator_fn)):
        return False
    try:
        has_text = bool(await eval_fn(_COMPOSER_NONEMPTY_JS))
    except Exception:  # noqa: BLE001
        return False
    if not has_text:
        return False
    try:
        btn_info = await eval_fn(_MARK_SEND_BUTTON_JS)
    except Exception:  # noqa: BLE001
        return False
    if not (isinstance(btn_info, Mapping) and btn_info.get("found")):
        return False
    mouse = getattr(page, "mouse", None)
    if mouse is not None and callable(getattr(mouse, "move", None)):
        try:
            await mouse.move(
                float(btn_info.get("x") or 500),
                float(btn_info.get("y") or 700),
                steps=6,
            )
        except Exception:  # noqa: BLE001
            pass
    try:
        await locator_fn('[data-arena-send="1"]').first.click()
    finally:
        try:
            await eval_fn(_CLEANUP_ARENA_DOM_MARKS_JS)
        except Exception:  # noqa: BLE001
            pass
    return True


async def _replay_eval_with_v2_token(
    page: Any,
    eval_url: str,
    post_data: str,
    v2_token: str,
) -> bool:
    eval_fn = getattr(page, "evaluate", None)
    if not (callable(eval_fn) and eval_url and post_data and v2_token):
        return False
    try:
        parsed = json.loads(post_data)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(parsed, dict):
        return False
    parsed["recaptchaV2Token"] = v2_token
    url_literal = json.dumps(eval_url)
    body_literal = json.dumps(json.dumps(parsed, separators=(",", ":")))
    script = f"""
    /* arena_replay_v2 */ () => {{
      window.fetch({url_literal}, {{
        method: "POST",
        credentials: "include",
        headers: {{
          "Content-Type": "application/json",
          "Accept": "text/event-stream, text/plain, */*"
        }},
        body: {body_literal}
      }}).catch(() => {{}});
      return true;
    }}
    """
    try:
        return bool(await eval_fn(script))
    except Exception:  # noqa: BLE001
        return False


async def _submit_prompt_via_composer(
    page: Any,
    prompt: str,
    *,
    model_id_override: str | None = None,
    generation_params: Mapping[str, Any] | None = None,
    has_submitted: Callable[[], bool] | None = None,
    install_legacy_tap_flags: bool = False,
    fast_submit: bool = False,
    mark: Callable[[str], None] | None = None,
) -> bool:
    locator_fn = getattr(page, "locator", None)
    eval_fn = getattr(page, "evaluate", None)
    if not (callable(locator_fn) and callable(eval_fn)):
        return False

    def _mark(name: str) -> None:
        # Phase marks are diagnostic only: a broken callback must never break
        # a submission.
        if mark is None:
            return
        try:
            mark(name)
        except Exception:  # noqa: BLE001
            pass

    patch_obj: dict[str, Any] = {}
    if model_id_override:
        patch_obj["modelAId"] = model_id_override
    if generation_params:
        patch_obj["generationConfig"] = dict(generation_params)
    if patch_obj:
        await eval_fn(_build_main_frame_body_patch_js(patch_obj))
    if install_legacy_tap_flags:
        patch_literal = json.dumps(patch_obj) if patch_obj else "null"
        await eval_fn(
            f"() => {{ window.__arenaBodyPatch = {patch_literal}; window.__arenaTapArmed = true; }}"
        )

    wait_fn = getattr(page, "wait_for_timeout", None)
    info = await eval_fn(_FIND_COMPOSER_JS)
    if (
        isinstance(info, Mapping)
        and not info.get("found")
        and callable(wait_fn)
    ):
        for _ in range(25):
            await wait_fn(40)
            info = await eval_fn(_FIND_COMPOSER_JS)
            if not isinstance(info, Mapping) or info.get("found"):
                break
    if not isinstance(info, Mapping) or not info.get("found"):
        return False
    _mark("composer.visible")

    composer = locator_fn('[data-arena-composer="1"]').first
    # focus() instead of click(): no actionability animation, no mouse move, no
    # click dispatch -- the composer only needs the caret, and fill()/type()
    # verify the element is editable anyway.  click() stays as a fallback for
    # pages whose composer only reacts to a real pointer event.
    focus_fn = getattr(composer, "focus", None)
    if callable(focus_fn):
        try:
            await focus_fn()
        except Exception:  # noqa: BLE001
            await composer.click()
    else:
        await composer.click()
    _mark("composer.focused")

    keyboard = getattr(page, "keyboard", None)
    single_line = "\n" not in prompt and "\r" not in prompt
    if (
        single_line
        and len(prompt) <= 32
        and keyboard is not None
        and callable(getattr(keyboard, "type", None))
    ):
        await keyboard.type(prompt, delay=2)
        try:
            non_empty = await eval_fn(_COMPOSER_NONEMPTY_JS)
        except Exception:  # noqa: BLE001
            non_empty = True
        if not non_empty:
            await composer.fill(prompt)
    else:
        first_line = prompt.splitlines()[0][:4] if prompt else ""
        if (
            first_line
            and keyboard is not None
            and callable(getattr(keyboard, "type", None))
        ):
            await keyboard.type(first_line, delay=2)
        await composer.fill(prompt)
    _mark("prompt.filled")

    if fast_submit:
        # Enter submits the composer in one event instead of mark-scan + mouse
        # move + button click (~200-300 ms).  Guarded: if the app did not start
        # an evaluation request, fall through to the explicit Send button, so a
        # composer that ignores Enter behaves exactly as before.
        press_fn = getattr(composer, "press", None)
        if callable(press_fn):
            try:
                await press_fn("Enter")
            except Exception:  # noqa: BLE001
                pass
            else:
                for _ in range(5):
                    if has_submitted is not None and has_submitted():
                        _mark("send.dispatched")
                        try:
                            await eval_fn(_CLEANUP_ARENA_DOM_MARKS_JS)
                        except Exception:  # noqa: BLE001
                            pass
                        return True
                    if callable(wait_fn):
                        await wait_fn(40)

    btn_info = await eval_fn(_MARK_SEND_BUTTON_JS)
    try:
        if isinstance(btn_info, Mapping) and btn_info.get("found"):
            send_btn = locator_fn('[data-arena-send="1"]').first
            await send_btn.click()
            _mark("send.dispatched")
            # Only press Enter as a fallback when no explicit submit/Send button
            # matched and no evaluation request or challenge has started yet;
            # pressing Enter on top of an explicit Send click fires a duplicate
            # submission while grecaptcha.enterprise.execute() is still running.
            if btn_info.get("confidence") == "fallback":
                # The button was matched by a heuristic, so the click may have
                # hit something inert.  Give the submit up to 500 ms to show up,
                # but stop as soon as it does instead of always paying the full
                # half second before pressing Enter.
                if callable(wait_fn):
                    for _ in range(20):
                        if has_submitted is not None and has_submitted():
                            break
                        await wait_fn(25)
                already_started = bool(has_submitted and has_submitted())
                if not already_started:
                    challenge = await _detect_post_submit_challenge(page)
                    already_started = challenge.get("kind") is not None
                if not already_started:
                    try:
                        still_has_text = bool(await eval_fn(_COMPOSER_NONEMPTY_JS))
                    except Exception:  # noqa: BLE001
                        still_has_text = False
                    if still_has_text:
                        await composer.press("Enter")
        else:
            await composer.press("Enter")
    finally:
        try:
            await eval_fn(_CLEANUP_ARENA_DOM_MARKS_JS)
        except Exception:  # noqa: BLE001
            pass
    return True


# --------------------------------------------------------------------------- #
# Real-time C++ CDP response streaming (Network.streamResourceContent)
# --------------------------------------------------------------------------- #

_CDP_ARM_TIMEOUT_SECONDS = 2.0


def _is_eval_stream_url_local(url: str) -> bool:
    return any(marker in url for marker in _EVAL_STREAM_URL_MARKERS)


class _CdpEvalStreamPump:
    """Stream Direct Chat HTTP 2xx evaluation bodies live over CDP.

    Uses ``Network.streamResourceContent`` + ``Network.dataReceived`` so raw
    wire chunks reach Python immediately as tokens arrive from ``arena.ai``
    (zero JS injection, zero ``resp.text()`` full-response wait, and raw wire
    UTF-8 bytes untouched by Blink's ``TextResourceDecoder``).
    """

    def __init__(self, cdp: Any, *, owns_session: bool = True) -> None:
        self._cdp = cdp
        self._owns_session = owns_session
        self._queue: asyncio.Queue[tuple[str, Any]] | None = None
        self._eval_requests: set[str] = set()
        self._captures: dict[str, dict[str, Any]] = {}
        self._unmatched_cdp_claims: deque[
            asyncio.Future[asyncio.Future[None] | None]
        ] = deque()
        self._unmatched_pw_waiters: deque[
            asyncio.Future[asyncio.Future[None] | None]
        ] = deque()
        self._tasks: set[asyncio.Task[None]] = set()
        self._listeners: list[tuple[str, Callable[..., Any]]] = []
        self._closed = False

    @classmethod
    async def attach(
        cls,
        context: Any,
        page: Any,
        *,
        existing_cdp: Any = None,
    ) -> _CdpEvalStreamPump | None:
        cdp = existing_cdp
        owns_session = False
        if cdp is None:
            new_cdp_fn = getattr(context, "new_cdp_session", None)
            if not callable(new_cdp_fn):
                return None
            try:
                cdp = await new_cdp_fn(page)
                owns_session = True
            except Exception:  # noqa: BLE001
                return None
        on_fn = getattr(cdp, "on", None)
        send_fn = getattr(cdp, "send", None)
        if not (callable(on_fn) and callable(send_fn)):
            if owns_session:
                detach_fn = getattr(cdp, "detach", None)
                if callable(detach_fn):
                    try:
                        await detach_fn()
                    except Exception:  # noqa: BLE001
                        pass
            return None
        try:
            await send_fn("Network.enable")
        except Exception:  # noqa: BLE001
            if owns_session:
                detach_fn = getattr(cdp, "detach", None)
                if callable(detach_fn):
                    try:
                        await detach_fn()
                    except Exception:  # noqa: BLE001
                        pass
            return None
        pump = cls(cdp, owns_session=owns_session)
        pump._install_listeners()
        return pump

    def _install_listeners(self) -> None:
        on_fn = getattr(self._cdp, "on", None)
        if not callable(on_fn):
            return
        pairs: tuple[tuple[str, Callable[[Any], None]], ...] = (
            ("Network.requestWillBeSent", self._on_request_will_be_sent),
            ("Network.responseReceived", self._on_response_received),
            ("Network.dataReceived", self._on_data_received),
            ("Network.loadingFinished", self._on_loading_finished),
            ("Network.loadingFailed", self._on_loading_failed),
        )
        for ev_name, handler in pairs:
            on_fn(ev_name, handler)
            self._listeners.append((ev_name, handler))

    def bind_queue(
        self, queue_obj: asyncio.Queue[tuple[str, Any]] | None
    ) -> None:
        self._queue = queue_obj
        self._eval_requests.clear()
        for entry in list(self._captures.values()):
            done_fut = entry.get("done_fut")
            if isinstance(done_fut, asyncio.Future) and not done_fut.done():
                done_fut.set_result(None)
        self._captures.clear()
        while self._unmatched_cdp_claims:
            fut = self._unmatched_cdp_claims.popleft()
            if not fut.done():
                fut.set_result(None)
        while self._unmatched_pw_waiters:
            fut = self._unmatched_pw_waiters.popleft()
            if not fut.done():
                fut.set_result(None)

    def _on_request_will_be_sent(self, params: Any) -> None:
        if self._closed or self._queue is None or not isinstance(params, Mapping):
            return
        req_id = params.get("requestId")
        req_obj = params.get("request")
        if not (isinstance(req_id, str) and req_id and isinstance(req_obj, Mapping)):
            return
        method = str(req_obj.get("method") or "GET").upper()
        url = str(req_obj.get("url") or "")
        if method == "POST" and _is_eval_stream_url_local(url):
            self._eval_requests.add(req_id)

    def _on_response_received(self, params: Any) -> None:
        if self._closed or self._queue is None or not isinstance(params, Mapping):
            return
        req_id = params.get("requestId")
        if not isinstance(req_id, str) or req_id not in self._eval_requests:
            return
        resp_obj = params.get("response")
        if not isinstance(resp_obj, Mapping):
            return
        status = int(resp_obj.get("status") or 200)
        if status >= 400:
            self._eval_requests.discard(req_id)
            return
        ct_val = ""
        raw_hdrs = resp_obj.get("headers")
        if isinstance(raw_hdrs, Mapping):
            for hk, hv in raw_hdrs.items():
                if str(hk).lower() == "content-type":
                    ct_val = str(hv or "")
                    break

        loop = asyncio.get_running_loop()
        if self._unmatched_pw_waiters:
            claim_fut = self._unmatched_pw_waiters.popleft()
        else:
            claim_fut = loop.create_future()
            self._unmatched_cdp_claims.append(claim_fut)

        done_fut: asyncio.Future[None] = loop.create_future()
        entry: dict[str, Any] = {
            "armed": False,
            "failed_arm": False,
            "pending_chunks": [],
            "ended": None,
            "content_type": ct_val,
            "queue": self._queue,
            "done_fut": done_fut,
        }
        self._captures[req_id] = entry
        task = loop.create_task(self._arm_stream(req_id, entry, claim_fut))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _emit_to_entry_queue(
        self, entry: Mapping[str, Any], item: tuple[str, Any]
    ) -> None:
        q = entry.get("queue")
        if isinstance(q, asyncio.Queue) and q is self._queue:
            q.put_nowait(item)

    async def _arm_stream(
        self,
        req_id: str,
        entry: dict[str, Any],
        claim_fut: asyncio.Future[asyncio.Future[None] | None],
    ) -> None:
        try:
            res = await asyncio.wait_for(
                self._cdp.send(
                    "Network.streamResourceContent", {"requestId": req_id}
                ),
                timeout=_CDP_ARM_TIMEOUT_SECONDS,
            )
            if not isinstance(res, Mapping):
                raise RuntimeError("empty streamResourceContent response")
            buf_b64 = res.get("bufferedData")
            buffered = (
                base64.b64decode(buf_b64)
                if isinstance(buf_b64, str) and buf_b64
                else b""
            )
        except Exception:  # noqa: BLE001
            entry["failed_arm"] = True
            self._captures.pop(req_id, None)
            self._eval_requests.discard(req_id)
            done_fut = entry["done_fut"]
            if not done_fut.done():
                done_fut.set_result(None)
            if not claim_fut.done():
                claim_fut.set_result(None)
            return

        done_fut = entry["done_fut"]
        if not claim_fut.done():
            claim_fut.set_result(done_fut)

        self._emit_to_entry_queue(entry, ("meta", entry["content_type"]))
        if buffered:
            self._emit_to_entry_queue(entry, ("chunk", buffered))
        pending: list[bytes] = entry["pending_chunks"]
        for raw_chunk in pending:
            if raw_chunk:
                self._emit_to_entry_queue(entry, ("chunk", raw_chunk))
        pending.clear()

        entry["armed"] = True
        ended = entry["ended"]
        if ended is not None:
            self._captures.pop(req_id, None)
            self._eval_requests.discard(req_id)
            self._emit_to_entry_queue(entry, ended)
            if not done_fut.done():
                done_fut.set_result(None)

    def _on_data_received(self, params: Any) -> None:
        if not self._captures or not isinstance(params, Mapping):
            return
        req_id = params.get("requestId")
        if not isinstance(req_id, str):
            return
        entry = self._captures.get(req_id)
        if entry is None or entry.get("failed_arm"):
            return
        b64 = params.get("data")
        if not isinstance(b64, str) or not b64:
            return
        try:
            raw = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            return
        if not raw:
            return
        if entry["armed"]:
            self._emit_to_entry_queue(entry, ("chunk", raw))
        else:
            entry["pending_chunks"].append(raw)

    def _on_loading_finished(self, params: Any) -> None:
        self._finish_request(params, ("eof", None))

    def _on_loading_failed(self, params: Any) -> None:
        if not isinstance(params, Mapping):
            return
        if params.get("canceled"):
            terminal: tuple[str, Any] = ("eof", None)
        else:
            err_text = str(params.get("errorText") or "network stream failed")
            terminal = ("stream_error", err_text)
        self._finish_request(params, terminal)

    def _finish_request(
        self, params: Any, terminal: tuple[str, Any]
    ) -> None:
        if not isinstance(params, Mapping):
            return
        req_id = params.get("requestId")
        if not isinstance(req_id, str):
            return
        self._eval_requests.discard(req_id)
        entry = self._captures.get(req_id)
        if entry is None or entry.get("failed_arm"):
            return
        if entry["armed"]:
            self._captures.pop(req_id, None)
            self._emit_to_entry_queue(entry, terminal)
            done_fut = entry["done_fut"]
            if not done_fut.done():
                done_fut.set_result(None)
        else:
            entry["ended"] = terminal

    async def await_claim_for_2xx(self) -> asyncio.Future[None] | None:
        if self._closed or self._queue is None:
            return None
        if self._unmatched_cdp_claims:
            claim_fut = self._unmatched_cdp_claims.popleft()
            return await claim_fut
        if not self._eval_requests:
            return None
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[asyncio.Future[None] | None] = (
            loop.create_future()
        )
        self._unmatched_pw_waiters.append(waiter)
        try:
            return await asyncio.wait_for(waiter, timeout=0.35)
        except asyncio.TimeoutError:
            try:
                self._unmatched_pw_waiters.remove(waiter)
            except ValueError:
                pass
            return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.bind_queue(None)
        remove_fn = getattr(self._cdp, "remove_listener", None)
        if callable(remove_fn):
            for ev_name, cb in self._listeners:
                try:
                    remove_fn(ev_name, cb)
                except Exception:  # noqa: BLE001
                    pass
        self._listeners.clear()
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        if self._owns_session:
            detach_fn = getattr(self._cdp, "detach", None)
            if callable(detach_fn):
                try:
                    await detach_fn()
                except Exception:  # noqa: BLE001
                    pass
