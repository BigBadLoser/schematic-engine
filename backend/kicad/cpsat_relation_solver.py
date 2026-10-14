from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ortools.sat.python import cp_model


@dataclass(frozen=True)
class PlacementCandidate:
    candidate_id: str
    member: str
    junction_id: str
    cost: int


@dataclass(frozen=True)
class PlacementSolution:
    junction_id: str
    candidate_ids: tuple[str, ...]
    objective: int
    status: str


def solve_relation_group(
    *,
    required_members: Iterable[str],
    junction_costs: dict[str, int],
    candidates: Iterable[PlacementCandidate],
    incompatible_pairs: Iterable[tuple[str, str]] = (),
    max_time_seconds: float = 2.0,
) -> PlacementSolution:
    candidate_list = list(candidates)
    if not junction_costs:
        raise ValueError("junction_costs must be non-empty")
    if not candidate_list:
        raise ValueError("candidates must be non-empty")

    by_id = {item.candidate_id: item for item in candidate_list}
    if len(by_id) != len(candidate_list):
        raise ValueError("candidate_id values must be unique")

    members = sorted(set(required_members))
    if not members:
        raise ValueError("required_members must be non-empty")
    unexpected = {item.member for item in candidate_list} - set(members)
    if unexpected:
        raise ValueError("candidates reference unexpected members: " + ", ".join(sorted(unexpected)))
    for member in members:
        if not any(item.member == member for item in candidate_list):
            raise ValueError(f"member {member} has no candidates")

    model = cp_model.CpModel()
    junction_vars = {
        junction_id: model.new_bool_var(f"junction::{junction_id}")
        for junction_id in sorted(junction_costs)
    }
    model.add_exactly_one(junction_vars.values())

    candidate_vars = {
        item.candidate_id: model.new_bool_var(f"candidate::{item.candidate_id}")
        for item in candidate_list
    }

    for member in members:
        model.add_exactly_one(
            candidate_vars[item.candidate_id]
            for item in candidate_list
            if item.member == member
        )

    for item in candidate_list:
        if item.junction_id not in junction_vars:
            raise ValueError(
                f"candidate {item.candidate_id} references unknown junction "
                f"{item.junction_id}"
            )
        model.add_implication(
            candidate_vars[item.candidate_id],
            junction_vars[item.junction_id],
        )

    for first_id, second_id in incompatible_pairs:
        if first_id not in candidate_vars or second_id not in candidate_vars:
            raise ValueError("incompatible pair references unknown candidate")
        model.add(candidate_vars[first_id] + candidate_vars[second_id] <= 1)

    objective_terms = [
        int(cost) * junction_vars[junction_id]
        for junction_id, cost in junction_costs.items()
    ]
    objective_terms.extend(
        int(item.cost) * candidate_vars[item.candidate_id]
        for item in candidate_list
    )
    model.minimize(sum(objective_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(max_time_seconds)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise ValueError("CP-SAT relation-group placement is infeasible")

    chosen_junctions = [
        junction_id
        for junction_id, var in junction_vars.items()
        if solver.boolean_value(var)
    ]
    if len(chosen_junctions) != 1:
        raise RuntimeError("CP-SAT returned an invalid junction selection")

    chosen = tuple(
        item.candidate_id
        for item in sorted(candidate_list, key=lambda entry: entry.candidate_id)
        if solver.boolean_value(candidate_vars[item.candidate_id])
    )
    chosen_members = [by_id[candidate_id].member for candidate_id in chosen]
    if len(chosen_members) != len(members) or set(chosen_members) != set(members):
        raise RuntimeError("CP-SAT did not select exactly one candidate per required member")
    return PlacementSolution(
        junction_id=chosen_junctions[0],
        candidate_ids=chosen,
        objective=int(round(solver.objective_value)),
        status="OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE",
    )

