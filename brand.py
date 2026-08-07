"""What the thing is called, and in whose company.

Two names, deliberately. **8008TUB3** is the project: the repository, the setup script, the
commit log, the thing a developer types. **BoobTube** is the product: what appears on the
television, in the menu, on the idents, at the top of the settings page.

The audience for each is different and so is the register. Leetspeak is a joke told to
someone who reads a repo; a family sitting down to watch television is not that person, and
a channel that announces itself in numbers reads as a computer pretending to be a TV — which
is precisely the impression this project exists to avoid.

The joke survives in the name itself and in the mark, which is genuinely the schematic symbol
for a coaxial connector. That is the right register: obvious once you see it, never insisted
upon, and perfectly defensible as an engineering drawing.
"""

from __future__ import annotations

# On screen. Anywhere a viewer might read it.
DISPLAY_NAME = "BoobTube"

# In the repo, the installer, the app bundle identifier, the docs.
PROJECT_NAME = "8008TUB3"

# A short form for tight spaces — the corner bug, a narrow menu header.
SHORT_NAME = "BoobTube"

# House colours, RGB. The ASS constants used for on-screen overlays are BGR, where the same
# three bytes mean a different colour entirely; keeping them in separate places with this
# note is cheaper than debugging cyan that should have been gold.
GOLD = (255, 200, 60)          # the centre conductor of the mark
PURPLE = (168, 92, 246)        # the shell
LILAC = (198, 170, 255)
PHOSPHOR_ASS = "&H55FF33&"     # BGR -> RGB(51, 255, 85), the on-screen menu green
