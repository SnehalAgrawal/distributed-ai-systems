"""Compare repeat runs: python compare.py runs/<a> runs/<b> runs/<c>

Prints whether the stable part of each report (PASS/FAIL of every prediction, counts that don't depend on timing)
is identical, line by line where it isn't, and the range of every measured number across the runs. Reads each
run's report.txt (write it with report.py first).
"""

import re
import sys
from pathlib import Path


def sections(path):
    text = (Path(path) / "report.txt").read_text()
    stable = text.split("== Stable ==\n", 1)[1].split("\n\nStable digest:", 1)[0].splitlines()
    digest = re.search(r"Stable digest: (\w+)", text).group(1)
    meas = text.split("== Measurements (vary between runs) ==\n", 1)[1].splitlines()
    return stable, digest, meas


def main(paths):
    runs = [sections(p) for p in paths]
    print("Runs:", ", ".join(Path(p).name for p in paths))
    digests = [d for _, d, _ in runs]
    print("Stable digests:", *digests, sep="\n  ")
    if len(set(digests)) == 1:
        print("Stable output: identical in all runs")
    else:
        print("Stable output: DIFFERS")
        for i, lines in enumerate(zip(*(s for s, _, _ in runs))):
            if len(set(lines)) > 1:
                print(f"  line {i}:", *lines, sep="\n    ")
    print("\nRanges across runs (min-max of each number, in order of appearance):")
    for lines in zip(*(m for _, _, m in runs)):
        head = lines[0].split(":")[0]
        numbers = [[float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", line.split(":", 1)[1])] for line in lines]
        if len({len(n) for n in numbers}) != 1:
            print(f"  {head}: (not comparable number by number)", *lines, sep="\n    ")
            continue
        # Deviation D6: braces in a line (JSON) are escaped before numbers become format fields.
        text = lines[0].split(":", 1)[1].replace("{", "{{").replace("}", "}}")
        labels = re.sub(r"-?\d+(?:\.\d+)?", "{}", text)
        ranges = [f"{min(col):g}" if min(col) == max(col) else f"{min(col):g}-{max(col):g}" for col in zip(*numbers)]
        print(f"  {head}:" + labels.format(*ranges))


if __name__ == "__main__":
    main(sys.argv[1:])
