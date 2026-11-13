"""Reading text from the mirror so Irish characters come out right.

Every loader goes through this module. The mirror holds files in several
encodings, often mislabelled (see ~/org/20261015_dgi-explore.org, "Text encoding"):

- most text is UTF-8, about 3,200 files with a byte-order mark (BOM);
- about 460 are Windows-1252, e.g. "Bord G\\xe1is" as a single byte;
- some mix UTF-8 and Windows-1252 in one file;
- 133 are double-encoded: UTF-8 read as Windows-1252 and saved again,
  so "Síochána" became "SÃ­ochÃ¡na";
- shapefile .dbf tables often don't say which encoding they use.

The rules:
1. Sniff the real file type from its first bytes, never the extension.
2. Decide the encoding from the whole file: BOM, then strict UTF-8, then
   Windows-1252. A file mixing both is decoded line by line.
3. Repair double encoding only where the mojibake pattern appears, and
   report every change so it can be logged and reviewed.
4. Normalise to Unicode NFC (one code point per fada) and drop the BOM.
   Typographic punctuation (’ “ ” –) is kept as published.
"""

import codecs
import io
import re
import unicodedata
from dataclasses import dataclass, field

import ftfy

CHUNK = 4 << 20

# ── file type ────────────────────────────────────────────────────────────────

_MAGIC = [
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),          # empty zip
    (b"\x1f\x8b", "gzip"),
    (b"%PDF", "pdf"),
    (b"\xd0\xcf\x11\xe0", "ole"),    # old Office: .xls, .doc
    (b"SQLite format 3\x00", "sqlite"),  # GeoPackage
    (b"\x89PNG", "image"),
    (b"\xff\xd8\xff", "image"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"Rar!", "rar"),
    (b"7z\xbc\xaf", "7z"),
]


def sniff_type(head: bytes) -> str:
    """The real type of a file from its first few KB.

    Returns one of: zip gzip pdf ole sqlite image tiff rar 7z
    html xml json text utf16-text binary empty.
    """
    if not head:
        return "empty"
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    if head[:2] in (b"\xff\xfe", b"\xfe\xff") or _looks_utf16(head):
        return "utf16-text"
    body = head.removeprefix(codecs.BOM_UTF8).lstrip(b" \t\r\n").lower()
    if body.startswith((b"<!doctype html", b"<html", b"<head")) or (
            body.startswith(b"<") and b"<html" in body[:2048]):
        return "html"
    if body.startswith(b"<?xml") or body.startswith(b"<"):
        return "xml"          # KML, GML, Atom, RSS, ...
    if body.startswith((b"{", b"[")):
        return "json"
    # Text has no NUL bytes and few control characters.
    sample = head[:8192]
    if b"\x00" in sample:
        return "binary"
    ctrl = sum(1 for b in sample if b < 32 and b not in (9, 10, 12, 13))
    return "binary" if ctrl > len(sample) * 0.01 else "text"


def _looks_utf16(head: bytes) -> bool:
    """UTF-16 without a BOM: every other byte of ASCII text is NUL."""
    s = head[:1024]
    if len(s) < 16:
        return False
    even, odd = s[0::2].count(0), s[1::2].count(0)
    return max(even, odd) > len(s) / 2 * 0.9 and min(even, odd) < len(s) / 2 * 0.1


# ── encoding detection ───────────────────────────────────────────────────────

# A valid UTF-8 multi-byte sequence (2, 3 or 4 bytes).
_UTF8_MB = re.compile(rb"[\xc2-\xdf][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}|[\xf0-\xf4][\x80-\xbf]{3}")
_HIGH = re.compile(rb"[\x80-\xff]")


@dataclass
class Encoding:
    encoding: str            # utf-8, utf-16, cp1252, ascii, or mixed (utf-8 + cp1252)
    had_bom: bool = False
    nonascii: bool = False   # any byte >= 0x80 at all


