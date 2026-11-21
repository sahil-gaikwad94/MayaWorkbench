"""Claim DAG validation.

Dependencies order implementation work, so an invalid graph is a modelling
problem the user must resolve — not something the tool reorders or silently
breaks. This module reports:

* dependencies that point at claims outside the selected set;
* self dependencies;
* dependency cycles, with a concrete cycle path for each strongly connected
  component;
* parent problems (missing parent, cross-document parent, parent cycles);
* cross-document dependencies as warnings, because they are legitimate but
  affect ordering.

It also produces a deterministic topological order and depth map for the result
of a valid graph, which the memo uses as its implementation order.
"""

from __future__ import annotations

import heapq
from collections import defaultdict

from mayaworkbench.domain.enums import ClaimKind
from mayaworkbench.domain.models import ClaimDependency, ResearchClaim
from mayaworkbench.research.models import ClaimDagValidation, DagIssue
from mayaworkbench.version import RESEARCH_PROTOCOL_VERSION


def validate_claim_dag(
    claims: list[ResearchClaim],
    edges: list[ClaimDependency],
    *,
    project_id: str,
    document_id: str | None = None,
) -> ClaimDagValidation:
    by_id: dict[str, ResearchClaim] = {}
    issues: list[DagIssue] = []
    for claim in claims:
        if claim.id in by_id:
            issues.append(
                DagIssue(
                    code="DUPLICATE_CLAIM_ID",
                    severity="ERROR",
                    claim_ids=[claim.id],
                    detail="Two claims share the same identifier; storage is inconsistent.",
                )
            )
            continue
        by_id[claim.id] = claim
        if claim.project_id != project_id:
            issues.append(
                DagIssue(
                    code="CROSS_PROJECT_CLAIM",
                    severity="ERROR",
                    claim_ids=[claim.id],
                    detail=f"Claim belongs to project {claim.project_id}, not {project_id}.",
                )
            )

    dependencies: dict[str, list[str]] = defaultdict(list)
    reverse: dict[str, list[str]] = defaultdict(list)
    clean_edges: list[tuple[str, str]] = []
    for edge in edges:
        if edge.claim_id not in by_id:
            continue
        if edge.depends_on_claim_id not in by_id:
            issues.append(
                DagIssue(
                    code="MISSING_DEPENDENCY",
                    severity="ERROR",
                    claim_ids=[edge.claim_id, edge.depends_on_claim_id],
                    detail=(
                        f"{edge.claim_id} depends on {edge.depends_on_claim_id}, which is not "
                        "in the selected claim set."
                    ),
                )
            )
            continue
        if edge.claim_id == edge.depends_on_claim_id:
            issues.append(
                DagIssue(
                    code="SELF_DEPENDENCY",
                    severity="ERROR",
                    claim_ids=[edge.claim_id],
                    detail=f"{edge.claim_id} depends on itself.",
                )
            )
        dependencies[edge.claim_id].append(edge.depends_on_claim_id)
        reverse[edge.depends_on_claim_id].append(edge.claim_id)
        clean_edges.append((edge.claim_id, edge.depends_on_claim_id))
        source_doc = by_id[edge.claim_id].document_id
        target_doc = by_id[edge.depends_on_claim_id].document_id
        if source_doc and target_doc and source_doc != target_doc:
            issues.append(
                DagIssue(
                    code="CROSS_DOCUMENT_DEPENDENCY",
                    severity="WARNING",
                    claim_ids=[edge.claim_id, edge.depends_on_claim_id],
                    detail=(
                        f"{edge.claim_id} ({source_doc}) depends on {edge.depends_on_claim_id} "
                        f"from document {target_doc}."
                    ),
                )
            )

    issues.extend(_parent_issues(by_id))

    components = _strongly_connected(sorted(by_id), dependencies)
    cycle_nodes: set[str] = set()
    cycles: list[list[str]] = []
    for component in components:
        if len(component) == 1 and component[0] not in dependencies.get(component[0], []):
            continue
        cycle_nodes.update(component)
        path = _find_cycle(component, dependencies)
        cycles.append(path)
        issues.append(
            DagIssue(
                code="DEPENDENCY_CYCLE",
                severity="ERROR",
                claim_ids=path,
                detail=(
                    "Dependency cycle: "
                    + " -> ".join(path)
                    + ". Break the cycle by removing or redirecting one edge; nothing was "
                    "reordered automatically."
                ),
            )
        )

    order, depth, unorderable = _topological_order(
        by_id, dependencies, cycle_nodes=cycle_nodes
    )
    roots = sorted(
        claim_id
        for claim_id in by_id
        if claim_id not in cycle_nodes and not dependencies.get(claim_id)
    )
    depended_on = {target for _source, target in clean_edges}
    leaves = sorted(
        claim_id
        for claim_id in by_id
        if claim_id not in cycle_nodes and claim_id not in depended_on
    )

    return ClaimDagValidation(
        protocol_version=RESEARCH_PROTOCOL_VERSION,
        project_id=project_id,
        document_id=document_id,
        claim_count=len(by_id),
        edge_count=len(clean_edges),
        claim_kind_counts=_kind_counts(by_id),
        issues=issues,
        cycles=cycles,
        topological_order=order,
        unorderable=unorderable,
        depth=depth,
        roots=roots,
        leaves=leaves,
    )


