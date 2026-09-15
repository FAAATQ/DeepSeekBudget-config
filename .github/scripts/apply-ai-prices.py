#!/usr/bin/env python3
"""Write `deepseek.json` from a model's reading of the two pricing pages - or refuse and say why.

Why this exists
---------------

`sync-prices.py` reads the pages with anchored regexes. That works, and it fails loudly rather
than guessing - but every anchor it relies on ("1M INPUT TOKENS (CACHE HIT)", an OFF-PEAK row
followed by a PEAK row, the last two cells of each) is a place where DeepSeek can restyle the
page and stop the sync. Not with a wrong number: with no number at all, until somebody rewrites
the parser.

A model reading the same page does not care about the layout. What it cannot be trusted with is
*inventing* a figure. A language model that misreads a cell produces a number that looks exactly
as plausible as a correct one, and it does not fail. So this script stands between the model's
reading and the file, and holds two lines:

**Grounding.** Every number the model reports must appear as a number on the raw page it claims
to have read from. A fabricated figure cannot pass, because the page has to contain it. The check
runs against the page text, never against anything else the model produced.

**A second, independent reading.** Grounding catches a number that is not on the page. It cannot
catch a number that IS on the page but in the wrong slot - swapping the two models' columns
passes grounding completely. So `sync-prices.py` parses the same pages with regexes and the two
readings must agree, field for field.

When the parser cannot run - the page was restyled, which is the whole reason for this path - the
comparison is skipped and the caller is told, through the exit code, that only one reader ran.
The numbers are still grounded; what is lost is the slot check, and a human reading the diff is
the replacement.

Usage:
    python3 .github/scripts/apply-ai-prices.py \\
        --facts facts.json --page-usd page-en.html --page-cny page-zh.html

Exit codes:
    0  a config was written (or nothing moved)
    1  refused - the reading cannot be trusted, nothing was written
    2  refused, and the parser could not run either, so only one reader saw the page
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CURRENCIES = ("USD", "CNY")
KEYS = ("inputCacheHit", "inputCacheMiss", "output")
TIERS = ("offPeak", "peak")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
TIME = re.compile(r"\d{2}:\d{2}\Z")


class Refused(Exception):
    """The model's reading cannot be trusted. Nothing is written."""


