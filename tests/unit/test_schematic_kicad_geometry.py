from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.schematic.kicad_geometry import (
    _transform, measured_scene_from_kicad, parse_kicad_sexpr,
)


MINIMAL_KICAD = r"""(kicad_sch
  (version 20260306)
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (rectangle (start -1 -1) (end 1 1))
        (pin passive line
          (at -3 0 0) (length 2)
          (name "~") (number "1"))
      )
    )
  )
  (symbol
    (lib_id "Device:R") (at 30 40 0) (unit 1)
    (property "Reference" "R7" (at 30 36 0)
      (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 30 44 0)
      (effects (font (size 1.27 1.27))))
  )
  (wire (pts (xy 10 40) (xy 27 40)) (uuid "wire-id"))
  (junction (at 10 40) (diameter 0))
  (label "SIG" (at 10 40 0) (effects (font (size 1.27 1.27))))
)"""


class KiCadGeometryExtractionTests(unittest.TestCase):
    def test_parser_is_balanced_and_understands_strings(self) -> None:
        self.assertEqual(parse_kicad_sexpr(MINIMAL_KICAD)[0], "kicad_sch")
        with self.assertRaises(ValueError):
            parse_kicad_sexpr("(kicad_sch (wire)")

    def test_scene_has_actual_vector_and_wire_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.kicad_sch"
            path.write_text(MINIMAL_KICAD)
            scene, coverage = measured_scene_from_kicad(path)
        entities = {i.id: i for i in scene.items}
        self.assertEqual(
            entities["R7:graphic"].bounds.as_dict(),
            {"left": 29.0, "top": 39.0, "right": 31.0, "bottom": 41.0},
        )
        self.assertEqual(
            entities["R7.1:terminal"].bounds.left, 27.0
        )
        self.assertEqual(
            entities["wire:0:0"].segment, ((10.0, 40.0), (27.0, 40.0))
        )
        self.assertEqual(entities["R7:field:Value"].measurement, "estimated")
        self.assertEqual(coverage["wire_net_attribution"], "not_yet_available")
        self.assertEqual(coverage["geometry_qa_gate"], "not_enabled")

    def test_optional_xml_netlist_attribution_preserves_all_measured_geometry(self):
        xml = (
            '<export><components>'
            '<comp ref="R7"><libsource lib="Device" part="R"/></comp>'
            '</components><libparts><libpart lib="Device" part="R">'
            '<pins><pin num="1"/></pins></libpart></libparts>'
            '<nets><net code="5" name="SIG">'
            '<node ref="R7" pin="1"/></net></nets></export>'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.kicad_sch"
            path.write_text(MINIMAL_KICAD)
            original, _ = measured_scene_from_kicad(path)
            attributed, coverage = measured_scene_from_kicad(path, netlist_xml=xml)
        original_by_id = {item.id: item for item in original.items}
        by_id = {item.id: item for item in attributed.items}
        self.assertEqual(set(by_id), set(original_by_id))
        self.assertEqual(by_id["wire:0:0"].segment,
                         original_by_id["wire:0:0"].segment)
        self.assertEqual(by_id["wire:0:0"].net, "SIG")
        self.assertIn("R7.1:terminal", by_id["wire:0:0"].attachments)
        self.assertEqual(coverage["wire_net_attribution"], "verified_xml_complete")
        self.assertEqual(coverage["attributed_segments"], 1)
        self.assertEqual(coverage["geometry_qa_gate"], "not_enabled")



def _scene_ids(source: str) -> set[str]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sample.kicad_sch"
        path.write_text(source)
        scene, _coverage = measured_scene_from_kicad(path)
    return {item.id for item in scene.items}


def _symbol_with(flags: str, pin_name: str = '"IN"', pin_effects: str = "") -> str:
    return rf"""(kicad_sch
  (version 20260306)
  (lib_symbols
    (symbol "Device:R"
      {flags}
      (symbol "R_1_1"
        (rectangle (start -1 -1) (end 1 1))
        (pin passive line
          (at -3 0 0) (length 2)
          (name {pin_name}) (number "1"{pin_effects}))
      )
    )
  )
  (symbol
    (lib_id "Device:R") (at 30 40 0) (unit 1)
    (property "Reference" "R7" (at 30 36 0) (effects (font (size 1.27 1.27))))
  )
)"""


class SymbolTransformTests(unittest.TestCase):
    """KiCad rotates a symbol and then mirrors it in page space (checked against kicad-cli by the sandbox's
    ``transform_oracle`` probe), so a quarter turn and a mirror do not commute."""

    def assertPoint(self, actual, expected):
        self.assertAlmostEqual(actual[0], expected[0], places=6)
        self.assertAlmostEqual(actual[1], expected[1], places=6)

    def test_rotation_is_counter_clockwise_on_the_page(self):
        # A pin on the library's +x axis points up the page after a quarter turn (library Y is up, page Y is down).
        self.assertPoint(_transform(2, 0, 10, 10, 90, None), (10, 8))
        self.assertPoint(_transform(2, 0, 10, 10, 180, None), (8, 10))

    def test_mirror_x_flips_the_page_vertically_after_the_rotation(self):
        self.assertPoint(_transform(2, 1, 0, 0, 90, "x"), (-1, 2))
        self.assertPoint(_transform(2, 1, 0, 0, 270, "x"), (1, -2))

    def test_mirror_y_flips_the_page_horizontally_after_the_rotation(self):
        self.assertPoint(_transform(2, 1, 0, 0, 90, "y"), (1, -2))
        self.assertPoint(_transform(2, 1, 0, 0, 270, "y"), (-1, 2))

    def test_mirror_and_half_turn_commute(self):
        for mirror in ("x", "y"):
            self.assertPoint(_transform(2, 1, 5, 5, 180, mirror), _transform(2, 1, 5, 5, 0, "y" if mirror == "x" else "x"))


class HiddenPinTextTests(unittest.TestCase):
    """KiCad hides a symbol's pin numbers and names with a symbol-level flag; no text is drawn then."""

    def test_pin_text_is_present_when_the_symbol_does_not_hide_it(self) -> None:
        ids = _scene_ids(_symbol_with(""))
        self.assertIn("R7.1:number", ids)
        self.assertIn("R7.1:name", ids)

    def test_symbol_level_hide_removes_pin_numbers_and_names(self) -> None:
        ids = _scene_ids(_symbol_with("(pin_numbers (hide yes)) (pin_names (offset 0) (hide yes))"))
        self.assertNotIn("R7.1:number", ids)
        self.assertNotIn("R7.1:name", ids)
        self.assertIn("R7.1:terminal", ids)       # the pin itself is still there
        self.assertIn("R7.1:stroke", ids)

    def test_numbers_and_names_are_hidden_independently(self) -> None:
        only_numbers = _scene_ids(_symbol_with("(pin_numbers (hide yes))"))
        self.assertNotIn("R7.1:number", only_numbers)
        self.assertIn("R7.1:name", only_numbers)
        only_names = _scene_ids(_symbol_with("(pin_names (offset 0.254) (hide yes))"))
        self.assertIn("R7.1:number", only_names)
        self.assertNotIn("R7.1:name", only_names)

    def test_legacy_bare_hide_token_and_hide_no_are_understood(self) -> None:
        self.assertNotIn("R7.1:number", _scene_ids(_symbol_with("(pin_numbers hide)")))
        self.assertIn("R7.1:number", _scene_ids(_symbol_with("(pin_numbers (hide no))")))

    def test_a_pin_that_hides_its_own_text_still_does(self) -> None:
        ids = _scene_ids(_symbol_with("", pin_effects=" (effects (hide yes))"))
        self.assertNotIn("R7.1:number", ids)


class TextEstimateCalibrationTests(unittest.TestCase):
    """The estimated boxes are calibrated against KiCad's own rendering (FINDINGS section 11)."""

    def scene_items(self, flags: str = ""):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.kicad_sch"
            path.write_text(_symbol_with(flags))
            scene, _coverage = measured_scene_from_kicad(path)
        return {item.id: item for item in scene.items}

    def test_text_is_about_as_wide_as_kicad_draws_it(self):
        from backend.schematic.kicad_geometry import _text_width
        # KiCad's stroke font measured at the default 1.27 mm size: 3 glyphs about 3.2 mm, 8 about 8.9 mm.
        self.assertAlmostEqual(_text_width("GND", 1.27), 3.2, delta=0.3)
        self.assertAlmostEqual(_text_width("OSC_OUT", 1.27), 8.0, delta=0.9)
        self.assertGreater(_text_width("+3V3", 1.27), _text_width("GND", 1.27))

    def test_a_pin_number_sits_above_a_horizontal_pin(self):
        number = self.scene_items()["R7.1:number"].bounds
        pin_y = 40.0
        self.assertLess(number.bottom, pin_y)             # clear of the pin line, on its upper side
        self.assertAlmostEqual((number.left + number.right) / 2, 28.0, places=3)   # over the pin's midpoint

    def test_a_pin_name_runs_inward_from_the_pins_inner_end(self):
        name = self.scene_items()["R7.1:name"].bounds
        inner_end_x = 29.0                                # the pin is 2 mm long, ending at the body
        self.assertGreaterEqual(name.left, inner_end_x + 0.5 - 1e-6)    # past the default pin-name offset
        self.assertAlmostEqual((name.top + name.bottom) / 2, 40.0, places=3)

    def test_a_field_turns_with_its_symbol(self):
        def value_box(rotation: int):
            source = MINIMAL_KICAD.replace("(at 30 40 0) (unit 1)", f"(at 30 40 {rotation}) (unit 1)")
            source = source.replace('(property "Value" "10k" (at 30 44 0)', '(property "Value" "10kohm" (at 30 44 0)')
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "sample.kicad_sch"
                path.write_text(source)
                scene, _coverage = measured_scene_from_kicad(path)
            return {item.id: item for item in scene.items}["R7:field:Value"].bounds

        upright = value_box(0)
        turned = value_box(90)
        self.assertGreater(upright.right - upright.left, upright.bottom - upright.top)      # reads left to right
        self.assertGreater(turned.bottom - turned.top, turned.right - turned.left)          # reads up the page
        half_turn = value_box(180)
        self.assertGreater(half_turn.right - half_turn.left, half_turn.bottom - half_turn.top)

    def test_a_local_label_is_centred_above_its_anchor(self):
        source = MINIMAL_KICAD.replace('(label "SIG" (at 10 40 0)', '(label "SIGNAL" (at 10 40 0)')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.kicad_sch"
            path.write_text(source)
            scene, _coverage = measured_scene_from_kicad(path)
        box = {item.id: item for item in scene.items}["label:0"].bounds
        self.assertAlmostEqual((box.left + box.right) / 2, 10.0, places=3)      # centred along the wire
        self.assertLess(box.top, 40.0 - 0.8)                                    # rising above it
        self.assertGreater(box.bottom, 40.0)                                    # descenders just below

    def test_a_global_label_runs_away_from_its_anchor(self):
        source = MINIMAL_KICAD.replace(
            '(label "SIG" (at 10 40 0) (effects (font (size 1.27 1.27))))',
            '(global_label "SIG" (shape input) (at 10 40 180) (effects (font (size 1.27 1.27))))',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.kicad_sch"
            path.write_text(source)
            scene, _coverage = measured_scene_from_kicad(path)
        box = {item.id: item for item in scene.items}["global_label:0"].bounds
        self.assertAlmostEqual(box.right, 10.0, places=3)      # rotation 180: the flag is at the anchor, text to its left
        self.assertLess(box.left, 10.0 - 3.0)                  # a flag and three glyphs, well over 3 mm

    def test_the_pin_name_offset_of_the_symbol_is_used(self):
        wide = self.scene_items("(pin_names (offset 1.5))")["R7.1:name"].bounds
        self.assertGreaterEqual(wide.left, 29.0 + 1.5 - 1e-6)


if __name__ == "__main__":
    unittest.main()
