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


def test_a_hidden_tab_stays_hidden_whatever_else_styles_it():
    """The general form of the bug below.

    `[data-tab]{display:none}` is one attribute -- specificity (0,1,0) --
    so any single class that sets `display` ties it, and whichever is
    written last wins. `.grid{display:grid}` beat it that way and put the
    three Overview cards on every tab, including Library. `:not(.tab-on)`
    makes the rule (0,2,0) and the question of source order goes away.
    """
    for sel, body in css_rules():
        if "[data-tab]" not in sel or ".tab-on" in sel.split("[data-tab]")[1][:9]:
            continue
        if "display:none" not in body.replace(" ", ""):
            continue
        assert ":not(.tab-on)" in sel, (
            f"`{sel.strip()}` hides tabs on attribute specificity alone, so any "
            f"later class setting `display` will show that panel on every tab")
        break
    else:
        raise AssertionError("no rule hides the panels of an inactive tab")


@pytest.mark.parametrize("element", [
    "cstState", "cstWhy", "cstPaired", "cstSeen", "cstBatt", "cstFree",
    "cstLast", "cstDot", "btnFreeNowS", "btnPair", "btnRepair",
])
def test_the_companion_panel_is_wired(element):
    """Pairing used to be a code you typed with no way to check it worked."""
    assert f'id="{element}"' in HTML, f"companion panel is missing #{element}"
    assert element in HTML.split("</body>")[0], f"#{element} is never read by the script"


# ---- a redraw must not throw away what you were doing --------------------

def test_a_figure_change_does_not_rebuild_the_timeline():
    """The bug: pressing "Send the whole month" made Library unusable.

    The feeder writes to the ledger once per file and every write pushes an
    event, so the timeline redrew several times a second while a month was
    going out. Each redraw replaced every <details>, so the month you were
    reading slammed shut -- and restoring `open` fired `toggle`, which
    refetched the body. The figures have to be writable without rebuilding
    the tree around them.
    """
    src = HTML[HTML.index("async function drawTimeline()"):]
    src = src[:src.index("function monthFigureText")]
    assert "paintFigures(" in src, \
        "drawTimeline has no path that updates figures without rebuilding"
    fast = src.index("paintFigures(")
    wipe = src.index('tlBody.innerHTML = ""')
    assert fast < wipe, \
        "the rebuild is reached before the figures-only path can bail out"
    assert "return;" in src[fast:wipe], \
        "the figures-only path falls through into the rebuild anyway"


def test_the_month_body_cache_is_not_emptied_on_every_redraw():
    """It existed so reopening a month would not flash "Loading…", and it
    was cleared on every change -- which is exactly when it was needed."""
    assert "monthCache.clear()" not in HTML


def test_the_queue_list_is_not_rebuilt_when_it_has_not_changed():
    """Milder than the timeline and the same cause: the list was emptied
    and rebuilt on every event, losing the scroll of whoever was reading."""
    src = HTML[HTML.index("async function renderQueue()"):]
    src = src[:src.index('qList.innerHTML = ""')]
    assert "qListSignature" in src, \
        "renderQueue rebuilds the list without checking whether it changed"


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
    guard = re.search(r"if \(!tlOpenYears\.size && !tlTouched([^)]*)\)", HTML)
    assert guard, "the default is not guarded by the user's own choice"
    # And by a choice made on an earlier visit: a year restored from the
    # last session is a deliberate one too, and the default used to open
    # the newest year on top of it.
    assert "tlRestored" in guard.group(1), \
        "the default ignores what the last visit left open"
    toggle = HTML[HTML.index('yd.addEventListener("toggle"'):]
    assert "tlTouched = true" in toggle[:200], \
        "opening a year by hand does not disable the default"


# ---- sending says the same thing at every level --------------------------

def test_one_builder_draws_every_send_control():
    """Year, month and category each rolled their own: a year offered
    nothing, a month's control was inside its own expanded body, and a
    category could send what was left but never send again. Three levels,
    three vocabularies, drifting apart one edit at a time."""
    assert "function sendControls(" in HTML
    # Used at all four places that can send.
    assert HTML.count("sendControls(") >= 5, \
        "the builder exists but the levels are still drawing their own"


def test_the_library_asks_in_the_button_not_in_a_browser_dialog():
    """confirm() is a different window, with buttons you did not style,
    asking about a page you can no longer see."""
    start = HTML.index("function paintMonthBody(")
    body = HTML[start:HTML.index("function ", start + 40)]
    assert "confirm(" not in body, "Library still opens a browser dialog"
    assert "armed(" in body, "and nothing replaced it"


def test_an_armed_button_disarms_itself():
    """A stray first press must not leave a loaded button on the page."""
    src = HTML[HTML.index("function armed("):]
    src = src[:src.index("\nasync function sendPeriod")]
    assert "setTimeout(disarm" in src


