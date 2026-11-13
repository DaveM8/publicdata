"""Irish characters must come out right.

The first group uses small made-up inputs. The second uses real mirror files
found on 13 Oct 2026, each with a known problem, and checks the exact
strings; those are skipped if the mirror isn't mounted.
"""

import subprocess
import zipfile
from pathlib import Path

import pytest

from dgi_explore import textio

MIRROR = Path("/mnt/storage/datagovie_mirror/files")


# ── made-up inputs ───────────────────────────────────────────────────────────

def write(tmp_path, data: bytes, name="f.csv"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_utf8(tmp_path):
    p = write(tmp_path, "Síochána,Páirc an Chrócaigh\n".encode())
    t = textio.read_text(p)
    assert t.encoding.encoding == "utf-8"
    assert t.text == "Síochána,Páirc an Chrócaigh\n"


def test_utf8_bom_dropped(tmp_path):
    p = write(tmp_path, b"\xef\xbb\xbf" + "Ainm,Contae\nÁras,Corcaigh\n".encode())
    t = textio.read_text(p)
    assert t.encoding.had_bom
    assert t.text.startswith("Ainm,")            # no ﻿ glued to the first column


def test_cp1252(tmp_path):
    p = write(tmp_path, "Bord Gáis,Mary’s Street,Áras\n".encode("cp1252"))
    t = textio.read_text(p)
    assert t.encoding.encoding == "cp1252"
    assert t.text == "Bord Gáis,Mary’s Street,Áras\n"   # ’ kept as published


def test_cp1252_only_late_in_file(tmp_path):
    p = write(tmp_path, b"a,b\n" * 2_000_000 + "Cionn tSáile\n".encode("cp1252"))
    assert textio.detect_encoding(p).encoding == "cp1252"


def test_mixed_line_by_line(tmp_path):
    p = write(tmp_path, "Dún Laoghaire\n".encode() + "Bord Gáis\n".encode("cp1252")
              + "Síochána\n".encode())
    t = textio.read_text(p)
    assert t.encoding.encoding == "mixed"
    assert t.text == "Dún Laoghaire\nBord Gáis\nSíochána\n"


def test_double_encoded_repaired_and_reported(tmp_path):
    bad = "Garda Síochána,Páirc an Chrócaigh,St. Catherine’s".encode().decode("cp1252").encode()
    p = write(tmp_path, b"x\n" + bad + b"\n")
    t = textio.read_text(p)
    assert "Garda Síochána,Páirc an Chrócaigh,St. Catherine’s" in t.text
    assert len(t.fixes) == 1 and "Ã" in t.fixes[0][0]


def test_clean_text_left_alone():
    for s in ["Síochána", "naïve café", "Ó Súilleabháin", "50 µg/m³", "€1,000", "Mary’s"]:
        assert textio.clean(s) == s


def test_nfc(tmp_path):
    p = write(tmp_path, "Síochána\n".encode())       # i + combining acute
    t = textio.read_text(p)
    assert t.text == "Síochána\n" and t.nfc_changed


def test_utf16(tmp_path):
    p = write(tmp_path, "Ainm\tÁit\nÓ Néill\tCiarraí\n".encode("utf-16"))
    assert textio.sniff_type(p.read_bytes()[:4096]) == "utf16-text"
    assert textio.read_text(p).text == "Ainm\tÁit\nÓ Néill\tCiarraí\n"


def test_search_key():
    assert textio.search_key("Garda SÍOCHÁNA") == textio.search_key("garda siochana")
    assert textio.search_key("Mary’s") == "mary's"
    assert textio.search_key("PÃ¡irc an ChrÃ³caigh") == "pairc an chrocaigh"


@pytest.mark.parametrize("head,kind", [
    (b"PK\x03\x04rest", "zip"), (b"%PDF-1.7", "pdf"), (b"\xd0\xcf\x11\xe0\xa1", "ole"),
    (b"\xef\xbb\xbf<!DOCTYPE html><html>", "html"), (b"  <html lang=en>", "html"),
    (b'<?xml version="1.0"?><kml>', "xml"), (b'{"version":"2.0"}', "json"),
    (b"SQLite format 3\x00", "sqlite"), (b"a,b,c\n1,2,3\n", "text"), (b"", "empty"),
])
def test_sniff_type(head, kind):
    assert textio.sniff_type(head) == kind


def dbf(records: bytes, ldid=0x00):
    header = bytearray(32)
    header[8:10] = (32).to_bytes(2, "little")
    header[29] = ldid
    return bytes(header) + records


def test_shapefile_encoding_rules():
    assert textio.shapefile_encoding(dbf("Cionn tSáile".encode("cp1252")))[0] == "CP1252"
    assert textio.shapefile_encoding(dbf("Cionn tSáile".encode()))[0] == "UTF-8"
    assert textio.shapefile_encoding(dbf("Cionn tSáile".encode("cp1252")), "UTF-8")[0] == "CP1252"
    assert textio.shapefile_encoding(dbf("Áras".encode("iso-8859-15")), "ISO 885915")[0] == "ISO-8859-15"
    assert textio.shapefile_encoding(dbf("Árá".encode()[:-1]))[0] == "UTF-8"   # cut mid-character


# ── real mirror files ────────────────────────────────────────────────────────

needs_mirror = pytest.mark.skipif(not MIRROR.is_dir(), reason="mirror not mounted")


@needs_mirror
@pytest.mark.parametrize("rel,encoding,must_contain", [
    ("Competition_and_Consumer_Protection_Commission/Authorised_Credit_Intermediaries_632ee6f0/"
     "5338e3dd_2026-09-30-authorised-credit-intermediaries-list.csv",
     "cp1252", ["Bord Gáis Energy", "Áras Smith O'brien", "St Mary’S Street"]),
    ("Dublin_City_Council/Museums_and_Archives_DCC_078cf66c/"
     "90145fe4_opendata-publication-checklistv0.1.xlsx-dataset-museums-and-archives.csv",
     "utf-8", ["Garda Siochána Museum", "An Garda Síochána"]),
    ("Dublin_City_Council/Museums_and_Archives_DCC_078cf66c/"
     "e666b0fc_opendata-publication-checklistv0.1.xlsx-dataset-museums-and-archives.geojson",
     "utf-8", ["Garda Siochána Museum"]),
    ("Dublin_City_Council/Places_of_worship_DCC_7d2499cc/26fabb5a_dataset-places-of-worship-12042024.csv",
     "utf-8", ["St. Catherine’s Church"]),
    ("National_Museum_of_Ireland/NMI_Natural_History_Mammal_Collection_608822e3/4bc543ad_NH_Mammalia.csv",
     "utf-16", []),
    ("ESB/ESB_EV_Public_Charging_Network_2f2db759/6fae1602_its-data-ecars-sites-roi-ni.csv",
     "utf-8", []),
])
def test_real_file(rel, encoding, must_contain):
    t = textio.read_text(MIRROR / rel)
    assert t.encoding.encoding == encoding
    for s in must_contain:
        assert s in t.text, s
    for bad in ("Ã¡", "Ã­", "â€™", "�", "﻿"):
        assert bad not in t.text, f"{bad!r} left in {rel}"


@needs_mirror
def test_real_double_encoded_fixes_logged():
    t = textio.read_text(MIRROR / "Dublin_City_Council/Places_of_worship_DCC_7d2499cc/"
                                  "26fabb5a_dataset-places-of-worship-12042024.csv")
    assert any("â€™" in before and "’" in after for before, after in t.fixes)


@needs_mirror
def test_real_shapefile_cionn_tsaile():
    """CSO Garda sub-districts: Windows-1252, no .cpg, language byte 0x00."""
    z = MIRROR / "Central_Statistics_Office/unnamed_130705f9/5933c585_Garda_Sub_Districts.zip"
    member = "Garda Sub Districts/Census2011_Garda_Subdistricts_Nov2013"
    data = zipfile.ZipFile(z).read(member + ".dbf")
    enc, why = textio.shapefile_encoding(data)
    assert enc == "CP1252", why
    out = subprocess.run(["ogrinfo", "--config", "SHAPE_ENCODING", enc, "-ro", "-al", "-q",
                          f"/vsizip/{z}/{member}.shp"], capture_output=True, text=True).stdout
    assert "Cionn tSáile" in out and "�" not in out


# ── letters lost before publication (U+FFFD in the source file) ──────────────

KNOWN = ["Fianna Fáil", "Dún Laoghaire-Rathdown", "Túath Housing", "An Cabhán", "Muineachán",
         "Áras an Uachtaráin", "Clúid Housing", "Fáil", "Dún", "Túath", "Cabhán", "Muineachán", "Áras", "Clúid"]


def test_restore_unambiguous_words():
    r = textio.Restorer(KNOWN, min_count=1)
    fixes = []
    assert r.restore("Fianna F�il", fixes) == "Fianna Fáil"
    assert r.restore("D�n Laoghaire-Rathdown") == "Dún Laoghaire-Rathdown"
    assert r.restore("An Cabh�n-Muineach�n") == "An Cabhán-Muineachán"
    assert r.restore("�ras") == "Áras"
    assert r.restore("D�N") == "DÚN"
    assert fixes == [("Fianna F�il", "Fianna Fáil")]


def test_restore_leaves_ambiguous_and_unknown():
    r = textio.Restorer(["Cáca", "Cóca"], min_count=1)
    assert r.restore("C�ca") == "C�ca"            # two candidates: left as published
    assert r.restore("Xyz�q") == "Xyz�q"


def test_restore_degrees():
    r = textio.Restorer([], min_count=1)
    assert r.restore("53�51.489'N, 6�15'W") == "53°51.489'N, 6°15'W"


def test_printable():
    assert textio.printable("Túath Housing")
    assert not textio.printable("?�9L�\x01��sb\x02�|�")


def test_restore_o_before_name():
    r = textio.Restorer([], min_count=1)
    assert r.restore("Kildare North � Riain S") == "Kildare North Ó Riain S"
    assert r.restore("a � b") == "a � b"            # not before a capitalised name: left


def test_restore_possessive_and_seed_names():
    r = textio.Restorer([], min_count=1)
    assert r.restore("St Laurence�s Hospital") == "St Laurence’s Hospital"
    assert r.restore("�'Snodaigh") == "Ó'Snodaigh"
    assert r.restore("�amon � Cu�v") == "Éamon Ó Cuív"
    assert r.restore("R�is�n") == "Róisín"


def test_restore_across_apostrophe():
    r = textio.Restorer([], min_count=1)
    assert r.restore("�'R�ord�in") == "Ó'Ríordáin"
