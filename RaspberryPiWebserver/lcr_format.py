#!/usr/bin/env python3
# Ported from Digital_Multitool commit f22527f (instruments.py / lcr_format.py)
#
# MODE_UNITS, format_si and format_measurement come from lcr_format.py.
# OVERLOAD_CODE and as_number come from continuous_log.py of the same commit.
# display_reading is an arss addition (issue #40). Non-ASCII unit symbols are
# written as \u escapes so this source file stays ASCII; the strings are the
# same as in the source.
"""Pure formatting helpers for the BK 894 LCR readout (no Flask, no VISA).

Knows, per measurement mode, what the primary/secondary quantities are and
formats both with auto-scaled SI prefixes. Also maps "no number" readings
(NaN, infinities, the SCPI overload codes) to None for display. The CSV is
never filtered: it keeps whatever the instrument answered.
"""
import math

OHM = '\u03a9'      # Greek capital omega
MICRO = '\u00b5'    # micro sign
THETA = '\u03b8'    # Greek small theta
DEGREE = '\u00b0'   # degree sign

# mode -> (primary unit, secondary name, secondary unit)
# '' = dimensionless; angle units are printed as-is (no SI prefix).
MODE_UNITS = {
    'CPD':  ('F', 'D',   ''),
    'CPQ':  ('F', 'Q',   ''),
    'CPG':  ('F', 'G',   'S'),
    'CPRP': ('F', 'Rp',  OHM),
    'CSD':  ('F', 'D',   ''),
    'CSQ':  ('F', 'Q',   ''),
    'CSRS': ('F', 'Rs',  OHM),
    'LSRS': ('H', 'Rs',  OHM),
    'LSRD': ('H', 'Rdc', OHM),
    'LPRS': ('H', 'Rs',  OHM),
    'LPRP': ('H', 'Rp',  OHM),
    'RX':   (OHM, 'X',   OHM),
    'ZTD':  (OHM, THETA, DEGREE),
    'ZTR':  (OHM, THETA, 'rad'),
}

_PREFIXES = [(1e-12, 'p'), (1e-9, 'n'), (1e-6, MICRO), (1e-3, 'm'),
             (1.0, ''), (1e3, 'k'), (1e6, 'M'), (1e9, 'G')]

# dimensionless and angle units get a plain number, never an SI prefix
_NO_PREFIX = {'', DEGREE, 'rad'}


def format_si(value, unit, digits=4):
    """value -> '3.3 nF': SI prefix chosen so the mantissa lands in [1, 1000).

    None -> '--' (a failed/absent reading must never crash the readout).
    Values below the smallest prefix keep it (e.g. '0.05 pF').
    """
    if value is None:
        return '--'
    value = float(value)
    if unit in _NO_PREFIX:
        return f'{value:.{digits}g} {unit}'.rstrip()
    if value == 0:
        return f'0 {unit}'
    mag = abs(value)
    scale, prefix = _PREFIXES[0]
    for s, p in _PREFIXES:
        if mag >= s:
            scale, prefix = s, p
    return f'{value / scale:.{digits}g} {prefix}{unit}'


def format_measurement(mode, primary, secondary):
    """(mode, primary, secondary) -> (primary_str, secondary_str).

    e.g. ('CPD', 3.3e-9, 0.0012) -> ('3.3 nF', 'D: 0.0012').
    Unknown modes fall back to bare numbers rather than raising.
    """
    p_unit, s_name, s_unit = MODE_UNITS.get((mode or '').upper(),
                                            ('', '', ''))
    p_str = format_si(primary, p_unit)
    s_str = format_si(secondary, s_unit)
    if s_name:
        s_str = f'{s_name}: {s_str}'
    return p_str, s_str


# A reading this large is an instrument saying "no number", never a value.
# SCPI's codes for that are 9.9E37 (INFinity), -9.9E37 (NINFinity) and
# 9.91E37 (NAN). What the BK 894 itself answers on an open fixture or an
# overload has not been bench-checked (issue #40 asks for it).
OVERLOAD_CODE = 9.9e37


def as_number(value):
    """A reading as a finite float, or None when it carries no number.

    None, NaN, infinities, booleans, unparseable strings and the SCPI
    overload codes (anything at or above OVERLOAD_CODE in size) all come back
    None, so a failed read can never be displayed as a value."""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or abs(v) >= OVERLOAD_CODE:
        return None
    return v


def json_safe_raw(value):
    """arss: a raw reading for a JSON body. Finite floats pass through
    unchanged (9.9e37 included, it is finite); NaN and infinities become
    their string form, because Flask would otherwise emit bare NaN/Infinity
    tokens that JSON.parse rejects."""
    if value is None or isinstance(value, bool):
        return value
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(v):
        return str(v)
    return value


def display_reading(mode, primary, secondary, status):
    """arss (issue #40): the display-safe view of one raw reading.

    Returns a dict for the API: values mapped through as_number (None for
    overload/invalid), the units and secondary name for the mode, the
    formatted text, and an 'overload' flag when a raw value was present but
    carried no number."""
    mode_key = (mode or '').upper()
    p_unit, s_name, s_unit = MODE_UNITS.get(mode_key, ('', '', ''))
    p = as_number(primary)
    s = as_number(secondary)
    p_text, s_text = format_measurement(mode_key, p, s)
    overload = ((primary is not None and p is None)
                or (secondary is not None and s is None))
    if status is None:
        status_text = '--'
    elif status == 0:
        status_text = 'OK'
    else:
        # The meaning of nonzero BK894 status codes is not documented here
        # yet; show the code rather than guess.
        status_text = f'status {status}'
    return {
        'mode': mode_key or None,
        'primary': p,
        'secondary': s,
        'primary_unit': p_unit,
        'secondary_name': s_name,
        'secondary_unit': s_unit,
        'primary_text': 'OVL' if (primary is not None and p is None) else p_text,
        'secondary_text': (f'{s_name}: OVL' if s_name else 'OVL')
        if (secondary is not None and s is None) else s_text,
        'status_text': status_text,
        'overload': overload,
    }
