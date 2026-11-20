"""Tiny max-flow (Edmonds–Karp) for feasibility proofs. Graphs here have < 300 nodes."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Hashable


class FlowGraph:
    def __init__(self) -> None:
        self.cap: dict[Hashable, dict[Hashable, int]] = defaultdict(dict)

    def add(self, u: Hashable, v: Hashable, capacity: int) -> None:
        self.cap[u][v] = self.cap[u].get(v, 0) + capacity
        self.cap[v].setdefault(u, 0)

    def max_flow(self, source: Hashable, sink: Hashable) -> tuple[int, dict]:
        """Returns (value, flow) where flow[u][v] is the flow on edge u->v."""
        residual = {u: dict(vs) for u, vs in self.cap.items()}
        total = 0
        while True:
            parent: dict[Hashable, Hashable] = {source: source}
            queue = deque([source])
            while queue and sink not in parent:
                u = queue.popleft()
                for v, c in sorted(residual[u].items(), key=lambda kv: repr(kv[0])):
                    if c > 0 and v not in parent:
                        parent[v] = u
                        queue.append(v)
            if sink not in parent:
                break
            bottleneck, v = None, sink
            while v != source:
                u = parent[v]
                bottleneck = (
                    residual[u][v] if bottleneck is None else min(bottleneck, residual[u][v])
                )
                v = u
            v = sink
            while v != source:
                u = parent[v]
                residual[u][v] -= bottleneck
                residual[v][u] += bottleneck
                v = u
            total += bottleneck
        flow = {
            u: {
                v: self.cap[u][v] - residual[u][v]
                for v in self.cap[u]
                if self.cap[u][v] > 0 and self.cap[u][v] - residual[u][v] > 0
            }
            for u in self.cap
        }
        self._residual = residual
        return total, flow

    def reachable_from(self, source: Hashable) -> set:
        """Nodes reachable in the residual graph after max_flow (the source side of a min cut)."""
        seen, queue = {source}, deque([source])
        while queue:
            u = queue.popleft()
            for v, c in self._residual[u].items():
                if c > 0 and v not in seen:
                    seen.add(v)
                    queue.append(v)
        return seen
