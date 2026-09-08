from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShellStateContract:
    """Executable UX contract for one currently reachable Headquarters state."""

    key: str
    state_kind: str
    route: str
    required_text: tuple[str, ...]
    required_actions: tuple[str, ...] = ()


PROHIBITED_PRIMARY_TOKENS = (
    "prf_",
    "sqlite",
    "traceback",
    "payload_json",
    "idempotency_key",
    "transport_route_id",
)

REPRESENTATIVE_SHELL_STATES = (
    ShellStateContract(
        key="first-profile",
        state_kind="create-profile",
        route="/",
        required_text=("Welcome to your Headquarters", "Artist name", "Local-first"),
        required_actions=("/profile/create",),
    ),
    ShellStateContract(
        key="profile-selection",
        state_kind="select-profile",
        route="/",
        required_text=("Who are you working as today?", "Your local Artists"),
        required_actions=("/profile/select",),
    ),
    ShellStateContract(
        key="no-song",
        state_kind="running-no-song",
        route="/",
        required_text=("What are we making today?", "Start a Song"),
        required_actions=("/song/start",),
    ),
    ShellStateContract(
        key="active-song",
        state_kind="running-home",
        route="/",
        required_text=("Pick up where you left off", "Your active Song", "Resume Song"),
    ),
    ShellStateContract(
        key="no-focus",
        state_kind="running-now",
        route="/now",
        required_text=("What matters now", "No Focus Session active", "Make", "Finish"),
        required_actions=("/focus/set",),
    ),
    ShellStateContract(
        key="active-focus",
        state_kind="running-now",
        route="/now",
        required_text=("What matters now", "Focus active", "End Focus"),
        required_actions=("/focus/set", "/focus/end"),
    ),
    ShellStateContract(
        key="settings",
        state_kind="running-settings",
        route="/settings",
        required_text=("Your N0TE", "Quit N0TE", "Connections"),
        required_actions=("/quit",),
    ),
    ShellStateContract(
        key="blocked-ownership",
        state_kind="blocked",
        route="/",
        required_text=("This Artist is already open", "Nothing was overwritten"),
    ),
    ShellStateContract(
        key="recovery",
        state_kind="recovery",
        route="/",
        required_text=("needs recovery", "Nothing was overwritten"),
    ),
)


