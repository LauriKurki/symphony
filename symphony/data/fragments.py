from typing import Iterator

import jax
import jax.numpy as jnp
import jraph
import numpy as np
import chex

from symphony import datatypes


def generate_fragments(
    rng: chex.PRNGKey,
    graph: jraph.GraphsTuple,
    n_species: int,
    nn_tolerance: float = 0.01,
    max_radius: float = 2.03,
    mode: str = "nn",
    heavy_first: bool = False,
    beta_com: float = 0.0,
    max_targets_per_graph: int = 1,
) -> Iterator[datatypes.Fragments]:
    """Generative sequence for a molecular graph.

    Args:
        rng: The random number generator.
        graph: The molecular graph.
        n_species: The number of different species considered.
        nn_tolerance: Tolerance for the nearest neighbours.
        max_radius: The maximum distance of the focus-target
        mode: How to generate the fragments. Either "nn" or "radius".
        heavy_first: If true, the hydrogen atoms in the molecule will be placed last.
        beta_com: Inverse temperature value for the center of mass.

    Returns:
        A sequence of fragments.
    """
    assert mode in ["nn", "radius"]
    n = len(graph.nodes.positions)

    assert (
        len(graph.n_edge) == 1 and len(graph.n_node) == 1
    ), "Only single graphs supported."
    assert n >= 2, "Graph must have at least two nodes."

    # compute edge distances
    dist = np.linalg.norm(
        graph.nodes.positions[graph.receivers] - graph.nodes.positions[graph.senders],
        axis=1,
    )  # [n_edge]

    # make fragments
    try:
        rng, visited_nodes, frag = _make_first_fragment(
            rng,
            graph,
            dist,
            n_species,
            nn_tolerance,
            max_radius,
            mode,
            heavy_first,
            beta_com,
            max_targets_per_graph=max_targets_per_graph,
        )
        yield frag

        for _ in range(n - 2):
            rng, visited_nodes, frag = _make_middle_fragment(
                rng,
                visited_nodes,
                graph,
                dist,
                n_species,
                nn_tolerance,
                max_radius,
                mode,
                heavy_first,
                max_targets_per_graph=max_targets_per_graph,
            )
            yield frag
    except ValueError:
        pass
    else:
        assert len(visited_nodes) == n

        yield _make_last_fragment(graph, n_species, max_targets_per_graph)


def _make_first_fragment(
    rng,
    graph,
    dist,
    n_species,
    nn_tolerance,
    max_radius,
    mode,
    heavy_first=False,
    beta_com=0.0,
    max_targets_per_graph: int = 1,
):
    # get distances from (approximate) center of mass - assume all atoms have the same mass
    com = np.average(
        graph.nodes.positions,
        axis=0,
        weights=(graph.nodes.species > 0) if heavy_first else None,
    )
    distances_com = jnp.linalg.norm(graph.nodes.positions - com, axis=1)
    probs_com = jax.nn.softmax(-beta_com * distances_com**2)
    rng, k = jax.random.split(rng)
    if heavy_first and (graph.nodes.species != 0).sum() > 0:
        heavy_indices = np.argwhere(graph.nodes.species != 0).squeeze(-1)
        first_node = jax.random.choice(k, heavy_indices, p=probs_com[heavy_indices])
    else:
        first_node = jax.random.choice(
            k, np.arange(0, len(graph.nodes.positions)), p=probs_com
        )
    first_node = int(first_node)

    mask = graph.senders == first_node
    if heavy_first and (mask & graph.nodes.species[graph.receivers] > 0).sum() > 0:
        mask = mask & (graph.nodes.species[graph.receivers] > 0)
    if mode == "nn":
        min_dist = dist[mask].min()
        targets = graph.receivers[mask & (dist < min_dist + nn_tolerance)]
        del min_dist
    if mode == "radius":
        targets = graph.receivers[mask & (dist < max_radius)]

    if len(targets) == 0:
        raise ValueError("No targets found.")

    species_probability = np.zeros((graph.nodes.positions.shape[0], n_species))
    species_probability[first_node] = _normalized_bitcount(
        graph.nodes.species[targets], n_species
    )

    # pick a random target species
    rng, species_rng = jax.random.split(rng)
    target_species = jax.random.choice(species_rng, graph.nodes.species[targets]).reshape((1,))
    targets_of_same_species = targets[graph.nodes.species[targets] == target_species]
    # get all potential positions for that species
    rng, target_rng = jax.random.split(rng)
    target = jax.random.choice(target_rng, targets_of_same_species)
    target_positions = (
        graph.nodes.positions[targets_of_same_species]
        - graph.nodes.positions[first_node]
    )
    rng, k = jax.random.split(rng)
    target_positions = jax.random.permutation(k, target_positions)[
        :max_targets_per_graph
    ]

    sample = _into_fragment(
        graph,
        visited=np.array([first_node]),
        focus_node=first_node,
        target_species_probability=species_probability,
        target_species=target_species,
        target_positions=target_positions,
        stop=False,
        max_targets_per_graph=max_targets_per_graph,
    )

    visited = np.array([first_node, target])
    return rng, visited, sample