def detect_encoding(path) -> Encoding:
    """Decide a text file's encoding by reading all of it.

    Reading only the start isn't enough: 1,023 large files in the mirror are
    plain ASCII for their first megabyte.
    """
    with open(path, "rb") as f:
        head = f.read(4)
        if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return Encoding("utf-16", had_bom=True, nonascii=True)
        f.seek(0)
        if _looks_utf16(f.read(1024)):
            return Encoding("utf-16-le" if head[1:2] == b"\x00" else "utf-16-be", nonascii=True)
        f.seek(0)
        bom = head.startswith(codecs.BOM_UTF8)
        if bom:
            f.seek(3)
        dec = codecs.getincrementaldecoder("utf-8")()
        utf8_ok, nonascii, valid_mb, tail = True, False, 0, b""
        while chunk := f.read(CHUNK):
            if not nonascii and _HIGH.search(chunk):
                nonascii = True
            if utf8_ok:
                try:
                    dec.decode(chunk)
                except UnicodeDecodeError:
                    utf8_ok = False
            if nonascii:
                # Count UTF-8 sequences, overlapping chunk edges by 3 bytes.
                buf = tail + chunk
                valid_mb += len(_UTF8_MB.findall(buf[:-3] if len(buf) > 3 else buf))
                tail = buf[-3:]
        if utf8_ok:
            try:
                dec.decode(b"", final=True)
            except UnicodeDecodeError:
                utf8_ok = False
    if utf8_ok:
        return Encoding("utf-8" if nonascii or bom else "ascii", had_bom=bom, nonascii=nonascii)
    # Not valid UTF-8. If it still holds real UTF-8 sequences, it's a mix.
    return Encoding("mixed" if valid_mb >= 2 else "cp1252", had_bom=bom, nonascii=True)


def _latin1_fallback(err):
    """cp1252 leaves 0x81 0x8D 0x8F 0x90 0x9D undefined; map them like Latin-1."""
    bad = err.object[err.start:err.end]
    return bad.decode("latin-1"), err.end


codecs.register_error("dgi-latin1", _latin1_fallback)


def decode_bytes(raw: bytes, encoding: str) -> str:
    """Decode with the encoding from detect_encoding()."""
    if encoding in ("utf-8", "ascii"):
        return raw.decode("utf-8-sig")
    if encoding.startswith("utf-16"):
        return raw.decode("utf-16" if encoding == "utf-16" else encoding)
    if encoding == "cp1252":
        return raw.removeprefix(codecs.BOM_UTF8).decode("cp1252", errors="dgi-latin1")
    if encoding == "mixed":
        return "".join(decode_line(line) for line in raw.removeprefix(codecs.BOM_UTF8).splitlines(keepends=True))
    return raw.decode(encoding, errors="dgi-latin1")


def decode_line(line: bytes) -> str:
    """One line of a mixed file: UTF-8 if it's valid UTF-8, otherwise Windows-1252."""
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        return line.decode("cp1252", errors="dgi-latin1")


# ── repairing and normalising ────────────────────────────────────────────────

# Signs of double encoding: "Ã" followed by a second Latin-1 byte (á é í ó ú
# Á É Í Ó Ú all become "Ã" + one character), or "â€" (’ “ ” – …).
MOJIBAKE = re.compile("Ã[\u0080-¿]|â€|Â[ -¿]")

_FTFY = ftfy.TextFixerConfig(
    unescape_html=False, remove_terminal_escapes=False, fix_latin_ligatures=False,
    fix_character_width=False, uncurl_quotes=False, fix_line_breaks=False,
    fix_surrogates=False, remove_control_chars=False, normalization="NFC",
    explain=False)


def fix_mojibake(s: str) -> str:
    """Undo double encoding ("PÃ¡irc" → "Páirc") where it's present; else unchanged."""
    if not MOJIBAKE.search(s):
        return s
    return ftfy.fix_text(s, _FTFY)


def clean(s: str) -> str:
    """Repair double encoding, drop a leading BOM, normalise to NFC."""
    s = fix_mojibake(s).removeprefix("﻿")
    return unicodedata.normalize("NFC", s)


_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def search_key(s: str) -> str:
    """Accent- and case-insensitive form for matching: 'Síochána' → 'siochana'.

    For matching and joins only; always display the original text.
    """
    s = unicodedata.normalize("NFKD", clean(s).translate(_QUOTES))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.lower().split())


# ── letters lost before publication ──────────────────────────────────────────

# Some published files already contain U+FFFD (�) where a letter was: the
# publisher's own conversion destroyed it ("T�ath Housing", "Fianna F�il",
# "D�n Laoghaire"). No decoding brings it back. Restorer makes a careful guess:
# a damaged word is restored only when exactly one known word matches it.
# Used for search and embedding text, never silently: callers log the changes.

