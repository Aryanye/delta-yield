"""Build the tradable stock-FnO universe from the Kite instruments dump.

Stocks only. Every index (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50,
SENSEX, BANKEX, ...) is excluded, both by name and by a positive check that the
underlying has an NSE equity listing.

The instruments dump is a public endpoint, so this module works without a
session token -- handy for validating the universe outside market hours.
"""
from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests

import config

NFO_URL = "https://api.kite.trade/instruments/NFO"
NSE_URL = "https://api.kite.trade/instruments/NSE"
CACHE_NFO = config.DATA_DIR / "instruments_nfo.csv"
CACHE_NSE = config.DATA_DIR / "instruments_nse.csv"
CACHE_TTL_SECONDS = 6 * 3600


@dataclass(frozen=True)
class Contract:
    """One option contract."""
    token: int
    tradingsymbol: str
    name: str            # underlying, e.g. HDFCBANK
    expiry: date
    strike: float
    opt_type: str        # CE | PE
    lot_size: int

    @property
    def key(self) -> str:
        return f"NFO:{self.tradingsymbol}"


@dataclass(frozen=True)
class Future:
    token: int
    tradingsymbol: str
    name: str
    expiry: date
    lot_size: int

    @property
    def key(self) -> str:
        return f"NFO:{self.tradingsymbol}"


@dataclass
class Universe:
    stocks: List[str]                          # underlying names, sorted
    expiries: List[date]                       # nearest-first, NUM_EXPIRIES long
    options: List[Contract]
    futures: Dict[tuple, Future]               # (name, expiry) -> Future
    equity_token: Dict[str, int]               # name -> NSE equity token
    company: Dict[str, str]                    # name -> company name (for search)
    lot_size: Dict[tuple, int]                 # (name, expiry) -> lot size
    built_at: datetime

    def options_for(self, name: str, expiry: date) -> List[Contract]:
        return [c for c in self.options if c.name == name and c.expiry == expiry]


def _download(url: str, cache: Path, force: bool = False) -> str:
    """Fetch an instruments dump, using a short-lived on-disk cache."""
    if not force and cache.exists():
        age = time.time() - cache.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            return cache.read_text()
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    cache.write_text(resp.text)
    return resp.text


def _parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def build_universe(force_refresh: bool = False,
                   only: Optional[List[str]] = None) -> Universe:
    """Return the stocks-only FnO universe for the nearest NUM_EXPIRIES expiries."""
    nfo_rows = list(csv.DictReader(io.StringIO(_download(NFO_URL, CACHE_NFO, force_refresh))))
    nse_rows = list(csv.DictReader(io.StringIO(_download(NSE_URL, CACHE_NSE, force_refresh))))

    # Positive stock check: the underlying must exist as an NSE equity share.
    # This is what keeps indices out even if NSE lists a new index we have not
    # enumerated in config.INDEX_NAMES.
    equity_token: Dict[str, int] = {}
    company_name: Dict[str, str] = {}
    for row in nse_rows:
        if row.get("segment") == "NSE" and row.get("instrument_type") == "EQ":
            equity_token[row["tradingsymbol"]] = int(row["instrument_token"])
            company_name[row["tradingsymbol"]] = (row.get("name") or "").strip()

    def is_stock(name: str) -> bool:
        return name not in config.INDEX_NAMES and name in equity_token

    today = date.today()

    # Expiries: only those with stock options, nearest first, not yet expired.
    expiry_set = set()
    for row in nfo_rows:
        if row["instrument_type"] in ("CE", "PE") and is_stock(row["name"]):
            exp = _parse_date(row["expiry"])
            if exp and exp >= today:
                expiry_set.add(exp)
    expiries = sorted(expiry_set)[: config.NUM_EXPIRIES]
    keep_expiries = set(expiries)

    options: List[Contract] = []
    futures: Dict[tuple, Future] = {}
    lot_size: Dict[tuple, int] = {}
    stocks = set()

    for row in nfo_rows:
        name = row["name"]
        if not is_stock(name):
            continue
        if only and name not in only:
            continue
        exp = _parse_date(row["expiry"])
        if exp not in keep_expiries:
            continue
        itype = row["instrument_type"]
        lot = int(row["lot_size"])

        if itype in ("CE", "PE"):
            options.append(Contract(
                token=int(row["instrument_token"]),
                tradingsymbol=row["tradingsymbol"],
                name=name,
                expiry=exp,
                strike=float(row["strike"]),
                opt_type=itype,
                lot_size=lot,
            ))
            lot_size[(name, exp)] = lot
            stocks.add(name)
        elif itype == "FUT":
            futures[(name, exp)] = Future(
                token=int(row["instrument_token"]),
                tradingsymbol=row["tradingsymbol"],
                name=name,
                expiry=exp,
                lot_size=lot,
            )

    return Universe(
        stocks=sorted(stocks),
        expiries=expiries,
        options=options,
        futures=futures,
        equity_token={k: v for k, v in equity_token.items() if k in stocks},
        company={k: v for k, v in company_name.items() if k in stocks},
        lot_size=lot_size,
        built_at=datetime.now(),
    )


if __name__ == "__main__":
    u = build_universe()
    print(f"stocks           : {len(u.stocks)}")
    print(f"expiries         : {[e.isoformat() for e in u.expiries]}")
    print(f"option contracts : {len(u.options)}")
    print(f"futures          : {len(u.futures)}")
    missing_fut = [(n, e) for n in u.stocks for e in u.expiries if (n, e) not in u.futures]
    print(f"missing futures  : {len(missing_fut)} {missing_fut[:5]}")
    leaked = [s for s in u.stocks if s in config.INDEX_NAMES]
    print(f"index leakage    : {len(leaked)} {leaked}")
