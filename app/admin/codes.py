"""F8-T1 — the layer-installer error registry: every way a candidate layer can
be rejected or flagged, in words a volunteer can act on.

This module is the single source of truth for those messages. Nothing else in
the installer writes operator-facing prose: a check decides *that* something is
wrong and supplies the concrete detail (which columns, which files, how many
shapes), and the text comes from here. That keeps the codes stable enough to
quote in a support request, searchable in the docs, and translatable later.

Who raises what
    The checks in `app.admin.validate` decide PIP-L003 through PIP-L011,
    PIP-L015 through PIP-L018 and PIP-L020 from an already-loaded frame of
    shapes. PIP-L001, PIP-L002, PIP-L012, PIP-L013, PIP-L014 and PIP-L019 are
    raised by the file reader (F8-T2), because only the reader touches bytes,
    zip archives, and web addresses. They are defined here anyway so that there
    is exactly one registry and one numbering scheme.

    PIP-L019 is the one entry whose existence is worth explaining. "I opened
    this file and it holds several maps — which did you mean?" was originally
    filed under PIP-L001, whose text says the file "never got as far as
    opening" and asks the operator to check that their download finished. It is
    the opposite situation: the read succeeded, the tool knows exactly what is
    inside, and the only missing thing is the operator's choice. A valid
    GeoPackage carrying two layers was being reported as a corrupt file.

House rules for the text
    Every string in this table is read by someone who has never heard of a
    coordinate reference system, a projection, or a sidecar file. Say what
    happened, why it matters for *this* tool, and exactly what to do next. Name
    the real thing — a file extension, a column name, a count. Never say
    "invalid" or "malformed" without saying concretely what that means. Never
    blame the operator. Where a technical word is genuinely unavoidable, define
    it in the same sentence (see PIP-L003's `fix`, the worked example).

    One verb for the act. The operator *installs* a layer — every entry says
    install, none says commit. "Commit" is what this codebase calls the final
    step internally, and to a volunteer it means "promise". The jargon table in
    tests/test_admin_validate.py holds the line.

ArcGIS / ArcPy equivalent
    ArcGIS surfaces this class of problem as `arcpy.ExecuteError` text from
    tools like Define Projection, Check Geometry / Repair Geometry, and the
    "Unknown Coordinate System" warning in the Catalog pane — messages written
    for a GIS analyst. This registry plays the same role for someone who is not
    one: the same underlying conditions, described without the vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SEVERITY_BLOCKING = "blocking"  # cannot be installed
SEVERITY_WARNING = "warning"  # can be installed once acknowledged

SEVERITIES = (SEVERITY_BLOCKING, SEVERITY_WARNING)

# Sort weight: blocking findings come before warnings.
_SEVERITY_RANK = {SEVERITY_BLOCKING: 0, SEVERITY_WARNING: 1}


class UnknownLayerCodeError(KeyError):
    """A code was requested that is not in the registry.

    This is a programming mistake — a typo in a check — not an operator error,
    so it raises loudly rather than degrading to a generic message. A finding
    with no registry entry would reach the browser with no text at all.
    """


@dataclass(frozen=True)
class LayerCode:
    """One way a candidate layer can be rejected or flagged.

    `code` is stable forever once published: operators quote it, the docs index
    it, and later tasks match on it. `title` is one short line for a heading;
    `what`, `why` and `fix` are the three things the reader needs, in that
    order.
    """

    code: str
    severity: str
    title: str
    what: str
    why: str
    fix: str

    @property
    def is_blocking(self) -> bool:
        return self.severity == SEVERITY_BLOCKING


def _entry(
    code: str, severity: str, title: str, what: str, why: str, fix: str
) -> LayerCode:
    return LayerCode(
        code=code, severity=severity, title=title, what=what, why=why, fix=fix
    )


LAYER_CODES: dict[str, LayerCode] = {
    entry.code: entry
    for entry in (
        _entry(
            "PIP-L001",
            SEVERITY_BLOCKING,
            "This file could not be read as map data at all",
            "The tool tried to read this file and could not make sense of it. "
            "Whatever is inside, it is not in any of the formats this tool can "
            "read a map out of — it never got as far as opening.",
            "That is a different problem from a map file that opens and turns "
            "out to have nothing drawn in it. A spreadsheet, a picture of a map, "
            "a text document, and a file cut short partway through a download "
            "all look like this, and none of them can be read as a map.",
            "Check that you sent the file you meant to, and that it finished "
            "downloading — a download that stops partway through leaves a file "
            "the tool cannot read. The file types that work are a shapefile set "
            "(a .shp together with the other files that came with it), a "
            ".geojson, or a .gpkg. If you exported this from a mapping program, "
            "export it again and choose one of those. If someone else sent it to "
            "you, ask them for one of those three instead.",
        ),
        _entry(
            "PIP-L002",
            SEVERITY_BLOCKING,
            "This shapefile is missing some of its pieces",
            "A shapefile is not one file — it is a small set of files that share "
            "the same name and have to travel together, and some of them did not "
            "arrive.",
            "Each missing piece carries something the tool needs: the table of "
            "names and numbers for each area, the lookup table that lets the "
            "tool find a shape quickly, or the record of where on Earth the "
            "shapes sit. Without them the shapes cannot be read or placed on a "
            "map.",
            "Go back to the folder or the zip file you got this from, and send "
            "every file whose name matches the .shp — not the .shp on its own. "
            "If you only ever received the one file, ask whoever sent it for the "
            "complete set, or ask for a .gpkg or .geojson instead, which keep "
            "everything in a single file.",
        ),
        _entry(
            "PIP-L003",
            SEVERITY_BLOCKING,
            "This map file doesn't say where on Earth it belongs",
            "This map file draws its shapes out of plain numbers, but nothing "
            "inside it records where on Earth those numbers sit.",
            "The tool has to line your shapes up against a real street address "
            "before it can say which area that address falls in, and it cannot "
            "do that without knowing where on Earth the shapes belong. A layer "
            "installed this way would also stop the service from starting up at "
            "all, so it would take the whole site down, not just this layer.",
            "Every map file has somewhere to record where on Earth its shapes "
            "belong — the setting a mapping program calls the projection, which "
            "means exactly that — and in this one it is empty. The tool will not "
            "guess it, because a wrong guess puts your areas in another part of "
            "the world and nothing anywhere says so. What puts it right depends "
            "on the kind of file you sent, which is what the sentences above "
            "are about. If none of it is yours to fix, ask whoever gave you the "
            "file to send it again with that setting filled in, or to send "
            "the same areas as a .gpkg, which keeps the setting inside the one "
            "file.",
        ),
        _entry(
            "PIP-L004",
            SEVERITY_BLOCKING,
            "The location information in this file disagrees with the numbers "
            "inside it",
            "What this file says about where on Earth its shapes sit does not "
            "match the numbers stored inside it. Either it says the numbers are "
            "plain latitude and longitude — the kind a phone shows for a place, "
            "which never run past 180 and 90 — while holding numbers far too big "
            "to be those; or it says they are measured on a local grid, in feet "
            "or metres out from a fixed local starting point, while holding "
            "numbers that are plainly latitude and longitude. The sentence that "
            "follows says which way round this one is.",
            "One of the two is wrong, and the tool cannot tell which. If it "
            "believed the file, your areas would be placed in the wrong part of "
            "the world, every address looked up would fall outside all of them, "
            "and the service would keep answering as if nothing were the matter. "
            "This nearly always means the location information was set by hand, "
            "or copied over from a different file.",
            "The person who prepared this file can say which of the two is "
            "right, and if you did not make it yourself that is the first thing "
            "to ask them for — a copy with the setting corrected. If you did "
            "make it, open it in your mapping program and change what it says "
            "about where on Earth the shapes sit so that it matches the numbers "
            "already in the file, rather than changing the numbers; then export "
            "it again and send the new copy.",
        ),
        _entry(
            "PIP-L005",
            SEVERITY_BLOCKING,
            "This file has no shapes in it",
            "The file opened correctly, but there is nothing drawn inside it — "
            "zero areas.",
            "There is nothing to install and nothing to draw on the preview map "
            "for you to check, so the tool has no way to show you what you would "
            "be installing.",
            "This usually means the export was run with a filter that matched "
            "nothing, or that a selection was active and empty when the file was "
            "saved. If you have a mapping program, open the file, confirm you "
            "can actually see the areas on screen, clear any selection or "
            "filter, and export it again. If you do not have one, ask whoever "
            "prepared the file to open it and check that the areas really are "
            "inside it before sending another copy.",
        ),
        _entry(
            "PIP-L006",
            SEVERITY_BLOCKING,
            "This file holds points or lines, not areas",
            "The things drawn in this file are single spots or single lines. "
            "They have no inside — nothing enclosed.",
            "This tool answers exactly one question: which area does this "
            "address fall inside? A spot or a line has no inside, so no address "
            "can ever fall in one.",
            "You most likely have the wrong file out of a set. Agencies commonly "
            "publish the outlines of their districts and the centre points of "
            "those same districts as two separate downloads, and the outlines "
            "are the ones you want. Look for a file described as boundaries, "
            "areas, or districts rather than points, centroids, or centrelines.",
        ),
        _entry(
            "PIP-L007",
            SEVERITY_BLOCKING,
            "This file mixes different kinds of shapes",
            "Some of the things drawn in this file are enclosed areas and some "
            "are not — there are spots or lines mixed in among the areas.",
            "The tool installs a file as one layer, all of it or none. The spots "
            "and lines would sit inside that layer as entries no address can "
            "ever fall in, so part of your map would quietly answer nothing and "
            "look exactly like a genuine miss.",
            "Open the file in your mapping program, keep only the enclosed "
            "areas, and export those to a new file. If you were not expecting a "
            "mixture, it usually means two datasets were merged at some point; "
            "ask whoever prepared it for the boundaries on their own.",
        ),
        _entry(
            "PIP-L008",
            SEVERITY_WARNING,
            "Some outlines cross over themselves",
            "A few of the outlines in this file double back and cross their own "
            "edge, so along those few shapes there is no single answer to which "
            "side is inside.",
            "Near those crossings an address just outside a boundary could be "
            "reported as inside it, or the other way round. Everything away from "
            "those particular edges is unaffected, and the rest of the file is "
            "fine.",
            "Look at them before you decide: the preview map marks each one, "
            "and the sentence above gives its row number in the file, counting "
            "every row from the top, so you can find it in the file's own table "
            "as well. The tool can straighten these out for you as it installs "
            "the layer, and that is usually the right choice — it only nudges "
            "the points where an outline crosses itself and leaves every other "
            "edge alone. If these areas matter to you exactly as drawn, ask "
            "whoever prepared the file to clean them up and send a new copy "
            "instead.",
        ),
        _entry(
            "PIP-L009",
            SEVERITY_BLOCKING,
            "That short name is already taken",
            "The short name you chose for this layer is already in use by a "
            "layer that is installed.",
            "The short name is how a request asks for one particular layer, so "
            "two layers cannot share one. The second would hide the first, and "
            "answers would quietly start coming from the wrong map.",
            "Pick a different short name. Adding whatever makes this one "
            "different usually reads well — the year, the county, or the kind of "
            "district. For example wards_2026, or precincts_cook.",
        ),
        _entry(
            "PIP-L010",
            SEVERITY_BLOCKING,
            "A column you chose has nothing in it",
            "One of the columns you picked to report back with each area is "
            "either not in this file at all, or it is there but blank for every "
            "single area.",
            "Those columns are the entire answer someone gets back — the ward "
            "number, the district name. If one of them is blank, every lookup "
            "would come back with an empty space where the answer should be, and "
            "nothing anywhere would report a problem — the empty answer would "
            "arrive looking exactly like a real one.",
            "The columns this file actually has are listed beside the preview. "
            "Pick one of those that has values in it. If the "
            "column you wanted is genuinely not there, this may be the wrong "
            "file, or those values may live in a separate table that has to be "
            "attached to the shapes before exporting.",
        ),
        _entry(
            "PIP-L011",
            SEVERITY_BLOCKING,
            "Two columns in this file have the same name",
            "This file has more than one column with the same name, or names "
            "that differ only in capital letters — which becomes the same name "
            "once the layer is saved.",
            "When two columns share a name there is no way to say which of them "
            "you meant, so the tool cannot promise that the value it reports "
            "comes from the column you were looking at when you chose it.",
            "If you have a mapping program, open the file, rename one of the two "
            "so that every name is different, and export it again. Repeated "
            "names usually turn up after two tables have been attached to each "
            "other, and the copy you do not need can normally just be deleted. "
            "If you do not have one, ask whoever prepared the file to do that "
            "renaming and send a new copy.",
        ),
        _entry(
            "PIP-L012",
            SEVERITY_BLOCKING,
            "This compressed file is too big to unpack safely",
            "The zip file you sent is either bigger than this tool will accept, "
            "or it claims to unpack into far more than its own size suggests it "
            "could hold.",
            "Unpacking it could fill the disk and take the service down for "
            "everyone. A file that swells by an enormous amount when it is "
            "opened is also a known way of attacking a service, so the tool "
            "stops instead of guessing which this is.",
            "If this is just a large dataset, unpack it yourself and send only "
            "the one layer you want — a single .gpkg is usually far smaller than "
            "a zip full of shapefiles. If you did not make this zip yourself, do "
            "not unpack it on your own computer either; ask whoever sent it what "
            "is inside.",
        ),
        _entry(
            "PIP-L013",
            SEVERITY_BLOCKING,
            "This compressed file tries to write outside its own folder",
            "Inside this zip file, at least one item is addressed to somewhere "
            "outside the temporary folder the tool unpacks into.",
            "Unpacking it as written could overwrite files elsewhere on this "
            "machine. Ordinary map data never needs to do that, so the tool "
            "stops and touches nothing.",
            "Do not unpack this zip on your own computer either. Ask whoever "
            "sent it to send the map files on their own, not zipped, or to send "
            "a single .gpkg instead. If it came from a public download page, let "
            "whoever runs that page know about the file.",
        ),
        _entry(
            "PIP-L014",
            SEVERITY_BLOCKING,
            "That web address did not return map data",
            "The tool fetched the web address you gave it and what came back is "
            "not map data — it is a web page, a sign-in screen, or an error "
            "message from the far end.",
            "There is nothing in that answer to draw or install. A sign-in "
            "screen in particular means the data is not public, and this tool "
            "only works with data that is.",
            "Check that the address points straight at the data itself rather "
            "than at the page describing it. For an ArcGIS service the address "
            "ends with /FeatureServer/0 or /MapServer/0. Pasting the address "
            "into your browser is a quick test: if you get a normal-looking web "
            "page instead of a wall of text or a file download, the address is "
            "the wrong one. The first part of what came back is shown above, and "
            "often names the real problem.",
        ),
        _entry(
            "PIP-L015",
            SEVERITY_WARNING,
            "This layer is unusually large",
            "This file holds far more areas, or far more fine detail in each "
            "outline, than the layers this service normally carries.",
            "The service keeps every installed layer in memory the whole time it "
            "is running. A layer this size will make it slower to start, use a "
            "lot more memory, and on a small machine it may not fit at all.",
            "It will still install. Before you install it, ask whether you need "
            "every area in it — if you only ever serve one county, trim it to "
            "that county and export again. Many published files also come in a "
            "simplified version with less fine detail in each outline; that "
            "version is usually the better fit and looks the same at street "
            "level. If you install this one as it is, the sign of trouble is the "
            "service taking noticeably longer to answer its first lookup after a "
            "restart, or not coming back up at all — so restart it once and "
            "check that it does.",
        ),
        _entry(
            "PIP-L016",
            SEVERITY_WARNING,
            "This layer sits nowhere near the ones already installed",
            "The ground this file covers does not overlap any of the layers "
            "already installed on this service.",
            "That is perfectly fine if you meant to add somewhere new. If you "
            "did not, it is the usual sign of the wrong file — the neighbouring "
            "county, a whole state instead of one city, or a year whose "
            "boundaries covered somewhere else.",
            "Look at the preview map before you install it. If you recognise the "
            "outline, go ahead. If it is not where you expected, go back to "
            "where you downloaded the file and check which place it covers.",
        ),
        _entry(
            "PIP-L017",
            SEVERITY_WARNING,
            "Nothing in this file says how old it is",
            "The tool looked for a date saying when these boundaries were drawn "
            "or last changed, and there is none. Shapefiles carry no such date "
            "at all — the only date inside one is when the file itself was "
            "written out, which tells you nothing about the boundaries.",
            "District and precinct lines get redrawn, and a service answering "
            "from last cycle's lines answers wrongly, confidently, and without "
            "complaint. The tool cannot warn you about that, because the file "
            "does not know either.",
            "You have to check this yourself before you install it. Go back to "
            "the page you downloaded it from and read the date published there, "
            "and look at the preview map for a boundary you know has changed "
            "recently. Write down what you find in the notes as you install it, "
            "so the next person knows what they are looking at.",
        ),
        _entry(
            "PIP-L018",
            SEVERITY_WARNING,
            "Shapefile column names are cut short",
            "When data is saved as a shapefile every column name is cut down to "
            "ten letters. A column called ward_precinct comes out as ward_preci, "
            "and there is no way to recover the full name from the file.",
            "The name you saw in the documentation, or on the agency's own web "
            "map, may not be the name inside this file — and the name inside "
            "this file is the one that will show up in every answer the service "
            "gives out.",
            "Check the cut-down names above against the ones you expected, and "
            "make sure you picked the columns you meant. If the "
            "shortened names are hard to tell apart, ask for the same data as a "
            ".gpkg instead; that format keeps names at full length.",
        ),
        _entry(
            "PIP-L019",
            SEVERITY_BLOCKING,
            "This file holds more than one map, so you have to say which",
            "This file opened and read perfectly well, and there is more than "
            "one separate set of areas inside it. This tool installs one set at "
            "a time, and nothing you have sent so far says which of them you "
            "meant.",
            "There is nothing the matter with the file — this is the normal way "
            "an agency publishes several things at once, wards and precincts "
            "and boundaries all in the one download. Taking the first one for "
            "you would be a guess, and a wrong guess installs a real, "
            "correct-looking map of the wrong thing: the preview would look "
            "right, every answer would arrive looking right, and nothing "
            "anywhere would say it was not the one you asked for.",
            "The names of the ones inside it are listed above. Choose the one "
            "you want and send the file again with that choice, and the rest of "
            "the file is simply left alone. If you would rather not choose "
            "here, open the file in a mapping program and export just the areas "
            "you want to a file of their own, or ask whoever sent it for that "
            "one on its own.",
        ),
        _entry(
            "PIP-L020",
            SEVERITY_WARNING,
            "This will replace a layer that is already installed",
            "The short name you chose is the one an installed layer already "
            "uses, so installing this puts these areas in that layer's place "
            "and the areas that are there now are dropped.",
            "That is exactly right if you meant to update it — the same "
            "districts, redrawn. If you did not mean to, every answer that "
            "comes from that short name today will come from this file from now "
            "on, and the layer it is replacing is not kept anywhere.",
            "Look at the preview map and satisfy yourself that these are the "
            "areas you meant to put in its place, and that they cover the same "
            "ground the old ones did. If what you wanted was a second layer "
            "beside the one already installed rather than in place of it, go "
            "back and give this one a short name of its own — the year or the "
            "county usually reads well, as in wards_2026.",
        ),
    )
}


def get_code(code: str) -> LayerCode:
    """The registry entry for `code`, or `UnknownLayerCodeError` if there is none."""
    try:
        return LAYER_CODES[code]
    except KeyError:
        raise UnknownLayerCodeError(
            f"unknown layer code {code!r}; registered: {sorted(LAYER_CODES)}"
        ) from None


def _json_safe(value: Any) -> Any:
    """Reduce a value to something `json.dumps` accepts.

    Findings travel to a browser in a later task, and the detail a check
    collects comes out of pandas — numpy integers, numpy floats, pandas index
    objects. Left alone those blow up at encoding time, far from the check that
    produced them.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    item_method = getattr(value, "item", None)  # numpy scalar
    if callable(item_method):
        try:
            return _json_safe(item_method())
        except (ValueError, TypeError):
            pass
    tolist_method = getattr(value, "tolist", None)  # numpy array / pandas Index
    if callable(tolist_method):
        try:
            return _json_safe(tolist_method())
        except (ValueError, TypeError):
            pass
    return str(value)


