"""
Draft, review and store the labels of the autopilot's strategy prompts.

    # 1. Draft by keyword (no AI). Keeps any label already marked reviewed.
    python scripts/draft_prompt_labels.py draft

    # 1b. Optionally let an AI improve the draft, 20 prompts per call (about 5 calls).
    python scripts/draft_prompt_labels.py draft --ai gemini --model <model>

    # 2. Open prompt_labels.csv in a spreadsheet, correct the labels, set reviewed
    #    to yes for each row you checked, and save it as CSV.

    # 3. Store the reviewed spreadsheet.
    python scripts/draft_prompt_labels.py import prompt_labels.csv

Run from backend/. Lists in the spreadsheet are separated by ";", for example
"london;overlap". Allowed values are in app/core/prompt_labels.py (VOCAB).
"""
import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("APP_ENV", "development")

from app.core import prompt_labels  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
CSV_FILE = BACKEND / "prompt_labels.csv"
COLUMNS = ["number", "styles", "market", "sessions", "volatility", "timeframes", "direction", "reviewed", "prompt"]


def read_prompts() -> dict[str, str]:
    """Prompt number -> text, from prompt_list.txt ("PROMPT #N:" blocks)."""
    text = (BACKEND / "prompt_list.txt").read_text(encoding="utf-8")
    out, number, lines = {}, None, []
    for line in text.splitlines():
        m = re.match(r"^PROMPT\s*#(\d+):?\s*$", line.strip(), re.I)
        if m:
            if number is not None:
                out[number] = " ".join(lines).strip()
            number, lines = m.group(1), []
        elif line.strip() and number is not None:
            lines.append(line.strip())
    if number is not None:
        out[number] = " ".join(lines).strip()
    return out


def existing() -> dict:
    try:
        return json.loads(prompt_labels.LABELS_FILE.read_text()).get("prompts", {})
    except FileNotFoundError:
        return {}


def save(labels: dict) -> None:
    ordered = {k: labels[k] for k in sorted(labels, key=int)}
    payload = {"_about": "Labels for prompt_list.txt. Draft with scripts/draft_prompt_labels.py, "
                         "review in prompt_labels.csv, store with its import command.",
               "prompts": ordered}
    prompt_labels.LABELS_FILE.write_text(json.dumps(payload, indent=1) + "\n")
    reviewed = sum(1 for v in labels.values() if v.get("reviewed"))
    print(f"Saved {len(labels)} prompts to {prompt_labels.LABELS_FILE.name} ({reviewed} reviewed)")


def export_csv(labels: dict, prompts: dict) -> None:
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for num in sorted(labels, key=int):
            v = labels[num]
            w.writerow([num, ";".join(v["styles"]), v["market"], ";".join(v["sessions"]),
                        ";".join(v["volatility"]), ";".join(v["timeframes"]), v["direction"],
                        "yes" if v.get("reviewed") else "no", prompts.get(num, "")[:300]])
    print(f"Wrote {CSV_FILE.name} for review")


def ai_draft(prompts: dict, draft: dict, provider: str, model: str) -> dict:
    """Ask an AI to improve the keyword draft, 20 prompts per call."""
    from openai import OpenAI
    from app.core.config import settings
    from app.core.providers import PROVIDERS, get_all_api_keys
    keys = get_all_api_keys(provider, settings)
    if not keys:
        sys.exit(f"No key for {provider} in .env")
    client = OpenAI(base_url=PROVIDERS[provider]["base_url"], api_key=keys[0])
    vocab = json.dumps({k: list(v) for k, v in prompt_labels.VOCAB.items()})
    numbers = [n for n in sorted(prompts, key=int) if not draft.get(n, {}).get("reviewed")]
    for start in range(0, len(numbers), 20):
        batch = {n: {"prompt": prompts[n], "draft": {k: v for k, v in draft[n].items() if k != "reviewed"}}
                 for n in numbers[start:start + 20]}
        reply = client.chat.completions.create(model=model, temperature=0, messages=[
            {"role": "system", "content": "You label trading strategy prompts. Use only these values: " + vocab +
             '. "market" and "direction" are single values; the others are lists. Return ONLY a JSON object '
             'mapping each prompt number to its labels (styles, market, sessions, volatility, timeframes, direction).'},
            {"role": "user", "content": json.dumps(batch)},
        ]).choices[0].message.content or ""
        match = re.search(r"\{.*\}", reply, re.S)
        try:
            proposed = json.loads(match.group(0)) if match else {}
        except ValueError:
            proposed = {}
        for num, labels in proposed.items():
            labels = {**labels, "reviewed": False}
            if str(num) in batch and not prompt_labels.validate(labels):
                draft[str(num)] = labels
        print(f"AI labelled prompts {numbers[start]}..{numbers[min(start + 19, len(numbers) - 1)]}")
    return draft


def cmd_draft(args) -> None:
    prompts = read_prompts()
    stored = existing()
    labels = {}
    for num, text in prompts.items():
        labels[num] = stored[num] if stored.get(num, {}).get("reviewed") else prompt_labels.draft_by_keyword(text)
    if args.ai:
        labels = ai_draft(prompts, labels, args.ai, args.model)
    save(labels)
    export_csv(labels, prompts)


def cmd_import(args) -> None:
    labels, problems = {}, []
    with open(args.file, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            split = lambda v: [x.strip() for x in (v or "").split(";") if x.strip()]  # noqa: E731
            entry = {"styles": split(row["styles"]), "market": row["market"].strip(),
                     "sessions": split(row["sessions"]), "volatility": split(row["volatility"]),
                     "timeframes": split(row["timeframes"]), "direction": row["direction"].strip(),
                     "reviewed": row["reviewed"].strip().lower() in ("yes", "y", "true", "1")}
            found = prompt_labels.validate(entry)
            if found:
                problems.append(f"prompt {row['number']}: " + "; ".join(found))
            labels[row["number"].strip()] = entry
    if problems:
        sys.exit("Not saved. Fix these rows:\n  " + "\n  ".join(problems))
    save(labels)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--ai", help="provider to improve the draft, e.g. gemini")
    d.add_argument("--model", help="that provider's model")
    i = sub.add_parser("import")
    i.add_argument("file")
    args = parser.parse_args()
    if args.command == "draft" and args.ai and not args.model:
        parser.error("--ai needs --model")
    {"draft": cmd_draft, "import": cmd_import}[args.command](args)