def load_parser():
    spec = importlib.util.spec_from_file_location("sync_prices", HERE / "sync-prices.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def flatten(page: str) -> str:
    """The page as the model sees it: tags stripped, whitespace collapsed.

    The model is handed this form, so grounding has to run against the same form. Taking the raw
    HTML would also work - the digits survive - but then a number living in an attribute or an
    inline script would count as "printed on the page", which is a wider door than intended.
    """
    import html as html_module

    return re.sub(r"\s+", " ", html_module.unescape(re.sub(r"<[^>]+>", " ", page)))


def numeric_tokens(text: str) -> set[float]:
    """Every number printed anywhere in the page, as a float.

    Compared as numbers rather than as text because the two representations genuinely differ:
    the page prints `27` where the config writes `27.0`. A textual check would report a
    perfectly correct reading as a fabrication.
    """
    return {float(token) for token in re.findall(r"\d+(?:\.\d+)?", text)}


def check_shape(facts: object) -> None:
    if not isinstance(facts, dict):
        raise Refused(f"the model's answer is not a JSON object: {type(facts).__name__}")

    schedule = facts.get("schedule")
    if not isinstance(schedule, dict):
        raise Refused("the answer has no `schedule` object")

    days = schedule.get("days")
    if not isinstance(days, list) or not days:
        raise Refused("`schedule.days` is missing or empty")
    unknown = [d for d in days if d not in DAYS]
    if unknown:
        raise Refused(f"`schedule.days` contains {unknown}, which are not weekday abbreviations")

    windows = schedule.get("windows")
    if not isinstance(windows, list) or not windows:
        raise Refused("`schedule.windows` is missing or empty")
    for pair in windows:
        ok = (
            isinstance(pair, list)
            and len(pair) == 2
            and all(isinstance(x, str) and TIME.match(x) for x in pair)
        )
        if not ok:
            raise Refused(f"`schedule.windows` has a malformed entry: {pair!r}")

    models = facts.get("models")
    if not isinstance(models, list) or not models:
        raise Refused("the answer has no `models` array")
    seen = set()
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"]:
            raise Refused(f"a model entry has no id: {model!r}")
        if model["id"] in seen:
            raise Refused(f"{model['id']} is listed twice")
        seen.add(model["id"])
        for currency in CURRENCIES:
            block = model.get(currency)
            if not isinstance(block, dict):
                raise Refused(f"{model['id']}: no {currency} prices")
            for key in KEYS:
                triple = block.get(key)
                if not isinstance(triple, dict):
                    raise Refused(f"{model['id']}/{currency}: no `{key}`")
                for tier in TIERS:
                    value = triple.get(tier)
                    # `bool` is an `int` in Python; a stray `true` must not read as 1.
                    if not isinstance(value, (int, float)) or isinstance(value, bool):
                        raise Refused(
                            f"{model['id']}/{currency}/{key}.{tier} is not a number: {value!r}"
                        )


def check_grounding(facts: dict, page_by_currency: dict[str, str]) -> None:
    printed = {
        currency: numeric_tokens(flatten(page)) for currency, page in page_by_currency.items()
    }
    absent = []
    for model in facts["models"]:
        for currency in CURRENCIES:
            for key in KEYS:
                for tier in TIERS:
                    value = float(model[currency][key][tier])
                    if value not in printed[currency]:
                        absent.append(f"{model['id']} {currency} {key}.{tier} = {value}")
    if absent:
        raise Refused(
            "these numbers are not printed on the page they were said to come from, so they "
            "were not read - they were supplied:\n    " + "\n    ".join(absent)
        )


def as_by_model(facts: dict) -> dict:
    """Reshape into the layout `sync_prices.rewrite` edits: model -> currency -> key -> bands.

    Every model is given the *same* band lists, each holding one figure per model in the page's
    column order. That is not a redundancy - `rewrite` looks up `list(by_model).index(model)` and
    uses that position to pick from `block['offPeak']`, which is how the parser's output is
    shaped too. Giving each model a single-entry list of its own values reads as the obvious
    thing and is wrong: the second model then indexes past the end.
    """
    ids = [model["id"] for model in facts["models"]]
    by_model = {mid: {currency: {} for currency in CURRENCIES} for mid in ids}
    for currency in CURRENCIES:
        for key in KEYS:
            for tier in TIERS:
                column = [float(model[currency][key][tier]) for model in facts["models"]]
                for mid in ids:
                    by_model[mid][currency].setdefault(key, {})[tier] = list(column)
    return by_model


def compare_with_parser(facts: dict, parser_by_model: dict) -> list[str]:
    """Return the disagreements between the two readings. Empty means they agree."""
    ids = [model["id"] for model in facts["models"]]
    for currency, parsed in parser_by_model.items():
        for key in KEYS:
            for tier in TIERS:
                if len(parsed[key][tier]) != len(ids):
                    raise Refused(
                        f"the parser found {len(parsed[key][tier])} {currency} {key} {tier} "
                        f"figures but the model listed {len(ids)} models"
                    )

    disagreements = []
    for index, model in enumerate(facts["models"]):
        for currency, parsed in parser_by_model.items():
            for key in KEYS:
                for tier in TIERS:
                    mine = float(model[currency][key][tier])
                    theirs = float(parsed[key][tier][index])
                    if mine != theirs:
                        disagreements.append(
                            f"{model['id']} {currency} {key}.{tier}: "
                            f"model read {mine}, the parser read {theirs}"
                        )
    return disagreements


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts", default="facts.json")
    # The raw HTML, not the flattened text: the parser needs the table elements, and grounding
    # flattens internally so both readers are looking at the same page.
    ap.add_argument("--page-usd", default="page-en.html")
    ap.add_argument("--page-cny", default="page-zh.html")
    ap.add_argument("--config", default="deepseek.json")
    ap.add_argument("--pr-body", default="PR_BODY.md")
    args = ap.parse_args()

    page_by_currency = {
        "USD": Path(args.page_usd).read_text(encoding="utf-8"),
        "CNY": Path(args.page_cny).read_text(encoding="utf-8"),
    }

    try:
        facts = json.loads(Path(args.facts).read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"::error::the model returned nothing usable ({exc})", file=sys.stderr)
        return 1

    parser = load_parser()

    try:
        check_shape(facts)
        check_grounding(facts, page_by_currency)
    except Refused as exc:
        print(f"::error::refusing to write the config. {exc}", file=sys.stderr)
        return 1

    # The second reader. It is allowed to fail - a restyled page is the whole reason this path
    # exists - but that failure is reported rather than swallowed, because it means the slot
    # check did not happen.
    try:
        parser_by_model = {
            currency: parser.parse_prices(page, currency)
            for currency, page in page_by_currency.items()
        }
    except AssertionError as exc:
        parser_by_model = None
        print(
            f"::warning::the regex parser could not read the page ({exc}). Only the model read "
            f"it this run: every number below is grounded in the page text, but nothing "
            f"independently confirmed which column it came from. Review the diff closely."
        )

    try:
        if parser_by_model is None:
            windows = facts["schedule"]["windows"]
        else:
            disagreements = compare_with_parser(facts, parser_by_model)
            if disagreements:
                raise Refused(
                    "the model and the regex parser read the same page differently:\n    "
                    + "\n    ".join(disagreements)
                )
            windows = parser.parse_windows(page_by_currency["USD"])

        by_model = as_by_model(facts)
        original = Path(args.config).read_text(encoding="utf-8")
        verified = today()
        updated, changes = parser.rewrite(original, by_model, windows, verified)
    except (Refused, AssertionError) as exc:
        print(f"::error::refusing to write the config. {exc}", file=sys.stderr)
        return 1

    if updated == original:
        print("no change: the published figures already match this config")
        return 0

    Path(args.config).write_text(updated, encoding="utf-8")
    print(f"{len(changes)} change(s):")
    for change in changes:
        print(f"  - {change}")

    readers = (
        "Both readers ran and agreed: the model read the page, and the regex parser read it "
        "independently and got the same figures."
        if parser_by_model is not None
        else "**Only one reader ran.** The regex parser could not read the page this time - it "
        "has most likely been restyled. The figures below were checked against the page text "
        "(every one of them is printed there), but the model was the only reader, so nothing "
        "independently confirmed which column each figure came from."
    )
    Path(args.pr_body).write_text(
        "\n".join(
            [
                "Scheduled sync of DeepSeek's published pricing pages.",
                "",
                f"Source: {parser.EN}",
                f"Verified: {verified}",
                "",
                "```",
                *changes,
                "```",
                "",
                readers,
                "",
                "Every number was checked against the raw page text before this file was written: "
                "a figure that is not printed on the page is refused, not committed.",
                "",
                "**Review the diff before merging.** A wrong number here is displayed to every "
                "user, and the app's own validation only rejects changes over 20x.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return 0 if parser_by_model is not None else 2


def today() -> str:
    # GitHub runners are UTC; the field is a plain date and is compared for ordering only.
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


if __name__ == "__main__":
    sys.exit(main())
