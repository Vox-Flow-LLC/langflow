from typing import Any, Dict, Generator, List, Type, Union

from langflow.graph.edge.base import ContractEdge
from langflow.graph.graph.constants import lazy_load_vertex_dict
from langflow.graph.graph.utils import process_flow
from langflow.graph.vertex.base import Vertex
from langflow.graph.vertex.types import (
    FileToolVertex,
    LLMVertex,
    ToolkitVertex,
)
from langflow.interface.tools.constants import FILE_TOOLS
from langflow.utils import payload
from loguru import logger


class Graph:
    """A class representing a graph of nodes and edges."""

    def __init__(
        self,
        nodes: List[Dict],
        edges: List[Dict[str, str]],
    ) -> None:
        self._nodes = nodes
        self._edges = edges
        self.raw_graph_data = {"nodes": nodes, "edges": edges}

        self.top_level_nodes = []
        for node in self._nodes:
            if node_id := node.get("id"):
                self.top_level_nodes.append(node_id)

        self._graph_data = process_flow(self.raw_graph_data)
        self._nodes = self._graph_data["nodes"]
        self._edges = self._graph_data["edges"]
        self._build_graph()

    def __setstate__(self, state):
        self.__dict__.update(state)
        for edge in self.edges:
            edge.reset()
            edge.validate_edge()

    @classmethod
    def from_payload(cls, payload: Dict) -> "Graph":
        """
        Creates a graph from a payload.

        Args:
            payload (Dict): The payload to create the graph from.˜`

        Returns:
            Graph: The created graph.
        """
        if "data" in payload:
            payload = payload["data"]
        try:
            nodes = payload["nodes"]
            edges = payload["edges"]
            return cls(nodes, edges)
        except KeyError as exc:
            logger.exception(exc)
            raise ValueError(
                f"Invalid payload. Expected keys 'nodes' and 'edges'. Found {list(payload.keys())}"
            ) from exc

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Graph):
            return False
        return self.__repr__() == other.__repr__()

    def _build_graph(self) -> None:
        """Builds the graph from the nodes and edges."""
        self.vertices = self._build_vertices()
        self.edges = self._build_edges()
        for edge in self.edges:
            edge.source.add_edge(edge)
            edge.target.add_edge(edge)

        # This is a hack to make sure that the LLM node is sent to
        # the toolkit node
        self._build_node_params()
        # remove invalid nodes
        self._validate_nodes()

    def _build_node_params(self) -> None:
        """Identifies and handles the LLM node within the graph."""
        llm_node = None
        for node in self.vertices:
            node._build_params()
            if isinstance(node, LLMVertex):
                llm_node = node

        if llm_node:
            for node in self.vertices:
                if isinstance(node, ToolkitVertex):
                    node.params["llm"] = llm_node

    def _validate_nodes(self) -> None:
        """Check that all nodes have edges"""
        if len(self.vertices) == 1:
            return
        for node in self.vertices:
            if not self._validate_node(node):
                raise ValueError(
                    f"{node.vertex_type} is not connected to any other components"
                )

    def _validate_node(self, node: Vertex) -> bool:
        """Validates a node."""
        # All nodes that do not have edges are invalid
        return len(node.edges) > 0

    def get_vertex(self, node_id: str) -> Union[None, Vertex]:
        """Returns a node by id."""
        return next((node for node in self.vertices if node.id == node_id), None)

    def get_nodes_with_target(self, node: Vertex) -> List[Vertex]:
        """Returns the nodes connected to a node."""
        connected_nodes: List[Vertex] = [
            edge.source for edge in self.edges if edge.target == node
        ]
        return connected_nodes

    def build(self) -> Any:
        """Builds the graph."""
        # Get root node
        root_node = payload.get_root_node(self)
        if root_node is None:
            raise ValueError("No root node found")
        return root_node.build()

    def topological_sort(self) -> List[Vertex]:
        """
        Performs a topological sort of the vertices in the graph.

        Returns:
            List[Vertex]: A list of vertices in topological order.

        Raises:
            ValueError: If the graph contains a cycle.
        """
        # States: 0 = unvisited, 1 = visiting, 2 = visited
        state = {node: 0 for node in self.vertices}
        sorted_vertices = []

        def dfs(node):
            if state[node] == 1:
                # We have a cycle
                raise ValueError(
                    "Graph contains a cycle, cannot perform topological sort"
                )
            if state[node] == 0:
                state[node] = 1
                for edge in node.edges:
                    if edge.source == node:
                        dfs(edge.target)
                state[node] = 2
                sorted_vertices.append(node)

        # Visit each node
        for node in self.vertices:
            if state[node] == 0:
                dfs(node)

        return list(reversed(sorted_vertices))

    def layered_topological_sort(self) -> List[List[Vertex]]:
        state = {node: 0 for node in self.nodes}
        layers = []

        def dfs(node, current_layer):
            if state[node] == 1:
                raise ValueError(
                    "Graph contains a cycle, cannot perform topological sort"
                )
            if state[node] == 0:
                state[node] = 1
                for edge in node.edges:
                    if edge.source == node:
                        dfs(edge.target, current_layer + 1)
                state[node] = 2
                while len(layers) <= current_layer:
                    layers.append([])
                layers[current_layer].append(node)
                node.layer = current_layer

        for node in self.nodes:
            if state[node] == 0:
                dfs(node, 0)

        return layers

    def generator_build(self) -> Generator[Vertex, None, None]:
        """Builds each vertex in the graph and yields it."""
        sorted_vertices = self.topological_sort()
        logger.debug("There are %s vertices in the graph", len(sorted_vertices))
        yield from sorted_vertices

    def get_node_neighbors(self, node: Vertex) -> Dict[Vertex, int]:
        """Returns the neighbors of a node."""
        neighbors: Dict[Vertex, int] = {}
        for edge in self.edges:
            if edge.source == node:
                neighbor = edge.target
                if neighbor not in neighbors:
                    neighbors[neighbor] = 0
                neighbors[neighbor] += 1
            elif edge.target == node:
                neighbor = edge.source
                if neighbor not in neighbors:
                    neighbors[neighbor] = 0
                neighbors[neighbor] += 1
        return neighbors

    def _build_edges(self) -> List[ContractEdge]:
        """Builds the edges of the graph."""
        # Edge takes two nodes as arguments, so we need to build the nodes first
        # and then build the edges
        # if we can't find a node, we raise an error

        edges: List[ContractEdge] = []
        for edge in self._edges:
            source = self.get_vertex(edge["source"])
            target = self.get_vertex(edge["target"])
            if source is None:
                raise ValueError(f"Source node {edge['source']} not found")
            if target is None:
                raise ValueError(f"Target node {edge['target']} not found")
            edges.append(ContractEdge(source, target, edge))
        return edges

    def _get_vertex_class(
        self, node_type: str, node_lc_type: str, node_id: str
    ) -> Type[Vertex]:
        """Returns the node class based on the node type."""
        node_name = node_id.split("-")[0]
        if node_name in lazy_load_vertex_dict.VERTEX_TYPE_MAP:
            return lazy_load_vertex_dict.VERTEX_TYPE_MAP[node_name]

        if node_type in FILE_TOOLS:
            return FileToolVertex
        if node_type in lazy_load_vertex_dict.VERTEX_TYPE_MAP:
            return lazy_load_vertex_dict.VERTEX_TYPE_MAP[node_type]
        return (
            lazy_load_vertex_dict.VERTEX_TYPE_MAP[node_lc_type]
            if node_lc_type in lazy_load_vertex_dict.VERTEX_TYPE_MAP
            else Vertex
        )

    def _build_vertices(self) -> List[Vertex]:
        """Builds the vertices of the graph."""
        nodes: List[Vertex] = []
        for node in self._nodes:
            node_data = node["data"]
            node_type: str = node_data["type"]  # type: ignore
            node_lc_type: str = node_data["node"]["template"]["_type"]  # type: ignore
            node_id = node["id"]

            VertexClass = self._get_vertex_class(node_type, node_lc_type, node_id)
            vertex = VertexClass(node)
            vertex.set_top_level(self.top_level_nodes)
            nodes.append(vertex)

        return nodes

    def get_children_by_node_type(self, node: Vertex, node_type: str) -> List[Vertex]:
        """Returns the children of a node based on the node type."""
        children = []
        node_types = [node.data["type"]]
        if "node" in node.data:
            node_types += node.data["node"]["base_classes"]
        if node_type in node_types:
            children.append(node)
        return children

    def __repr__(self):
        node_ids = [node.id for node in self.vertices]
        edges_repr = "\n".join(
            [f"{edge.source.id} --> {edge.target.id}" for edge in self.edges]
        )
        return f"Graph:\nNodes: {node_ids}\nConnections:\n{edges_repr}"
