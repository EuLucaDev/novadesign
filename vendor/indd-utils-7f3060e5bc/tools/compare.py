#!/usr/bin/env python3
"""Compare converter output with reference IDML files.

For every INDD file in the corpus that has a reference IDML from the same
major version (the `idml` column of corpus/inventory.tsv, written by
tools/inventory.py), convert it with `target/release/indd` and compare the
result with the reference. Files with the same SHA-256 are converted
once. Reports, per element type: how many referenced elements we produce
(matched by Self), and per attribute how often our value matches. Also
reports story text agreement.

Usage: python3 -I tools/compare.py [--limit N] [--detail TAG]... [--file SUBSTR]
                                   [--schemas DIR --jing DIR] [--bin PATH]
                                   [--all] [--exclude PREFIX]... [--jobs N]
                                   [--shortfalls N] [--trusted] [--stale N]
                                   [--gaps N] [--out DIR] [--cache DIR]
                                   [--valid-cache FILE] [--subtotal PREFIX]...
Run from the repository root after `cargo build --release`. With --schemas
and --jing, also validates every output with tools/validate.sh, in batches
of VALIDATE_BATCH files (one Jing run per schema for a whole batch). --bin runs
another converter binary, for example a copy of the previous build.
Conversions run in --jobs threads and the comparisons of pairs in --jobs
worker processes.

--cache DIR keeps every conversion in DIR/<first 16 hex digits of the
binary's SHA-256>/, named by cache_key (the SHA-256 of the INDD file and
a short hash of its file name): the package (.idml), a record of the exit
status, stderr and the package's entries (.json), and for pairs the output
of `indd xmp` and `indd uids`. A later run with the same binary reuses them
instead of converting. A run killed by a signal or a timeout is not kept.
It also keeps the result of each pair's comparison in DIR/compare/, named
by a SHA-256 of everything the result depends on (comparison_key): the
reference IDML, the output package, the xmp and uids outputs, the pair's
path and file name, --show, the source of this file and the Python
version. A later run, with any binary, reuses it for a pair whose output
did not change.
--valid-cache FILE keeps schema validation results by part (schema and
SHA-256 of the part; for the same schemas, Jing and tools/validate.sh) and
validates only parts it does not list, in reduced packages; the results
are the same as validating every package.
--subtotal PREFIX also reports the pairs whose path does not start with
PREFIX (conversion failures, schema failures, story text, value coverage)
and adds them to summary.json under "subtotals".

--all also converts every other INDD and INDT file under corpus/ (any
version, either byte order, without a usable IDML), validates the output
if schemas are given, and reports failures for those files separately.
Files the converter rejects for a correct reason (not an INDD file,
InDesign 1.x, truncated, no object database: REJECTIONS) are reported as
rejected files, not as conversion failures.
--exclude leaves out files whose path under corpus/ starts with PREFIX.
Converter warnings are counted by kind over all converted files.

Pairs are trustworthy or stale (the IDML shows a different save than the
INDD; rule and evidence in docs/measurement.md). Totals are printed for all
pairs and for trustworthy pairs; --trusted limits the element tables,
shortfalls and details to trustworthy pairs, --stale N lists stale pairs.

Last comes the headline (docs/measurement.md): value coverage, the share
of reference values the conversion reproduces, over trustworthy pairs and
over all pairs; document scores (pairs with coverage >= 99 % and >= 99.9 %);
extra values (values the output has and the reference does not); and the
biggest gaps (--gaps N). pairs.tsv, gaps.tsv and gaps-all.tsv go to --out
(default target/compare), with values.tsv and values-all.tsv, which list
every key with its values and how many are reproduced, and extras.tsv and
extras-all.tsv, which list the extra values by key. summary.json holds the
numbers of the README's "Current numbers" section, which
tools/readme_numbers.py writes into the README.

--shortfalls N lists the N element types and attributes that fall short
(missing elements; wrong or missing attribute values; differing story
text) in the most documents.

Embedded file data (`Contents`) is compared by digest, so it is reported as
`md5:<hex> <length>` rather than as the full text.
"""

import argparse
import fnmatch
import functools
import gc
import hashlib
import json
import multiprocessing
import os
import pickle
import re
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "target" / "release" / "indd"
# Converted files validated together in one tools/validate.sh run.
VALIDATE_BATCH = 600
# Error messages of files the converter rejects for a correct reason:
# they are not INDD files it can read. Start of the message, and how the
# report names them.
REJECTIONS = [
    ("not an INDD file", "not INDD files"),
    ("not supported yet: InDesign 1.x", "InDesign 1.x files"),
    ("file truncated", "truncated"),
    ("no object database", "without an object database"),
]


def error_message(stderr):
    """The error of a failed conversion: the last line that is not a
    warning."""
    lines = [x for x in stderr.splitlines() if not x.startswith("warning: ")]
    return lines[-1] if lines else ""


def split_rejected(failed):
    """(rejected, failures) of a list of (name, stderr)."""
    rejected = [f for f in failed
                if any(error_message(f[1]).startswith(p) for p, _ in REJECTIONS)]
    return rejected, [f for f in failed if f not in rejected]


def rejection_kinds(rejected):
    """The rejected files counted by kind, as text."""
    kinds = Counter(next(n for p, n in REJECTIONS if error_message(err).startswith(p))
                    for _, err in rejected)
    return ", ".join(f"{n:,} {name}" for name, n in kinds.most_common())


def corpus_path(path):
    """A path under corpus/ as a string relative to corpus/, like
    str(path.relative_to(...)), which takes several times as long."""
    prefix, s = str(ROOT / "corpus") + os.sep, str(path)
    if not s.startswith(prefix):
        raise ValueError(f"{path} is not under {ROOT / 'corpus'}")
    return s[len(prefix):]


def inventory():
    """Rows of corpus/inventory.tsv (tools/inventory.py), as dicts."""
    lines = (ROOT / "corpus" / "inventory.tsv").read_text().splitlines()
    cols = lines[0].split("\t")
    return [dict(zip(cols, line.split("\t"))) for line in lines[1:]]


def known_digests():
    """Path under corpus/ -> (SHA-256, size in bytes) of the files the
    inventory lists with a digest, for digest()."""
    return {r["path"]: (r["sha256"], r.get("bytes")) for r in inventory() if r.get("sha256")}


def digest(indd, known):
    """SHA-256 of a corpus file. The inventory's digest is taken when the
    inventory lists the file with its present size (the inventory records no
    modification time); otherwise the file is read."""
    path = corpus_path(indd)
    sha, size = known.get(path, (None, None))
    if sha and size == str(indd.stat().st_size):
        return sha
    return file_sha256(indd)


def cache_key(indd, sha):
    """The name of a conversion under a --cache folder: the INDD's SHA-256
    and a short hash of its file name, which the output contains
    (designmap.xml's Name)."""
    return f"{sha}-{hashlib.sha256(os.fsencode(indd.name)).hexdigest()[:8]}"


def pairs(limit, substr, exclude, seen, known):
    """Corpus files with a same-version reference IDML, without duplicates.
    Adds the digest of each file to `seen`."""
    out = []
    for row in inventory():
        path, order, ver, dom = row["path"], row["order"], row["version"], row["idml_dom"]
        if order != "LE" or not dom or ver.split(".")[0] != dom.split(".")[0]:
            continue
        if substr and substr not in path or any(path.startswith(e) for e in exclude):
            continue
        indd = ROOT / "corpus" / path
        d = digest(indd, known)
        if d in seen:
            continue
        seen.add(d)
        idml = row.get("idml") or str(Path(path).with_suffix(".idml"))
        out.append((indd, ROOT / "corpus" / idml))
    return out[:limit] if limit else out


def unpaired(substr, exclude, seen, known):
    """Every other INDD and INDT file under corpus/, without duplicates."""
    out = []
    for indd in sorted((ROOT / "corpus").rglob("*")):
        if indd.suffix.lower() not in (".indd", ".indt") or not indd.is_file():
            continue
        path = corpus_path(indd)
        if substr and substr not in path or any(path.startswith(e) for e in exclude):
            continue
        d = digest(indd, known)
        if d in seen:
            continue
        seen.add(d)
        out.append(indd)
    return out


def warning_kind(msg):
    """A warning with its numbers and quoted names replaced, for counting."""
    msg = re.sub(r'"[^"]*"', '"…"', msg)
    msg = re.sub(r"\b0x[0-9a-fA-F]+\b", "#", msg)
    return re.sub(r"\b\d+(\.\d+)?\b", "#", msg)