@dataclass(frozen=True)
class Finding:
    """One registry entry, fired, with the runtime facts that made it fire.

    `specifics` is the one sentence naming the real thing — which columns, which
    files, how many shapes — and is slotted between `what` and `why` in the
    rendered `message`. `detail` carries the same facts in machine-readable
    form, for the preview page to highlight rows or columns with.
    """

    code: str
    severity: str
    title: str
    what: str
    why: str
    fix: str
    specifics: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_blocking(self) -> bool:
        return self.severity == SEVERITY_BLOCKING

    @property
    def message(self) -> str:
        """The whole finding as one paragraph: what, the specifics, why, the fix."""
        return " ".join(
            part.strip()
            for part in (self.what, self.specifics, self.why, self.fix)
            if part and part.strip()
        )

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable form of this finding."""
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "what": self.what,
            "specifics": self.specifics,
            "why": self.why,
            "fix": self.fix,
            "message": self.message,
            "detail": dict(self.detail),
        }


def build_finding(
    code: str, *, specifics: str = "", detail: dict[str, Any] | None = None
) -> Finding:
    """Fire `code`, pinning the registry text to this run's concrete facts."""
    entry = get_code(code)
    return Finding(
        code=entry.code,
        severity=entry.severity,
        title=entry.title,
        what=entry.what,
        why=entry.why,
        fix=entry.fix,
        specifics=specifics.strip(),
        detail=_json_safe(detail or {}),
    )


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Most severe first, then by code — the same list every run, given the same
    input, so the preview page does not reshuffle between refreshes."""
    return sorted(
        findings, key=lambda found: (_SEVERITY_RANK.get(found.severity, 99), found.code)
    )


def has_blocking(findings: list[Finding]) -> bool:
    """True if anything in `findings` stops this layer being committed."""
    return any(found.is_blocking for found in findings)