SHELL_CSS = r"""
:root {
  color-scheme: dark;
  --color-bg: #0b0d10;
  --color-surface: #13171c;
  --color-surface-raised: #191f26;
  --color-field: #0e1216;
  --color-text: #f3f5f7;
  --color-muted: #a9b2bd;
  --color-border: #2a323c;
  --color-accent: #d9ff63;
  --color-accent-ink: #11150a;
  --color-danger: #ffb4aa;
  --color-status-good: #c8f77d;
  --color-status-caution: #ffd38b;
  --radius-panel: 18px;
  --radius-control: .75rem;
  --target-min: 44px;
  --shell-max: 1180px;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
* { box-sizing: border-box; }
html { background: var(--color-bg); font-size: 16px; }
body {
  margin: 0;
  min-height: 100vh;
  color: var(--color-text);
  background: radial-gradient(circle at 12% 0%, #18202a 0, var(--color-bg) 38rem);
}
a { color: inherit; }
.skip-link, .nav a, button, .button {
  min-height: 44px;
  min-height: var(--target-min);
}
.skip-link {
  position: absolute;
  left: 1rem;
  top: -4rem;
  z-index: 10;
  padding: .75rem 1rem;
  background: var(--color-accent);
  color: var(--color-accent-ink);
  border-radius: var(--radius-control);
}
.skip-link:focus { top: 1rem; }
.shell { width: min(var(--shell-max), calc(100% - 2rem)); margin: 0 auto; padding: 1.25rem 0 3rem; }
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 1rem;
  padding: .5rem 0 1.25rem;
}
.topbar > .row { min-width: 0; justify-content: flex-end; }
.brand {
  min-width: 0;
  display: flex;
  gap: .7rem;
  align-items: center;
  font-weight: 760;
  letter-spacing: -.02em;
  text-decoration: none;
  overflow-wrap: anywhere;
}
.brand-mark {
  flex: 0 0 auto;
  width: 2.1rem;
  height: 2.1rem;
  display: grid;
  place-items: center;
  border: 1px solid var(--color-border);
  border-radius: .7rem;
  background: var(--color-surface);
}
.local-badge {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: .45rem;
  color: var(--color-muted);
  font-size: .88rem;
  overflow-wrap: anywhere;
}
.local-badge::before {
  content: "";
  flex: 0 0 auto;
  width: .55rem;
  height: .55rem;
  border: 1px solid currentColor;
  border-radius: 999px;
  background: var(--color-accent);
}
.layout { display: grid; grid-template-columns: 13rem minmax(0, 1fr); gap: 1.25rem; align-items: start; }
.nav { position: sticky; top: 1rem; display: grid; gap: .35rem; }
.nav a {
  min-width: 0;
  display: flex;
  align-items: center;
  padding: .7rem .85rem;
  color: var(--color-muted);
  text-decoration: none;
  border: 1px solid transparent;
  border-radius: .8rem;
  overflow-wrap: anywhere;
}
.nav a:hover, .nav a[aria-current="page"] {
  color: var(--color-text);
  background: var(--color-surface);
  border-color: var(--color-border);
}
main { min-width: 0; }
.hero {
  padding: clamp(1.35rem, 4vw, 3rem);
  border: 1px solid var(--color-border);
  border-radius: calc(var(--radius-panel) + 4px);
  background: linear-gradient(150deg, var(--color-surface-raised), var(--color-surface));
  box-shadow: 0 24px 70px rgba(0,0,0,.22);
}
.eyebrow {
  margin: 0 0 .55rem;
  color: var(--color-accent);
  font-size: .78rem;
  font-weight: 760;
  letter-spacing: .11em;
  text-transform: uppercase;
}
h1 {
  margin: 0;
  max-width: 18ch;
  font-size: clamp(2rem, 6vw, 4.6rem);
  line-height: .98;
  letter-spacing: -.055em;
  overflow-wrap: anywhere;
}
.lede { max-width: 62ch; margin: 1rem 0 0; color: var(--color-muted); font-size: 1.05rem; line-height: 1.65; }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem; margin-top: 1rem; }
.card {
  min-width: 0;
  padding: 1.25rem;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-panel);
  background: var(--color-surface);
  background: color-mix(in srgb, var(--color-surface) 94%, transparent);
}
.card h2 { margin: 0 0 .45rem; font-size: 1.05rem; overflow-wrap: anywhere; }
.card p { margin: .35rem 0; color: var(--color-muted); line-height: 1.55; overflow-wrap: anywhere; }
.song-name {
  margin-top: .5rem !important;
  color: var(--color-text) !important;
  font-size: clamp(1.35rem, 4vw, 2rem);
  font-weight: 760;
  letter-spacing: -.035em;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.stack { display: grid; gap: .75rem; }
.row { min-width: 0; display: flex; flex-wrap: wrap; gap: .65rem; align-items: center; }
label { display: block; margin-bottom: .4rem; font-weight: 650; }
input[type="text"], textarea {
  width: 100%;
  min-height: 46px;
  padding: .72rem .8rem;
  color: var(--color-text);
  background: var(--color-field);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-control);
  font: inherit;
}
textarea { resize: vertical; line-height: 1.5; }
button, .button {
  min-width: 0;
  max-width: 100%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: .4rem;
  padding: .72rem 1rem;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-control);
  background: var(--color-surface-raised);
  color: var(--color-text);
  font: inherit;
  font-weight: 720;
  text-decoration: none;
  text-align: center;
  overflow-wrap: anywhere;
  cursor: pointer;
}
button.primary, .button.primary, button[aria-pressed="true"] {
  border-color: var(--color-accent);
  background: var(--color-accent);
  color: var(--color-accent-ink);
}
button.danger { color: var(--color-danger); }
button:hover, .button:hover { filter: brightness(1.08); }
button:focus-visible, a:focus-visible, input:focus-visible, textarea:focus-visible {
  outline: 3px solid var(--color-accent);
  outline-offset: 3px;
}
.status { display: inline-flex; align-items: center; gap: .55rem; color: var(--color-muted); }
.status::before {
  content: "";
  flex: 0 0 auto;
  width: .65rem;
  height: .65rem;
  border: 1px solid currentColor;
  border-radius: 999px;
  background: currentColor;
}
.status.good { color: var(--color-status-good); }
.status.caution { color: var(--color-status-caution); }
.notice {
  margin-top: 1rem;
  padding: 1rem;
  border-left: 3px solid var(--color-accent);
  background: var(--color-surface);
  color: var(--color-muted);
  border-radius: .6rem;
}
.muted { color: var(--color-muted); }
footer { padding: 2rem 0 0; color: var(--color-muted); font-size: .85rem; overflow-wrap: anywhere; }

@media (max-width: 760px) {
  .shell { width: min(100% - 1rem, var(--shell-max)); }
  .layout { grid-template-columns: 1fr; }
  .nav {
    position: static;
    grid-template-columns: none;
    grid-auto-flow: column;
    grid-auto-columns: minmax(5.5rem, 1fr);
    overflow-x: auto;
    overscroll-behavior-inline: contain;
    padding-bottom: .2rem;
  }
  .nav a { justify-content: center; min-width: 5.5rem; padding-inline: .55rem; }
  .grid { grid-template-columns: 1fr; }
  .hero { padding: 1.25rem; }
}

@media (max-width: 420px) {
  .topbar { align-items: flex-start; flex-direction: column; }
  .topbar > .row { width: 100%; justify-content: flex-start; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    scroll-behavior: auto !important;
    transition-duration: .001ms !important;
    animation-duration: .001ms !important;
    animation-iteration-count: 1 !important;
  }
}

@media (prefers-contrast: more) {
  :root {
    --color-muted: #d5dbe2;
    --color-border: #7e8996;
  }
  .hero, .card, .brand-mark, input[type="text"], textarea, button, .button, .nav a[aria-current="page"] {
    border-width: 2px;
  }
}

@media (forced-colors: active) {
  :root {
    --color-bg: Canvas;
    --color-surface: Canvas;
    --color-surface-raised: Canvas;
    --color-field: Field;
    --color-text: CanvasText;
    --color-muted: CanvasText;
    --color-border: CanvasText;
    --color-accent: Highlight;
    --color-accent-ink: HighlightText;
    --color-danger: CanvasText;
    --color-status-good: CanvasText;
    --color-status-caution: CanvasText;
  }
  body, .hero, .card { background-image: none; box-shadow: none; }
  button.primary, .button.primary, button[aria-pressed="true"] {
    background: Highlight;
    color: HighlightText;
    border-color: Highlight;
    forced-color-adjust: none;
  }
  button:focus-visible, a:focus-visible, input:focus-visible, textarea:focus-visible { outline-color: Highlight; }
  .local-badge::before, .status::before { background: CanvasText; }
}
""".strip()