XMP_NS = {"rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
          "xmp": "http://ns.adobe.com/xap/1.0/",
          "xmpMM": "http://ns.adobe.com/xap/1.0/mm/",
          "stRef": "http://ns.adobe.com/xap/1.0/sType/ResourceRef#",
          "stEvt": "http://ns.adobe.com/xap/1.0/sType/ResourceEvent#"}
# A pair is stale if the INDD was saved more than this long after the
# IDML's metadata date (docs/measurement.md). A PDF is a different save if
# its date and the INDD's are further apart than this.
STALE_AFTER = timedelta(hours=1)


def xmp_descriptions(packet):
    """The rdf:Description elements of the document in an XMP packet."""
    if not packet:
        return []
    start = packet.find(b"<x:xmpmeta")
    end = packet.rfind(b"</x:xmpmeta>")
    if start < 0 or end < 0:
        return []
    try:
        meta = ET.fromstring(packet[start:end + len(b"</x:xmpmeta>")])
    except ET.ParseError:
        return []
    return meta.findall("rdf:RDF/rdf:Description", XMP_NS)


def xmp_value(el, name):
    """A simple XMP property of an element, written as an attribute or as a
    child element (`name` is "prefix:Local")."""
    prefix, local = name.split(":")
    key = "{%s}%s" % (XMP_NS[prefix], local)
    text = el.get(key) or el.findtext(key)
    return text.strip() if text else None


def modify_date(packet):
    """xmp:ModifyDate of the document in an XMP packet, or None."""
    for desc in xmp_descriptions(packet):
        text = xmp_value(desc, "xmp:ModifyDate")
        if text:
            try:
                d = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            # Without a time zone, compare clock times.
            return d.replace(tzinfo=d.tzinfo or timezone.utc)
    return None


def xmp_instances(packet):
    """(xmpMM:InstanceID, instance ID of xmpMM:DerivedFrom, instance IDs of
    the xmpMM:History events in order) of the document in an XMP packet.
    Missing values are None; the history is a list."""
    instance = derived = None
    history = []
    for desc in xmp_descriptions(packet):
        instance = instance or xmp_value(desc, "xmpMM:InstanceID")
        df = desc.find("xmpMM:DerivedFrom", XMP_NS)
        if df is not None and derived is None:
            derived = xmp_value(df, "stRef:instanceID")
        h = desc.find("xmpMM:History", XMP_NS)
        if h is not None:
            history += [xmp_value(li, "stEvt:instanceID")
                        for li in h.iter("{%s}li" % XMP_NS["rdf"])]
    return instance, derived, [i for i in history if i]


def pdf_stale_reasons(indd_xmp, pdf_xmp, pdf_pages, idml_pages, pdf_creator):
    """Why a PDF beside an INDD may not show the same save as the INDD
    (empty if there is no sign of that). `indd_xmp` and `pdf_xmp` are XMP
    packets; `pdf_creator` is the PDF's Creator entry. See
    docs/measurement.md, "Same-save PDFs", for the evidence."""
    out = []
    instance, _, history = xmp_instances(indd_xmp)
    derived = xmp_instances(pdf_xmp)[1]
    if derived is None:
        out.append("PDF names no source instance")
    elif derived != instance and derived not in history:
        out.append("PDF source instance not in the INDD history")
    indd_date, pdf_date = modify_date(indd_xmp), modify_date(pdf_xmp)
    if indd_date is None or pdf_date is None:
        out.append("no ModifyDate")
    elif abs(pdf_date - indd_date) > STALE_AFTER:
        out.append("PDF and INDD saved over an hour apart")
    if pdf_pages is None or idml_pages is None or pdf_pages != idml_pages:
        out.append("page count differs")
    if not (pdf_creator or "").startswith("Adobe InDesign"):
        out.append("PDF not written by InDesign")
    return out


def idml_modify_date(idml):
    """xmp:ModifyDate in an IDML package's META-INF/metadata.xml, or None."""
    try:
        with zipfile.ZipFile(idml) as z:
            return modify_date(z.read("META-INF/metadata.xml"))
    except (KeyError, zipfile.BadZipFile, OSError):
        return None


def stale_reasons(indd_date, idml_date, indd_uids, ref_el, ref_st, our_st):
    """Why a pair's IDML may not show the same save as its INDD (empty if
    there is no sign of that). `indd_uids` maps each UID in the INDD
    database to its class (None for a slot without a class). See
    docs/measurement.md for the evidence."""
    out = []
    if indd_date is None or idml_date is None:
        out.append("no ModifyDate")
    elif indd_date < idml_date:
        out.append("IDML modified after the INDD")
    elif indd_date - idml_date > STALE_AFTER:
        out.append("INDD saved over an hour after the IDML")
    ref_uids = {int(s[1:], 16) for _, s in ref_el if re.fullmatch(r"u[0-9a-f]+", s)}
    if indd_uids is not None and ref_uids - indd_uids.keys():
        out.append("IDML objects not in the INDD")
    if indd_uids is not None and any(indd_uids.get(u, 0) is None for u in ref_uids):
        out.append("IDML objects deleted in the INDD")
    if set(our_st) - set(ref_st):
        out.append("INDD stories not in the IDML")
    return out


def validate(outs, args):
    """Schema errors of outputs, as {path: [messages]} (valid outputs are
    left out). Runs tools/validate.sh once over all of them; it batches the
    Jing runs."""
    errs = defaultdict(list)
    if not outs:
        return errs
    env = dict(os.environ, VALIDATE_JOBS=str(max(1, args.jobs)))
    v = subprocess.run(
        [ROOT / "tools" / "validate.sh", *map(str, outs), args.schemas, args.jing],
        capture_output=True, text=True, env=env)
    for line in v.stdout.splitlines():
        path, _, msg = line.partition("\t")
        errs[path].append(msg)
    if v.returncode not in (0, 1) or v.returncode == 1 and not errs or "jing" in errs:
        raise SystemExit(f"tools/validate.sh failed: {v.stderr.strip()} {errs.get('jing')}")
    return errs


# Elements that wrap part of a story's text without being text themselves.
# IDML puts them around ranges (ParagraphStyleRange > HyperlinkTextSource >
# CharacterStyleRange, XMLElement > ParagraphStyleRange) or inside a range
# around text; text_ranges looks through them (docs/measurement.md).
WRAPPERS = {"XMLElement", "HyperlinkTextSource", "CrossReferenceSource", "Change",
            "EndnoteRange"}
# Children that are neither text nor a character position.
NOT_TEXT = {"Properties", "XMLAttribute"}
# Elements that hold text of their own inside a story: their ranges are
# counted in a text flow of their own (docs/measurement.md).
NESTED = {"Footnote", "Cell", "Note"}


ERROR = re.compile(r"(?P<part>[^:]+):(?P<line>\d+):(?P<col>\d+): error: (?P<msg>.*)")


# Form fields that InDesign writes inside a character range when they are
# anchored in text, where the schema does not allow them.
ANCHORED_FORM_FIELDS = ("CheckBox", "RadioButton", "TextBox", "ComboBox", "ListBox",
                        "SignatureField")


def endnote_errors(idml, errs):
    """The schema errors of `idml` as (part, message) without positions,
    if every one is a deviation of InDesign's own markup from the 21.5
    schema: an `Endnote` in a character range, an element inside an
    `EndnoteRange`, or a form field anchored in a character range. None
    otherwise (docs/measurement.md)."""
    out = set()
    z = zipfile.ZipFile(idml)
    for e in errs:
        m = ERROR.match(e)
        if not m:
            return None
        part, line, msg = m["part"], int(m["line"]), m["msg"]
        form = any(msg.startswith(f'element "{t}" not allowed here')
                   for t in ANCHORED_FORM_FIELDS)
        if not msg.startswith('element "Endnote" not allowed here') and \
                not (form and endnote_parent(z.read(part), line) == "CharacterStyleRange") and \
                endnote_parent(z.read(part), line) != "EndnoteRange":
            return None
        out.add((part, msg))
    return out


def endnote_parent(data, line):
    """The tag of the parent of the first element that starts on `line`."""
    import xml.parsers.expat
    stack, found = [], []
    p = xml.parsers.expat.ParserCreate()

    def start(tag, attrs):
        if not found and p.CurrentLineNumber == line:
            found.append(stack[-1] if stack else None)
        stack.append(tag)
    p.StartElementHandler = start
    p.EndElementHandler = lambda tag: stack.pop()
    p.Parse(data, True)
    return found[0] if found else None


