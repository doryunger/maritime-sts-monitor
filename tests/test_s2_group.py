from maritime_tip_cue.watch import s2


def det(k, lat, lon, score):
    return {"det_id": k, "lat": lat, "lon": lon, "score": score, "length_m": 200.0}


def single(lat, lon, b=False):
    chk = {"verdict": "single", "hull_a_lat": lat, "hull_a_lon": lon, "hull_a_length_m": 300.0, "hull_a_width_m": 50.0, "fix_heading": 0.0}
    if b:
        chk |= {"hull_b_lat": lat, "hull_b_lon": lon + 0.0006, "hull_b_length_m": 300.0, "hull_b_width_m": 50.0}
    return chk


def test_a_box_inside_a_single_hull_is_a_duplicate_only_when_the_rule_is_on():
    dets = [det("ship", 25.0, 56.0, 0.99), det("wake", 25.0009, 56.0, 0.8)]
    checks = {"ship": single(25.0, 56.0), "wake": {"verdict": "moving"}}
    assert s2.group(dets, checks, 10) == {}
    out = s2.group(dets, checks, 10, True)
    assert out["wake"]["role"] == "duplicate" and out["ship"]["role"] == "anchor"


def test_a_single_hull_never_hides_a_box_in_its_hull_b_rectangle():
    dets = [det("ship", 25.0, 56.0, 0.99), det("beside", 25.0, 56.0006, 0.9)]
    checks = {"ship": single(25.0, 56.0, b=True), "beside": {"verdict": "single"}}
    assert s2.group(dets, checks, 10, True) == {}


def test_two_singles_measuring_one_ship_keep_only_the_stronger():
    dets = [det("weak", 25.0, 56.0, 0.8), det("strong", 25.0003, 56.0, 0.99)]
    checks = {"weak": single(25.0002, 56.0), "strong": single(25.0002, 56.0)}
    out = s2.group(dets, checks, 10, True)
    assert out == {"strong": {"record": "001", "role": "anchor"}, "weak": {"record": "001", "role": "duplicate"}}


def test_a_pair_still_absorbs_first():
    pair = single(25.0, 56.0, b=True) | {"verdict": "pair"}
    dets = [det("p", 25.0, 56.0, 0.9), det("s", 25.0, 56.0006, 0.99)]
    checks = {"p": pair, "s": single(25.0, 56.0006)}
    out = s2.group(dets, checks, 10, True)
    assert out["p"]["role"] == "anchor" and out["s"]["role"] == "hull_b_box"


def test_the_stronger_box_is_kept_when_only_the_weaker_hull_covers_it():
    dets = [det("strong", 25.0, 56.0, 0.99), det("weak", 25.0009, 56.0, 0.95)]
    checks = {"strong": single(25.0, 56.0) | {"hull_a_length_m": 60.0}, "weak": single(25.0009, 56.0)}
    out = s2.group(dets, checks, 10, True)
    assert out["strong"]["role"] == "anchor" and out["weak"]["role"] == "duplicate"
