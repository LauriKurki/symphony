"""Generates molecules from a trained model."""

from typing import Sequence, Tuple, Iterable, Optional, Union

import os

from absl import flags
from absl import app
from absl import logging
import ase
import ase.data
from ase.db import connect
import ase.io
import ase.visualize
import jax
import jax.numpy as jnp
import jraph
import numpy as np

import analyses.analysis as analysis
from symphony import datatypes
from symphony.data import input_pipeline
from symphony import models

FLAGS = flags.FLAGS

import os
import queue
import shutil

import jax
import jax.numpy as jnp
import numpy as np
import matplotlib.pyplot as plt
import jraph
import ase


from analyses import analysis
from symphony.data import input_pipeline
from symphony import datatypes
from symphony.models import utils

def append_predictions(fragments: datatypes.Fragments, preds: datatypes.Predictions, merge_cutoff: float, nn_cutoff: float) -> Iterable[Tuple[int, datatypes.Fragments]]:
    """Appends the predictions to the fragments."""
    # Bring back to CPU.
    fragments = jax.tree_map(np.asarray, fragments)
    preds = jax.tree_map(np.asarray, preds)
    valids = jraph.get_graph_padding_mask(fragments)

    # Process each fragment.
    for valid, fragment, pred in zip(valids, jraph.unbatch(fragments), jraph.unbatch(preds)):
        if valid:
            yield append_predictions_to_fragment(fragment, pred, merge_cutoff, nn_cutoff)


def append_predictions_to_fragment(fragment: datatypes.Fragments, pred: datatypes.Predictions, merge_cutoff: float, nn_cutoff: float) -> Tuple[int, datatypes.Fragments]:
    """Appends the predictions to a single fragment."""
    focus_mask = pred.nodes.focus_mask
    target_relative_positions = pred.nodes.position_vectors[focus_mask]
    extra_positions = target_relative_positions + fragment.nodes.positions[focus_mask]
    extra_species = pred.nodes.target_species[focus_mask]
    stop = pred.globals.stop

    # Filter out positions too close to each other.
    new_positions = fragment.nodes.positions
    new_species = fragment.nodes.species
    for extra_position, extra_specie in zip(extra_positions, extra_species):
        if np.min(np.linalg.norm(new_positions - extra_position, axis=1)) < merge_cutoff:
            continue

        new_positions = np.concatenate([new_positions, [extra_position]], axis=0)
        new_species = np.concatenate([new_species, [extra_specie]], axis=0)

    atomic_numbers = np.asarray([1, 6, 7, 8, 9])
    new_fragment = input_pipeline.ase_atoms_to_jraph_graph(
        atoms=ase.Atoms(
            numbers=atomic_numbers[new_species],
            positions=new_positions
        ),
        atomic_numbers=atomic_numbers,
        nn_cutoff=nn_cutoff
    )
    new_fragment = new_fragment._replace(
        globals=fragment.globals
    )
    return stop, new_fragment


def _make_queue_iterator(q: queue.SimpleQueue):
    """Makes a non-blocking iterator from a queue."""
    while q.qsize() > 0:
        yield q.get(block=False)


