"""Electrical geometry attribution must never manufacture KiCad net membership."""
from __future__ import annotations

import unittest

from backend.schematic.net_attribution import attribute_wire_nets
from backend.schematic.presentation_geometry import (
    Bounds, GeometryItem, GeometryKind as K, GeometryScene,
)


def _xml(groups: dict[str, tuple[str, ...]]) -> str:
    refs = sorted({ref for group in groups.values() for ref in group})
    return (
        '<export><components>'
        + ''.join(f'<comp ref="{ref}"><libsource lib="Device" part="R"/></comp>' for ref in refs)
        + '</components><libparts><libpart lib="Device" part="R">'
        + '<pins><pin num="1"/></pins></libpart></libparts><nets>'
        + ''.join(
            f'<net code="{code}" name="NET_{code}">'
            + ''.join(f'<node ref="{ref}" pin="1"/>' for ref in members)
            + '</net>'
            for code, members in groups.items()
        ) + '</nets></export>'
    )


def _pin(ref: str, x: float, y: float) -> GeometryItem:
    return GeometryItem(f"{ref}.1:terminal", K.PIN_ENDPOINT, ref,
                        Bounds(x, y, x, y))


def _wire(tag: str, a: tuple[float, float], b: tuple[float, float]) -> GeometryItem:
    return GeometryItem(tag, K.WIRE, tag, Bounds(
        min(a[0],b[0]),min(a[1],b[1]),
        max(a[0],b[0]),max(a[1],b[1])
    ), segment=(a,b))


def _dot(tag: str, x: float, y: float) -> GeometryItem:
    return GeometryItem(tag,K.JUNCTION,tag,
                        Bounds(x-.25,y-.25,x+.25,y+.25),
                        measurement="estimated")


class ElectricalNetAttributionTests(unittest.TestCase):
    def test_crossing_without_explicit_junction_does_not_short_nets(self):
        scene=GeometryScene.from_items([
            _pin("R1",0,10),_pin("R2",20,10),
            _pin("R3",10,0),_pin("R4",10,20),
            _wire("horizontal",(0,10),(20,10)),
            _wire("vertical",(10,0),(10,20)),
        ])
        result=attribute_wire_nets(scene,netlist_xml=_xml({
            "1":("R1","R2"),"2":("R3","R4"),
        }))
        wires={x.id:x for x in result.scene.items}
        self.assertEqual(wires["horizontal"].net,"NET_1")
        self.assertEqual(wires["vertical"].net,"NET_2")
        self.assertEqual(result.coverage["disputed_wire_islands"],0)
        self.assertEqual(result.coverage["attributed_segments"],2)

    def test_explicit_junction_joins_three_branches_to_one_net(self):
        scene=GeometryScene.from_items([
            _pin("R1",0,10),_pin("R2",20,10),_pin("R3",10,0),
            _wire("h1",(0,10),(10,10)),
            _wire("h2",(10,10),(20,10)),
            _wire("v",(10,0),(10,10)),
            _dot("junction:0",10,10),
        ])
        result=attribute_wire_nets(scene,netlist_xml=_xml({"1":("R1","R2","R3")}))
        parts={x.id:x for x in result.scene.items}
        self.assertTrue(all(parts[x].net=="NET_1" for x in ("h1","h2","v","junction:0")))
        self.assertIn("junction:0",parts["v"].attachments)

    def test_wire_island_with_conflicting_authoritative_pins_stays_unknown(self):
        scene=GeometryScene.from_items([
            _pin("R1",0,0),_pin("R2",20,0),
            _wire("w",(0,0),(20,0)),
        ])
        result=attribute_wire_nets(scene,netlist_xml=_xml({
            "1":("R1",),"2":("R2",),
        }))
        self.assertIsNone(next(x for x in result.scene.items if x.id=="w").net)
        self.assertEqual(result.coverage["disputed_wire_islands"],1)
        self.assertEqual(result.coverage["unattributed_segments"],1)

    def test_unseeded_wires_are_never_inferred_from_nearby_labels(self):
        scene=GeometryScene.from_items([
            _pin("R1",0,0),
            _wire("wire",(20,0),(30,0)),
            GeometryItem("label",K.NET_LABEL,"label",
                         Bounds(19,0,31,2),net="NET_1",measurement="estimated"),
        ])
        result=attribute_wire_nets(scene,netlist_xml=_xml({"1":("R1",)}))
        self.assertIsNone(next(x for x in result.scene.items if x.id=="wire").net)
        self.assertEqual(result.coverage["unattributed_segments"],1)

    def test_collinear_overlap_propagates_one_verified_net(self):
        scene=GeometryScene.from_items([
            _pin("R1",0,0),_pin("R2",30,0),
            _wire("w1",(0,0),(20,0)),
            _wire("w2",(10,0),(30,0)),
        ])
        result=attribute_wire_nets(scene,netlist_xml=_xml({"1":("R1","R2")}))
        self.assertEqual([
            x.net for x in result.scene.items if x.kind==K.WIRE
        ],["NET_1","NET_1"])

    def test_unmarked_crossing_of_same_net_is_not_excused_by_distant_junction(self):
        scene=GeometryScene.from_items([
            _pin("R1",0,0),_pin("R2",10,-10),
            _wire("horizontal",(0,0),(20,0)),
            _wire("vertical",(10,-10),(10,10)),
            _wire("right",(20,0),(20,10)),
            _wire("bottom",(20,10),(10,10)),
            _dot("junction:0",20,10),
        ])
        result=attribute_wire_nets(scene,netlist_xml=_xml({"1":("R1","R2")}))
        items={x.id:x for x in result.scene.items}
        self.assertEqual(items["horizontal"].net,"NET_1")
        self.assertEqual(items["vertical"].net,"NET_1")
        self.assertNotIn("junction:0",items["horizontal"].attachments)
        self.assertNotIn("junction:0",items["vertical"].attachments)
        pair = {("horizontal","vertical"),("vertical","horizontal")}
        self.assertTrue(any(
            (conflict.first,conflict.second) in pair
            and conflict.reason=="wire_crossing_without_shared_junction"
            for conflict in result.scene.conflicts()
        ))

    def test_perpendicular_t_contact_without_dot_stays_unmerged(self):
        scene=GeometryScene.from_items([
            _pin("R1",0,10),_pin("R2",10,0),
            _wire("through",(0,10),(20,10)),
            _wire("stem",(10,0),(10,10)),
        ])
        result=attribute_wire_nets(scene,netlist_xml=_xml({
            "1":("R1",),"2":("R2",),
        }))
        wires={x.id:x.net for x in result.scene.items if x.kind==K.WIRE}
        self.assertEqual(wires,{"through":"NET_1","stem":"NET_2"})


if __name__=="__main__":
    unittest.main()
