"""The dashboard is one hand-edited HTML file with no build step.

Nothing type-checks it, so the failures it has actually had were structural:
a partial edit leaving two elements with one id, a settings key that quietly
lost its field, and three separate settings packed into a single flex row --
which is what made the settings page look, in the owner's words, clustered.
These are the cheap checks that would have caught each one.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "static" / "dashboard.html").read_text()


def spec_keys() -> list[str]:
    src = (ROOT / "app" / "settings.py").read_text()
    body = src[src.index("SPEC:"):src.index("@dataclass")]
    return re.findall(r'"(\w+)": \((?:str|bool|int),', body)


def settings_form() -> str:
    start = HTML.index('<form class="settings"')
    return HTML[start:HTML.index("</form>", start)]


def field_rows() -> list[str]:
    """Each `.field` row, which is one setting and its control."""
    form = settings_form()
    parts = re.split(r'<div class="field"', form)[1:]
    return [p.split('<div class="field"')[0] for p in parts]


def test_every_setting_has_exactly_one_control():
    """A key with no field cannot be changed; two fields disagree silently."""
    for key in spec_keys():
        assert HTML.count(f'id="s_{key}"') == 1, f"s_{key} is missing or duplicated"


def test_no_control_belongs_to_a_setting_that_does_not_exist():
    """An orphan input is saved into a key the server throws away."""
    known = set(spec_keys())
    for key in re.findall(r'id="s_(\w+)"', settings_form()):
        assert key in known, f"s_{key} has no entry in settings.SPEC"


def test_one_setting_per_row():
    """The clustering bug: three label/input pairs shared one flex row, and
    the page rendered them jumbled together."""
    for i, row in enumerate(field_rows()):
        inputs = row.count("<input")
        assert inputs == 1, f"settings row {i} holds {inputs} controls, expected 1"


def test_every_control_is_labelled():
    for i, row in enumerate(field_rows()):
        assert "<label for=" in row, f"settings row {i} has no label"


def test_labels_point_at_a_control_that_exists():
    for target in re.findall(r'<label for="(s_\w+)"', settings_form()):
        assert f'id="{target}"' in HTML, f"label points at missing {target}"


def test_element_ids_are_unique():
    """Two elements sharing an id means getElementById silently picks one."""
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate element ids: {dupes}"


def settings_index() -> str:
    # Search for the closing tag from the opening one: the tab bar is also a
    # <nav> and comes first in the document.
    start = HTML.index('<nav class="setnav"')
    return HTML[start:HTML.index("</nav>", start)]


def test_the_settings_index_points_at_real_sections():
    """A dead index entry is a link that does nothing."""
    nav = settings_index()
    hrefs = re.findall(r'href="#([\w-]+)"', nav)
    assert hrefs, "the settings index has no entries"
    for href in hrefs:
        assert f'id="{href}"' in HTML, f"index points at missing section #{href}"


def test_every_settings_section_is_in_the_index():
    """A section missing from the index is one you can only find by scrolling."""
    listed = set(re.findall(r'href="#([\w-]+)"', settings_index()))
    sections = re.findall(r'<section class="sgroup" id="([\w-]+)"', HTML)
    assert sections, "no settings sections found"
    assert set(sections) == listed, \
        f"index and sections disagree: {set(sections) ^ listed}"


def css_rules() -> list[tuple[str, str]]:
    """(selector, body) for every rule in the inline stylesheet."""
    style = HTML[HTML.index("<style>"):HTML.index("</style>")]
    style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)          # strip comments
    return re.findall(r"([^{}]+)\{([^{}]*)\}", style)


def test_the_grid_survives_the_tab_switcher():
    """`[data-tab].tab-on{display:block}` would flatten the two-column
    layout, so the rule that sets the grid has to out-specify it."""
    assert any(".setwrap" in sel and ".tab-on" in sel and "grid" in body
               for sel, body in css_rules()), \
        "the settings grid will be flattened to display:block by the tab switcher"


def test_the_settings_form_hides_with_its_tab():
    """The bug this guards: `.setwrap{display:grid}` without `.tab-on` ties
    `[data-tab]{display:none}` on specificity and beats it on source order,
    so the entire settings form rendered on every tab -- 3,289px of it,
    sitting between the Overview cards and the activity log."""
    for sel, body in css_rules():
        if ".setwrap" not in sel or ".tab-on" in sel:
            continue
        assert "display" not in body, (
            f"`{sel.strip()}` sets display outside a .tab-on rule, which "
            f"overrides [data-tab]{{display:none}} and shows settings on every tab")


@pytest.mark.parametrize("element", [
    "cstState", "cstWhy", "cstPaired", "cstSeen", "cstBatt", "cstFree",
    "cstLast", "cstDot", "btnFreeNowS", "btnPair", "btnRepair",
])
def test_the_companion_panel_is_wired(element):
    """Pairing used to be a code you typed with no way to check it worked."""
    assert f'id="{element}"' in HTML, f"companion panel is missing #{element}"
    assert element in HTML.split("</body>")[0], f"#{element} is never read by the script"


# ---- the figures on the front page have to agree with each other --------

def test_the_pipeline_bar_and_its_legend_use_the_same_number():
    """They were two lines apart and disagreed: the number subtracted the
    stranded rows, the bar did not, so the Waiting block was drawn 934 wide
    over a legend reading 294."""
    body = HTML[HTML.index("const stranded"):HTML.index("gaugeFill.style.width")]
    legend = re.search(r"nQueued\.textContent\s*=\s*([^;]+);", body).group(1)
    bar = re.search(r"segQueued\.style\.flexGrow\s*=\s*([^;]+);", body).group(1)
    assert "stranded" in legend, "the legend must exclude stranded rows"
    assert "stranded" in bar, \
        f"the bar does not subtract stranded rows while the legend does: {bar.strip()}"


def test_the_queue_badge_is_written_in_one_place():
    """Two writers made the tab flicker between 'Queue' and 'Queue (N)'."""
    writers = re.findall(r"badgeQueue\.textContent\s*=", HTML)
    assert len(writers) == 1, f"{len(writers)} writers of the queue badge"


def test_the_queue_badge_counts_the_panel_the_tab_opens_with():
    """It was changed to the outbox count on the belief that the outbox was
    all the tab listed. The outbox is the *second* panel; the tab opens on
    the waiting backlog, so a badge of 34 sat over a list of 294."""
    line = re.search(r"badgeQueue\.textContent\s*=\s*([^;]+);", HTML).group(1)
    assert "waiting" in line, \
        f"the badge does not count the backlog the tab leads with: {line.strip()}"


def test_the_tab_and_its_first_panel_do_not_share_a_name():
    """Tab 'Queue' over panel 'Queue' over panel 'In the outbox' is why
    "why is it called queue?" kept coming back."""
    first = re.search(r'<section class="queue"[^>]*>\s*<h2>([^<]+)</h2>', HTML)
    assert first, "could not find the first queue panel"
    assert first.group(1).strip().lower() != "queue", \
        "the first panel repeats the tab's name"


def test_a_freed_amount_is_reported_not_just_done():
    """The app reports bytes rather than an item count, so keying off items
    alone made every successful run read 'done' and never say how much."""
    for element in ("cpLast", "cstLast"):
        block = HTML[HTML.index(element + ".textContent"):]
        block = block[:block.index(";")]
        assert "freed_bytes" in block, \
            f"{element} ignores freed_bytes and will always say 'done'"


# ---- Library is a control surface now, not a report ---------------------

def library_sections() -> list[str]:
    """The Library panels, in the order they appear."""
    return re.findall(r'<section class="[^"]*" (?:id="[^"]*" )?data-tab="library">\s*'
                      r'<h2[^>]*>([^<]+)</h2>', HTML)


def test_the_months_panel_comes_first_in_library():
    """It is where sending happens. It used to sit third, behind two panels
    of reference material -- about three screens down."""
    sections = library_sections()
    assert sections, "no Library sections found"
    assert "month" in sections[0].lower(), \
        f"Library opens on {sections[0]!r} rather than the months"


def test_library_answers_what_is_outstanding_without_expanding_anything():
    """Every year starts collapsed, so the total used to be two clicks and a
    scroll away."""
    assert 'id="tlSummary"' in HTML
    body = HTML[HTML.index("tlSummary.innerHTML"):]
    body = body[:body.index("const years")]
    for label in ("to send", "in the outbox", "only if you ask", "not being sent"):
        assert label in body, f"the summary does not report {label!r}"


def test_a_month_carries_its_own_progress_bar():
    """Years had one and months did not, so finding the month that still
    needed work meant reading every figure in the year."""
    assert 'bar.className = "mb"' in HTML
    for state in ("done", "moving", "go"):
        assert f'seg(m.' in HTML and f'"{state}"' in HTML


def test_the_default_open_year_yields_to_a_deliberate_one():
    """Opening a useful year on first draw is a convenience; overriding what
    the user has chosen on every poll would be a bug."""
    assert "tlTouched" in HTML
    guard = re.search(r"if \(!tlOpenYears\.size && !tlTouched\)", HTML)
    assert guard, "the default is not guarded by the user's own choice"
    toggle = HTML[HTML.index('yd.addEventListener("toggle"'):]
    assert "tlTouched = true" in toggle[:200], \
        "opening a year by hand does not disable the default"
