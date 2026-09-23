"""Parse the extractor's free-text commodity string into the symbol array the dashboard filters on.

One implementation, shared by scripts/add_one_to_lode.py and scripts/load_royalties.py, which each
had their own copy. The old version dropped 42% of EDGAR's commodity strings and 15% of the pilot's
(438 of 1907 stored rows ended up with an empty array, and an empty array is excluded by every
commodity filter in the LODE board — including "Other"), because it only matched single bare words
against a 20-entry map and then fell back to "1-4 chars starting uppercase". That fallback also let
junk through: `Ag)`, `MOP)`, `Ru)` and `VHM)` are all in the database as if they were commodities.

What changed: parentheses are separators rather than kept text, hyphens are folded to spaces, every
known name inside a token is matched (longest first) instead of only an exact whole-string match, and
an unrecognised token is only accepted if it is a real symbol.
"""
from __future__ import annotations

import re

# name (lowercase) -> stored symbol. Ordered longest-first at match time, so "iron ore" wins over "iron".
NAME2SYM: dict[str, str] = {
    "gold": "Au", "silver": "Ag",
    "copper": "Cu", "lead": "Pb", "zinc": "Zn", "nickel": "Ni", "tin": "Sn",
    "aluminium": "Al", "aluminum": "Al", "bauxite": "Al",
    "iron": "Fe", "iron ore": "Fe", "taconite": "Fe", "magnetite": "Fe", "hematite": "Fe",
    "manganese": "Mn", "chromium": "Cr", "chromite": "Cr",
    "molybdenum": "Mo", "moly": "Mo", "tungsten": "W", "scheelite": "W", "wolframite": "W",
    "vanadium": "V", "niobium": "Nb", "tantalum": "Ta", "titanium": "Ti",
    "ilmenite": "Ti", "rutile": "Ti", "zircon": "Zr", "zirconium": "Zr",
    "lithium": "Li", "spodumene": "Li", "cobalt": "Co", "graphite": "C",
    "uranium": "U", "coal": "Coal", "metallurgical coal": "Coal", "thermal coal": "Coal",
    "anthracite": "Coal", "lignite": "Coal",
    "platinum": "Pt", "palladium": "Pd", "rhodium": "Rh", "ruthenium": "Ru",
    "iridium": "Ir", "osmium": "Os",
    "platinum group": "PGE", "platinum group metals": "PGE", "platinum group elements": "PGE",
    "pge": "PGE", "pgm": "PGE", "pgms": "PGE",
    "rare earth": "REE", "rare earths": "REE", "rare earth elements": "REE", "ree": "REE",
    "monazite": "REE", "neodymium": "REE", "praseodymium": "REE", "dysprosium": "REE",
    "potash": "Potash", "sylvite": "Potash", "muriate of potash": "Potash",
    "phosphate": "Phosphate", "phosphate rock": "Phosphate",
    "bromine": "Br", "iodine": "I", "nitrate": "Nitrate", "nitrates": "Nitrate",
    "silica": "Silica", "silica sand": "Silica", "frac sand": "Silica", "quartz": "Silica",
    "salt": "Salt", "halite": "Salt", "gypsum": "Gypsum",
    "borate": "B", "borates": "B", "boron": "B",
    "sulphur": "S", "sulfur": "S", "magnesium": "Mg", "magnesite": "Mg",
    "antimony": "Sb", "bismuth": "Bi", "beryllium": "Be", "tellurium": "Te",
    "selenium": "Se", "germanium": "Ge", "gallium": "Ga", "indium": "In", "cadmium": "Cd",
    "mercury": "Hg", "scandium": "Sc", "caesium": "Cs", "cesium": "Cs", "rubidium": "Rb",
    "diamond": "Diamond", "diamonds": "Diamond",
    "limestone": "Limestone", "barite": "Barite", "barytes": "Barite", "fluorspar": "Fluorspar",
    "trona": "Trona", "soda ash": "Trona", "diatomaceous earth": "Diatomite",
    "diatomite": "Diatomite", "zeolite": "Zeolite", "perlite": "Perlite", "talc": "Talc",
    "kaolin": "Kaolin", "bentonite": "Bentonite", "dolomite": "Dolomite", "coquina": "Limestone",
    "aggregate": "Aggregate", "gravel": "Aggregate", "sand": "Aggregate",
}

# Tokens that are already symbols. Nothing outside this set is accepted as one, which is what keeps
# 'U3O8', 'MOP', 'VHM' and stray ')' fragments out of the array.
SYMBOLS: set[str] = {
    "Au", "Ag", "Cu", "Pb", "Zn", "Ni", "Sn", "Al", "Fe", "Mn", "Cr", "Mo", "W", "V", "Nb", "Ta",
    "Ti", "Zr", "Li", "Co", "C", "U", "Br", "I", "S", "Mg", "Sb", "Bi", "Be", "Te", "B", "K", "P",
    "Pt", "Pd", "Rh", "Ru", "Ir", "Os", "Se", "Ge", "Ga", "In", "Cd", "Hg", "Sc", "Cs", "Rb", "Y",
    "PGE", "PGM", "PGMs", "REE",
}
_SYMBOL_ALIAS = {"PGM": "PGE", "PGMs": "PGE"}

_SPLIT = re.compile(r"[,/&+;()\[\]]|\band\b|\bwith\b", re.I)
_NAMES_LONGEST_FIRST = sorted(NAME2SYM, key=len, reverse=True)


def commodities(s: str | None) -> list[str]:
    """Free-text commodity string -> de-duplicated symbol list, order of first appearance."""
    out: list[str] = []
    for raw in _SPLIT.split(s or ""):
        tok = re.sub(r"\s+", " ", raw.replace("-", " ")).strip()
        if not tok:
            continue
        # every known name inside the token, longest first; blank each match so "iron ore" does not
        # also register as "iron"
        hay = tok.lower()
        hit = False
        for name in _NAMES_LONGEST_FIRST:
            m = re.search(rf"\b{re.escape(name)}\b", hay)
            if m:
                out.append(NAME2SYM[name])
                hay = hay[:m.start()] + " " * (m.end() - m.start()) + hay[m.end():]
                hit = True
        if hit:
            continue
        # not a name: accept only a genuine symbol, normalising case (au -> Au)
        cand = tok.title() if len(tok) > 1 else tok.upper()
        for form in (tok, cand, tok.upper(), cand.replace(" ", "")):
            if form in SYMBOLS:
                out.append(_SYMBOL_ALIAS.get(form, form))
                break
    seen: list[str] = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen
