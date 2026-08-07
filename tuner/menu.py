"""The on-screen menu: a 90s BIOS, on purpose.

Retro styling here isn't only a joke. A BIOS setup screen is chunky, high-contrast,
monospaced, and navigable with four keys — which is exactly what a TV three metres away and
a four-button clicker need. Modern TV interfaces get this wrong in both directions: too much
contrast subtlety to read from the sofa, too many gestures to drive from a clicker.

Rules this enforces structurally:

- **Four verbs only.** Up, down, select, back. Every screen must be completable with them.
- **No text entry, ever.** Anything needing a string — Wi-Fi password, SMB credentials, share
  paths — is not a menu item. It lives in the browser setup UI and appears here only as
  status. Typing a password on a TV with four buttons is not a UX, it is a hostage situation.
- **Every item shows its current value**, because you cannot hover for a tooltip.

Two renderers: plain text for a console or a preview, and ASS for mpv's OSD overlay so the
menu draws over live video with no compositor and no second process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .input import Event, Verb

WIDTH = 62


class ItemKind(Enum):
    SUBMENU = "submenu"
    CHOICE = "choice"      # cycles through a fixed list — clicker-safe
    ACTION = "action"
    INFO = "info"          # read-only, not selectable


@dataclass
class Item:
    label: str
    kind: ItemKind = ItemKind.ACTION
    value: str | None = None
    choices: list[str] = field(default_factory=list)
    children: list["Item"] = field(default_factory=list)
    action: Callable[[], str | None] | None = None
    help: str = ""

    @property
    def selectable(self) -> bool:
        return self.kind is not ItemKind.INFO

    def cycle(self) -> None:
        if self.kind is ItemKind.CHOICE and self.choices:
            index = self.choices.index(self.value) if self.value in self.choices else -1
            self.value = self.choices[(index + 1) % len(self.choices)]


@dataclass
class Screen:
    title: str
    items: list[Item]
    cursor: int = 0

    def move(self, delta: int) -> None:
        if not self.items:
            return
        index = self.cursor
        for _ in range(len(self.items)):
            index = (index + delta) % len(self.items)
            if self.items[index].selectable:
                self.cursor = index
                return

    @property
    def current(self) -> Item | None:
        if 0 <= self.cursor < len(self.items):
            return self.items[self.cursor]
        return None


class Menu:
    """A stack of screens driven by four verbs."""

    def __init__(self, root: Screen, version: str = "0.1"):
        self.stack: list[Screen] = [root]
        self.version = version
        self.status: str = ""
        self.visible = False

    @property
    def screen(self) -> Screen:
        return self.stack[-1]

    def handle(self, event: Event) -> None:
        if not self.visible:
            if event.verb is Verb.SELECT:
                self.visible = True
            return

        if event.verb is Verb.UP:
            self.screen.move(-1)
            self.status = ""
        elif event.verb is Verb.DOWN:
            self.screen.move(1)
            self.status = ""
        elif event.verb is Verb.BACK:
            if len(self.stack) > 1:
                self.stack.pop()
            else:
                self.visible = False
            self.status = ""
        elif event.verb is Verb.SELECT:
            self._activate()

    def _activate(self) -> None:
        item = self.screen.current
        if item is None:
            return
        if item.kind is ItemKind.SUBMENU:
            self.stack.append(Screen(item.label, item.children))
            self.status = ""
        elif item.kind is ItemKind.CHOICE:
            item.cycle()
            self.status = f"{item.label} set to {item.value}"
        elif item.kind is ItemKind.ACTION and item.action:
            self.status = item.action() or ""

    # ---------- rendering ----------

    def _rows(self) -> list[str]:
        screen = self.screen
        inner = WIDTH - 2
        rows: list[str] = []

        rows.append("╔" + "═" * inner + "╗")
        heading = f"  8008TUB3  {screen.title.upper()}"
        version = f"v{self.version}  "
        pad = inner - len(heading) - len(version)
        rows.append("║" + heading + " " * max(1, pad) + version + "║")
        rows.append("╠" + "═" * inner + "╣")
        rows.append("║" + " " * inner + "║")

        for index, item in enumerate(screen.items):
            marker = "►" if index == screen.cursor and item.selectable else " "
            label = item.label
            value = item.value or ""
            if item.kind is ItemKind.SUBMENU:
                value = "›"

            # Dot leaders, because a value floating in whitespace is hard to associate with
            # its label from across a room. Build the content then pad to an exact width —
            # computing the fill by arithmetic drifts by a column whenever the value is
            # empty, and in a menu like this the alignment is the whole look.
            head = f" {marker} {label} "
            tail = f" {value}  " if value else ""
            fill = max(1, inner - len(head) - len(tail))
            leader = ("·" * fill) if value else (" " * fill)
            content = (head + leader + tail).ljust(inner)[:inner]
            rows.append("║" + content + "║")

        rows.append("║" + " " * inner + "║")
        rows.append("╠" + "═" * inner + "╣")

        status = self.status or (screen.current.help if screen.current else "")
        if status:
            text = f"  {status[:inner - 4]}"
            rows.append("║" + text.ljust(inner) + "║")
            rows.append("╠" + "═" * inner + "╣")

        hint = "  ▲▼ MOVE     ● SELECT     ◄ BACK"
        rows.append("║" + hint.ljust(inner) + "║")
        rows.append("╚" + "═" * inner + "╝")
        return rows

    def render_text(self) -> str:
        return "\n".join(self._rows())

    def render_ass(self) -> str:
        """ASS markup for mpv's `osd-overlay`, drawn straight over live video.

        Monospace, hard drop shadow, amber on near-black — the colour scheme every BIOS and
        every cable box menu of the era converged on, and it survives being photographed off
        a TV better than white-on-grey.
        """
        header = (
            r"{\an7\pos(60,60)\fnmonospace\fs28\bord0\shad2"
            r"\1c&H00D7FF&\3c&H000000&\4c&H000000&\alpha&H00&}"
        )
        body = r"\N".join(row.replace("{", r"\{").replace("}", r"\}") for row in self._rows())
        return header + body


def build_root(state: dict) -> Screen:
    """The default menu tree.

    Deliberately shallow and small. Anything that needs typing is status here and lives in
    the browser UI; anything the user shouldn't touch mid-show isn't here at all.
    """
    picture = [
        Item("Aspect", ItemKind.CHOICE, state.get("aspect", "Pillarbox 4:3"),
             ["Pillarbox 4:3", "Stretch to fill", "Zoom to fill"],
             help="How 4:3 content fills a 16:9 screen"),
        Item("Overscan", ItemKind.CHOICE, state.get("overscan", "Off"), ["Off", "2%", "4%"],
             help="Trim the edges if your TV cuts the picture"),
        Item("Channel bug", ItemKind.CHOICE, state.get("bug", "4 seconds"),
             ["Off", "2 seconds", "4 seconds", "Always"],
             help="The corner overlay after a channel change"),
    ]
    audio = [
        Item("Volume", ItemKind.CHOICE, state.get("volume", "Normal"),
             ["Quiet", "Normal", "Loud"]),
        Item("Loudness match", ItemKind.INFO, "-18 LUFS",
             help="Shows and commercials are levelled to match"),
    ]
    channels = [
        Item("Start on", ItemKind.CHOICE, state.get("start_channel", "Last watched"),
             ["Last watched", "Channel 1", "Channel 3"]),
        Item("Kids mode", ItemKind.CHOICE, state.get("kids", "Off"), ["Off", "On"],
             help="Locks the guide and limits channels. Unlock in the browser."),
        Item("Rebuild schedule", ItemKind.ACTION,
             action=lambda: "Schedule rebuilding in the background",
             help="Use after adding new shows"),
    ]
    address = state.get("addr", "")
    network = [
        # The address is the single most useful thing this screen can show: it is how you get
        # from the sofa to the settings page. It goes first, and it is the real routed
        # address rather than a hostname that may or may not resolve.
        Item("Open in a browser", ItemKind.INFO, f"http://{address}" if address else "no network",
             help="Everything needing typing happens there, not here"),
        Item("Status", ItemKind.INFO, state.get("net", "Connected")),
        Item("Library", ItemKind.INFO, state.get("library", "nothing scheduled")),
        Item("Commercials", ItemKind.INFO, state.get("ads", "none yet")),
    ]
    about = [
        Item("Version", ItemKind.INFO, state.get("version", "0.1")),
        Item("Remote", ItemKind.INFO, state.get("remote", "CEC + clicker")),
        Item("Uptime", ItemKind.INFO, state.get("uptime", "3d 04:12")),
    ]

    return Screen("SETUP", [
        Item("PICTURE", ItemKind.SUBMENU, children=picture),
        Item("AUDIO", ItemKind.SUBMENU, children=audio),
        Item("CHANNELS", ItemKind.SUBMENU, children=channels),
        Item("NETWORK", ItemKind.SUBMENU, children=network),
        Item("ABOUT", ItemKind.SUBMENU, children=about),
    ])