def text_ranges(story):
    """Map (text flow, offset) -> attributes of the paragraph and character
    range starting there, with keys prefixed PSR. and CSR. Flow 0 is the
    story's own text; each footnote, table cell and note is a flow of its
    own, numbered in document order. Ranges inside wrapper elements count,
    at whatever depth. Offsets count Content in UTF-16 code units and every
    other element inside a character range (Br, Table, Footnote, an
    anchored frame) as one position; elements outside character ranges
    (StoryPreference, ...) are not text."""
    out = {}
    flows = 0

    def walk(el, pa, in_csr, flow, pos):
        """Walk the children of `el` inside the paragraph attributes `pa`;
        `in_csr` tells whether `el` is inside a character range. Returns
        the offset after them."""
        for ch in el:
            if ch.tag in NOT_TEXT:
                continue
            if ch.tag == "ParagraphStyleRange":
                pos = walk(ch, {"PSR." + k: v for k, v in props(ch).items()}, False, flow, pos)
            elif ch.tag == "CharacterStyleRange":
                start = pos
                pos = walk(ch, pa, True, flow, pos)
                if pos > start:
                    ca = {"CSR." + k: v for k, v in props(ch).items()}
                    # A range nested at the same offset (Change > range at
                    # the start of this one) keeps the offset.
                    out.setdefault((flow, start), {**pa, **ca})
            elif ch.tag in WRAPPERS:
                pos = walk(ch, pa, in_csr, flow, pos)
            elif ch.tag == "Content":
                pos += len((ch.text or "").encode("utf-16-le")) // 2
            else:
                if in_csr:
                    pos += 1
                nested(ch)
        return pos

    def nested(el):
        """Walk the text flows held in `el` (a table's cells, a footnote)."""
        nonlocal flows
        if el.tag in NESTED:
            flows += 1
            walk(el, {}, False, flows, 0)
            return
        for ch in el:
            if ch.tag != "Properties":
                nested(ch)

    walk(story, {}, False, 0, 0)
    return out


def load(path):
    """Map Self -> element, plus story texts, from an IDML package."""
    z = zipfile.ZipFile(path)
    elements = {}
    stories = {}
    ranges = {}
    for name in z.namelist():
        if not name.endswith(".xml"):
            continue
        try:
            root = ET.fromstring(z.read(name))
        except ET.ParseError as e:
            raise SystemExit(f"{path}: {name}: {e}")
        for el in root.iter():
            s = el.get("Self")
            if s is not None:
                if el.tag == "Font":
                    s = font_self(el, s)
                elements.setdefault((el.tag, s), el)
                # Children without Self (TextFramePreference, ...) are
                # compared as "<parent tag>/<child tag>" of the parent.
                for ch in el:
                    if ch.get("Self") is not None or ch.tag == "Properties":
                        continue
                    if len(ch.attrib):
                        elements.setdefault((f"{el.tag}/{ch.tag}", s), ch)
                        nested_options(elements, f"{el.tag}/{ch.tag}", s, ch)
                    else:
                        # A container without attributes (TransparencySetting):
                        # compare its children as "<parent>/<child>/<grandchild>".
                        for g in ch:
                            if g.get("Self") is None and len(g.attrib):
                                elements.setdefault((f"{el.tag}/{ch.tag}/{g.tag}", s), g)
        if name == "Resources/Preferences.xml":
            for el in root:
                elements.setdefault((el.tag, "Preferences"), el)
                nested_options(elements, el.tag, "Preferences", el)
        if name.startswith("Stories/"):
            for st in root.iter("Story"):
                stories[st.get("Self")] = story_text(st)
                ranges[st.get("Self")] = text_ranges(st)
    return elements, stories, ranges


# Children of a child element with attributes that are compared as
# "<parent>/<child>/<grandchild>" (docs/measurement.md, what is compared).
NESTED_OPTIONS = {("TextWrapPreference", "ContourOption")}


def nested_options(elements, tag, s, el):
    for g in el:
        if (el.tag, g.tag) in NESTED_OPTIONS and len(g.attrib):
            elements.setdefault((f"{tag}/{g.tag}", s), g)


def story_text(story):
    parts = []
    for el in story.iter():
        if el.tag == "Content":
            parts.append(el.text or "")
        elif el.tag == "Br":
            parts.append("\n")
    return "".join(parts)


def norm(v):
    """Normalise numbers so 1 == 1.0 and tiny float noise is ignored."""
    def fix(tok):
        try:
            f = float(tok)
        except ValueError:
            return tok
        return f"{f:.6g}"
    return " ".join(fix(t) for t in v.split(" "))


def canon(el):
    """A structured property (TabList, AllNestedStyles, ...) as one string:
    each leaf as tag=text with numbers normalised, in document order."""
    if len(el) == 0 and el.get("type") != "list":
        return f"{el.tag}={norm(el.text or '')}"
    return el.tag + "[" + ",".join(canon(c) for c in el) + "]"


def props(el):
    """Attributes plus <Properties> children (as P.Name)."""
    a = dict(el.attrib)
    p = el.find("Properties")
    if p is not None:
        for c in p:
            if c.get("type") == "list":
                a["P." + c.tag] = canon(c)
            elif len(c) == 0:
                text = c.text or ""
                # A line feed and tabs alone are the indentation of an
                # element written open and closed (docs/measurement.md).
                if re.fullmatch(r"\n\t*", text):
                    text = ""
                extra = sorted((k, v) for k, v in c.attrib.items() if k != "type")
                if extra:
                    text = " ".join(f"{k}={v}" for k, v in extra)
                if c.tag == "Contents":
                    n = norm(text)
                    text = f"md5:{hashlib.md5(n.encode()).hexdigest()} {len(n)}"
                a["P." + c.tag] = text
    return a


# Gap keys for values that could not be compared one by one.
NO_ELEMENT = "(element not produced)"
NO_TEXT = "(story text differs)"
NOT_IN_REFERENCE = "(element not in reference)"
ELEMENT = "(element)"


# Values left out of value coverage because they cannot be derived from an
# INDD file: (tag pattern, attribute or P.<Properties child> pattern,
# reason), patterns as in fnmatch. docs/measurement.md explains each entry.
EXCLUDED = [
    ("Font", "Status", "whether the font is installed on the exporting computer"),
    ("Document/{http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging}*", "src",
     "file names of the package parts, chosen by the IDML writer"),
    ("DocumentUser", "P.UserColor",
     "colour the exporting InDesign gives each user, not the colour in the INDD"),
    ("Assignment", "Name",
     "the exporting InDesign's name for unassigned content, which depends on its language"),
    ("Link", "LinkImportTime", "local time of the exporting computer; the INDD stores UTC"),
    ("Link", "LinkImportModificationTime",
     "local time of the exporting computer; the INDD stores UTC"),
    ("*", "BitmapPrinting", "evaluated by the exporting computer; not stored in the INDD"),
] + [
    ("Font", k, "axes of the font file on the exporting computer; not stored in the INDD")
    for k in ("NumDesignAxes", "DesignAxesName", "DesignAxesValues", "P.DesignAxesRange")
] + [
    ("*", k, "written by a plugin of the exporting InDesign; not in the IDML schema")
    for k in ("Observers", "Subjects", "DisplayErrorDialogs", "EventFilter", "JavaScript",
              "SubjectScriptTagFilter", "UseDebugger", "CompactedDataStore", "ComponentId")
] + [
    ("Story", "P.DocsFlowImportSettings",
     "written by a plugin of the exporting InDesign; not in the IDML schema"),
    ("Story", "P.WordsFlow*",
     "written by a plugin of the exporting InDesign; not in the IDML schema"),
    # ELEMENT leaves out the whole element, with all its values.
    ("Document/WordsFlowPreference", ELEMENT,
     "written by a plugin of the exporting InDesign; not in the IDML schema"),
]

# Attributes of a Link that describe the linked file as InDesign found it
# when it exported the IDML. When the IDML's LinkResourceURI differs from
# the converter's (which comes from the INDD), the link was resolved to
# another file on the exporting computer, and these values of that link
# are left out (docs/measurement.md).
RELINKED = re.compile(r"LinkResource.*|LinkImport.*")

# Font technology suffix that IDML adds to some family names, depending on
# the fonts installed on the exporting computer (docs/measurement.md).
FONT_SUFFIX = re.compile(r" \((OTF|TT|T1)\)$")


