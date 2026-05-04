"""Quick smoke test for sequence + reroute engines. Does not need network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.nlu import detect_language, extract_test_codes
from app.reroute_engine import decide_reroute
from app.sequence_engine import sequence_tests


def main() -> None:
    # Ravi types in Telugu mixed with English test names.
    text = "నాకు blood test, ECG, ultrasound, X-Ray కావాలి"
    print(f"Input: {text}")
    print(f"Detected language: {detect_language(text)}")
    codes = extract_test_codes(text)
    print(f"Extracted codes (in patient's order): {codes}")

    sequenced = sequence_tests(codes)
    print(f"Clinically sequenced: {sequenced}")
    assert sequenced == ["BLOOD", "ECG", "ULTRASOUND", "XRAY"], sequenced

    # Patient just finished BLOOD (index 1). ECG goes under maintenance.
    decision = decide_reroute(sequenced, current_index=1, unavailable_test="ECG")
    print(f"\nECG maintenance: {decision.action} -> {decision.new_sequence}")
    assert decision.action == "reordered"
    assert decision.new_sequence[-1] != "XRAY" or decision.new_sequence[-2] == "ECG"

    # X-Ray closed before X-Ray's turn.
    decision2 = decide_reroute(sequenced, current_index=3, unavailable_test="XRAY")
    print(f"X-Ray closed: {decision2.action} reserved={decision2.reserved_for_time}")
    assert decision2.action == "reserved_slot"

    # Out-of-order input still gets sequenced correctly.
    weird = sequence_tests(["XRAY", "ECG", "BLOOD", "ULTRASOUND"])
    print(f"\nOut-of-order input -> {weird}")
    assert weird == ["BLOOD", "ECG", "ULTRASOUND", "XRAY"]

    # Subset of tests.
    sub = sequence_tests(["XRAY", "BLOOD"])
    print(f"Subset -> {sub}")
    assert sub == ["BLOOD", "XRAY"]

    print("\nAll smoke checks passed.")


if __name__ == "__main__":
    main()
