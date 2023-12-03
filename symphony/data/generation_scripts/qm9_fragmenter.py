from typing import List

import logging
import os
from absl import logging
from absl import flags
from absl import app
import tqdm.contrib.concurrent
import ase
import jax
import numpy as np
import tensorflow as tf
import tqdm

from symphony.data import fragments
from symphony.data import input_pipeline
from symphony.data import qm9

FLAGS = flags.FLAGS


def generate_all_fragments(
    molecules: List[ase.Atoms],
    seed: int,
    start: int,
    end: int,
    output_dir: str,
    mode: str,
    heavy_first: bool,
    beta_com: float,
    nn_tolerance: float,
    nn_cutoff: float,
    max_radius: float,
):
    logging.info(f"Generating fragments {start}:{end} using seed {seed}")
    logging.info(f"Saving to {output_dir}")
    logging.info(f"Mode: {mode}, heavy_first: {heavy_first}, beta_com: {beta_com}")
    logging.info(
        f"NN tolerance: {nn_tolerance}, NN cutoff: {nn_cutoff}, max_radius: {max_radius}"
    )

    seed = jax.random.PRNGKey(seed)

    if start is not None and end is not None:
        molecules = molecules[start:end]

    atomic_numbers = np.array([1, 6, 7, 8, 9])
    molecules_as_graphs = [
        input_pipeline.ase_atoms_to_jraph_graph(
            molecule, atomic_numbers, nn_cutoff=nn_cutoff
        )
        for molecule in molecules
    ]

    signature = {
        # nodes
        "positions": tf.TensorSpec(shape=(None, 3), dtype=tf.float32),
        "species": tf.TensorSpec(shape=(None,), dtype=tf.int32),
        "focus_and_target_species_probs": tf.TensorSpec(
            shape=(None, len(atomic_numbers)), dtype=tf.float32
        ),
        # edges
        "senders": tf.TensorSpec(shape=(None,), dtype=tf.int32),
        "receivers": tf.TensorSpec(shape=(None,), dtype=tf.int32),
        # globals
        "stop": tf.TensorSpec(shape=(1,), dtype=tf.bool),
        "target_positions": tf.TensorSpec(shape=(1, 3), dtype=tf.float32),
        "target_species": tf.TensorSpec(shape=(1,), dtype=tf.int32),
        # n_node and n_edge
        "n_node": tf.TensorSpec(shape=(1,), dtype=tf.int32),
        "n_edge": tf.TensorSpec(shape=(1,), dtype=tf.int32),
    }

    def generator():
        for graph in tqdm.tqdm(molecules_as_graphs):
            frags = fragments.generate_fragments(
                seed,
                graph,
                len(atomic_numbers),
                nn_tolerance,
                max_radius,
                mode,
                heavy_first,
                beta_com,
            )
            frags = list(frags)

            skip = False
            for frag in frags:
                d = np.linalg.norm(frag.globals.target_positions)
                if d > max_radius:
                    logging.info(
                        f"Target position is too far away from the rest of the molecule. d={d} > max_radius={max_radius}",
                    )
                    skip = True

            if len(frags) == 0 or not frags[-1].globals.stop:
                logging.info("The last fragment is not a stop fragment.")
                skip = True

            if skip:
                continue

            for frag in frags:
                yield {
                    "positions": frag.nodes.positions.astype(np.float32),
                    "species": frag.nodes.species.astype(np.int32),
                    "focus_and_target_species_probs": frag.nodes.focus_and_target_species_probs.astype(
                        np.float32
                    ),
                    "senders": frag.senders.astype(np.int32),
                    "receivers": frag.receivers.astype(np.int32),
                    "stop": frag.globals.stop.astype(np.bool_),
                    "target_positions": frag.globals.target_positions.astype(
                        np.float32
                    ),
                    "target_species": frag.globals.target_species.astype(np.int32),
                    "n_node": frag.n_node.astype(np.int32),
                    "n_edge": frag.n_edge.astype(np.int32),
                }

    dataset = tf.data.Dataset.from_generator(generator, output_signature=signature)

    os.makedirs(output_dir, exist_ok=True)
    dataset.save(output_dir)


def _generate_all_fragments_wrapper(args):
    """Dummy wrapper to allow parallelization."""
    return generate_all_fragments(*args)


def main(unused_argv) -> None:
    del unused_argv

    logging.set_verbosity(logging.INFO)
    logging.set_stderrthreshold(logging.INFO)

    # Create a list of arguments to pass to generate_all_fragments
    molecules = qm9.load_qm9(
        "qm9_data",
        use_edm_splits=FLAGS.use_edm_splits,
        check_molecule_sanity=FLAGS.check_molecule_sanity,
    )
    chunk_size = FLAGS.chunk
    args_list = [
        (
            molecules,
            seed,
            start,
            start + chunk_size,
            os.path.join(
                FLAGS.output_dir,
                f"fragments_{seed:02d}_{start:06d}_{start + chunk_size:06d}",
            ),
            FLAGS.mode,
            FLAGS.heavy_first,
            FLAGS.beta_com,
            FLAGS.nn_tolerance,
            FLAGS.nn_cutoff,
            FLAGS.max_radius,
        )
        for seed in range(FLAGS.start_seed, FLAGS.end_seed)
        for start in range(0, len(molecules), chunk_size)
    ]

    # Create a pool of processes, and apply generate_all_fragments to each tuple of arguments.
    tqdm.contrib.concurrent.process_map(
        _generate_all_fragments_wrapper, args_list, chunksize=128
    )


if __name__ == "__main__":
    flags.DEFINE_integer("start_seed", 0, "Start random seed.")
    flags.DEFINE_integer("end_seed", 8, "End random seed.")
    flags.DEFINE_integer("chunk", 1000, "Number of molecules per fragment file.")
    flags.DEFINE_integer("start", None, "Start index.")
    flags.DEFINE_integer("end", None, "End index.")
    flags.DEFINE_bool(
        "check_molecule_sanity",
        False,
        "Whether to check molecule sanity. Note that this is incompatible with use_edm_splits=True.",
    )
    flags.DEFINE_bool("use_edm_splits", True, "Whether to use splits from EDM.")
    flags.DEFINE_string(
        "output_dir", "qm9_fragments_fixed/nn_edm/", "Output directory."
    )
    flags.DEFINE_string("mode", "nn", "Fragmentation mode.")
    flags.DEFINE_bool("heavy_first", False, "Heavy atoms first.")
    flags.DEFINE_float("beta_com", 0.0, "Beta for center of mass.")
    flags.DEFINE_float("nn_tolerance", 0.125, "NN tolerance (in Angstrom).")
    flags.DEFINE_float("nn_cutoff", 5.0, "NN cutoff (in Angstrom).")
    flags.DEFINE_float("max_radius", 2.03, "Max radius (in Angstrom).")

    app.run(main)
