"""The NanEyeC's two registers, field by field.

Mirrors firmware/src/naneye_regs.h (tests/test_regs.py holds the two to the same values).
DS000503 section 7 is the source for the fields; the units and recommended values are from
its tables, and the exposure arithmetic from section 6.5.1.

Each field says whether it is safe to change while streaming. The unsafe ones are left to
the firmware: output_mode and idle_mode would stop the link, and mclk_mode / high_speed
must match the clock rate the firmware generates.
"""

from __future__ import annotations

from dataclasses import dataclass

HEIGHT = 320
ROW_PP = 328
PP_BITS = 12
INTERFACE_PP = 648
SYNC_PP = 2 * ROW_PP
EOF_PP = 8
ROWS_IN_RESET_MAX = (HEIGHT - 2) // 2  # 159: rows in reset may not exceed the sensor's rows
ROWS_DELAY_MAX = 31


@dataclass(frozen=True)
class Field:
    name: str
    reg: int                  # 0 = CONFIG_0, 1 = CONFIG_1
    shift: int
    width: int
    editable: bool            # safe to change while streaming
    labels: tuple = ()        # meaning of each value, where the datasheet gives one
    recommended: int | None = None
    help: str = ""

    @property
    def max(self) -> int:
        return (1 << self.width) - 1

    def get(self, cfg0: int, cfg1: int) -> int:
        v = cfg1 if self.reg else cfg0
        return (v >> self.shift) & self.max

    def set(self, cfg0: int, cfg1: int, value: int) -> tuple[int, int]:
        value = max(0, min(int(value), self.max))
        mask = self.max << self.shift
        if self.reg:
            cfg1 = (cfg1 & ~mask) | (value << self.shift)
        else:
            cfg0 = (cfg0 & ~mask) | (value << self.shift)
        return cfg0, cfg1

    def label(self, value: int) -> str:
        return self.labels[value] if value < len(self.labels) else str(value)


FIELDS = (
    # CONFIG_0 (address 0)
    Field("rows_in_reset", 0, 8, 8, True,
          help="exposure: 0 is the longest, 159 the shortest"),
    Field("vrst_pix", 0, 6, 2, True, ("2.2 V", "2.4 V", "2.6 V", "2.8 V"), 2,
          "pixel reset voltage"),
    Field("ramp_gain", 0, 4, 2, True, ("0.79x", "0.99x", "1.32x", "1.97x"), 1,
          "ADC ramp gain"),
    Field("offset_ramp", 0, 2, 2, True, ("1.9 V", "2.0 V", "2.1 V", "2.2 V"), 3,
          "ADC ramp offset"),
    Field("output_curr", 0, 0, 2, True, ("3.9 mA", "5.8 mA", "7.7 mA", "9.6 mA"), 3,
          "SDAT output drive; lower is quieter but slower edges"),
    # CONFIG_1 (address 1)
    Field("rows_delay", 1, 11, 5, True,
          help="frame delay: 16n + 2 extra rows; slows the frame rate, lengthens exposure"),
    Field("bias_curr_increase", 1, 10, 1, True, ("off", "on"), 0, "pixel bias current"),
    Field("cds_gain", 1, 9, 1, True, ("1.3x", "2.0x"), 0, "column amplifier gain"),
    Field("output_mode", 1, 8, 1, False, ("SEIM", "LVDS"), 0, "set by the firmware"),
    Field("mclk_mode", 1, 6, 2, False, ("2x", "default", "/2", "/2"), None,
          "set by the firmware to match SCLK"),
    Field("vref", 1, 4, 2, True, ("1.9 V", "2.0 V", "2.1 V", "2.2 V"), 2,
          "ADC reference; recommended 0.1 V below offset_ramp"),
    Field("cvc_curr", 1, 2, 2, True, recommended=1, help="column voltage converter current"),
    Field("idle_mode", 1, 1, 1, False, ("streaming", "idle"), None, "set by the firmware"),
    Field("high_speed", 1, 0, 1, False, ("off", "on"), None,
          "set by the firmware to match SCLK"),
)
BY_NAME = {f.name: f for f in FIELDS}
EDITABLE = tuple(f for f in FIELDS if f.editable)


def unpack(cfg0: int, cfg1: int) -> dict:
    return {f.name: f.get(cfg0, cfg1) for f in FIELDS}


def pack(values: dict, cfg0: int = 0, cfg1: int = 0) -> tuple[int, int]:
    """Apply `values` (field name -> value) on top of an existing register pair."""
    for name, v in values.items():
        cfg0, cfg1 = BY_NAME[name].set(cfg0, cfg1, v)
    return cfg0, cfg1


def recommended(cfg0: int, cfg1: int) -> tuple[int, int]:
    """The datasheet's recommended analog settings, gains at unity (ramp 0.99x, CDS 1.3x).

    Exposure, frame delay and the firmware-owned fields are left as they are.
    """
    return pack({f.name: f.recommended for f in EDITABLE if f.recommended is not None},
                cfg0, cfg1)


# --- Exposure (DS000503 6.5.1), as in naneye_regs.h -----------------------------------
def rows_delay_pp(rows_delay: int) -> int:
    return (16 * (rows_delay & 0x1F) + 2) * ROW_PP


def exposure_pp(rows_in_reset: int, rows_delay: int) -> int:
    t_btw = INTERFACE_PP + SYNC_PP + rows_delay_pp(rows_delay)
    t_matrix = HEIGHT * ROW_PP + EOF_PP
    subtract = (2 * rows_in_reset + 2) * ROW_PP + 2 * ROW_PP
    total = t_btw + t_matrix
    return max(0, total - subtract)


def exposure_ms(rows_in_reset: int, rows_delay: int, sclk_hz: float) -> float:
    return exposure_pp(rows_in_reset, rows_delay) * PP_BITS / sclk_hz * 1e3


def frame_period_ms(rows_delay: int, sclk_hz: float) -> float:
    pp = INTERFACE_PP + SYNC_PP + rows_delay_pp(rows_delay) + HEIGHT * ROW_PP + EOF_PP
    return pp * PP_BITS / sclk_hz * 1e3


def rows(cfg0: int, cfg1: int, sclk_hz: float | None = None) -> list[tuple]:
    """(register, field, value, meaning, editable, differs_from_recommended) per field."""
    v = unpack(cfg0, cfg1)
    out = []
    for f in FIELDS:
        value = v[f.name]
        meaning = f.label(value) if f.labels else ""
        if f.name == "rows_in_reset" and sclk_hz:
            meaning = f"{exposure_ms(value, v['rows_delay'], sclk_hz):.1f} ms exposure"
        elif f.name == "rows_delay" and sclk_hz:
            meaning = f"{1e3 / frame_period_ms(value, sclk_hz):.1f} fps max"
        differs = f.recommended is not None and value != f.recommended
        out.append((f.reg, f.name, value, meaning, f.editable, differs))
    return out


def describe(cfg0: int, cfg1: int, sclk_hz: float | None = None) -> list[str]:
    """One line per field, for printing: 'rows_in_reset   0  102.4 ms exposure'."""
    lines = []
    for reg, name, value, meaning, editable, differs in rows(cfg0, cfg1, sclk_hz):
        if name in ("rows_in_reset", "rows_delay"):
            lines.append(f"CONFIG_{reg}  0x{(cfg1 if reg else cfg0):04X}")
        lines.append(f"  {name:<18} {value:>3}  {meaning}{' *' if differs else ''}"
                     f"{'' if editable else ' (fw)'}")
    return lines
