import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


@lru_cache
def load(name: str) -> dict:
    return json.loads((DATA / f"{name}.json").read_text())


def cfg() -> dict:
    return load("config")


def region_of(country: str | None) -> str | None:
    if not country:
        return None
    for region, countries in cfg()["regions"].items():
        if country.upper() in countries:
            return region
    return None


def ae_for(region: str | None) -> dict:
    owners = cfg()["owners"]["ae_by_region"]
    return owners.get(region or "", owners["default"])


def sdr() -> dict:
    return cfg()["owners"]["sdr"]
