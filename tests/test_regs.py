"""The host register model against the firmware's and the reference capture's values."""

import re
from pathlib import Path

import pytest

from naneye import regs

ROOT = Path(__file__).resolve().parents[1]


def test_reference_values_decode():
    # The reference host's writes (spec.md section 3.2).
    v = regs.unpack(0x009F, 0x0065)
    assert v["rows_in_reset"] == 0
    assert v["vrst_pix"] == 2 and v["ramp_gain"] == 1
    assert v["offset_ramp"] == 3 and v["output_curr"] == 3
    assert v["output_mode"] == 0 and v["idle_mode"] == 0
    assert v["mclk_mode"] == 1 and v["high_speed"] == 1
    assert v["vref"] == 2 and v["cvc_curr"] == 1


def test_fields_tile_both_registers_exactly():
    for reg in (0, 1):
        bits = 0
        for f in regs.FIELDS:
            if f.reg == reg:
                mask = f.max << f.shift
                assert bits & mask == 0, f"{f.name} overlaps another field"
                bits |= mask
        assert bits == 0xFFFF, f"CONFIG_{reg} has unassigned bits"


@pytest.mark.parametrize("cfg0,cfg1", [(0x009F, 0x0065), (0x9F9F, 0xF85C), (0, 0),
                                       (0xFFFF, 0xFFFF), (0x1234, 0xABCD)])
def test_pack_unpack_round_trip(cfg0, cfg1):
    assert regs.pack(regs.unpack(cfg0, cfg1)) == (cfg0, cfg1)


def test_set_one_field_leaves_the_rest():
    c0, c1 = regs.BY_NAME["ramp_gain"].set(0x009F, 0x009C, 3)
    assert c0 == 0x00BF and c1 == 0x009C
    c0, c1 = regs.BY_NAME["rows_delay"].set(0x009F, 0x009C, 31)
    assert c0 == 0x009F and c1 == 0xF89C


def test_set_clamps_to_field_width():
    c0, _ = regs.BY_NAME["ramp_gain"].set(0, 0, 9)
    assert regs.BY_NAME["ramp_gain"].get(c0, 0) == 3


def test_exposure_matches_firmware_selftest():
    # The same two cases main.cpp's SELFTEST checks.
    assert regs.exposure_pp(0, 0) == 105616
    assert regs.exposure_pp(127, 0) == 22304


def test_exposure_matches_measured_device_reply():
    # "EXP rows_in_reset=0 rows_delay=31 -> t_exp=268304 PP" on hardware, 2026-09-18.
    assert regs.exposure_pp(0, 31) == 268304


def test_limits_match_firmware_header():
    text = (ROOT / "firmware" / "src" / "naneye_regs.h").read_text()
    assert "ROWS_IN_RESET_MAX = (uint8_t)((HEIGHT - 2) / 2)" in text
    assert regs.ROWS_IN_RESET_MAX == 159
    assert re.search(r"ROWS_DELAY_MAX = 31;", text) and regs.ROWS_DELAY_MAX == 31


def test_firmware_owned_fields_are_not_editable():
    owned = {f.name for f in regs.FIELDS if not f.editable}
    assert owned == {"output_mode", "mclk_mode", "idle_mode", "high_speed"}


def test_recommended_sets_datasheet_values_and_keeps_the_rest():
    c0, c1 = regs.recommended(0x509F, 0x209C)  # rows_in_reset 80, rows_delay 4
    v = regs.unpack(c0, c1)
    assert v["rows_in_reset"] == 80 and v["rows_delay"] == 4
    assert v["vref"] == 2 and v["cvc_curr"] == 1
    assert v["mclk_mode"] == 2  # firmware-owned, untouched


def test_describe_lists_every_field():
    lines = regs.describe(0x009F, 0x009C, sclk_hz=12375000)
    text = "\n".join(lines)
    for f in regs.FIELDS:
        assert f.name in text
    assert "102.4 ms" in text