def font_name(tag, key, v, family=None):
    """A value with the font technology suffix removed if it names a font
    family: FontFamily Name, Font FontFamily, AppliedFont and BulletsFont.
    A Font's Name starts with its family (passed as family)."""
    if (tag, key) in (("FontFamily", "Name"), ("Font", "FontFamily")) or \
            key.endswith(("P.AppliedFont", "P.BulletsFont")):
        return FONT_SUFFIX.sub("", v)
    if (tag, key) == ("Font", "Name") and family and v.startswith(family):
        return FONT_SUFFIX.sub("", family) + v[len(family):]
    return v


def font_self(el, s):
    """A Font's Self with the technology suffix removed from the family
    name it contains, so that fonts match whether or not the exporting
    computer added the suffix."""
    family = el.get("FontFamily") or ""
    base = FONT_SUFFIX.sub("", family)
    if base != family:
        return s.replace("Fontn" + family, "Fontn" + base, 1)
    return s


@functools.cache
def excluded(tag, key):
    return any(fnmatch.fnmatchcase(key, k) and fnmatch.fnmatchcase(tag, t)
               for t, k, _ in EXCLUDED)


def tree(el):
    """An element with its attributes, text and descendants as one string,
    numbers normalised: the value of a structured <Properties> child such as
    PathGeometry."""
    attrs = ",".join(f"{k}={norm(v)}" for k, v in sorted(el.attrib.items()) if k != "type")
    kids = ",".join(tree(c) for c in el)
    return f"{el.tag}({attrs})[{kids}]{norm((el.text or '').strip())}"


def values(tag, el):
    """The values of an element that value coverage counts: attributes and
    <Properties> children, without Self and without EXCLUDED values."""
    a = props(el)
    p = el.find("Properties")
    if p is not None:
        for c in p:
            if c.get("type") != "list" and len(c):
                a["P." + c.tag] = tree(c)
    return {k: v for k, v in a.items() if k != "Self" and not excluded(tag, k)}


def add_counts(counter, counts):
    """counter.update(counts), with the same result and key order and less
    work per key."""
    get = counter.get
    for k, v in counts.items():
        counter[k] = get(k, 0) + v


class Stats:
    """Comparison counts over a set of pairs."""

    def __init__(self):
        self.docs = 0
        # Value coverage: reference values, and those reproduced exactly.
        self.values = 0
        self.reproduced = 0
        # Links whose resource values are left out (RELINKED).
        self.relinked = 0
        # (tag, key) -> values wrong / missing, and documents affected.
        self.gap_wrong = Counter()
        self.gap_missing = Counter()
        self.gap_docs = Counter()
        self.gap_total = Counter()
        # Values the output has and the reference does not, by (tag, key),
        # and documents affected.
        self.extra = 0
        self.extra_values = Counter()
        self.extra_docs = Counter()
        # (pair, reproduced, values) for each pair.
        self.pairs = []
        self.found = Counter()
        self.total = Counter()
        # (tag, attribute, "ok" / "wrong" / "missing") -> values; attributes()
        # groups them by attribute.
        self.attr_ok = Counter()
        self.story_ok = Counter()
        self.short_docs = Counter()  # (kind, tag, attribute) -> documents affected

    COUNTERS = ("gap_wrong", "gap_missing", "gap_docs", "gap_total", "extra_values",
                "extra_docs", "found", "total", "attr_ok", "story_ok", "short_docs")

    def __getstate__(self):
        """Plain dicts in place of the Counters, which unpickle several times
        slower."""
        state = dict(vars(self))
        for c in self.COUNTERS:
            state[c] = dict(state[c])
        return state

    def __setstate__(self, state):
        for c in self.COUNTERS:
            state[c] = Counter(state[c])
        vars(self).update(state)

    def attributes(self):
        """(tag, attribute) -> Counter of "ok", "wrong" and "missing"."""
        out = defaultdict(Counter)
        for (tag, k, kind), n in self.attr_ok.items():
            out[(tag, k)][kind] = n
        return out

    def add(self, other, counts=True):
        """Add the counts of `other`. Without `counts`, only the numbers of
        pairs, values and stories (subtotal)."""
        self.docs += other.docs
        self.values += other.values
        self.reproduced += other.reproduced
        self.extra += other.extra
        self.relinked += other.relinked
        self.pairs += other.pairs
        add_counts(self.story_ok, other.story_ok)
        if not counts:
            return
        for c in self.COUNTERS:
            if c != "story_ok":
                add_counts(getattr(self, c), getattr(other, c))

    def coverage(self):
        return self.reproduced / self.values if self.values else 1.0

    def quantile(self, q):
        """Value coverage of the pair at quantile q (0 = lowest)."""
        cov = sorted(ok / n if n else 1.0 for _, ok, n in self.pairs)
        return cov[min(len(cov) - 1, int(q * len(cov)))] if cov else 1.0

    def document_score(self, at_least):
        """Pairs whose value coverage is at least `at_least`."""
        return sum(1 for _, ok, n in self.pairs if n == 0 or ok / n >= at_least)

    def gaps(self):
        """(tag, key), documents, values wrong, values missing; most
        documents first, then most values."""
        rows = [(k, d, self.gap_wrong[k], self.gap_missing[k]) for k, d in self.gap_docs.items()]
        return sorted(rows, key=lambda r: (-r[1], -(r[2] + r[3]), r[0]))

    def extras(self):
        """(tag, key), documents, extra values; most documents first."""
        rows = [(k, d, self.extra_values[k]) for k, d in self.extra_docs.items()]
        return sorted(rows, key=lambda r: (-r[1], -r[2], r[0]))

    def story_line(self):
        st = sum(self.story_ok.values())
        return (f"{self.story_ok['ok']}/{st} exact, {self.story_ok['wrong']} differ, "
                f"{self.story_ok['missing']} missing")


def compare_pair(name, ref, ours, examples, show):
    """Stats of one pair: `ref` and `ours` are load() results. Adds up to
    `show` mismatches per attribute to `examples`."""
    ref_el, ref_st, ref_rg = ref
    our_el, our_st, our_rg = ours
    p = Stats()
    p.docs = 1
    short = set()
    for (tag, s), el in ref_el.items():
        p.total[tag] += 1
        mine = our_el.get((tag, s))
        if mine is None:
            short.add(("element", tag, ""))
            continue
        p.found[tag] += 1
        mine = props(mine)
        for k, v in props(el).items():
            if k == "Self":
                continue
            if k not in mine:
                p.attr_ok[(tag, k, "missing")] += 1
                short.add(("attribute", tag, k))
            elif norm(mine[k]) == norm(v):
                p.attr_ok[(tag, k, "ok")] += 1
            else:
                p.attr_ok[(tag, k, "wrong")] += 1
                short.add(("attribute", tag, k))
                if len(examples[(tag, k)]) < show:
                    examples[(tag, k)].append((name, s, v, mine[k]))
    for sid, rgs in ref_rg.items():
        if our_st.get(sid) != ref_st.get(sid):
            continue
        mine = our_rg.get(sid, {})
        for start, attrs in rgs.items():
            p.total["TextRange"] += 1
            if start not in mine:
                short.add(("element", "TextRange", ""))
                continue
            p.found["TextRange"] += 1
            for k, v in attrs.items():
                o = mine[start].get(k)
                if o is None:
                    p.attr_ok[("TextRange", k, "missing")] += 1
                    short.add(("attribute", "TextRange", k))
                elif norm(o) == norm(v):
                    p.attr_ok[("TextRange", k, "ok")] += 1
                else:
                    p.attr_ok[("TextRange", k, "wrong")] += 1
                    short.add(("attribute", "TextRange", k))
                    if len(examples[("TextRange", k)]) < show:
                        examples[("TextRange", k)].append((name, sid, start, v, o))
    for sid, text in ref_st.items():
        if sid not in our_st:
            p.story_ok["missing"] += 1
            short.add(("element", "Story", ""))
        elif our_st[sid] == text:
            p.story_ok["ok"] += 1
        else:
            p.story_ok["wrong"] += 1
            short.add(("text", "Story", ""))
            if len(examples[("Story", "text")]) < max(show, 3):
                examples[("Story", "text")].append((name, sid, text[:80], our_st[sid][:80]))
    p.short_docs.update(short)
    value_coverage(p, ref, ours)
    return p


