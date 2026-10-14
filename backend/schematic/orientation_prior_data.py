"""How often humans draw a part each way: generated, do not edit.

Source: scripts/ci/sandbox/probes/conventions/out/parts.csv (every part on the KiCad demo sheets, by extract.py).
Regenerate: PYTHONPATH=. python scripts/ci/sandbox/probes/conventions/make_prior.py
"""
from __future__ import annotations

PRIOR: dict[tuple[str, str], dict] = {
    ('capacitor', 'across_rails'): {'feature': 'axis', 'n': 677, 'shares': {'vertical': 0.969, 'horizontal': 0.031}},
    ('capacitor', 'series'): {'feature': 'axis', 'n': 217, 'shares': {'horizontal': 0.8986, 'vertical': 0.1014}},
    ('capacitor', 'shunt_ground'): {'feature': 'rail_pin_dir', 'n': 315, 'shares': {'down': 0.8921, 'left': 0.0635, 'right': 0.0222, 'up': 0.0222}},
    ('capacitor', 'shunt_supply'): {'feature': 'rail_pin_dir', 'n': 14, 'shares': {'down': 0.5, 'up': 0.4286, 'right': 0.0714}},
    ('capacitor_polarized', 'across_rails'): {'feature': 'axis', 'n': 20, 'shares': {'vertical': 1.0}},
    ('capacitor_polarized', 'shunt_ground'): {'feature': 'rail_pin_dir', 'n': 21, 'shares': {'down': 0.619, 'right': 0.1429, 'up': 0.1429, 'left': 0.0952}},
    ('connector', 'multipin'): {'feature': 'orientation', 'n': 156, 'shares': {'0/none': 0.5321, '180/x': 0.2628, '180/none': 0.1154, '0/x': 0.0705, '90/none': 0.0128, '270/none': 0.0064}},
    ('connector', 'shunt_ground'): {'feature': 'rail_pin_dir', 'n': 22, 'shares': {'right': 0.4091, 'left': 0.3636, 'down': 0.2273}},
    ('diode', 'across_rails'): {'feature': 'axis', 'n': 19, 'shares': {'vertical': 0.8947, 'horizontal': 0.1053}},
    ('diode', 'multipin'): {'feature': 'orientation', 'n': 19, 'shares': {'0/none': 0.6316, '90/none': 0.2105, '270/none': 0.1053, '180/x': 0.0526}},
    ('diode', 'series'): {'feature': 'axis', 'n': 29, 'shares': {'horizontal': 0.5517, 'vertical': 0.4483}},
    ('diode', 'shunt_ground'): {'feature': 'rail_pin_dir', 'n': 62, 'shares': {'down': 0.9032, 'left': 0.0323, 'right': 0.0323, 'up': 0.0323}},
    ('diode', 'shunt_supply'): {'feature': 'rail_pin_dir', 'n': 18, 'shares': {'left': 0.5, 'up': 0.3333, 'right': 0.1667}},
    ('ic', 'multipin'): {'feature': 'orientation', 'n': 208, 'shares': {'0/none': 0.7788, '180/x': 0.0673, '270/none': 0.0625, '270/x': 0.0337, '180/none': 0.0288, '90/x': 0.0288}},
    ('inductor', 'series'): {'feature': 'axis', 'n': 23, 'shares': {'horizontal': 0.8696, 'vertical': 0.1304}},
    ('inductor', 'shunt_supply'): {'feature': 'rail_pin_dir', 'n': 22, 'shares': {'right': 0.5455, 'left': 0.4091, 'up': 0.0455}},
    ('led', 'series'): {'feature': 'axis', 'n': 19, 'shares': {'vertical': 0.8947, 'horizontal': 0.1053}},
    ('led', 'shunt_ground'): {'feature': 'rail_pin_dir', 'n': 41, 'shares': {'down': 0.7805, 'right': 0.2195}},
    ('other', 'multipin'): {'feature': 'orientation', 'n': 336, 'shares': {'0/none': 0.7381, '180/none': 0.0744, '180/x': 0.0744, '90/none': 0.0744, '270/none': 0.0268, '0/x': 0.006, '90/x': 0.006}},
    ('other', 'series'): {'feature': 'axis', 'n': 12, 'shares': {'horizontal': 0.6667, 'vertical': 0.3333}},
    ('other', 'shunt_ground'): {'feature': 'rail_pin_dir', 'n': 22, 'shares': {'down': 0.5455, 'left': 0.3636, 'right': 0.0909}},
    ('other', 'shunt_supply'): {'feature': 'rail_pin_dir', 'n': 10, 'shares': {'left': 0.6, 'right': 0.2, 'down': 0.1, 'up': 0.1}},
    ('resistor', 'multipin'): {'feature': 'orientation', 'n': 24, 'shares': {'0/none': 0.625, '90/none': 0.2917, '0/x': 0.0833}},
    ('resistor', 'series'): {'feature': 'axis', 'n': 463, 'shares': {'horizontal': 0.7343, 'vertical': 0.2657}},
    ('resistor', 'shunt_ground'): {'feature': 'rail_pin_dir', 'n': 279, 'shares': {'down': 0.7312, 'left': 0.1147, 'right': 0.1039, 'up': 0.0502}},
    ('resistor', 'shunt_supply'): {'feature': 'rail_pin_dir', 'n': 416, 'shares': {'up': 0.7644, 'left': 0.1154, 'right': 0.0962, 'down': 0.024}},
    ('source', 'across_rails'): {'feature': 'axis', 'n': 10, 'shares': {'vertical': 1.0}},
    ('source', 'shunt_ground'): {'feature': 'rail_pin_dir', 'n': 39, 'shares': {'down': 0.9487, 'up': 0.0513}},
    ('transformer', 'multipin'): {'feature': 'orientation', 'n': 13, 'shares': {'0/none': 0.7692, '180/x': 0.2308}},
    ('transistor_bjt', 'multipin'): {'feature': 'orientation', 'n': 27, 'shares': {'0/none': 0.5556, '0/x': 0.2963, '180/x': 0.1111, '180/none': 0.037}},
    ('transistor_fet', 'multipin'): {'feature': 'orientation', 'n': 54, 'shares': {'0/none': 0.5185, '90/none': 0.1296, '180/none': 0.1111, '180/x': 0.0741, '0/x': 0.0556, '270/none': 0.0556, '270/x': 0.037, '90/x': 0.0185}},
}