LOST = "�"
_LOST_WORD = re.compile(r"[\w" + LOST + r"]*" + LOST + r"[\w" + LOST + r"]*")   # apostrophes split words
_DEGREE = re.compile(r"(\d)" + LOST + r"(\s?\d+(?:[.,]\d+)?\s?['′’])")
# A lost letter standing alone before a capitalised name: in Irish names that
# is Ó (Ó Riain, Ó Súilleabháin), the only one-letter word with a fada.
_O_NAME = re.compile(r"(?<![\w" + LOST + r"])" + LOST + r"(?=['’ ]" + LOST + r"?[A-ZÁÉÍÓÚ][a-záéíóú" + LOST + r"])")
# A lost apostrophe before a possessive s: "Kearney�s", "St Laurence�s".
_POSSESSIVE = re.compile(r"(?<=[A-Za-z])" + LOST + r"(?=s\b)")

# Irish names and words too rare in the mirror's clean text to be learned from
# it, but common in names that publishers' files lost letters from.
IRISH_SEED = """Éamon Éamonn Cuív Éilis Deaglán Róisín Seán Séamus Pádraig Máire Máirtín Síle Siobhán Seosamh
Ríordáin Ríada Snodaigh Súilleabháin Ceallaigh Dálaigh Néill Briain Brádaigh Gráinne Áine Aoife Caoimhe
Ciarán Colmán Dónal Dónall Éanna Fiachra Lorcán Niamh Órla Ruairí Tomás Bríd Éire Éireann Síochána Fáil Féin
Sláinte Slán Dáil Teachta Uachtarán Taoiseach Tánaiste Gaeltacht Gaeilge Údarás Ráth Átha Baile Cnoc Cill Dún
Raithlín Staisiún Triantáin Bhrí Mhíl Árann Inis Oírr Meáin Toraí Gaoth Dobhair Cois Fharraige Ciarraí""".split()
_WORD = re.compile(r"[^\W\d_]+")   # hyphenated and apostrophe names: each part separately


class Restorer:
    def __init__(self, texts=(), min_count=2, seed=IRISH_SEED):
        """Learn known words (those with letters outside ASCII) from clean text."""
        counts = {w.lower(): min_count for w in seed}
        for t in texts:
            if not t or LOST in t:
                continue
            for w in _WORD.findall(t):
                if not w.isascii():
                    k = w.lower()
                    counts[k] = counts.get(k, 0) + 1
        self.words = {}
        for w, n in counts.items():
            if n >= min_count:
                self.words.setdefault(len(w), []).append(w)
        self.cache = {}

    def _candidates(self, bad):
        key = bad.lower()
        if key not in self.cache:
            pat = re.compile("^" + "".join("." if ch == LOST else re.escape(ch) for ch in key) + "$")
            found = [w for w in self.words.get(len(key), ()) if pat.match(w)]
            self.cache[key] = found
        return self.cache[key]

    def restore(self, text, fixes=None):
        """Text with lost letters restored where the word is unambiguous."""
        if not text or LOST not in text:
            return text
        out = _DEGREE.sub(r"\1°\2", text)
        out = _O_NAME.sub("Ó", out)
        out = _POSSESSIVE.sub("’", out)

        def fix(m):
            bad = m.group(0)
            cands = self._candidates(bad)
            if len(cands) != 1:
                return bad
            letters = [b for b in bad if b != LOST and b.isalpha()]
            all_caps = bool(letters) and all(b.isupper() for b in letters)
            # The restored letter's case: capital in an all-caps word or at
            # the start of a word ("�ras" -> "Áras"), otherwise small.
            return "".join((c.upper() if all_caps or i == 0 else c) if b == LOST else b
                           for i, (b, c) in enumerate(zip(bad, cands[0])))

        new = _LOST_WORD.sub(fix, out)
        if fixes is not None and new != text:
            fixes.append((text, new))
        return new


def printable(s, max_bad=0.1):
    """False for binary junk (blob fields read as text): mostly control or lost characters."""
    if not s:
        return True
    bad = sum(1 for ch in s if ch == LOST or (ord(ch) < 32 and ch not in "\t\n\r"))
    return bad / len(s) <= max_bad


# ── reading whole files ──────────────────────────────────────────────────────

@dataclass
class Text:
    text: str
    encoding: Encoding
    fixes: list = field(default_factory=list)   # (before, after) lines changed by fix_mojibake
    nfc_changed: bool = False