def value_coverage(p, ref, ours):
    """Count reference values and the reproduced ones into `p`: element
    presence, attribute and <Properties> values, story text, and the start
    and attributes of each text range. Also count the values the output has
    and the reference does not (extra values), with the same keys."""
    ref_el, ref_st, ref_rg = ref
    our_el, our_st, our_rg = ours
    total = Counter()
    wrong = Counter()
    missing = Counter()
    extra = Counter()

    def compare(tag, theirs, mine):
        for k, v in theirs.items():
            key = (tag, k)
            total[key] += 1
            o = mine.get(k)
            if o is None:
                missing[key] += 1
            elif norm(font_name(tag, k, o, mine.get("FontFamily"))) != \
                    norm(font_name(tag, k, v, theirs.get("FontFamily"))):
                wrong[key] += 1
        for k in mine.keys() - theirs.keys():
            extra[(tag, k)] += 1

    for (tag, s), el in ref_el.items():
        if excluded(tag, ELEMENT):
            continue
        theirs = values(tag, el)
        mine = our_el.get((tag, s))
        if mine is None:
            n = 1 + len(theirs)
            total[(tag, NO_ELEMENT)] += n
            missing[(tag, NO_ELEMENT)] += n
            continue
        total[(tag, ELEMENT)] += 1
        mine = values(tag, mine)
        if tag == "Link" and \
                mine.get("LinkResourceURI") not in (None, theirs.get("LinkResourceURI")):
            theirs = {k: v for k, v in theirs.items() if not RELINKED.fullmatch(k)}
            mine = {k: v for k, v in mine.items() if not RELINKED.fullmatch(k)}
            p.relinked += 1
        compare(tag, theirs, mine)
    for (tag, s), el in our_el.items():
        if (tag, s) not in ref_el and not excluded(tag, ELEMENT):
            extra[(tag, NOT_IN_REFERENCE)] += 1 + len(values(tag, el))
    for sid, text in ref_st.items():
        key = ("Story", "(text)")
        total[key] += 1
        if sid not in our_st:
            missing[key] += 1
        elif our_st[sid] != text:
            wrong[key] += 1
    for sid in our_st.keys() - ref_st.keys():
        extra[("Story", "(text)")] += 1
        extra[("TextRange", NOT_IN_REFERENCE)] += sum(1 + len(a) for a in
                                                      our_rg.get(sid, {}).values())
    for sid, rgs in ref_rg.items():
        if our_st.get(sid) != ref_st.get(sid):
            n = sum(1 + len(a) for a in rgs.values())
            total[("TextRange", NO_TEXT)] += n
            missing[("TextRange", NO_TEXT)] += n
            continue
        mine = our_rg.get(sid, {})
        for start, attrs in rgs.items():
            if start not in mine:
                n = 1 + len(attrs)
                total[("TextRange", NO_ELEMENT)] += n
                missing[("TextRange", NO_ELEMENT)] += n
                continue
            total[("TextRange", ELEMENT)] += 1
            compare("TextRange", attrs, mine[start])
        for start in mine.keys() - rgs.keys():
            extra[("TextRange", NOT_IN_REFERENCE)] += 1 + len(mine[start])
    n = sum(total.values())
    bad = sum(wrong.values()) + sum(missing.values())
    p.values += n
    p.reproduced += n - bad
    p.gap_total.update(total)
    p.gap_wrong.update(wrong)
    p.gap_missing.update(missing)
    p.gap_docs.update(k for k in total if wrong[k] or missing[k])
    p.extra += sum(extra.values())
    p.extra_values.update(extra)
    p.extra_docs.update(k for k, v in extra.items() if v)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def cache_dir(binary, root):
    """The folder of a converter binary's outputs under a --cache root:
    the first 16 hex digits of the binary's SHA-256."""
    return Path(root) / file_sha256(binary)[:16]


def package_entries(path):
    """[name, SHA-256 of the content] of each entry of a package, in order;
    None if it is not a readable ZIP file."""
    try:
        with zipfile.ZipFile(path) as z:
            return [[i.filename, hashlib.sha256(z.read(i)).hexdigest()] for i in z.infolist()]
    except (OSError, zipfile.BadZipFile):
        return None


# Exit statuses of the converter itself (src/main.rs; 101 is a panic). Any
# other status means the process was killed, so the run is not kept.
OWN_EXITS = (0, 1, 2, 101)


def run_killable(cmd, timeout, text):
    """subprocess.run with capture; (exit status or "timeout", stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=text, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout", "" if text else b"", ""
    return r.returncode, r.stdout, r.stderr


def convert_cached(binary, indd, key, folder, pair, timeout=None):
    """Convert `indd` to folder/KEY.idml and return the conversion's record,
    or the record of an earlier conversion into the same folder. The record
    (folder/KEY.json) has the exit status, stderr, seconds and, for an output, the
    package's SHA-256 and entries (package_entries). With `pair`, also runs
    `indd xmp` and `indd uids`, whose output goes to folder/KEY.xmp and
    folder/KEY.uids (left out when the command fails). A conversion that
    times out (exit status "timeout") or is killed by a signal is returned
    but not recorded, and neither is the xmp and uids step if one of them is."""
    folder = Path(folder)
    rec_path, out = folder / f"{key}.json", folder / f"{key}.idml"
    tmp = folder / f"{key}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        rec = json.loads(rec_path.read_text())
    except (OSError, ValueError):
        rec = None
    changed = False
    if rec is None or rec["exit"] == 0 and not out.exists():
        part = tmp.with_suffix(".idml")
        t = time.time()
        code, _, stderr = run_killable([binary, "convert", indd, part], timeout, True)
        rec = {"exit": code, "stderr": stderr, "secs": round(time.time() - t, 2)}
        if code == 0:
            rec["package"] = file_sha256(part)
            rec["entries"] = package_entries(part)
            part.replace(out)
        else:
            part.unlink(missing_ok=True)
        if code not in OWN_EXITS:
            return rec
        changed = True
    if pair and "meta" not in rec:
        killed = False
        for cmd in ("xmp", "uids"):
            code, stdout, _ = run_killable([binary, cmd, indd], timeout, False)
            if code == 0:
                tmp.write_bytes(stdout)
                tmp.replace(folder / f"{key}.{cmd}")
            killed = killed or code not in OWN_EXITS
        if not killed:
            rec["meta"] = True
            changed = True
    if changed:
        tmp.write_text(json.dumps(rec))
        tmp.replace(rec_path)
    return rec


def read_uids(path):
    """UID -> class (None for a slot without a class) from the output of
    `indd uids`, or None if there is none."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    uids = {}
    for line in text.splitlines():
        uid, cls = line.split("\t")
        uids[int(uid)] = None if cls == "-" else int(cls, 16)
    return uids


def compare_job(rel, name, idml, folder, key, show, save=None):
    """Compare one converted pair (convert_cached); runs in a worker
    process. (Stats, stale reasons, examples). With `save`, the result is
    also written to that file (comparison cache)."""
    folder = Path(folder)
    xmp = folder / f"{key}.xmp"
    indd_date = modify_date(xmp.read_bytes()) if xmp.exists() else None
    uids = read_uids(folder / f"{key}.uids")
    ref, ours = load(idml), load(folder / f"{key}.idml")
    reasons = stale_reasons(indd_date, idml_modify_date(idml), uids, ref[0], ref[1], ours[1])
    examples = defaultdict(list)
    p = compare_pair(name, ref, ours, examples, show)
    p.pairs = [(rel, p.reproduced, p.values)]
    examples = dict(examples)
    if save:
        # Only built-in types, so that the file loads whatever module name
        # this file runs under.
        tmp = save.with_name(f"{save.name}.{os.getpid()}.tmp")
        tmp.write_bytes(pickle.dumps((p.__getstate__(), reasons, examples),
                                     pickle.HIGHEST_PROTOCOL))
        tmp.replace(save)
    return p, reasons, examples


@functools.cache
def comparison_code():
    """SHA-256 of what a comparison runs: the source of this file (it
    imports no other module of the repository) and the Python version."""
    h = hashlib.sha256(Path(__file__).read_bytes())
    h.update(sys.version.encode())
    return h.hexdigest()


def comparison_key(rel, name, idml, folder, key, rec, show):
    """The name of a pair's result in the comparison cache: a SHA-256 of
    everything compare_job reads or is given. The output package is named
    by its SHA-256 in the conversion record, so a result is reused by any
    converter binary that produces the same output."""
    folder = Path(folder)
    parts = [comparison_code(), rel, name, str(show), file_sha256(idml),
             rec.get("package") or file_sha256(folder / f"{key}.idml")]
    for ext in ("xmp", "uids"):
        f = folder / f"{key}.{ext}"
        parts.append(file_sha256(f) if f.exists() else "-")
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def cached_comparison(rel, name, idml, folder, key, rec, show, cache, procs):
    """compare_job's result for a pair, from the comparison cache folder
    `cache` or computed in `procs` and added to it. (result, taken from
    the cache)."""
    path = Path(cache) / f"{comparison_key(rel, name, idml, folder, key, rec, show)}.pickle"
    try:
        state, reasons, examples = pickle.loads(path.read_bytes())
        p = Stats.__new__(Stats)
        p.__setstate__(state)
        return (p, reasons, examples), True
    except Exception:
        # Not cached, or a damaged file, which compare_job replaces.
        pass
    return procs.submit(compare_job, rel, name, idml, folder, key, show, path).result(), False


