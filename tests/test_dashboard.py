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


def test_the_grid_survives_the_tab_switcher():
    """`[data-tab].tab-on{display:block}` overrides a plain `.setwrap{display:grid}`
    and flattens the two-column layout. The selector has to out-specify it."""
    assert ".setwrap.tab-on{" in HTML.replace(" ", "").replace("\n", ""), \
        "the settings grid will be flattened to display:block by the tab switcher"


@pytest.mark.parametrize("element", [
    "cstState", "cstWhy", "cstPaired", "cstSeen", "cstBatt", "cstFree",
    "cstLast", "cstDot", "btnFreeNowS", "btnPair", "btnRepair",
])
def test_the_companion_panel_is_wired(element):
    """Pairing used to be a code you typed with no way to check it worked."""
    assert f'id="{element}"' in HTML, f"companion panel is missing #{element}"
    assert element in HTML.split("</body>")[0], f"#{element} is never read by the script"