def test_the_send_control_does_not_toggle_the_row_it_sits_in():
    """It lives inside a <summary>, where any click opens or closes the
    thing it is attached to."""
    src = HTML[HTML.index("function sendControls("):]
    src = src[:src.index("\nfunction monthFigureText")]
    assert "stopPropagation" in src


def test_a_period_send_only_drops_its_own_months_from_the_cache():
    """The cache stops a reopened month flashing "Loading…". A year's send
    clearing all of it would be that bug, arriving by a new route."""
    src = HTML[HTML.index("async function sendPeriod("):]
    src = src[:src.index("\n// `period` is")]
    assert "monthCache.clear()" not in src
    assert "monthCache.delete(key)" in src


def test_both_summaries_count_their_caret_as_a_column():
    """summary::before is itself the first grid item, so adding a control
    without widening the track list pushes the last child onto a second row
    and into column one. Documented, and walked into twice."""
    year = HTML[HTML.index(".tl-yr > summary{"):][:220]
    month = HTML[HTML.index(".tl-mo > summary{"):][:260]
    # caret + name + figures + controls
    assert "grid-template-columns:auto 1fr auto auto" in year
    # caret + name + counts + figures + tags + controls
    assert "minmax(66px,auto) 1fr auto auto" in month


def test_every_tab_in_the_bar_is_one_setTab_will_switch_to():
    """setTab falls back to "overview" for a name it does not know, rather
    than failing -- so a tab with a panel and a link in the bar can show
    nothing at all and give no clue why. It did."""
    bar = set(re.findall(r'data-for="(\w+)"', HTML))
    panels = set(re.findall(r'data-tab="(\w+)"', HTML))
    known = set(re.findall(r'const TABS = \[([^\]]+)\]', HTML)[0]
                .replace('"', "").split(","))
    assert bar <= known, f"in the bar but not in TABS: {bar - known}"
    assert panels <= known, f"has a panel but not in TABS: {panels - known}"


# ---- the Dates tab --------------------------------------------------------

def test_the_dates_tab_has_a_panel_a_link_and_a_renderer():
    assert 'data-tab="dates"' in HTML
    assert 'data-for="dates"' in HTML
    assert "async function renderDates(" in HTML
    assert '"dates"' in HTML[HTML.index("const TABS = ["):][:200]


def test_grouping_and_sorting_do_not_go_back_to_the_server():
    """A few thousand rows is a few hundred KB. Paging it server-side would
    make every regroup a round trip to answer a question the page already
    holds the data for."""
    src = HTML[HTML.index("function groupDates("):HTML.index("async function signOff(")]
    assert "fetch(" not in src


def test_every_grouping_offered_has_a_key():
    """A select option with no branch groups everything under one heading
    and looks like the data is wrong rather than the code."""
    opts = set(re.findall(r'<option value="(\w+)">', 
                          HTML[HTML.index('id="dGroup"'):HTML.index('id="dSort"')]))
    src = HTML[HTML.index("function groupDates("):]
    src = src[:src.index("function sortDates(")]
    for key in opts:
        assert f"{key}:" in src, f"group '{key}' is offered but not implemented"


def test_every_sort_offered_has_a_comparator():
    start = HTML.index('id="dSort"')
    opts = set(re.findall(r'<option value="(\w+)">',
                          HTML[start:HTML.index("</select>", start)]))
    src = HTML[HTML.index("function sortDates("):]
    src = src[:src.index("function groupHeading(")]
    for key in opts:
        assert f"{key}:" in src, f"sort '{key}' is offered but not implemented"


def test_a_file_with_nothing_to_write_is_not_offered_a_sign_off():
    """The unfixable group is the ceiling, not work waiting to be done.
    A button there would never do anything and would never stop appearing."""
    src = HTML[HTML.index("function dateRow("):HTML.index("async function renderDates(")]
    assert "(r.writes || []).length" in src
    assert "nothing can be written" in src


def test_reading_again_is_offered_only_where_nothing_else_can_judge():
    """It was offered for every row lacking Immich's answer -- which, on a
    library read before 2.16.0 kept one, was every row there was: a button
    on every group offering to download files whose verdict needed nothing
    downloaded. The server says which rows it could not judge."""
    src = HTML[HTML.index("async function renderDates("):HTML.index("async function renderOwed(")]
    assert "r => r.stale" in src
    assert "!r.says" not in src


def test_the_ceiling_is_read_off_the_writes_not_a_stored_label():
    """The label is decided when the file is read, and the settings can move
    after that. Nothing to write is what makes a file the ceiling."""
    src = HTML[HTML.index("function groupDates("):HTML.index("function sortDates(")]
    fault = src[src.index("fault:"):src.index("zone:")]
    assert "r.writes" in fault and '"unfixable"' in fault