@functools.cache
def schema_selection():
    """The parts tools/validate.sh validates and their schemas, read from
    the script: ({part: schema}, {folder: schema of its direct .xml
    children})."""
    text = (ROOT / "tools" / "validate.sh").read_text()
    folders = re.search(r"\bfor d in ([^;]+); do", text)
    parts = re.search(r"\bfor p in ((?:[^;\\]|\\\n)+); do", text)
    if not folders or not parts or "-name designmap.xml" not in text:
        raise SystemExit("tools/validate.sh: part selection not found; "
                         "update compare.schema_selection")
    single = {"designmap.xml": "designmap.rnc"}
    single.update({p + ".xml": p + ".rnc" for p in parts[1].replace("\\", " ").split()})
    by_folder = {f: f"{f}/{n}.rnc" for f, n in (d.split(":") for d in folders[1].split())}
    return single, by_folder


def part_schema(name):
    """The schema tools/validate.sh validates a package entry with, or None."""
    single, by_folder = schema_selection()
    if name in single:
        return single[name]
    folder, _, rest = name.partition("/")
    if folder in by_folder and rest.endswith(".xml") and "/" not in rest:
        return by_folder[folder]
    return None


class ValidationCache:
    """Schema errors of package parts (--valid-cache). Jing validates each
    part on its own against the schema its path selects, so the errors of a
    part depend only on the schema and the part's bytes. The file is a TSV
    of a key of the schemas, Jing and tools/validate.sh; the schema; the
    part's SHA-256; and its errors as a JSON list, without the part name.
    Results under another key are ignored."""

    def __init__(self, path, schemas, jing):
        h = hashlib.sha256((ROOT / "tools" / "validate.sh").read_bytes())
        for d in (Path(schemas), Path(jing)):
            for f in sorted(d.rglob("*")):
                if f.is_file():
                    h.update(str(f.relative_to(d)).encode() + b"\0" + f.read_bytes())
        self.key = h.hexdigest()[:16]
        self.path = Path(path)
        self.known = {}
        # Parts taken from the cache and validated, in this run.
        self.hits = self.validated = 0
        # A run killed while appending leaves at most a broken last line,
        # which is skipped.
        if self.path.exists():
            for line in self.path.read_text(errors="replace").splitlines():
                try:
                    key, schema, sha, errs = line.split("\t", 3)
                    if key == self.key:
                        self.known[(schema, sha)] = [] if errs == "[]" else json.loads(errs)
                except ValueError:
                    continue

    def add(self, results):
        """Record {(schema, part SHA-256): errors}, one write per line."""
        new = {k: e for k, e in results.items() if k not in self.known}
        self.known.update(new)
        if new:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab", buffering=0) as f:
                if f.tell():
                    with open(self.path, "rb") as r:
                        r.seek(-1, os.SEEK_END)
                        if r.read(1) != b"\n":
                            f.write(b"\n")
                for k, e in new.items():
                    f.write(f"{self.key}\t{k[0]}\t{k[1]}\t{json.dumps(e)}\n".encode())


def validate_cached(outs, args, cache):
    """Schema errors of packages given as (path, entries or None; see
    package_entries), as {path: [messages]} like validate(). With a
    ValidationCache, only parts it does not know are validated: they are
    copied into reduced packages for one tools/validate.sh run."""
    if cache is None:
        return validate([p for p, _ in outs], args)
    listed = []
    todo = {}   # (schema, part SHA-256) -> (package, part name)
    for path, entries in outs:
        if entries is None:
            entries = package_entries(path)
        parts = [(n, (part_schema(n), s)) for n, s in entries or [] if part_schema(n)]
        listed.append((path, parts))
        for n, key in parts:
            if key in cache.known:
                cache.hits += 1
            elif key not in todo:
                todo[key] = (path, n)
    if todo:
        by_package = defaultdict(dict)
        for key, (path, n) in todo.items():
            by_package[path][n] = key
        found = {key: [] for key in todo}
        with tempfile.TemporaryDirectory() as tmp:
            reduced = {}
            for i, (path, names) in enumerate(by_package.items()):
                r = Path(tmp) / f"{i}.idml"
                with zipfile.ZipFile(path) as z, zipfile.ZipFile(r, "w") as w:
                    for n in names:
                        w.writestr(n, z.read(n))
                reduced[str(r)] = names
            for r, msgs in validate(list(reduced), args).items():
                for m in msgs:
                    part, _, rest = m.partition(":")
                    if part not in reduced[r]:
                        raise SystemExit(f"tools/validate.sh: unexpected message {m}")
                    found[reduced[r][part]].append(rest)
        cache.add(found)
        cache.validated += len(todo)
    errs = {}
    for path, parts in listed:
        msgs = [f"{n}:{m}" for n, key in parts for m in cache.known[key]]
        if msgs:
            errs[str(path)] = msgs
    return errs


