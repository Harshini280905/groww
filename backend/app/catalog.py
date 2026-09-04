"""Instrument catalog — the single source of truth for what this app can resolve.

Two jobs:
  1. Search. A user typing "HDFC" means HDFCBANK; typing "tata" should
     surface TCS, TATAMOTORS and TATASTEEL. Making them guess the exact NSE
     ticker is a bad experience and was a real complaint.
  2. Symbol → BSE scrip code. BSE identifies instruments numerically
     (RELIANCE = 500325), so the mapping has to live somewhere; keeping it
     HERE rather than inside the BSE adapter means search and resolution
     can never drift apart. If it's searchable, it's resolvable.

Deliberately limited to instruments whose BSE scrip code is verified —
inventing scrip codes would produce symbols that autocomplete happily and
then fail to fetch, which is worse than not offering them. A production
build would seed this from BSE's own listed-securities endpoint at startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Instrument:
    symbol: str        # NSE ticker
    name: str          # Company name, as a person would say it
    bse_scrip: str     # BSE numeric scrip code
    sector: str


CATALOG: tuple[Instrument, ...] = (
    Instrument("RELIANCE",   "Reliance Industries",            "500325", "Energy"),
    Instrument("TCS",        "Tata Consultancy Services",      "532540", "IT"),
    Instrument("INFY",       "Infosys",                        "500209", "IT"),
    Instrument("HDFCBANK",   "HDFC Bank",                      "500180", "Banking"),
    Instrument("ICICIBANK",  "ICICI Bank",                     "532174", "Banking"),
    Instrument("SBIN",       "State Bank of India",            "500112", "Banking"),
    Instrument("ITC",        "ITC",                            "500875", "FMCG"),
    Instrument("BHARTIARTL", "Bharti Airtel",                  "532454", "Telecom"),
    Instrument("LT",         "Larsen & Toubro",                "500510", "Infrastructure"),
    Instrument("KOTAKBANK",  "Kotak Mahindra Bank",            "500247", "Banking"),
    Instrument("HINDUNILVR", "Hindustan Unilever",             "500696", "FMCG"),
    Instrument("AXISBANK",   "Axis Bank",                      "532215", "Banking"),
    Instrument("MARUTI",     "Maruti Suzuki India",            "532500", "Auto"),
    Instrument("ASIANPAINT", "Asian Paints",                   "500820", "Consumer"),
    Instrument("BAJFINANCE", "Bajaj Finance",                  "500034", "Financials"),
    Instrument("SUNPHARMA",  "Sun Pharmaceutical Industries",  "524715", "Pharma"),
    Instrument("NESTLEIND",  "Nestle India",                   "500790", "FMCG"),
    Instrument("WIPRO",      "Wipro",                          "507685", "IT"),
    Instrument("ONGC",       "Oil & Natural Gas Corporation",  "500312", "Energy"),
    Instrument("NTPC",       "NTPC",                           "532555", "Power"),
    Instrument("POWERGRID",  "Power Grid Corporation",         "532898", "Power"),
    Instrument("TATAMOTORS", "Tata Motors",                    "500570", "Auto"),
    Instrument("TATASTEEL",  "Tata Steel",                     "500470", "Metals"),
    Instrument("M&M",        "Mahindra & Mahindra",            "500520", "Auto"),
    Instrument("HCLTECH",    "HCL Technologies",               "532281", "IT"),
    Instrument("TITAN",      "Titan Company",                  "500114", "Consumer"),
    Instrument("ULTRACEMCO", "UltraTech Cement",               "532538", "Cement"),
    Instrument("COALINDIA",  "Coal India",                     "533278", "Mining"),
    Instrument("ADANIENT",   "Adani Enterprises",              "512599", "Conglomerate"),
    Instrument("JSWSTEEL",   "JSW Steel",                      "500228", "Metals"),
    Instrument("BAJAJFINSV", "Bajaj Finserv",                  "532978", "Financials"),
    Instrument("TECHM",      "Tech Mahindra",                  "532755", "IT"),
    Instrument("GRASIM",     "Grasim Industries",              "500300", "Cement"),
    Instrument("INDUSINDBK", "IndusInd Bank",                  "532187", "Banking"),
    Instrument("DRREDDY",    "Dr. Reddy's Laboratories",       "500124", "Pharma"),
    Instrument("CIPLA",      "Cipla",                          "500087", "Pharma"),
    Instrument("EICHERMOT",  "Eicher Motors",                  "505200", "Auto"),
    Instrument("HEROMOTOCO", "Hero MotoCorp",                  "500182", "Auto"),
    Instrument("DIVISLAB",   "Divi's Laboratories",            "532488", "Pharma"),
    Instrument("BPCL",       "Bharat Petroleum Corporation",   "500547", "Energy"),
)

_BY_SYMBOL: dict[str, Instrument] = {i.symbol: i for i in CATALOG}


def get(symbol: str) -> Optional[Instrument]:
    return _BY_SYMBOL.get(symbol.upper().strip())


def get_scrip_code(symbol: str) -> Optional[str]:
    inst = get(symbol)
    return inst.bse_scrip if inst else None


def display_name(symbol: str) -> Optional[str]:
    inst = get(symbol)
    return inst.name if inst else None


def search(query: str, limit: int = 8) -> list[Instrument]:
    """Rank matches so the obvious answer comes first.

    Tiers, best to worst:
      0  exact ticker            "TCS"   -> TCS
      1  ticker starts with q    "HDFC"  -> HDFCBANK
      2  company name starts     "infos" -> INFY
      3  a name word starts      "bank"  -> HDFCBANK, ICICIBANK, ...
      4  substring anywhere      "tata"  -> TCS (Tata Consultancy), TATAMOTORS
      5  sector match            "pharma"-> SUNPHARMA, CIPLA, ...
    """
    q = (query or "").strip().upper()
    if not q:
        return []

    scored: list[tuple[int, str, Instrument]] = []
    for inst in CATALOG:
        sym = inst.symbol.upper()
        name = inst.name.upper()
        sector = inst.sector.upper()

        if sym == q:
            rank = 0
        elif sym.startswith(q):
            rank = 1
        elif name.startswith(q):
            rank = 2
        elif any(word.startswith(q) for word in name.replace("&", " ").split()):
            rank = 3
        elif q in sym or q in name:
            rank = 4
        elif sector.startswith(q):
            rank = 5
        else:
            continue
        # Secondary sort by symbol keeps results stable and predictable.
        scored.append((rank, sym, inst))

    scored.sort(key=lambda t: (t[0], t[1]))
    return [inst for _, _, inst in scored[:limit]]
