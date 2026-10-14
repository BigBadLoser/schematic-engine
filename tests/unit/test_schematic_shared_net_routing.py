"""Three-plus pin nets are proposed as one measured electrical tree."""
from __future__ import annotations

import unittest
from pathlib import Path

from backend.schematic.intent import NetIntent, PinEndpoint
from backend.schematic.presentation_geometry import (
    Bounds, GeometryItem, GeometryKind as K, GeometryScene,
)
from backend.schematic.shared_net_routing import _path_hits_pin, plan_shared_net
from scripts.schematic_validation_fixtures import rc_filter


def scene(*points, blockers=()):
    items=[
        GeometryItem(f"{ref}.{pin}:terminal",K.PIN_ENDPOINT,ref,
                     Bounds(x,y,x,y))
        for ref,pin,x,y in points
    ]
    items.extend(
        GeometryItem(f"obstacle:{i}",K.BODY,f"obstacle:{i}",Bounds(*bbox))
        for i,bbox in enumerate(blockers)
    )
    return GeometryScene.from_items(items)


class SharedElectricalNetTests(unittest.TestCase):
    def setUp(self):
        self.net=NetIntent(
            "CLK", (PinEndpoint("A","1"),PinEndpoint("B","1"),PinEndpoint("C","1")),
            routing="wire",
        )
        self.geometry=scene(
            ("A","1",0,0),("B","1",10.16,10.16),("C","1",10.16,-10.16),
        )

    def test_steiner_junction_reduces_total_shared_wire_cost(self):
        result=plan_shared_net(self.net,self.geometry)
        self.assertEqual(result.strategy,"steiner_mst")
        self.assertEqual(result.junction,(10.16,0))
        self.assertEqual(len(result.edges),3)
        self.assertEqual(result.estimated_length_mm,30.48)
        self.assertEqual({part["net"] for part in result.edges},{"CLK"})
        self.assertTrue(any("point1" in e or "point2" in e for e in result.edges))

    def test_edge_ids_are_unique_between_nets_in_one_grouped_transaction(self):
        first=plan_shared_net(self.net,self.geometry)
        different=NetIntent(
            "OSC_OUT", self.net.endpoints, routing="wire",
        )
        second=plan_shared_net(different,self.geometry)
        ids=[e["id"] for p in (first,second) for e in p.edges]
        self.assertEqual(len(ids),len(set(ids)))

    def test_body_containing_junction_is_not_selected(self):
        blocked=scene(
            ("A","1",0,0),("B","1",10.16,10.16),("C","1",10.16,-10.16),
            blockers=((8.89,-1.27,11.43,1.27),),
        )
        plan=plan_shared_net(self.net,blocked)
        if plan.junction is not None:
            x,y=plan.junction
            self.assertFalse(8.89<=x<=11.43 and -1.27<=y<=1.27)

    def test_junction_moves_when_a_branch_would_pass_through_another_nets_pin(self):
        net=NetIntent(
            "FB",(PinEndpoint("U","2"),PinEndpoint("R","2"),PinEndpoint("S","1")),
            routing="wire",
        )
        base=(("U","2",91.44,147.32),("R","2",91.44,123.19),("S","1",157.48,137.16))
        # Alone, the best junction is in the column the two left terminals share.
        self.assertEqual(plan_shared_net(net,scene(*base)).junction,(91.44,137.16))
        # Another net's pin on that column, between U.2 and the junction: the straight
        # branch from U.2 would be laid through it and join the two nets.
        plan=plan_shared_net(net,scene(*base,("X","3",91.44,142.24)))
        self.assertIsNotNone(plan.junction)
        self.assertNotEqual(plan.junction,(91.44,137.16))

    def test_a_foreign_pin_that_no_branch_touches_changes_nothing(self):
        net=NetIntent(
            "FB",(PinEndpoint("U","2"),PinEndpoint("R","2"),PinEndpoint("S","1")),
            routing="wire",
        )
        base=(("U","2",91.44,147.32),("R","2",91.44,123.19),("S","1",157.48,137.16))
        plan=plan_shared_net(net,scene(*base,("X","3",100.0,142.24)))
        self.assertEqual(plan.junction,(91.44,137.16))

    def test_no_straight_junction_branch_ever_passes_through_another_nets_pin(self):
        # The tree without a junction is always available and never penalised for this, so a
        # junction whose aligned branch crosses a foreign pin can never be the planner's answer.
        import random
        rng=random.Random(7)
        grid=1.27

        def snap(value):
            return round(round(value/grid)*grid,6)

        checked=0
        for case in range(150):
            count=rng.choice((3,4))
            wanted=count+rng.randint(1,6)
            points=[]
            while len(points)<wanted:
                point=(snap(rng.uniform(0,40)),snap(rng.uniform(0,40)))
                if point not in points:
                    points.append(point)
            # One symbol per pin, as in a real scene: the planner keeps clear of each symbol's box.
            terminals=[(f"T{i}","1",x,y) for i,(x,y) in enumerate(points[:count])]
            foreign=[(f"F{i}","1",x,y) for i,(x,y) in enumerate(points[count:])]
            net=NetIntent(
                "N",tuple(PinEndpoint(f"T{i}","1") for i in range(count)),routing="wire",
            )
            plan=plan_shared_net(net,scene(*terminals,*foreign))
            if plan.junction is None:
                continue
            keep=1.5*grid
            jx,jy=plan.junction
            for _,_,px,py in (*terminals,*foreign):
                self.assertFalse(
                    abs(px-jx) < keep and abs(py-jy) < keep,
                    ("junction inside a symbol's keep-out",case,plan.junction,(px,py)),
                )
            at={f"T{i}.1":(x,y) for i,(_,_,x,y) in enumerate(terminals)}
            foreign_points=[(x,y) for _,_,x,y in foreign]
            for edge in plan.edges:
                ends=[
                    at[f"{edge[f'ref{s}']}.{edge[f'pin{s}']}"] if f"ref{s}" in edge
                    else tuple(edge[f"point{s}"])
                    for s in ("1","2")
                ]
                if plan.junction in ends and (
                        ends[0][0]==ends[1][0] or ends[0][1]==ends[1][1]):
                    checked+=1
                    self.assertFalse(
                        _path_hits_pin(((ends[0],ends[1]),),foreign_points),
                        (case,ends,foreign_points),
                    )
        self.assertGreater(checked,50)   # the loop really exercised straight junction branches

    def test_path_through_pin_check_covers_both_axes_and_the_end_points(self):
        pins=[(5.0,5.0)]
        self.assertTrue(_path_hits_pin((((5.0,0.0),(5.0,10.0)),),pins))   # vertical, interior
        self.assertTrue(_path_hits_pin((((0.0,5.0),(10.0,5.0)),),pins))   # horizontal, interior
        self.assertTrue(_path_hits_pin((((5.0,5.0),(5.0,10.0)),),pins))   # pin at a segment end
        self.assertFalse(_path_hits_pin((((5.0,0.0),(5.0,4.0)),),pins))   # stops short of it
        self.assertFalse(_path_hits_pin((((6.0,0.0),(6.0,10.0)),),pins))   # next column over
        self.assertFalse(_path_hits_pin((((5.0,5.0),(5.0,5.0)),),pins))   # zero-length segment

    def test_junction_keeps_the_routers_clearance_from_other_symbols(self):
        pins=(("A","1",0,0),("B","1",10.16,10.16),("C","1",10.16,-10.16))
        # The router keeps 1.5 grids (1.905 mm) from a symbol. A body 1.5 mm above the natural
        # junction puts it inside that margin, where a route would have to cut through the symbol.
        near=scene(*pins,blockers=((9.5,1.5,10.8,2.5),))
        self.assertNotEqual(plan_shared_net(self.net,near).junction,(10.16,0))
        far=scene(*pins,blockers=((30.0,0.0,31.0,1.0),))
        self.assertEqual(plan_shared_net(self.net,far).junction,(10.16,0))

    def test_missing_measurement_rejected(self):
        with self.assertRaisesRegex(ValueError,"Missing measured"):
            plan_shared_net(self.net,scene(("A","1",0,0)))

    def test_plan_is_reference_and_circuit_independent(self):
        new=NetIntent(
            "VOUT", (PinEndpoint("K9","1"),PinEndpoint("R2","5"),
                      PinEndpoint("J7","2")),routing="wire"
        )
        geom=scene(
            ("K9","1",0,0),("R2","5",10.16,10.16),("J7","2",10.16,-10.16)
        )
        result=plan_shared_net(new,geom)
        self.assertEqual(result.estimated_length_mm,
                         plan_shared_net(self.net,self.geometry).estimated_length_mm)
        self.assertEqual(result.strategy,"steiner_mst")

    def test_fixture_multi_terminal_nets_use_plan_not_driver_star(self):
        circuit=rc_filter()
        net=next(net for net in circuit.nets if net.name=="VOUT")
        self.assertEqual(len(net.endpoints),3)
        self.assertEqual(
            set(ep.token for ep in net.endpoints),
            {"R1.2","C1.1","J2.1"},
        )

    def test_runtime_patch_never_shadows_global_geometry_reader(self):
        # The grouped route substitution is now checked in, not applied at
        # runtime. A function-local import would shadow the module-level
        # geometry reader and fail with UnboundLocalError, even if that
        # import appears in a branch.
        import ast
        root=Path(__file__).resolve().parents[2]
        module=ast.parse(
            (root/"backend/schematic/direct_workflow.py").read_text()
        )
        methods=[
            node for node in ast.walk(module)
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))
            and node.name=="_route_semantic_nets"
        ]
        self.assertEqual(len(methods),1)
        for node in ast.walk(methods[0]):
            if isinstance(node,ast.ImportFrom):
                self.assertNotIn(
                    "measured_scene_from_kicad",
                    {alias.asname or alias.name for alias in node.names},
                )

    def test_grouped_routing_integration_keeps_safe_baseline(self):
        # The grouped router and grouped relation routing are part of the
        # checked-in source (no CI-time source rewriting).
        root=Path(__file__).resolve().parents[2]
        workflow=(root/"backend/schematic/direct_workflow.py").read_text()
        router=(root/"backend/kicad/mcp_extended_server.py").read_text()
        self.assertIn('len(endpoints) >= 3',workflow)
        self.assertIn('"protect_wires":',workflow)
        self.assertIn('from .shared_net_routing import plan_shared_net',workflow)
        self.assertIn('pending_local_connections.extend(topology.edges)',workflow)
        self.assertIn('protect_wires',router)
        self.assertIn("treat_existing_as_foreign",router)
        self.assertIn("foreign pin",router)


if __name__=="__main__":
    unittest.main()