def main():
    # The run keeps every pair's counts, millions of objects that the cyclic
    # garbage collector would scan again and again; it creates no garbage
    # cycles worth collecting.
    gc.disable()
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--detail", action="append", default=[],
                    help="print attribute table for this tag (repeatable)")
    ap.add_argument("--file", help="only pairs whose path contains this")
    ap.add_argument("--show", type=int, default=0, help="show N mismatches per attribute")
    ap.add_argument("--schemas", help="IDML RelaxNG schema directory (validate output)")
    ap.add_argument("--jing", help="directory with jing.jar, isorelax.jar, saxon.jar")
    ap.add_argument("--bin", default=str(BIN), help="converter binary")
    ap.add_argument("--all", action="store_true",
                    help="also convert and validate files without a reference IDML")
    ap.add_argument("--exclude", action="append", default=[],
                    help="leave out files whose path under corpus/ starts with this (repeatable)")
    ap.add_argument("--warnings", type=int, default=15,
                    help="number of warning kinds to list")
    ap.add_argument("--shortfalls", type=int, default=0,
                    help="rank the N biggest shortfalls by documents affected")
    ap.add_argument("--trusted", action="store_true",
                    help="element tables, shortfalls and details over trustworthy pairs only")
    ap.add_argument("--stale", type=int, default=0,
                    help="list N stale pairs with their reasons")
    ap.add_argument("--gaps", type=int, default=30,
                    help="print the N biggest value coverage gaps")
    ap.add_argument("--out", default=str(ROOT / "target" / "compare"),
                    help="directory for pairs.tsv and gaps*.tsv (default target/compare)")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 1,
                    help="conversions, comparisons and schema validations run in parallel")
    ap.add_argument("--cache", metavar="DIR",
                    help="keep the conversions in DIR/<binary hash>/ and reuse them")
    ap.add_argument("--valid-cache", metavar="FILE",
                    help="schema validation results by part SHA-256, reused and extended")
    ap.add_argument("--subtotal", action="append", default=[], metavar="PREFIX",
                    help="also report the pairs whose path does not start with PREFIX")
    args = ap.parse_args()
    check = bool(args.schemas and args.jing)

    every = Stats()     # all pairs
    trusted = Stats()   # pairs without a sign of staleness
    stale = []          # (name, reasons)
    pair_rows = []      # (name, Stats, stale reasons) per pair
    examples = defaultdict(list)
    failures = []
    warnings = Counter()
    warned_files = Counter()
    seen = set()
    known = known_digests()
    todo = pairs(args.limit, args.file, args.exclude, seen, known)
    others = unpaired(args.file, args.exclude, seen, known) if args.all else []
    other_failures = []
    # Paths under corpus/ of the paired files that fail, and of those whose
    # output is invalid, for --subtotal.
    failed_paths = {}
    invalid_paths = set()
    vcache = ValidationCache(args.valid_cache, args.schemas, args.jing) \
        if check and args.valid_cache else None

    def count_warnings(stderr):
        kinds = set()
        for line in stderr.splitlines():
            if line.startswith("warning: "):
                kind = warning_kind(line[len("warning: "):])
                warnings[kind] += 1
                kinds.add(kind)
        for kind in kinds:
            warned_files[kind] += 1

    other_invalid = []
    invalid = []
    # Paired outputs whose only schema errors are InDesign's endnote
    # markup: (name, errors, normalised errors, reference IDML, path). They
    # pass if the reference has the same errors.
    endnote_pending = []
    with tempfile.TemporaryDirectory() as tmp:
        folder = cache_dir(args.bin, args.cache) if args.cache else Path(tmp)
        folder.mkdir(parents=True, exist_ok=True)
        # (INDD, reference IDML or None, cache key of the INDD)
        items = [(indd, None) for indd in others] + todo
        items = [(indd, idml, cache_key(indd, digest(indd, known))) for indd, idml in items]
        threads = ThreadPoolExecutor(max(1, args.jobs))
        procs = ProcessPoolExecutor(max(1, args.jobs),
                                    mp_context=multiprocessing.get_context("forkserver"))
        # A pair is compared in a worker process as soon as it is converted.
        # With --cache, a thread first looks for the result in the
        # comparison cache.
        ccache = Path(args.cache) / "compare" if args.cache else None
        if ccache:
            ccache.mkdir(parents=True, exist_ok=True)
            lookups = ThreadPoolExecutor(max(1, args.jobs))
        jobs = {}
        reused = 0
        lock = threading.Lock()

        def comparison(n):
            indd, idml, key = items[n]
            rel = corpus_path(indd)
            with lock:
                if n not in jobs and ccache:
                    jobs[n] = lookups.submit(cached_comparison, rel, indd.name, idml, folder,
                                             key, conversions[n].result(), args.show, ccache,
                                             procs)
                elif n not in jobs:
                    jobs[n] = procs.submit(compare_job, rel, indd.name, idml, folder, key,
                                           args.show)
                return jobs[n]

        def compare_when_converted(n):
            def done(f):
                if not f.exception() and f.result()["exit"] == 0:
                    comparison(n)
            return done

        # Pairs are converted first, so that their comparisons start early.
        order = sorted(range(len(items)), key=lambda n: items[n][1] is None)
        conversions = {}
        for n in order:
            indd, idml, key = items[n]
            conversions[n] = threads.submit(convert_cached, args.bin, indd, key, folder,
                                            idml is not None)
            if idml is not None:
                conversions[n].add_done_callback(compare_when_converted(n))
        # Files are taken in batches of VALIDATE_BATCH: one tools/validate.sh
        # run per batch. Without --cache, the batch's outputs are deleted
        # after it, which bounds the space they take.
        for start in range(0, len(items), VALIDATE_BATCH):
            batch = range(start, min(start + VALIDATE_BATCH, len(items)))
            recs = {n: conversions[n].result() for n in batch}
            errs = validate_cached([(folder / f"{items[n][2]}.idml", recs[n]["entries"])
                                    for n in batch if recs[n]["exit"] == 0],
                                   args, vcache) if check else {}
            for n in batch:
                indd, idml, key = items[n]
                rec, out = recs[n], folder / f"{key}.idml"
                rel = corpus_path(indd)
                count_warnings(rec["stderr"])
                if rec["exit"] != 0:
                    if idml is None:
                        other_failures.append((indd.name, rec["stderr"].strip()))
                    else:
                        failures.append((indd.name, rec["stderr"].strip()))
                        failed_paths[rel] = rec["stderr"].strip()
                    continue
                e = errs.get(str(out))
                if e:
                    endnotes = endnote_errors(out, e) if idml else None
                    if endnotes:
                        endnote_pending.append((indd.name, e, endnotes, idml, rel))
                    elif idml:
                        invalid.append((indd.name, e))
                        invalid_paths.add(rel)
                    else:
                        other_invalid.append((indd.name, e))
                if idml is not None:
                    result = comparison(n).result()
                    if ccache:
                        result, hit = result
                        reused += hit
                    p, reasons, ex = result
                    for k, found in ex.items():
                        room = (max(args.show, 3) if k == ("Story", "text") else args.show) \
                            - len(examples[k])
                        examples[k] += found[:max(room, 0)]
                    pair_rows.append((rel, p, reasons))
                    every.add(p)
                    if reasons:
                        stale.append((rel, reasons))
                    else:
                        trusted.add(p)
                if not args.cache:
                    out.unlink(missing_ok=True)
        threads.shutdown()
        if ccache:
            lookups.shutdown()
        procs.shutdown()
        endnote_passed = []
        if endnote_pending:
            refs = [(ref, None) for _, _, _, ref, _ in endnote_pending]
            ref_errs = validate_cached(refs, args, vcache)
            for name, e, endnotes, ref, rel in endnote_pending:
                theirs = endnote_errors(ref, ref_errs.get(str(ref), [])) or set()
                if endnotes <= theirs:
                    endnote_passed.append(name)
                else:
                    invalid.append((name, e))
                    invalid_paths.add(rel)

    rejected, failures = split_rejected(failures)
    other_rejected, other_failures = split_rejected(other_failures)
    if rejected:
        print(f"rejected files: {len(rejected)} of {len(todo)} paired files "
              f"({rejection_kinds(rejected)})")
    print(f"conversion failures: {len(failures)} of {len(todo)} paired files")
    for name, err in failures[:10]:
        print(f"  {name}: {err}")
    if check:
        print(f"schema validation failures: {len(invalid)}")
        for name, errs in invalid[:10]:
            print(f"  {name}: {len(errs)} errors, {errs[:3]}")
        print(f"schema errors accepted (endnote and anchored form field markup, as in "
              f"the reference): {len(endnote_passed)} files")
        if vcache:
            print(f"schema validation: {vcache.validated} parts validated, "
                  f"{vcache.hits} known from {args.valid_cache}")
    if args.all:
        print(f"files without a reference: {len(others)}, rejected: {len(other_rejected)}"
              + (f" ({rejection_kinds(other_rejected)})" if other_rejected else "")
              + f", conversion failures: {len(other_failures)}")
        for name, err in other_failures[:10]:
            print(f"  {name}: {err}")
        if check:
            print(f"files without a reference: schema validation failures: {len(other_invalid)}")
            with_endnotes = [n for n, errs in other_invalid if any(
                'element "Endnote"' in e or "EndnoteRange" in e for e in errs)]
            if with_endnotes:
                print(f"  with endnote markup errors: {len(with_endnotes)}: {with_endnotes[:10]}")
            with_forms = [n for n, errs in other_invalid if any(
                f'element "{t}" not allowed here' in e for e in errs for t in ANCHORED_FORM_FIELDS)]
            if with_forms:
                print(f"  with anchored form field errors: {len(with_forms)}: {with_forms[:10]}")
            for name, errs in other_invalid[:10]:
                print(f"  {name}: {len(errs)} errors, {errs[:3]}")
    if args.cache:
        print(f"comparisons: {reused} of {len(pair_rows)} pairs taken from {ccache}")
    print(f"warnings: {sum(warnings.values())} (count, files, kind)")
    for kind, count in warnings.most_common(args.warnings):
        print(f"  {count:6} {warned_files[kind]:4}  {kind[:150]}")
    print(f"story text: {every.story_line()}")
    reasons = Counter(r for _, rs in stale for r in rs)
    print(f"trustworthy pairs: {trusted.docs} of {every.docs}; stale: {len(stale)} "
          f"({', '.join(f'{r}: {n}' for r, n in reasons.most_common())})")
    print(f"trustworthy pairs: story text: {trusted.story_line()}")
    for name, rs in stale[:args.stale]:
        print(f"  stale: {name}: {'; '.join(rs)}")
    st = trusted if args.trusted else every
    which = "trustworthy pairs" if args.trusted else "all pairs"
    print(f"\nelements, {which} (produced / in reference):")
    for tag, n in st.total.most_common():
        print(f"  {tag:34} {st.found[tag]:6} / {n:6}")
    if args.shortfalls:
        print(f"\nshortfalls by documents affected (of {st.docs} {which}):")
        attrs = st.attributes()

        def instances(kind, tag, k):
            """How many elements, values or stories fall short."""
            if kind == "attribute":
                return attrs[(tag, k)]["wrong"] + attrs[(tag, k)]["missing"]
            if kind == "element":
                return st.total[tag] - st.found[tag]
            return st.story_ok["wrong"]
        ranked = sorted(st.short_docs.items(), key=lambda kv: (-kv[1], -instances(*kv[0]), kv[0]))
        for (kind, tag, k), n in ranked[:args.shortfalls]:
            what = f"{st.story_ok['wrong']} stories differ"
            if kind == "attribute":
                c = attrs[(tag, k)]
                what = f"{c['wrong']} wrong, {c['missing']} missing of {sum(c.values())}"
            elif kind == "element":
                what = f"{st.total[tag] - st.found[tag]} of {st.total[tag]} not produced"
            print(f"  {n:5}  {tag + (' ' + k if k else ''):56} {what}")
    for tag in args.detail:
        print(f"\nattributes of {tag}, {which} (ok / wrong / missing):")
        rows = [(k, c) for (t, k), c in st.attributes().items() if t == tag]
        for k, c in sorted(rows, key=lambda kc: -sum(kc[1].values())):
            print(f"  {k:40} {c['ok']:6} {c['wrong']:6} {c['missing']:6}")
            for ex in examples[(tag, k)]:
                print(f"      {ex}")
    for ex in examples[("Story", "text")]:
        print("  story mismatch:", ex)
    headline(args, every, trusted, pair_rows)
    subtotals = {prefix: subtotal(prefix, check, todo, pair_rows, failed_paths, invalid_paths)
                 for prefix in args.subtotal}
    write_summary(Path(args.out) / "summary.json", args, check,
                  (todo, failures, rejected, invalid),
                  (others, other_failures, other_rejected, other_invalid),
                  every, trusted, subtotals)