def generate_molecules(
    workdir: str,
    outputdir: str,
    focus_and_atom_type_inverse_temperature: float,
    position_inverse_temperature: float,
    step: str,
    num_seeds: int,
    init_molecules: Sequence[Union[str, ase.Atoms]],
    max_num_atoms: int,
    num_node_for_padding: int,
    num_edge_for_padding: int,
    num_graph_for_padding: int,
    merge_cutoff: float,
    steps_for_weight_averaging: Optional[Sequence[int]] = None
):
    """Generates molecules from a trained model at the given workdir."""
    # Create initial molecule, if provided.
    if isinstance(init_molecules, str):
        init_molecule, init_molecule_name = analysis.construct_molecule(init_molecules)
        logging.info(
            f"Initial molecule: {init_molecule.get_chemical_formula()} with numbers {init_molecule.numbers} and positions {init_molecule.positions}"
        )
        init_molecules = [init_molecule] * num_seeds
        init_molecule_names = [init_molecule_name] * num_seeds
    else:
        assert len(init_molecules) == num_seeds
        init_molecule_names = [init_molecule.get_chemical_formula() for init_molecule in init_molecules]

    # Load model.
    name = analysis.name_from_workdir(workdir)
    if steps_for_weight_averaging is not None:
        logging.info("Loading model averaged from steps %s", steps_for_weight_averaging)
        model, params, config = analysis.load_weighted_average_model_at_steps(
            workdir, steps_for_weight_averaging, run_in_evaluation_mode=True
        )
    else:
        model, params, config = analysis.load_model_at_step(
            workdir, step, run_in_evaluation_mode=True
        )
    apply_fn = jax.jit(model.apply)

    # Log config.
    logging.info(config.to_dict())

    # Create output directories.
    molecules_outputdir = os.path.join(
        outputdir,
        name,
        f"fait={focus_and_atom_type_inverse_temperature}",
        f"pit={position_inverse_temperature}",
        f"step={step}",
        "molecules",
    )
    os.makedirs(molecules_outputdir, exist_ok=True)

    init_fragments = [
        input_pipeline.ase_atoms_to_jraph_graph(
            init_molecule, models.ATOMIC_NUMBERS, config.nn_cutoff
        ) for init_molecule in init_molecules]

    fragment_pool = queue.SimpleQueue()
    for seed, init_fragment in enumerate(init_fragments):
        init_fragment = init_fragment._replace(
            globals=np.asarray([seed], dtype=np.int32)
        )
        fragment_pool.put(init_fragment)

    padding_budget = dict(
        n_node=num_node_for_padding,
        n_edge=num_edge_for_padding,
        n_graph=num_graph_for_padding
    )

    rng = jax.random.PRNGKey(0)
    generated_molecules = []
    while len(generated_molecules) < num_seeds and fragment_pool.qsize() > 0:
        fragments = next(jraph.dynamically_batch(_make_queue_iterator(fragment_pool),
                                                **padding_budget))

        apply_rng, rng = jax.random.split(rng)
        preds = apply_fn(params, apply_rng, fragments, focus_and_atom_type_inverse_temperature, position_inverse_temperature)
        for stop, new_fragment in append_predictions(fragments, preds, merge_cutoff=merge_cutoff, nn_cutoff=config.nn_cutoff):
            if stop or new_fragment.nodes.species.shape[0] == max_num_atoms:
                generated_molecules.append((stop, new_fragment))
            else:
                fragment_pool.put(new_fragment)

    # Add the remaining fragments to the generated molecules.
    while fragment_pool.qsize() > 0:
        unfinished_fragment = fragment_pool.get(block=False)
        generated_molecules.append((False, unfinished_fragment))

    generated_molecules_ase = []
    for stop, fragment in generated_molecules:
        seed = fragment.globals.item()
        init_molecule_name = init_molecule_names[seed]
        generated_molecule_ase = ase.Atoms(
            symbols=utils.get_atomic_numbers(fragment.nodes.species),
            positions=fragment.nodes.positions
        )

        if stop:
            logging.info("Generated %s", generated_molecule_ase.get_chemical_formula())
            output_file = f"{init_molecule_name}_seed={seed}.xyz"
        else:
            logging.info("STOP was not produced ...")
            output_file = f"{init_molecule_name}_seed={seed}_no_stop.xyz"

        generated_molecule_ase.write(os.path.join(molecules_outputdir, output_file))
        generated_molecules_ase.append(generated_molecule_ase)

    # Save the generated molecules as an ASE database.
    output_db = os.path.join(
        molecules_outputdir, f"generated_molecules_init={init_molecule_name}.db"
    )
    with connect(output_db) as conn:
        for mol in generated_molecules_ase:
            conn.write(mol)


def main(unused_argv: Sequence[str]) -> None:
    del unused_argv

    workdir = os.path.abspath(FLAGS.workdir)
    generate_molecules(
        workdir,
        FLAGS.outputdir,
        FLAGS.focus_and_atom_type_inverse_temperature,
        FLAGS.position_inverse_temperature,
        FLAGS.step,
        FLAGS.num_seeds,
        FLAGS.init,
        FLAGS.max_num_atoms,
        FLAGS.num_node_for_padding,
        FLAGS.num_edge_for_padding,
        FLAGS.num_graph_for_padding,
        FLAGS.merge_cutoff,
        FLAGS.steps_for_weight_averaging,
    )


if __name__ == "__main__":
    flags.DEFINE_string("workdir", None, "Workdir for model.")
    flags.DEFINE_string(
        "outputdir",
        os.path.join(os.getcwd(), "analyses", "analysed_workdirs"),
        "Directory where molecules should be saved.",
    )
    flags.DEFINE_float(
        "focus_and_atom_type_inverse_temperature",
        1.0,
        "Inverse temperature value for sampling the focus and atom type.",
        short_name="fait",
    )
    flags.DEFINE_float(
        "position_inverse_temperature",
        1.0,
        "Inverse temperature value for sampling the position.",
        short_name="pit",
    )
    flags.DEFINE_string(
        "step",
        "best",
        "Step number to load model from. The default corresponds to the best model.",
    )
    flags.DEFINE_integer(
        "num_seeds",
        128,
        "Seeds to attempt to generate molecules from.",
    )
    flags.DEFINE_string(
        "init",
        "C",
        "An initial molecular fragment to start the generation process from.",
    )
    flags.DEFINE_integer(
        "max_num_atoms",
        30,
        "Maximum number of atoms to generate per molecule.",
    )
    flags.DEFINE_integer(
        "num_node_for_padding",
        320,
        "Number of nodes to pad to.",
    )
    flags.DEFINE_integer(
        "num_edge_for_padding",
        640,
        "Number of edges to pad to.",
    )
    flags.DEFINE_integer(
        "num_graph_for_padding",
        16,
        "Number of graphs to pad to.",
    )
    flags.DEFINE_float(
        "merge_cutoff",
        0.5,
        "Cutoff for merging atoms.",
    )
    flags.DEFINE_list(
        "steps_for_weight_averaging",
        None,
        "Steps to average parameters over. If None, the model at the given step is used.",
    )
    flags.mark_flags_as_required(["workdir"])
    app.run(main)