def test_every_fault_the_server_can_name_has_a_heading():
    """A key missing from FAULT still groups -- under its raw name, with no
    explanation, which is how a new state arrives looking like a bug."""
    from app import diagnose
    block = HTML[HTML.index("const FAULT = {"):HTML.index("const ZONE = {")]
    for key in (diagnose.BLANK_FAULT, diagnose.ABSENT_FAULT,
                diagnose.UNFIXABLE, diagnose.UNRECORDED):
        assert f"{key}:" in block, key


def test_a_proposal_worked_out_again_says_what_it_was():
    """Otherwise the only record that a stored time was wrong is a number
    quietly changing on screen."""
    src = HTML[HTML.index("function dateRow("):HTML.index("async function renderDates(")]
    assert "r.revised_from" in src


def test_the_whole_held_list_can_be_read_again():
    """The group control offers this only where nothing can be judged again.
    A verdict still comes from bytes read once, so a file replaced in Immich
    -- or a doubt about what was read -- has no other answer."""
    src = HTML[HTML.index("async function renderDates("):HTML.index("async function renderOwed(")]
    assert "dReadAll" in src and "Read all " in src
    assert 'id="dReadAll"' in HTML, "and somewhere to put it"


def test_a_correction_can_be_taken_back_and_done_again():
    """The date in a delivered file is whatever the rules said that day, and
    the copy in Google Photos keeps it. Sending a fresh one is the only way
    to change it, and the control says so before it arms."""
    src = HTML[HTML.index("async function takeBack("):HTML.index("// ---- sending, one control")]
    assert "/api/dates/take-back" in src
    assert "all_corrected" in src, "the whole set, for a rule that has moved"
    assert src.count("armed(") >= 2, "per file and in bulk, both armed"
    assert "Google Photos" in src, "it must say the old copy stays there"


def test_bulk_sign_off_is_armed_like_every_other_irreversible_control():
    src = HTML[HTML.index("async function renderDates("):HTML.index("async function renderOwed(")]
    assert "armed(b," in src
    assert "stopPropagation" in src, "it sits in a <summary> and would toggle it"


def test_already_sent_files_are_called_out_rather_than_grouped_away():
    """Signing one of these off sends a corrected copy alongside the old
    one, which is a different decision and needs a different action first."""
    src = HTML[HTML.index("async function renderDates("):]
    src = src[:src.index("async function renderOwed(")]
    assert "already_sent" in src
    assert "arrives as a second photo" in src


def test_pushing_to_immich_is_shown_as_coming_soon_with_the_reason():
    src = HTML[HTML.index("async function renderOwed("):]
    src = src[:src.index("\n// ---- sending")]
    assert "coming soon" in src
    assert "d.why" in src, "the reason comes from the server, not a copy of it"


def test_an_empty_dates_list_says_which_of_its_two_causes_it_is():
    """"Nothing held" means either every file read was fine or nothing was
    read, and those are opposite. Offering both and letting the reader pick
    sent a whole year through unexamined and read as a clean result."""
    src = HTML[HTML.index("async function renderDates("):]
    src = src[:src.index("async function renderOwed(")]
    assert "d.checking" in src and "d.checked" in src
    assert "is off in Settings" in src
    assert "or the check is off" not in src, "the old guess is still there"


def test_the_tally_says_so_even_when_something_is_held():
    """With checking off a short list is not a good sign; it is an absence
    of evidence."""
    src = HTML[HTML.index("async function renderDates("):]
    src = src[:src.index("async function renderOwed(")]
    assert "not checking" in src


def test_no_hand_written_list_decides_what_a_checkbox_is():
    """There was one, and adding a checkbox without adding its name to it
    made the control completely inert: fillSettings wrote the stored value
    into `.value` instead of `.checked`, so it always drew unticked, and
    readSettings sent that same `.value` back instead of what had been
    clicked. `check_dates` could be ticked, saved, and reported as saved,
    and nothing had read it at any point.

    The DOM already knows what a checkbox is.
    """
    assert "const BOOLS" not in HTML
    fill = HTML[HTML.index("function fillSettings("):HTML.index("function readSettings(")]
    read = HTML[HTML.index("function readSettings("):]
    read = read[:read.index("function renderConnection(")]
    assert 'el.type === "checkbox"' in fill
    assert 'el.type === "checkbox"' in read


def test_every_boolean_setting_has_a_checkbox_to_match():
    """A bool rendered as a text input round-trips its own string and never
    reads what was clicked -- the same failure by a different route."""
    spec = (ROOT / "app" / "settings.py").read_text()
    bools = set(re.findall(r'"(\w+)": \(bool,', spec))
    for key in bools:
        m = re.search(r'<input([^>]*?)id="s_' + key + r'"([^>]*)>', HTML)
        assert m, f"s_{key} has no control"
        assert 'type="checkbox"' in m.group(0), \
            f"s_{key} is a bool but not a checkbox"


