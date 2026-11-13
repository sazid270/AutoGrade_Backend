"""Ad-hoc pipeline smoke test on the real uploaded sheets (run from backend/)."""

import sys

from omr import pipeline as p


def run(path: str, key_size: int) -> None:
    print(f"\n=== {path.split('/')[-1]} ===")
    data = open(path, "rb").read()
    try:
        img = p.decode_image(data)
        gray = p.preprocess(img)
        print(f"blur variance: {p.blur_variance(gray):.0f}")
        p.check_blur(gray)
        gray, reg = p.register(gray)
        gray, angle = p.deskew(gray)
        gray = p.normalize_scale(gray)
        print(f"deskew={angle}deg warped={reg['warped']} size={gray.shape[1]}x{gray.shape[0]}")
        key = {str(i): "ABCD"[(i - 1) % 4] for i in range(1, key_size + 1)}
        layout = p.detect_layout(gray, p.expected_option_count(key))
        print(f"layout: {layout['layout_note']}")
        roll, conf = p.read_roll_no(gray, layout["roll_block"])
        print(f"roll_no: {roll!r} confidence={conf}")
        records = p.recognize(gray, layout)
        needs = p.flag_questions(gray, records, key, p.expected_option_count(key))
        totals = p.score_records(records, key, 1, 0, 0)
        print(f"flagged for review: {needs}")
        print(f"totals: {totals}")
        shown = records[: key_size if key_size else 10]
        for record in shown:
            letters = ",".join(
                f"{o['letter']}={o['ratio']:.2f}{'*' if o['state'] != 'empty' else ''}"
                for o in record["options"]
            )
            flags = f" flags={record['flags']}" if record["flags"] else ""
            print(
                f"  Q{record['q']:>3}: {letters} -> sel={record['selected']} "
                f"exp={record['expected']} {record['outcome']}{flags}"
            )
    except p.SheetRejected as exc:
        print(f"REJECTED: {exc}")


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        run(arg, key_size=20)