def _make_middle_fragment(
    rng,
    visited,
    graph,
    dist,
    n_species,
    nn_tolerance,
    max_radius,
    mode,
    heavy_first=False,
    max_targets_per_graph: int = 1,
):
    n_nodes = len(graph.nodes.positions)
    senders, receivers = graph.senders, graph.receivers

    mask = jnp.isin(senders, visited) & ~jnp.isin(receivers, visited)

    if heavy_first:
        heavy = graph.nodes.species > 0
        if heavy.sum() > heavy[visited].sum():
            mask = (
                mask
                & (graph.nodes.species[senders] > 0)
                & (graph.nodes.species[receivers] > 0)
            )

    if mode == "nn":
        min_dist = dist[mask].min()
        mask = mask & (dist < min_dist + nn_tolerance)
        del min_dist
    if mode == "radius":
        mask = mask & (dist < max_radius)

    counts = np.zeros((n_nodes, n_species))
    for focus_node in range(n_nodes):
        targets = receivers[(senders == focus_node) & mask]
        counts[focus_node] = np.bincount(
            graph.nodes.species[targets], minlength=n_species
        )

    if np.sum(counts) == 0:
        raise ValueError("No targets found.")

    target_species_probability = counts / np.sum(counts)

    # pick a random focus node
    rng, k = jax.random.split(rng)
    focus_probability = _normalized_bitcount(senders[mask], n_nodes)
    focus_node = jax.random.choice(k, n_nodes, p=focus_probability)
    focus_node = int(focus_node)

    # pick a random target species
    targets = receivers[(senders == focus_node) & mask]
    rng, species_rng = jax.random.split(rng)
    target_species = jax.random.choice(species_rng, graph.nodes.species[targets]).reshape((1,))
    targets_of_same_species = targets[graph.nodes.species[targets] == target_species]
    # get all potential positions for that species
    rng, target_rng = jax.random.split(rng)
    target_node = jax.random.choice(target_rng, targets_of_same_species)
    target_positions = (
        graph.nodes.positions[targets_of_same_species]
        - graph.nodes.positions[focus_node]
    )
    rng, k = jax.random.split(rng)
    target_positions = jax.random.permutation(k, target_positions)[
        :max_targets_per_graph
    ]

    new_visited = np.concatenate([visited, np.array([target_node])])

    sample = _into_fragment(
        graph,
        visited,
        focus_node,
        target_species_probability,
        target_species,
        target_positions,
        stop=False,
        max_targets_per_graph=max_targets_per_graph,
    )

    return rng, new_visited, sample


def _make_last_fragment(graph, n_species, max_targets_per_graph: int = 1):
    n_nodes = len(graph.nodes.positions)
    return _into_fragment(
        graph,
        visited=np.arange(len(graph.nodes.positions)),
        focus_node=0,
        target_species_probability=np.zeros((n_nodes, n_species)),
        target_species=np.array([0]),
        target_positions=np.zeros((1, 3)),
        stop=True,
        max_targets_per_graph=max_targets_per_graph,
    )


def _into_fragment(
    graph,
    visited,
    focus_node,
    target_species_probability,
    target_species,
    target_positions,
    stop,
    max_targets_per_graph,
):
    pos = graph.nodes.positions
    assert target_positions.shape[0] <= max_targets_per_graph
    # for batching purposes
    target_positions_padded = np.zeros((max_targets_per_graph, 3))
    target_positions_padded[: target_positions.shape[0]] = target_positions
    target_position_mask = np.zeros(
        (target_positions_padded.shape[0],), dtype=np.bool_
    )
    target_position_mask[: target_positions.shape[0]] = True
    nodes = datatypes.FragmentsNodes(
        positions=pos,
        species=graph.nodes.species,
        focus_and_target_species_probs=target_species_probability,
    )
    globals = datatypes.FragmentsGlobals(
        stop=np.array([stop], dtype=bool),  # [1]
        target_species=np.array(target_species),  # [1]
        target_positions=target_positions_padded[None],  # [max_targets_per_graph, 3]
        target_position_mask=target_position_mask[None],  # [max_targets_per_graph]
    )
    graph = graph._replace(nodes=nodes, globals=globals)

    if stop:
        assert len(visited) == len(pos)
        return graph
    else:
        # put focus node at the beginning
        visited = _move_first(visited, focus_node)
        visited = np.asarray(visited)

        # return subgraph
        return subgraph(graph, visited)


def _move_first(xs, x):
    return np.roll(xs, -np.where(xs == x)[0][0])


def _normalized_bitcount(xs, n: int):
    assert xs.ndim == 1
    return np.bincount(xs, minlength=n) / len(xs)


def subgraph(graph: jraph.GraphsTuple, nodes: np.ndarray) -> jraph.GraphsTuple:
    """Extract a subgraph from a graph.

    Args:
        graph: The graph to extract a subgraph from.
        nodes: The indices of the nodes to extract.

    Returns:
        The subgraph.
    """
    assert (
        len(graph.n_edge) == 1 and len(graph.n_node) == 1
    ), "Only single graphs supported."

    # Find all edges that connect to the nodes.
    edges = np.isin(graph.senders, nodes) & np.isin(graph.receivers, nodes)

    new_node_indices = -np.ones(graph.n_node[0], dtype=int)
    new_node_indices[nodes] = np.arange(len(nodes))

    return jraph.GraphsTuple(
        nodes=jax.tree_util.tree_map(lambda x: x[nodes], graph.nodes),
        edges=jax.tree_util.tree_map(lambda x: x[edges], graph.edges),
        globals=graph.globals,
        senders=new_node_indices[graph.senders[edges]],
        receivers=new_node_indices[graph.receivers[edges]],
        n_node=np.array([len(nodes)]),
        n_edge=np.array([np.sum(edges)]),
    )
