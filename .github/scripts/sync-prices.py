#!/usr/bin/env python3
"""Read DeepSeek's published prices and rewrite `deepseek.json` only where they differ.

Why this exists
---------------

Prices and peak windows are published by DeepSeek, not by us. Transcribing them by hand is the
one step in this repository that can silently go wrong: a mistyped digit produces a config that
looks entirely plausible, and the app will happily display it. This script removes the
transcription, and leaves the judgement to a human by opening a **pull request** rather than
committing — see `.github/workflows/sync-prices.yml`.

Two properties this script must have
------------------------------------

**It fails loudly or not at all.** Every step asserts. A page that has been redesigned must stop
the run, never produce a number that merely looks reasonable. This is not hypothetical: the first
draft of the parser read the row labelled `MAX OUTPUT` (a 384K context-length row) as the output
*price*, and reported `384.0` without complaining.

**It rewrites numbers, not the file.** Re-serialising the JSON with `json.dumps(indent=2)` expands
the inline arrays and turns a one-line price change into a 500-byte diff, which is exactly the
kind of noise that gets a real change waved through. So the edits are made in place, by line, and
everything else — key order, comments-as-`notes`, spacing — is left untouched.
"""

import html
import json
import re
import sys
import urllib.request
from pathlib import Path

EN = "https://api-docs.deepseek.com/quick_start/pricing"
ZH = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
UA = "DeepSeekBudget-config/1.0 (+https://github.com/FAAATQ/DeepSeekBudget-config)"
CONFIG = Path("deepseek.json")

# The label cell exists only on the OFF-PEAK row of each pair; the PEAK row beneath inherits the
# category through a rowspan and carries one fewer cell. Substring matching alone therefore finds
# the wrong row — see the module docstring.
CATEGORIES = [
    ("inputCacheHit", ("CACHE HIT", "缓存命中")),
    ("inputCacheMiss", ("CACHE MISS", "缓存未命中")),
    ("output", ("1M OUTPUT TOKENS", "百万tokens输出")),
]


def get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200, f"{url} -> HTTP {response.status}"
        return response.read().decode("utf-8")


def table_rows(page: str) -> list[list[str]]:
    table = re.search(r"<table.*?</table>", page, re.S)
    assert table, "no <table> on the page — the docs site changed shape"
    rows = []
    for row in re.findall(r"<tr.*?</tr>", table.group(0), re.S):
        cells = re.findall(r"<t[hd].*?</t[hd]>", row, re.S)
        rows.append(
            [
                re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()
                for cell in cells
            ]
        )
    return rows


def numbers(text: str) -> list[float]:
    return [float(f) for f in re.findall(r"[\d.]+", text.replace(",", ""))]


def is_off_peak(row: list[str]) -> bool:
    joined = " ".join(row)
    return "OFF-PEAK" in joined.upper() or "空闲" in joined


def parse_prices(page: str, currency: str) -> dict:
    """`{category: {"offPeak": [...], "peak": [...]}}`, one value per model, column order."""
    rows = table_rows(page)
    start = next(
        (i for i, r in enumerate(rows) if "PRICING" in " ".join(r).upper() or "价格" in " ".join(r)),
        None,
    )
    assert start is not None, f"{currency}: no pricing block on the page"
    block = rows[start:]

    prices = {}
    for key, needles in CATEGORIES:
        index = next(
            (
                i
                for i, row in enumerate(block)
                if is_off_peak(row)
                and any(n in " ".join(row) or n in " ".join(row).upper() for n in needles)
            ),
            None,
        )
        assert index is not None, f"{currency}: no off-peak row for {key}"

        off = numbers(" ".join(block[index][-2:]))
        assert len(off) == 2, f"{currency}: {key} off-peak has {len(off)} values, expected 2"

        peak_row = block[index + 1]
        assert not is_off_peak(peak_row), f"{currency}: the row after {key} off-peak is not peak"
        peak = numbers(" ".join(peak_row[-2:]))
        assert len(peak) == 2, f"{currency}: {key} peak has {len(peak)} values, expected 2"

        prices[key] = {"offPeak": off, "peak": peak}
    return prices


def parse_windows(page: str) -> list[list[str]]:
    """The peak-window sentence. This changing matters more than any price changing."""
    flat = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))
    found = re.findall(r"\b(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\b", flat)
    assert found, "no time windows on the page — the sentence moved or was reworded"
    return [list(pair) for pair in dict.fromkeys(found)]


def fmt(value: float) -> str:
    """Render a price the way the file already writes them: no trailing `.0`."""
    return str(int(value)) if float(value).is_integer() else repr(value)