def read_text(path, max_fixes=200) -> Text:
    """Read a whole text file with correct decoding, repair and NFC normalisation."""
    enc = detect_encoding(path)
    with open(path, "rb") as f:
        raw = f.read()
    text = decode_bytes(raw, enc.encoding).removeprefix("﻿")
    fixes = []
    if MOJIBAKE.search(text):
        out = []
        for line in text.splitlines(keepends=True):
            fixed = fix_mojibake(line)
            if fixed != line and len(fixes) < max_fixes:
                fixes.append((line.rstrip("\r\n"), fixed.rstrip("\r\n")))
            out.append(fixed)
        text = "".join(out)
    nfc = unicodedata.normalize("NFC", text)
    return Text(nfc, enc, fixes, nfc_changed=nfc != text)


def iter_lines(path, enc: Encoding, fixes: list | None = None, max_fixes=200):
    """Stream a text file's lines, decoded, repaired and NFC-normalised.

    For files too big to read at once. Lines changed by the mojibake repair
    are appended to `fixes` as (line_number, before, after).
    """
    with open(path, "rb") as f:
        if enc.encoding.startswith("utf-16"):
            lines = io.TextIOWrapper(f, encoding="utf-16" if enc.encoding == "utf-16" else enc.encoding,
                                     newline="")
        elif enc.encoding == "mixed":
            lines = (decode_line(b) for b in f)
        else:
            codec = "utf-8-sig" if enc.encoding in ("utf-8", "ascii") else "cp1252"
            lines = io.TextIOWrapper(f, encoding=codec, errors="strict" if codec == "utf-8-sig" else "dgi-latin1",
                                     newline="")
        for n, line in enumerate(lines, 1):
            if n == 1:
                line = line.removeprefix("﻿")
            if MOJIBAKE.search(line):
                fixed = fix_mojibake(line)
                if fixed != line and fixes is not None and len(fixes) < max_fixes:
                    fixes.append((n, line.rstrip("\r\n"), fixed.rstrip("\r\n")))
                line = fixed
            elif not line.isascii():
                line = unicodedata.normalize("NFC", line)
            yield line


# ── shapefile .dbf encoding ──────────────────────────────────────────────────

# DBF "language driver" byte (offset 29) → code page, for the values seen in the mirror.
_LDID = {0x01: "CP437", 0x02: "CP850", 0x03: "CP1252", 0x57: "CP1252", 0x58: "CP1252", 0x59: "CP1252"}

_CPG = {"UTF-8": "UTF-8", "UTF8": "UTF-8", "65001": "UTF-8",
        "ANSI 1252": "CP1252", "1252": "CP1252", "CP1252": "CP1252", "WINDOWS-1252": "CP1252",
        "ISO 88591": "ISO-8859-1", "88591": "ISO-8859-1", "ISO-8859-1": "ISO-8859-1",
        "ISO 885915": "ISO-8859-15", "885915": "ISO-8859-15", "ISO-8859-15": "ISO-8859-15"}


def shapefile_encoding(dbf: bytes, cpg: str | None = None) -> tuple[str, str]:
    """The encoding to give GDAL (SHAPE_ENCODING) for a .dbf, and why.

    The text itself decides when it can: valid UTF-8 with non-ASCII bytes is
    UTF-8 whatever the labels say. Otherwise the .cpg file, then the DBF
    language byte, then Windows-1252 (what ArcGIS writes by default). GDAL's
    own default turns "Cionn tSáile" in the CSO's Garda sub-district file into
    "Cionn tS�ile", which is why this exists.
    """
    header_len = int.from_bytes(dbf[8:10], "little")
    body = dbf[header_len:]
    declared = _CPG.get(cpg.strip().upper()) if cpg and cpg.strip() else None
    if _HIGH.search(body):
        try:
            # final=False: the caller may pass only the start of a big .dbf,
            # cut in the middle of a character.
            codecs.getincrementaldecoder("utf-8")().decode(body, final=False)
            return "UTF-8", "content is valid UTF-8"
        except UnicodeDecodeError:
            if declared and declared != "UTF-8":
                return declared, f".cpg says {cpg.strip()}"
            ldid = _LDID.get(dbf[29]) if len(dbf) > 29 else None
            if ldid:
                return ldid, f"language byte 0x{dbf[29]:02x}"
            return "CP1252", "not UTF-8, no usable label: assumed Windows-1252"
    return declared or "UTF-8", "ASCII only" + (f" (.cpg says {cpg.strip()})" if cpg else "")
