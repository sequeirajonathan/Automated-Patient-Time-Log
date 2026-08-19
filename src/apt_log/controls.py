"""Controls the portal recognises by sight, because the app names none of them.

Two jobs, and they have to agree, which is why they live together:

  * naming a control the app ships nameless (inMyTeam's agency filter is an
    EditText with no id, no description and no caption the portal may print);
  * deciding that THAT control's text is safe to show.

The second is the delicate one. Editable text is withheld everywhere else and
must stay withheld — it is what has been typed, and on a credential screen
that is a password or the code inMyTeam just sent. The exception here is not
"editable text is fine after all". It is narrower than that: a control this
table has positively identified, by app and shape and place, on a screen that
is not asking for a credential, is a CHOOSER — its content is picked from a
list the app drew, and on this screen that content is an agency's name.

Identification happens against the hierarchy rather than against the parsed
document, because that is where the raw text is and where the decision has to
be made. Both the feed (which decides what to publish) and the reflow (which
decides what to draw) ask the same object, so they cannot drift apart and
start disagreeing about which box is which.
"""

from __future__ import annotations

INMYTEAM = "com.inmyteam.inmyteam"

# Under the title bar. A code screen also carries a single unnamed EditText
# and it sits in the middle of the page; this is the cheap half of telling
# them apart, and it holds even if the wording below ever changes.
TOP_BAND = 0.12

# The sign-in walk, in the app's own words. Nothing on any of these screens is
# ever named or disclosed here — reaching the code screen is the destination
# of a macro that texts a real person, and §12 already paid for mistaking one
# inMyTeam screen for another.
CREDENTIAL_MARKERS = (
    "verify your account",
    "enter your code",
    "sign in with your phone",
    "verifique su cuenta",
    "introduzca su código",
)

AGENCY_FILTER = "papp.imt.agency_filter"
REFRESH = "papp.imt.refresh"
DRAWER = "papp.drawer"

# Android's own description for a navigation drawer's handle, which apps
# inherit without translating. It is chrome, not content — the portal owns its
# own chrome and says it in her language — so this one is renamed for EVERY
# app rather than for inMyTeam alone.
#
# The app's WORDS are a different matter and are left exactly as the app says
# them. "Today", "Tomorrow", the tab captions, a patient's name: translating a
# live care app's own content would mean inventing words the record does not
# contain, and the portal has no business doing that.
DRAWER_WORDS = ("open navigation drawer", "navigation drawer",
                "abrir el panel de navegación", "abrir cajón de navegación")


class Naming:
    """What this screen's nameless controls are, if anything."""

    __slots__ = ("_package", "_w", "_h", "_credential")

    def __init__(self, package: str, width: int, height: int,
                 credential: bool) -> None:
        self._package = package
        self._w = width
        self._h = height
        self._credential = credential

    def key(self, cls: str, rid: str, bounds: list[int],
            txt: str = "") -> str:
        """The portal's name for this control, or "" for every other control.

        Deliberately conservative: a miss costs the blank box that was there
        before, and a false positive puts a wrong name on a real control.
        """
        if self._credential:
            return ""
        if (txt or "").strip().lower() in DRAWER_WORDS:
            return DRAWER
        if self._package != INMYTEAM or rid:
            return ""
        if not self._w or not self._h or len(bounds) != 4:
            return ""
        x1, _y1, x2, y2 = bounds
        if y2 > self._h * TOP_BAND:
            return ""
        width = x2 - x1
        if cls == "EditText" and width > self._w * 0.5:
            return AGENCY_FILTER
        if cls == "View" and width < self._w * 0.10 and x1 > self._w * 0.85:
            return REFRESH
        return ""

    def discloses(self, cls: str, rid: str, bounds: list[int]) -> bool:
        """Whether this control's text may be shown though it is editable.

        Only the agency filter, and only because this table says what that box
        IS. Its content is chosen from a list, not typed — and the caregiver
        cannot tell which agency she is looking at from a box that shows the
        word "filter" whatever is in it. Reported exactly that way: the phone
        showed the agency and the portal showed the label.
        """
        return self.key(cls, rid, bounds) == AGENCY_FILTER


# Names that REPLACE the app's own caption rather than fill a blank.
#
# The two kinds are not the same and the difference is the whole point. The
# drawer's caption is Android's untranslated chrome, so the portal's word for
# it is better in every case. The filter's is CONTENT — the agency she picked
# — and the portal's word for it is better only while the box is empty. Show
# "Filtrar por agencia" over a chosen agency and the page is hiding the one
# thing she opened it to check.
REPLACING = frozenset((DRAWER,))


def replaces(key: str) -> bool:
    return key in REPLACING


def naming(xml: str, package: str, width: int, height: int) -> Naming:
    """Read the screen once, so every node can be asked cheaply."""
    lowered = (xml or "").lower()
    credential = any(m in lowered for m in CREDENTIAL_MARKERS)
    return Naming(package, width, height, credential)