def rewrite(text: str, by_model: dict[str, dict], windows: list[list[str]], verified: str):
    """Edit the numbers in place. Returns (new_text, list_of_human_readable_changes).

    `verifiedAt` is only touched when something else actually moved. Bumping it on a run that
    changed nothing would open a pull request every single day whose entire diff is a date.
    """
    lines = text.split("\n")
    changes: list[str] = []
    model = None
    currency = None

    for i, line in enumerate(lines):
        id_match = re.match(r'\s*"id":\s*"([^"]+)"', line)
        if id_match:
            model = id_match.group(1)
            currency = None
            continue

        currency_match = re.match(r'\s*"(CNY|USD)":\s*\{\s*$', line)
        if currency_match:
            currency = currency_match.group(1)
            continue

        price_match = re.match(
            r'(\s*"(inputCacheHit|inputCacheMiss|output)":\s*\{ "offPeak": )'
            r"([\d.]+)(, \"peak\": )([\d.]+)( \},?\s*)$",
            line,
        )
        if price_match and model in by_model and currency in by_model[model]:
            prefix, key, was_off, middle, was_peak, suffix = price_match.groups()
            block = by_model[model][currency].get(key)
            if block is None:
                continue
            index = list(by_model).index(model)
            off, peak = block["offPeak"][index], block["peak"][index]
            # Compare as numbers, never as text: the file writes `9.0` where `fmt` would write
            # `9`, and a textual comparison turns that into a phantom change on every run.
            if float(off) != float(was_off) or float(peak) != float(was_peak):
                changes.append(
                    f"{model} {currency} {key}: offPeak {was_off} -> {fmt(off)}, "
                    f"peak {was_peak} -> {fmt(peak)}"
                )
                lines[i] = f"{prefix}{fmt(off)}{middle}{fmt(peak)}{suffix}"

        window_match = re.match(r'(\s*"windows": )(\[\[.*\]\])(,?\s*)$', line)
        if window_match:
            prefix, was, suffix = window_match.groups()
            now = json.dumps(windows, separators=(", ", ": "))
            if now != was:
                changes.append(f"peak windows: {was} -> {now}")
                lines[i] = f"{prefix}{now}{suffix}"

    if not changes:
        return text, []

    for i, line in enumerate(lines):
        if re.match(r'\s*"verifiedAt":', line):
            # Anchor on the key. Replacing the first quoted run on the line rewrites the key
            # itself — which is what this did before: `"2026-09-13": "2026-09-12",`.
            lines[i] = re.sub(r'("verifiedAt":\s*)"[^"]*"', rf'\1"{verified}"', line)
            break

    return "\n".join(lines), changes


def main() -> int:
    live = json.loads(CONFIG.read_text(encoding="utf-8"))
    ids = [m["id"] for m in live["models"]]

    en, zh = get(EN), get(ZH)
    by_model = {mid: {"USD": {}, "CNY": {}} for mid in ids}
    for mid in ids:
        for currency, page in (("USD", en), ("CNY", zh)):
            by_model[mid][currency] = parse_prices(page, currency)
    windows = parse_windows(en)

    # Everything the app would reject is caught here, before a PR is ever opened.
    for mid in ids:
        for currency in ("USD", "CNY"):
            for key in ("inputCacheHit", "inputCacheMiss", "output"):
                for tier in ("offPeak", "peak"):
                    value = by_model[mid][currency][key][tier]
                    assert len(value) == len(ids), f"{mid}/{currency}/{key}: wrong model count"

    original = CONFIG.read_text(encoding="utf-8")
    verified = _today()
    updated, changes = rewrite(original, by_model, windows, verified)

    if updated == original:
        print("no change: the published figures already match this config")
        return 0

    CONFIG.write_text(updated, encoding="utf-8")
    print(f"{len(changes)} change(s):")
    for change in changes:
        print(f"  - {change}")

    summary = Path("PR_BODY.md")
    summary.write_text(
        "\n".join(
            [
                "Scheduled check of DeepSeek's published pricing page.",
                "",
                f"Source: {EN}",
                f"Verified: {verified}",
                "",
                "```",
                *changes,
                "```",
                "",
                "Prices are transcribed from both language editions, which DeepSeek publishes",
                "independently — neither is an FX conversion of the other.",
                "",
                "**Review the diff before merging.** A wrong number here is displayed to every",
                "user, and the app's own validation only rejects changes over 20x.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


def _today() -> str:
    # GitHub runners are UTC; the field is a plain date and is compared for ordering only.
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


if __name__ == "__main__":
    sys.exit(main())
