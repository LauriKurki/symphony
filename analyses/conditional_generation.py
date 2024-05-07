# Imports
from typing import *
import ase
import ase.db
import ase.io
import os
import numpy as np
import re
import sys
import tensorflow as tf

from absl import flags, app
import analyses.generate_molecules as generate_molecules
from symphony.data.datasets import perov
from configs.root_dirs import get_root_dir


def get_fragment_list(mols: Sequence[ase.Atoms], num_mols: int):
    fragments = []
    for i in range(num_mols):
        mol = mols[i]
        num_atoms = len(mol)
        for j in range(num_atoms):
            if mol.numbers[j] == 1:
                fragment = ase.Atoms(
                    positions=np.vstack([mol.positions[:j], mol.positions[j + 1 :]]),
                    numbers=np.concatenate([mol.numbers[:j], mol.numbers[j + 1 :]]),
                    cell=mol.cell,
                    pbc=True
                )
                fragments.append(fragment)
    return fragments


def main(unused_argv: Sequence[str]):
    beta_species = 1.0
    beta_position = 1.0
    step = flags.FLAGS.step
    num_seeds_per_chunk = 1
    max_num_atoms = 200
    max_num_steps = 10
    num_mols = 20
    outputdir = "conditional_generation"

    all_mols = perov.load_perov(get_root_dir('perov5'), supercell=False)
    mols_by_split = {"train": all_mols['train'][:num_mols], "test": all_mols['test'][-num_mols:]}

    for split, split_mols in mols_by_split.items():
        # Ensure that the number of molecules is a multiple of num_seeds_per_chunk.
        mol_list = get_fragment_list(split_mols, num_mols)
        mol_list = split_mols[
            : num_seeds_per_chunk * (len(split_mols) // num_seeds_per_chunk)
        ]
        print(f"Number of fragments for {split}: {len(mol_list)}")

        gen_mol_list = generate_molecules.generate_molecules(
            flags.FLAGS.workdir,
            os.path.join(flags.FLAGS.outputdir, split),
            beta_species,
            beta_position,
            step,
            len(mol_list),
            num_seeds_per_chunk,
            mol_list,
            max_num_atoms,
            max_num_steps,
            flags.FLAGS.visualize,
        )


if __name__ == "__main__":
    flags.DEFINE_string(
        "workdir",
        "/data/NFS/potato/songk/spherical-harmonic-net/workdirs/",
        "Workdir for model.",
    )
    flags.DEFINE_string(
        "outputdir",
        os.path.join(os.getcwd(), "conditional_generation", "analysed_workdirs"),
        "Directory where molecules should be saved.",
    )
    flags.DEFINE_bool(
        "visualize",
        False,
        "Whether to visualize the generation process step-by-step.",
    )
    flags.DEFINE_string(
        "step",
        "best",
        "Step number to load model from. The default corresponds to the best model.",
    )
    app.run(main)