def test_the_page_names_every_phase_the_feeder_sets():
    """A phase with no label falls through to the byte figures, which is
    the stalled-looking bar this was added to replace."""
    feeder_src = (ROOT / "app" / "feeder.py").read_text()
    block = HTML[HTML.index("const PHASE = {"):]
    block = block[:block.index("};")]
    set_by = set(re.findall(r'phase\("(\w+)"\)', feeder_src))
    set_by.add("fetching")          # the starting value, set on the entry
    named = set(re.findall(r'^\s+(\w+):\s+"', block, re.M))
    assert set_by <= named, f"the feeder sets phases the page cannot name: {set_by - named}"


def test_every_send_control_counts_what_has_not_been_asked_for():
    """`remaining` counts files already forced, so a button reading it
    cannot move when pressed: it said "Send 1,620" before and after, and
    pressing again did the same nothing. Forcing turns resting into
    sending, so a count of resting goes to zero and the button says so by
    disappearing."""
    for call in re.findall(r"sendControls\([^;]*?\);", HTML, re.S):
        if "remaining:" not in call:
            continue
        # The key is `remaining:`; what matters is which field it reads.
        assert ".remaining" not in call, \
            f"reads .remaining, which includes files already asked for:\n{call}"
        assert ".resting" in call, f"reads neither:\n{call}"


def test_a_send_control_repaints_with_the_figures():
    """Left to the structure rebuild it keeps whatever number it was born
    with, which after a send is the one number certainly wrong."""
    src = HTML[HTML.index("function sendControls("):]
    src = src[:src.index("\nfunction monthFigureText")]
    assert "wrap.repaint" in src
    assert "if (busy) return;" in src, \
        "reading the label back also matched a button that had finished"
    paint = HTML[HTML.index("function paintFigures("):]
    paint = paint[:paint.index("function monthNode(")]
    assert paint.count("repaint(") == 2, "months and years both"


def test_the_dates_badge_is_written_on_every_tick():
    """It was written only by renderDates(), which runs when the tab is
    opened -- so until somebody thought to look there was nothing anywhere
    saying files were being kept back, which is the one thing a badge is
    for."""
    render = HTML[HTML.index("async function renderDates("):]
    render = render[:render.index("async function renderOwed(")]
    assert "badgeDates" not in render, \
        "two writers disagreeing on every tick is how Queue came to flicker"
    assert "badgeDates.hidden = !c.held" in HTML, \
        "and the tick must write it from the counts the poll already carries"


# ---- what the reader was doing, across a reload --------------------------
#
# A redraw already keeps open sections and scroll position. A refresh threw
# all of it away: the Dates tab came back grouped by whatever the markup
# listed first, and Library re-opened the newest year on top of whichever
# one was being read. The same fault as the redraw bug, by a slower route.

def test_storage_is_wrapped_because_it_is_not_always_there():
    """A private window, or a browser told to block site data, throws on
    the first read rather than returning nothing."""
    for fn in ("function remember(", "function recall("):
        src = HTML[HTML.index(fn):]
        src = src[:src.index("\n}")]
        assert "catch" in src, f"{fn} can throw and take the page with it"


@pytest.mark.parametrize("key", ["openYears", "openMonths", "openFiles",
                                 "dGroup", "dSort", "dOpen"])
def test_each_choice_is_both_kept_and_restored(key):
    """Written and never read is the same as not written at all."""
    assert f'remember("{key}"' in HTML, f"{key} is never written"
    # dGroup and dSort are restored by a loop over their ids, so the literal
    # never appears inside recall(). What matters is that something reads
    # them back, not how it spells it.
    restored = (f'recall("{key}"' in HTML
                or re.search(r'\["dGroup", "dSort"\]\.forEach[\s\S]{0,400}'
                             r'recall\(id', HTML) and key in ("dGroup", "dSort"))
    assert restored, f"{key} is written and never read"


def test_the_dates_groups_do_not_reopen_on_every_render():
    """That list redraws after every sign-off, so a group collapsed by hand
    came back the moment a file was signed off out of it."""
    src = HTML[HTML.index("async function renderDates("):]
    src = src[:src.index("async function renderOwed(")]
    assert "if (i === 0) det.open = true;" not in src
    assert "dOpen === null ? i === 0" in src, \
        "the first-group default must be for somebody who has never chosen"


def test_a_grouping_this_build_no_longer_offers_is_not_restored():
    """A renamed key would select nothing and silently group everything as
    one, which looks like the data is wrong rather than the memory."""
    src = HTML[HTML.index('["dGroup", "dSort"].forEach'):]
    src = src[:src.index("window.addEventListener")]
    assert "o.value === kept" in src
