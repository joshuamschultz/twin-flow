import { useMemo } from "react";
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  Position,
  type Edge,
  type Node,
  type NodeProps,
} from "reactflow";
import "reactflow/dist/style.css";
import type { FloorGraph, FloorNode } from "../api";
import { layoutFloor } from "./floorLayout";

function LocationNode({ data }: NodeProps<FloorNode>) {
  return (
    <div className="floor-node floor-node-location">
      <Handle type="target" position={Position.Left} />
      <div className="floor-node-title">{data.label}</div>
      <div className="floor-node-meta">
        {data.capacity !== undefined && <span>capacity {data.capacity}</span>}
        {data.labor_skill && <span>{data.labor_skill}</span>}
        {data.machine && <span>{data.machine}</span>}
        {data.time_model && <span className="tag">{data.time_model}</span>}
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function StockNode({ data }: NodeProps<FloorNode>) {
  return (
    <div className="floor-node floor-node-stock">
      <Handle type="target" position={Position.Left} />
      <div className="floor-node-title">{data.label}</div>
      {data.uom && <div className="floor-node-meta">{data.uom}</div>}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { location: LocationNode, stock: StockNode };

export function FloorMap({ floor }: { floor: FloorGraph }) {
  const { nodes, edges } = useMemo(() => {
    const positions = layoutFloor(floor.nodes, floor.edges);
    const flowNodes: Node<FloorNode>[] = floor.nodes.map((node) => ({
      id: node.id,
      type: node.kind,
      position: positions[node.id] ?? { x: 0, y: 0 },
      data: node,
    }));
    const flowEdges: Edge[] = floor.edges.map((edge, index) => ({
      id: `${edge.source}->${edge.target}-${index}`,
      source: edge.source,
      target: edge.target,
      label: edge.label,
      type: "smoothstep",
      animated: false,
    }));
    return { nodes: flowNodes, edges: flowEdges };
  }, [floor]);

  if (floor.nodes.length === 0) {
    return <div className="empty-state">This model declares no floor nodes.</div>;
  }

  return (
    <div className="floor-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
        nodesDraggable
        nodesConnectable={false}
        elementsSelectable
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
