import type { FloorEdge, FloorNode } from "../api";

export interface LayoutPosition {
  x: number;
  y: number;
}

const X_SPACING = 240;
const Y_SPACING = 120;

/**
 * A simple layered left-to-right layout: each node's layer is the longest
 * path from any source (a node with no incoming edge), so stocks feeding a
 * center sit left of it and routing steps read in process order. Nodes
 * within a layer stack vertically, ordered by id for stability.
 */
export function layoutFloor(
  nodes: FloorNode[],
  edges: FloorEdge[],
): Record<string, LayoutPosition> {
  const ids = nodes.map((node) => node.id);
  const outgoing = new Map<string, string[]>();
  const incomingCount = new Map<string, number>();
  for (const id of ids) {
    outgoing.set(id, []);
    incomingCount.set(id, 0);
  }
  for (const edge of edges) {
    if (!outgoing.has(edge.source) || !incomingCount.has(edge.target)) continue;
    outgoing.get(edge.source)!.push(edge.target);
    incomingCount.set(edge.target, (incomingCount.get(edge.target) ?? 0) + 1);
  }

  const layer = new Map<string, number>();
  const queue: string[] = [];
  const remaining = new Map(incomingCount);
  for (const id of ids) {
    if (remaining.get(id) === 0) {
      layer.set(id, 0);
      queue.push(id);
    }
  }

  // Kahn's algorithm, tracking the longest path (max predecessor layer + 1)
  // so a node fed by two chains lands after both of them.
  let cursor = 0;
  const visitedEdgeCount = new Map<string, number>();
  while (cursor < queue.length) {
    const current = queue[cursor++];
    const currentLayer = layer.get(current) ?? 0;
    for (const next of outgoing.get(current) ?? []) {
      const seen = (visitedEdgeCount.get(next) ?? 0) + 1;
      visitedEdgeCount.set(next, seen);
      layer.set(next, Math.max(layer.get(next) ?? 0, currentLayer + 1));
      const stillWaiting = (remaining.get(next) ?? 0) - seen;
      if (stillWaiting <= 0 && !queue.includes(next)) {
        queue.push(next);
      }
    }
  }
  // Anything not reached (isolated or part of a cycle we didn't guard) gets layer 0.
  for (const id of ids) {
    if (!layer.has(id)) layer.set(id, 0);
  }

  const byLayer = new Map<number, string[]>();
  for (const id of ids) {
    const l = layer.get(id) ?? 0;
    if (!byLayer.has(l)) byLayer.set(l, []);
    byLayer.get(l)!.push(id);
  }

  const positions: Record<string, LayoutPosition> = {};
  for (const [l, layerIds] of byLayer) {
    layerIds.sort();
    layerIds.forEach((id, index) => {
      positions[id] = { x: l * X_SPACING, y: index * Y_SPACING };
    });
  }
  return positions;
}