def _kind_counts(by_id: dict[str, ResearchClaim]) -> dict[str, int]:
    counts = {kind.value: 0 for kind in ClaimKind}
    for claim in by_id.values():
        counts[claim.kind.value] = counts.get(claim.kind.value, 0) + 1
    return counts


def _parent_issues(by_id: dict[str, ResearchClaim]) -> list[DagIssue]:
    issues: list[DagIssue] = []
    for claim in by_id.values():
        if not claim.parent_claim_id:
            continue
        parent = by_id.get(claim.parent_claim_id)
        if parent is None:
            issues.append(
                DagIssue(
                    code="MISSING_PARENT",
                    severity="ERROR",
                    claim_ids=[claim.id, claim.parent_claim_id],
                    detail=(
                        f"{claim.id} names parent {claim.parent_claim_id}, which is not in the "
                        "selected claim set."
                    ),
                )
            )
            continue
        if claim.document_id and parent.document_id and claim.document_id != parent.document_id:
            issues.append(
                DagIssue(
                    code="CROSS_DOCUMENT_PARENT",
                    severity="WARNING",
                    claim_ids=[claim.id, parent.id],
                    detail=(
                        f"{claim.id} has a parent in a different document "
                        f"({parent.document_id})."
                    ),
                )
            )
        chain: list[str] = []
        cursor: ResearchClaim | None = claim
        while cursor is not None and cursor.parent_claim_id:
            if cursor.id in chain:
                issues.append(
                    DagIssue(
                        code="PARENT_CYCLE",
                        severity="ERROR",
                        claim_ids=[*chain, cursor.id],
                        detail="Parent chain forms a cycle: " + " -> ".join([*chain, cursor.id]),
                    )
                )
                break
            chain.append(cursor.id)
            cursor = by_id.get(cursor.parent_claim_id)
    return issues


def _strongly_connected(
    nodes: list[str], adjacency: dict[str, list[str]]
) -> list[list[str]]:
    """Iterative Kosaraju; recursion would risk a stack overflow on large DAGs."""
    visited: set[str] = set()
    order: list[str] = []
    for start in nodes:
        if start in visited:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        visited.add(start)
        while stack:
            node, index = stack[-1]
            neighbours = adjacency.get(node, [])
            if index < len(neighbours):
                stack[-1] = (node, index + 1)
                neighbour = neighbours[index]
                if neighbour not in visited:
                    visited.add(neighbour)
                    stack.append((neighbour, 0))
                continue
            order.append(node)
            stack.pop()

    reversed_graph: dict[str, list[str]] = defaultdict(list)
    for node, neighbours in adjacency.items():
        for neighbour in neighbours:
            reversed_graph[neighbour].append(node)

    assigned: set[str] = set()
    components: list[list[str]] = []
    for node in reversed(order):
        if node in assigned:
            continue
        component: list[str] = []
        stack = [node]
        assigned.add(node)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbour in reversed_graph.get(current, []):
                if neighbour not in assigned:
                    assigned.add(neighbour)
                    stack.append(neighbour)
        components.append(sorted(component))
    return components


def _find_cycle(component: list[str], adjacency: dict[str, list[str]]) -> list[str]:
    members = set(component)
    start = component[0]
    path: list[str] = []
    on_path: dict[str, int] = {}
    stack: list[tuple[str, list[str]]] = [(start, adjacency.get(start, []))]
    while stack:
        node, neighbours = stack[-1]
        if node not in on_path:
            on_path[node] = len(path)
            path.append(node)
        if not neighbours:
            stack.pop()
            if node in on_path:
                del on_path[node]
                path.pop()
            continue
        neighbour = neighbours.pop(0)
        if neighbour not in members:
            continue
        if neighbour in on_path:
            return [*path[on_path[neighbour] :], neighbour]
        stack.append((neighbour, list(adjacency.get(neighbour, []))))
    return [start, start]


def _topological_order(
    by_id: dict[str, ResearchClaim],
    dependencies: dict[str, list[str]],
    *,
    cycle_nodes: set[str],
) -> tuple[list[str], dict[str, int], list[str]]:
    nodes = [claim_id for claim_id in by_id if claim_id not in cycle_nodes]
    in_degree = {node: 0 for node in nodes}
    dependents: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for target in dependencies.get(node, []):
            if target in in_degree:
                in_degree[node] += 1
                dependents[target].append(node)
    heap: list[tuple[int, str]] = [
        (by_id[node].ordinal, node) for node in nodes if in_degree[node] == 0
    ]
    heapq.heapify(heap)
    order: list[str] = []
    depth: dict[str, int] = {}
    while heap:
        _ordinal, node = heapq.heappop(heap)
        order.append(node)
        depth[node] = 1 + max(
            (depth.get(target, 0) for target in dependencies.get(node, []) if target in depth),
            default=-1,
        )
        for dependent in dependents.get(node, []):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                heapq.heappush(heap, (by_id[dependent].ordinal, dependent))
    unorderable = sorted(cycle_nodes)
    return order, depth, unorderable


__all__ = ["validate_claim_dag"]