def subtotal(prefix, check, todo, pair_rows, failed_paths, invalid_paths):
    """Print and return the numbers of the pairs whose path under corpus/
    does not start with `prefix` (--subtotal)."""
    def inside(rel):
        return not rel.startswith(prefix)
    files = sum(1 for indd, _ in todo if inside(corpus_path(indd)))
    every, trusted = Stats(), Stats()
    for rel, p, reasons in pair_rows:
        if inside(rel):
            every.add(p, counts=False)
            if not reasons:
                trusted.add(p, counts=False)
    failed = [(rel, err) for rel, err in failed_paths.items() if inside(rel)]
    failures = split_rejected(failed)[1]
    invalid = sum(1 for rel in invalid_paths if inside(rel)) if check else None
    print(f"\nwithout {prefix}: conversion failures: {len(failures)} of {files} paired files")
    if check:
        print(f"without {prefix}: schema validation failures: {invalid}")
    print(f"without {prefix}: story text: {every.story_line()}")
    print(f"without {prefix}: trustworthy pairs: {trusted.docs} of {every.docs}; "
          f"story text: {trusted.story_line()}")
    print(f"without {prefix}: value coverage: trustworthy pairs {trusted.coverage():.2%} "
          f"({trusted.reproduced} of {trusted.values} values), all pairs {every.coverage():.2%}")
    return {"files": files, "failures": len(failures), "invalid": invalid,
            "trustworthy": pair_numbers(trusted), "all_pairs": pair_numbers(every)}


def pair_numbers(st):
    """The numbers of a set of pairs in summary.json."""
    return {"pairs": st.docs, "values": st.values, "reproduced": st.reproduced,
            "stories": {"exact": st.story_ok["ok"], "differ": st.story_ok["wrong"],
                        "missing": st.story_ok["missing"]}}


def write_summary(path, args, check, paired, unpaired_files, every, trusted, subtotals):
    """Write the numbers of the README's "Current numbers" section, and the
    options they were measured with, as JSON (tools/readme_numbers.py).
    `paired` and `unpaired_files` are (files, failures, rejected,
    invalid): conversion failures of valid files, and files rejected for a
    correct reason (REJECTIONS)."""
    def files(group, converted):
        todo, failures, rejected, invalid = group
        if not converted:
            return None
        return {"files": len(todo),
                "failures": [error_message(err) for _, err in failures],
                "rejected": [error_message(err) for _, err in rejected],
                "invalid": len(invalid) if check else None}

    summary = {
        "options": {"all": args.all, "validated": check, "exclude": args.exclude,
                    "limit": args.limit, "file": args.file},
        "paired": files(paired, True),
        "others": files(unpaired_files, args.all),
        "trustworthy": pair_numbers(trusted),
        "all_pairs": pair_numbers(every),
    }
    if subtotals:
        summary["subtotals"] = subtotals
    path.write_text(json.dumps(summary, indent=1) + "\n")


def gap_name(key):

    tag, k = key
    return f"{tag} {k}"


def write_gaps(path, st):
    with open(path, "w") as f:
        f.write("tag\tkey\tdocuments\twrong\tmissing\tvalues\n")
        for key, docs, w, m in st.gaps():
            f.write(f"{key[0]}\t{key[1]}\t{docs}\t{w}\t{m}\t{st.gap_total[key]}\n")


def write_extras(path, st):
    with open(path, "w") as f:
        f.write("tag\tkey\tdocuments\textra\n")
        for key, docs, n in st.extras():
            f.write(f"{key[0]}\t{key[1]}\t{docs}\t{n}\n")


def write_values(path, st):
    """Every key with its values and how many are reproduced, for checking
    that a change reproduces no fewer values of any key."""
    with open(path, "w") as f:
        f.write("tag\tkey\tvalues\treproduced\twrong\tmissing\n")
        for key in sorted(st.gap_total):
            w, m = st.gap_wrong[key], st.gap_missing[key]
            f.write(f"{key[0]}\t{key[1]}\t{st.gap_total[key]}\t{st.gap_total[key] - w - m}"
                    f"\t{w}\t{m}\n")


def headline(args, every, trusted, pair_rows):
    """Print value coverage, document scores and the biggest gaps; write
    pairs.tsv, gaps.tsv (trustworthy pairs) and gaps-all.tsv to --out."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "pairs.tsv", "w") as f:
        f.write("path\ttrusted\tcoverage\treproduced\tvalues\textra\tstories"
                "\tstories_identical\tstale reasons\n")
        for name, p, reasons in sorted(pair_rows, key=lambda r: r[1].coverage()):
            f.write(f"{name}\t{not reasons}\t{p.coverage():.5f}\t{p.reproduced}\t{p.values}"
                    f"\t{p.extra}\t{sum(p.story_ok.values())}\t{p.story_ok['ok']}"
                    f"\t{'; '.join(reasons)}\n")
    write_gaps(out / "gaps.tsv", trusted)
    write_gaps(out / "gaps-all.tsv", every)
    write_extras(out / "extras.tsv", trusted)
    write_extras(out / "extras-all.tsv", every)
    write_values(out / "values.tsv", trusted)
    write_values(out / "values-all.tsv", every)
    print("\nheadline (docs/measurement.md):")
    for label, st in (("trustworthy pairs", trusted), ("all pairs", every)):
        n = st.docs or 1
        print(f"  {label}: value coverage {st.coverage():.2%} "
              f"({st.reproduced} of {st.values} values); documents at >= 99 %: "
              f"{st.document_score(0.99)}/{st.docs} ({st.document_score(0.99) / n:.1%}), "
              f">= 99.9 %: {st.document_score(0.999)}/{st.docs} "
              f"({st.document_score(0.999) / n:.1%})")
        print(f"    extra values (in the output, not in the reference): {st.extra} "
              f"({st.extra / (st.values or 1):.2%} of reference values); "
              f"links resolved to another file at export: {st.relinked}")
        print(f"    per pair: lowest {st.quantile(0):.1%}, 10th percentile {st.quantile(0.1):.1%}, "
              f"median {st.quantile(0.5):.1%}, 90th percentile {st.quantile(0.9):.1%}, "
              f"highest {st.quantile(1):.1%}")
    rows = trusted.gaps()
    print(f"\nvalue gaps, trustworthy pairs, by documents affected "
          f"(documents, values wrong, missing, of values; all in {out / 'gaps.tsv'}):")
    for key, docs, w, m in rows[:args.gaps]:
        print(f"  {docs:5} {w:7} {m:7} {trusted.gap_total[key]:8}  {gap_name(key)[:90]}")
    print("\nvalue gaps, trustworthy pairs, by values "
          "(documents, values wrong, missing, of values):")
    for key, docs, w, m in sorted(rows, key=lambda r: -(r[2] + r[3]))[:args.gaps // 2]:
        print(f"  {docs:5} {w:7} {m:7} {trusted.gap_total[key]:8}  {gap_name(key)[:90]}")
    rows = trusted.extras()
    print(f"\nextra values, trustworthy pairs, by documents affected "
          f"(documents, extra values; all in {out / 'extras.tsv'}):")
    for key, docs, n in rows[:args.gaps // 2]:
        print(f"  {docs:5} {n:7}  {gap_name(key)[:90]}")


if __name__ == "__main__":
    main()
