from maritime_tip_cue.sts import fixtures


def line(name: str, t: dict) -> str:
    return f"{name:<24} pairs {t['found']}/{t['pairs']}  singles called pair {t['false']}/{t['singles']}"


def main() -> None:
    results = fixtures.s2_results()
    print("S2 hull check (fixtures)")
    for s in fixtures.S2_SETS:
        t = fixtures.tally([(i["label_user"], r["verdict"]) for i, r in results if i["set"] == s])
        print(line(f"  {s}", t))
    t = fixtures.tally([(i["label_user"], r["verdict"]) for i, r in results if i["set"] in fixtures.S2_SETS])
    print(line("  all four sets", t))
    print("S1 pass check (fixtures)")
    for name in fixtures.s1_passes():
        res = fixtures.s1_results(name)
        print(line(f"  {name}", fixtures.tally([(o["label_user"], r["verdict"]) for o, r in res])))


if __name__ == "__main__":
    main()
