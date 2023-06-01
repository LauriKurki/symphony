##########################################################
# From G-SchNet repository                               #
# https://github.com/atomistic-machine-learning/G-SchNet #
##########################################################

import argparse
import collections
import itertools
import logging
import os
import pickle
import time
import sys

from ase import Atoms
from ase.db import connect
from multiprocessing import Process, Queue
import numpy as np
from openbabel import openbabel as ob
from openbabel import pybel
import pandas as pd
import tqdm
import yaml

sys.path.append("..")

from analyses import analysis
from analyses.check_valence import check_valence
from analyses.utility_functions import run_threaded


def get_parser():
    """Setup parser for command line arguments"""
    main_parser = argparse.ArgumentParser()
    main_parser.add_argument(
        "mol_path",
        help="Path to generated molecules as an ASE db, "
        'computed statistics ("generated_molecules_statistics.pkl") will be '
        "stored in the same directory as the input file/s ",
    )
    main_parser.add_argument(
        "--data_path",
        help="Path to training data base (if provided, "
        "generated molecules can be compared/matched with "
        "those in the training data set)",
        default=None,
    )
    main_parser.add_argument(
        "--model_path",
        help="Path of directory containing the model that " "generated the molecules. ",
        default=None,
    )
    main_parser.add_argument(
        "--valence",
        default=[1, 1, 6, 4, 7, 3, 8, 2, 9, 1],
        type=int,
        nargs="+",
        help="the valence of atom types in the form "
        "[type1 valence type2 valence ...] "
        "(default: %(default)s)",
    )
    main_parser.add_argument(
        "--print_file",
        help="Use to limit the printing if results are "
        "written to a file instead of the console ("
        "e.g. if running on a cluster)",
        action="store_true",
    )
    main_parser.add_argument(
        "--threads",
        type=int,
        default=8,
        help="Number of threads used (set to 0 to run "
        "everything sequentially in the main thread,"
        " default: %(default)s)",
    )
    main_parser.add_argument(
        "--init",
        type=str,
        default="C",
        help="An initial molecular fragment to start the generation process from.",
    )

    return main_parser


def filter_unique(mols, valid=None, use_bits=False):
    """
    Identify duplicate molecules among a large amount of generated structures.
    The first found structure of each kind is kept as valid original and all following
    duplicating structures are marked as invalid (the molecular fingerprint and
    canonical smiles representation is used which means that different spatial
    conformers of the same molecular graph cannot be distinguished).

    Args:
        mols (list of ase.Atoms): list of all generated molecules
        valid (numpy.ndarray, optional): array of the same length as mols which flags
            molecules as valid (invalid molecules are not considered in the comparison
            process), if None, all molecules in mols are considered as valid (default:
            None)
        use_bits (bool, optional): set True to use the list of non-zero bits instead of
            the pybel.Fingerprint object when comparing molecules (results are
            identical, default: False)

    Returns:
        valid (numpy.ndarray): array of the same length as mols which flags molecules as
            valid (identified duplicates are now marked as invalid in contrast to the
            flag in input argument valid)
        duplicating (numpy.ndarray): array of length n_mols where entry i is -1 if molecule i is
            an original structure (not a duplicate) and otherwise it is the index j of
            the original structure that molecule i duplicates (j<i)
        duplicating_count (numpy.ndarray): array of length n_mols that is 0 for all duplicates and the
            number of identified duplicates for all original structures (therefore
            the sum over this array is the total number of identified duplicates)
    """
    if valid is None:
        valid = np.ones(len(mols), dtype=bool)
    else:
        valid = valid.copy()
    accepted_dict = {}
    duplicating = -np.ones(len(mols), dtype=int)
    duplicate_count = np.zeros(len(mols), dtype=int)
    for i, mol1 in enumerate(mols):
        if not valid[i]:
            continue
        mol_key = _get_atoms_per_type_str(mol1)
        found = False
        if mol_key in accepted_dict:
            for j, mol2 in accepted_dict[mol_key]:
                # compare fingerprints
                fp1, smiles1, symbols1 = get_fingerprint(mol1, use_bits=use_bits)
                fp2, smiles2, symbols2 = get_fingerprint(mol2, use_bits=use_bits)
                if tanimoto_similarity(fp1, fp2, use_bits=use_bits) >= 1:
                    # compare canonical smiles representation
                    if (
                        symbols1 == symbols2
                        or get_mirror_can(mol) == symbols1
                    ):
                        found = True
                        valid[i] = False
                        duplicating[i] = j
                        duplicate_count[j] += 1
                        break
        if not found:
            accepted_dict = _update_dict(accepted_dict, key=mol_key, val=(i, mol1))
    return valid, duplicating, duplicate_count


def filter_unique_threaded(
    mols, valid=None, n_threads=16, n_mols_per_thread=5, print_file=True, prog_str=None
):
    """
    Identify duplicate molecules among a large amount of generated structures using
    multiple CPU-threads. The first found structure of each kind is kept as valid
    original and all following duplicating structures are marked as invalid (the
    molecular fingerprint and canonical smiles representation is used which means that
    different spatial conformers of the same molecular graph cannot be distinguished).

    Args:
        mols (list of ase.Atoms): list of all generated molecules
        valid (numpy.ndarray, optional): array of the same length as mols which flags
            molecules as valid (invalid molecules are not considered in the comparison
            process), if None, all molecules in mols are considered as valid (default:
            None)
        n_threads (int, optional): number of additional threads used (default: 16)
        n_mols_per_thread (int, optional): number of molecules that are processed by
            each thread in each iteration (default: 5)
        print_file (bool, optional): set True to suppress printing of progress string
            (default: True)
        prog_str (str, optional): specify a custom progress string (if None,
            no progress will be printed, default: None)

    Returns:
        numpy.ndarray: array of the same length as mols which flags molecules as
            valid (identified duplicates are now marked as invalid in contrast to the
            flag in input argument valid)
        numpy.ndarray: array of length n_mols where entry i is -1 if molecule i is
            an original structure (not a duplicate) and otherwise it is the index j of
            the original structure that molecule i duplicates (j<i)
        numpy.ndarray: array of length n_mols that is 0 for all duplicates and the
            number of identified duplicates for all original structures (therefore
            the sum over this array is the total number of identified duplicates)
    """
    if valid is None:
        valid = np.ones(len(mols), dtype=bool)
    else:
        valid = valid.copy()
    if len(mols) < 3 * n_threads * n_mols_per_thread or n_threads == 0:
        return filter_unique(mols, valid, use_bits=True)
    current = 0
    still_valid = np.zeros_like(valid)
    working_flag = np.zeros(n_threads, dtype=bool)
    duplicating = []
    goal = n_threads * n_mols_per_thread

    # set up threads and queues
    threads = []
    qs_in = []
    qs_out = []
    for i in range(n_threads):
        qs_in += [Queue(1)]
        qs_out += [Queue(1)]
        threads += [
            Process(
                target=_filter_worker, name=str(i), args=(qs_out[-1], qs_in[-1], mols)
            )
        ]
        threads[-1].start()

    # get first two mini-batches (workers do not need to process first one)
    new_idcs, current, dups = _filter_mini_batch(mols, valid, current, goal)
    duplicating += dups  # maintain list of which molecules are duplicated
    newly_accepted = new_idcs
    still_valid[newly_accepted] = 1  # trivially accept first batch
    newly_accepted_dict = _create_mol_dict(mols, newly_accepted)
    new_idcs, current, dups = _filter_mini_batch(mols, valid, current, goal)
    duplicating += dups

    # submit second mini batch to workers
    start = 0
    for i, q_out in enumerate(qs_out):
        if start >= len(new_idcs):
            continue
        end = start + n_mols_per_thread
        q_out.put((False, newly_accepted_dict, new_idcs[start:end]))
        working_flag[i] = 1
        start = end

    # loop while the worker threads have data to process
    k = 1
    while np.any(working_flag == 1):
        # get new mini batch
        new_idcs, current, dups = _filter_mini_batch(mols, valid, current, goal)

        # gather results from workers
        newly_accepted = []
        newly_accepted_dict = {}
        for i, q_in in enumerate(qs_in):
            if working_flag[i]:
                returned = q_in.get()
                newly_accepted += returned[0]
                duplicating += returned[1]
                newly_accepted_dict = _update_dict(
                    newly_accepted_dict, new_dict=returned[2]
                )
                working_flag[i] = 0

        # submit gathered results and new mini batch molecules to workers
        start = 0
        for i, q_out in enumerate(qs_out):
            if start >= len(new_idcs):
                continue
            end = start + n_mols_per_thread
            q_out.put((False, newly_accepted_dict, new_idcs[start:end]))
            working_flag[i] = 1
            start = end

        # set validity according to gathered data
        still_valid[newly_accepted] = 1
        duplicating += dups

        k += 1
        if (
            ((k % 10) == 0 or current >= len(mols))
            and not print_file
            and prog_str is not None
        ):
            print("\033[K", end="\r", flush=True)
            print(
                f"{prog_str} ({100 * min(current/len(mols), 1):.2f}%)",
                end="\r",
                flush=True,
            )

    # stop worker threads and join
    for i, q_out in enumerate(qs_out):
        q_out.put((True,))
        threads[i].join()
        threads[i].terminate()

    # fix statistics about duplicates
    duplicating, duplicate_count = _process_duplicates(duplicating, len(mols))

    return still_valid, duplicating, duplicate_count


def _get_atoms_per_type_str(mol, type_infos = {1: 'H', 6: 'C', 7: 'N', 8: 'O', 9: 'F'}):
    """
    Get a string representing the atomic composition of a molecule (i.e. the number
    of atoms per type in the molecule, e.g. H2C3O1, where the order of types is
    determined by increasing nuclear charge).

    Args:
        mol (ase.Atoms): molecule

    Returns:
        str: the atomic composition of the molecule
    """
    n_atoms_per_type = np.bincount(mol.numbers, minlength=10)
    s = ""
    for t, n in zip(type_infos.keys(), n_atoms_per_type):
        s += f'{type_infos[t]}{int(n):d}'
    return s


def _create_mol_dict(mols, idcs=None):
    """
    Create a dictionary holding indices of a list of molecules where the key is a
    string that represents the atomic composition (i.e. the number of atoms per type in
    the molecule, e.g. H2C3O1, where the order of types is determined by increasing
    nuclear charge). This is especially useful to speed up the comparison of molecules
    as candidate structures with the same composition of atoms can easily be accessed
    while ignoring all molecules with different compositions.

    Args:
        mols (list of utility_classes.Molecule or numpy.ndarray): the molecules or
            the atomic numbers of the molecules which are referenced in the dictionary
        idcs (list of int, optional): indices of a subset of the molecules in mols that
            shall be put into the dictionary (if None, all structures in mol will be
            referenced in the dictionary, default: None)

    Returns:
        dict (str->list of int): dictionary with the indices of molecules in mols
            ordered by their atomic composition
    """
    if idcs is None:
        idcs = range(len(mols))
    mol_dict = {}
    for idx in idcs:
        mol = mols[idx]
        mol_key = _get_atoms_per_type_str(mol)
        mol_dict = _update_dict(mol_dict, key=mol_key, val=idx)
    return mol_dict


def _update_dict(old_dict, **kwargs):
    """
    Update an existing dictionary (any->list of any) with new entries where the new
    values are either appended to the existing lists if the corresponding key already
    exists in the dictionary or a new list under the new key is created.

    Args:
        old_dict (dict (any->list of any)): original dictionary that shall be updated
        **kwargs: keyword arguments that can either be a dictionary of the same format
            as old_dict (new_dict=dict (any->list of any)) which will be merged into
            old_dict or a single key-value pair that shall be added (key=any, val=any)

    Returns:
        dict (any->list of any): the updated dictionary
    """
    if "new_dict" in kwargs:
        for key in kwargs["new_dict"]:
            if key in old_dict:
                old_dict[key] += kwargs["new_dict"][key]
            else:
                old_dict[key] = kwargs["new_dict"][key]
    if "val" in kwargs and "key" in kwargs:
        if kwargs["key"] in old_dict:
            old_dict[kwargs["key"]] += [kwargs["val"]]
        else:
            old_dict[kwargs["key"]] = [kwargs["val"]]
    return old_dict


def _filter_mini_batch(mols, valid, start, amount):
    """
    Prepare a mini-batch consisting of unique molecules (with respect to all molecules
    in the mini-batch) that can be divided and send to worker functions (see
    _filter_worker) to compare them to the database of all original (non-duplicate)
    molecules.

    Args:
        mols (list of ase.Atoms): list of all generated molecules
        valid (numpy.ndarray): array of the same length as mols which flags molecules as
            valid (invalid molecules are not put into a mini-batch but skipped)
        start (int): index of the first molecule in mols that should be put into a
            mini-batch
        amount (int): the total amount of molecules that shall be put into the
            mini-batch (note that the mini-batch can be smaller than amount if all
            molecules in mols have been processed already).

    Returns:
        list of int: list of indices of molecules in mols that have been put into the
            mini-batch (i.e. the prepared mini-batch)
        int: index of the first molecule in mols that is not yet put into a mini-batch
        list of list of int: list of lists where the inner lists have exactly
            two integer entries: the first being the index of an identified duplicate
            molecule (skipped and not put into the mini-batch) and the second being the
            index of the corresponding original molecule (put into the mini-batch)
    """
    count = 0
    accepted = []
    accepted_dict = {}
    duplicating = []
    max_mol = len(mols)
    while count < amount:
        if start >= max_mol:
            break
        if not valid[start]:
            start += 1
            continue
        mol1 = mols[start]
        mol_key = _get_atoms_per_type_str(mol1)
        found = False
        if mol_key in accepted_dict:
            for idx in accepted_dict[mol_key]:
                mol2 = mols[idx]
                if mol1.tanimoto_similarity(mol2, use_bits=True) >= 1:
                    if (
                        mol1.get_can() == mol2.get_can()
                        or mol1.get_can() == mol2.get_mirror_can()
                    ):
                        found = True
                        duplicating += [[start, idx]]
                        break
        if not found:
            accepted += [start]
            accepted_dict = _update_dict(accepted_dict, key=mol_key, val=start)
            count += 1
        start += 1
    return accepted, start, duplicating


def _filter_worker(q_in, q_out, all_mols):
    """
    Worker function for multi-threaded identification of duplicate molecules that
    iteratively receives small batches of molecules which it compares to all previously
    processed molecules that were identified as originals (non-duplicate structures).

    Args:
        q_in (multiprocessing.Queue): queue to receive a new job at each iteration
            (contains three entries: 1st a flag whether the job is done, 2nd a
            dictionary with indices of newly found original structures in the last
            iteration, and 3rd a list of indices of candidate molecules that shall be
            checked in the current iteration)
        q_out (multiprocessing.Queue): queue to send results of the current iteration
            (contains three entries: 1st a list with the indices of the candidates
            that were identified as originals, 2nd a list of lists where each inner
            list holds the index of an identified duplicate structure and the index
            of the original structure that it duplicates, and 3rd a dictionary with
            the indices of candidates that were identified as originals)
        all_mols (list of ase.Atoms): list with all generated molecules
    """
    accepted_dict = {}
    while True:
        data = q_in.get(True)
        if data[0]:
            break
        accepted_dict = _update_dict(accepted_dict, new_dict=data[1])
        mols = data[2]
        accept = []
        accept_dict = {}
        duplicating = []
        for idx1 in mols:
            found = False
            mol1 = all_mols[idx1]
            mol_key = _get_atoms_per_type_str(mol1)
            if mol_key in accepted_dict:
                for idx2 in accepted_dict[mol_key]:
                    mol2 = all_mols[idx2]
                    if mol1.tanimoto_similarity(mol2, use_bits=True) >= 1:
                        if (
                            mol1.get_can() == mol2.get_can()
                            or mol1.get_can() == mol2.get_mirror_can()
                        ):
                            found = True
                            duplicating += [[idx1, idx2]]
                            break
            if not found:
                accept += [idx1]
                accept_dict = _update_dict(accept_dict, key=mol_key, val=idx1)
        q_out.put((accept, duplicating, accept_dict))


def _process_duplicates(dups, n_mols):
    """
    Processes a list of duplicate molecules identified in a multi-threaded run and
    infers a proper list with the correct statistics for each molecule (how many
    duplicates of the structure are there and which is the first found structure of
    that kind)

    Args:
        dups (list of list of int): list of lists where the inner lists have exactly
            two integer entries: the first being the index of an identified duplicate
            molecule and the second being the index of the corresponding original
            molecule (which can also be a duplicate due to the applied multi-threading
            approach, hence this function is needed to identify such cases and fix
            the 'original' index to refer to the true original molecule, which is the
            first found structure of that kind)
        n_mols (int): the overall number of molecules that were examined

    Returns:
        numpy.ndarray: array of length n_mols where entry i is -1 if molecule i is
            an original structure (not a duplicate) and otherwise it is the index j of
            the original structure that molecule i duplicates (j<i)
        numpy.ndarray: array of length n_mols that is 0 for all duplicates and the
            number of identified duplicates for all original structures (therefore
            the sum over this array is the total number of identified duplicates)
    """
    duplicating = -np.ones(n_mols, dtype=int)
    duplicate_count = np.zeros(n_mols, dtype=int)
    if len(dups) == 0:
        return duplicating, duplicate_count
    dups = np.array(dups, dtype=int)
    duplicates = dups[:, 0]
    originals = dups[:, 1]
    duplicating[duplicates] = originals
    for original in originals:
        wrongly_assigned_originals = []
        while duplicating[original] >= 0:
            wrongly_assigned_originals += [original]
            original = duplicating[original]
        duplicating[np.array(wrongly_assigned_originals, dtype=int)] = original
        duplicate_count[original] += 1
    return duplicating, duplicate_count


def filter_new(
    mols, stats, stat_heads, model_path, data_path, print_file=False, n_threads=0
):
    """
    Check whether generated molecules correspond to structures in the training database
    used for either training, validation, or as test data and update statistics array of
    generated molecules accordingly.

    Args:
        mols (list of ase.Atoms): generated molecules
        stats (numpy.ndarray): statistics of all generated molecules where columns
            correspond to molecules and rows correspond to available statistics
            (n_statistics x n_molecules)
        stat_heads (list of str): the names of the statistics stored in each row in
            stats (e.g. 'F' for the number of fluorine atoms or 'R5' for the number of
            rings of size 5)
        model_path (str): path to the folder containing the trained model used to
            generate the molecules
        data_path (str): full path to the training database
        print_file (bool, optional): set True to limit printing (e.g. if it is
            redirected to a file instead of displayed in a terminal, default: False)
        n_threads (int, optional): number of additional threads to use (default: 0)

    Returns:
        numpy.ndarray: updated statistics of all generated molecules (stats['known']
        is 0 if a generated molecule does not correspond to a structure in the
        training database, it is 1 if it corresponds to a training structure,
        2 if it corresponds to a validation structure, and 3 if it corresponds to a
        test structure, stats['equals'] is -1 if stats['known'] is 0 and otherwise
        holds the index of the corresponding training/validation/test structure in
        the database at data_path)
    """
    print(f"\n\n2. Checking which molecules are new...")
    idx_known = stat_heads.index("known")

    # load training data
    dbpath = data_path
    if not os.path.isfile(dbpath):
        print(
            f"The provided training data base {dbpath} is no file, please specify "
            f"the correct path (including the filename and extension)!"
        )
        raise FileNotFoundError
    print(f"Using data base at {dbpath}...")

    if not os.path.exists(model_path):
        raise FileNotFoundError
    
    # Load config.
    saved_config_path = os.path.join(model_path, "config.yml")
    if not os.path.exists(saved_config_path):
        raise FileNotFoundError(f"No saved config found at {model_path}")

    logging.info("Saved config found at %s", saved_config_path)
    with open(saved_config_path, "r") as config_file:
        config = yaml.unsafe_load(config_file)

    train_idx = np.array(range(config.train_molecules[0], config.train_molecules[1]))
    val_idx = np.array(range(config.val_molecules[0], config.val_molecules[1]))
    test_idx = np.array(range(config.test_molecules[0], config.test_molecules[1]))
    train_idx = np.append(train_idx, val_idx)
    train_idx = np.append(train_idx, test_idx)

    print("\nComputing fingerprints of training data...")
    start_time = time.time()
    if n_threads <= 0:
        train_fps = _get_training_fingerprints(
            dbpath, train_idx, print_file
        )
    else:
        train_fps = {"fingerprints": [None for _ in range(len(train_idx))]}
        run_threaded(
            _get_training_fingerprints,
            {"train_idx": train_idx},
            {"dbpath": dbpath, "use_bits": True},
            train_fps,
            exclusive_kwargs={"print_file": print_file},
            n_threads=n_threads,
        )
    train_fps_dict = _get_training_fingerprints_dict(train_fps["fingerprints"])
    end_time = time.time() - start_time
    m, s = divmod(end_time, 60)
    h, m = divmod(m, 60)
    h, m, s = int(h), int(m), int(s)
    print(
        f'...{len(train_fps["fingerprints"])} fingerprints computed '
        f"in {h:d}h{m:02d}m{s:02d}s!"
    )

    print("\nComparing fingerprints...")
    start_time = time.time()
    if n_threads <= 0:
        results = _compare_fingerprints(
            mols,
            train_fps_dict,
            train_idx,
            [len(val_idx), len(test_idx)],
            stats.T,
            stat_heads,
            print_file,
        )
    else:
        results = {"stats": stats.T}
        run_threaded(
            _compare_fingerprints,
            {"mols": mols, "stats": stats.T},
            {
                "train_idx": train_idx,
                "train_fps": train_fps_dict,
                "thresh": [len(val_idx), len(test_idx)],
                "stat_heads": stat_heads,
                "use_bits": True,
            },
            results,
            exclusive_kwargs={"print_file": print_file},
            n_threads=n_threads,
        )
    stats = results["stats"].T
    stats[idx_known] = stats[idx_known]
    end_time = time.time() - start_time
    m, s = divmod(end_time, 60)
    h, m = divmod(m, 60)
    h, m, s = int(h), int(m), int(s)
    print(f"... needed {h:d}h{m:02d}m{s:02d}s.")
    print(
        f"Number of new molecules: "
        f"{sum(stats[idx_known] == 0)+sum(stats[idx_known] == 3)}"
    )
    print(
        f"Number of molecules matching training data: " f"{sum(stats[idx_known] == 1)}"
    )
    print(
        f"Number of molecules matching validation data: "
        f"{sum(stats[idx_known] == 2)}"
    )
    print(f"Number of molecules matching test data: " f"{sum(stats[idx_known] == 3)}")

    return stats


def _get_training_fingerprints(
    dbpath, train_idx, print_file=True, use_bits=False
):
    """
    Get the fingerprints (FP2 from Open Babel), canonical smiles representation,
    and atoms per type string of all molecules in the training database.

    Args:
        dbpath (str): path to the training database
        train_idx (list of int): list containing the indices of training, validation,
            and test molecules in the database (it is assumed
            that train_idx[0:n_train] corresponds to training data,
            train_idx[n_train:n_train+n_validation] corresponds to validation data,
            and train_idx[n_train+n_validation:] corresponds to test data)
        print_file (bool, optional): set True to suppress printing of progress string
            (default: True)
        use_bits (bool, optional): set True to return the non-zero bits in the
            fingerprint instead of the pybel.Fingerprint object (default: False)

    Returns:
        dict (str->list of tuple): dictionary with list of tuples under the key
        'fingerprints' containing the fingerprint, the canonical smiles representation,
        and the atoms per type string of each molecule listed in train_idx (preserving
        the order)
    """
    train_fps = []
    with connect(dbpath) as conn:
        if not print_file:
            print("0.00%", end="\r", flush=True)
        for i, idx in enumerate(train_idx):
            idx = int(idx)
            try:
                row = conn.get(idx + 1)
            except:
                print(f"error getting idx={idx}")
            at = row.toatoms()
            train_fps += [get_fingerprint(at, use_bits)]
            if (i % 100 == 0 or i + 1 == len(train_idx)) and not print_file:
                print("\033[K", end="\r", flush=True)
                print(f"{100 * (i + 1) / len(train_idx):.2f}%", end="\r", flush=True)
    return {"fingerprints": train_fps}


def get_fingerprint(ase_mol, use_bits=False):
    """
    Compute the molecular fingerprint (Open Babel FP2), canonical smiles
    representation, and number of atoms per type (e.g. H2O1) of a molecule.

    Args:
        ase_mol (ase.Atoms): molecule
        use_bits (bool, optional): set True to return the non-zero bits in the
            fingerprint instead of the pybel.Fingerprint object (default: False)

    Returns:
        pybel.Fingerprint or set of int: the fingerprint of the molecule or a set
            containing the non-zero bits of the fingerprint if use_bits=True
        str: the canonical smiles representation of the molecule
        str: the atom types contained in the molecule followed by number of
            atoms per type, e.g. H2C3O1, ordered by increasing atom type (nuclear
            charge)
    """
    mol = analysis.construct_pybel_mol(ase_mol)
    # use pybel to get fingerprint
    if use_bits:
        return (
            {*mol.calcfp().bits},
            mol.write("can"),
            _get_atoms_per_type_str(ase_mol),
        )
    else:
        return mol.calcfp(), mol.write("can"), _get_atoms_per_type_str(ase_mol)


def _get_training_fingerprints_dict(fps):
    """
    Convert a list of fingerprints into a dictionary where a string describing the
    number of types in each molecules (e.g. H2C3O1, ordered by increasing nuclear
    charge) is used as a key (allows for faster comparison of molecules as only those
    made of the same atoms can be identical).

    Args:
        fps (list of tuple): list containing tuples as returned by the get_fingerprint
            function (holding the fingerprint, canonical smiles representation, and the
            atoms per type string)

    Returns:
        dict (str->list of tuple): dictionary containing lists of tuples holding the
            molecular fingerprint, the canonical smiles representation, and the index
            of the molecule in the input list using the atoms per type string of the
            molecules as key (such that fingerprint tuples of all molecules with the
            exact same atom composition, e.g. H2C3O1, are stored together in one list)
    """
    fp_dict = {}
    for i, fp in enumerate(fps):
        fp_dict = _update_dict(fp_dict, key=fp[-1], val=fp[:-1] + (i,))
    return fp_dict


def get_mirror_can(mol):
        """
        Retrieve the canonical SMILES representation of the mirrored molecule (the
        z-coordinates are flipped).

        Args:
            mol (ase.Atoms): molecule

        Returns:
             String: canonical SMILES string of the mirrored molecule
        """
        # calculate canonical SMILES of mirrored molecule
        flipped = _flip_z(mol)  # flip z to mirror molecule using x-y plane
        mirror_can = pybel.Molecule(flipped).write("can")
        return mirror_can


def _flip_z(mol):
    """
    Flips the z-coordinates of atom positions (to get a mirrored version of the
    molecule).

    Args:
        mol (ase.Atoms): molecule
    Returns:
        an OBMol object where the z-coordinates of the atoms have been flipped
    """
    obmol = analysis.construct_obmol(mol)
    for atom in ob.OBMolAtomIter(obmol):
        x, y, z = atom.x(), atom.y(), atom.z()
        atom.SetVector(x, y, -z)
    obmol.ConnectTheDots()
    obmol.PerceiveBondOrders()
    return obmol


def tanimoto_similarity(mol, other_mol, use_bits=True):
        """
        Get the Tanimoto (fingerprint) similarity to another molecule.

        Args:
         mol (pybel.Fingerprint/list of bits set):
            representation of the second molecule
         other_mol (pybel.Fingerprint/list of bits set):
            representation of the second molecule
         use_bits (bool, optional): set True to calculate Tanimoto similarity
            from bits set in the fingerprint (default: True)

        Returns:
             float: Tanimoto similarity to the other molecule
        """
        if use_bits:
            n_equal = len(mol.intersection(other_mol))
            if len(mol) + len(other_mol) == 0:  # edge case with no set bits
                return 1.0
            return n_equal / (len(mol) + len(other_mol) - n_equal)
        else:
            return mol | other_mol


def _compare_fingerprints(
    mols,
    train_fps,
    train_idx,
    thresh,
    stats,
    stat_heads,
    print_file=True,
    use_bits=False,
    max_heavy_atoms=9,
):
    """
    Compare fingerprints of generated and training data molecules to update the
    statistics of the generated molecules (to which training/validation/test
    molecule it corresponds, if any).

    Args:
        mols (list of ase.Atoms): generated molecules
        train_fps (dict (str->list of tuple)): dictionary with fingerprints of
            training/validation/test data as returned by _get_training_fingerprints_dict
        train_idx (list of int): list that maps the index of fingerprints in the
            train_fps dict to indices of the underlying training database (it is assumed
            that train_idx[0:n_train] corresponds to training data,
            train_idx[n_train:n_train+n_validation] corresponds to validation data,
            and train_idx[n_train+n_validation:] corresponds to test data)
        thresh (tuple of int): tuple containing the number of validation and test
            data molecules (n_validation, n_test)
        stats (numpy.ndarray): statistics of all generated molecules where columns
            correspond to molecules and rows correspond to available statistics
            (n_statistics x n_molecules)
        stat_heads (list of str): the names of the statistics stored in each row in
            stats (e.g. 'F' for the number of fluorine atoms or 'R5' for the number of
            rings of size 5)
        print_file (bool, optional): set True to limit printing (e.g. if it is
            redirected to a file instead of displayed in a terminal, default: True)
        use_bits (bool, optional): set True if the fingerprint is provided as a list of
            non-zero bits instead of the pybel.Fingerprint object (default: False)
        max_heavy_atoms (int, optional): the maximum number of heavy atoms in the
            training data set (i.e. 9 for qm9, default: 9)

    Returns:
        dict (str->numpy.ndarray): dictionary containing the updated statistics under
            the key 'stats'
    """
    idx_known = stat_heads.index("known")
    idx_equals = stat_heads.index("equals")
    idx_val = stat_heads.index("valid_mol")
    n_val_mols, n_test_mols = thresh
    # get indices of valid molecules
    idcs = np.where(stats[:, idx_val])[0]
    if not print_file:
        print(f"0.00%", end="", flush=True)
    for i, idx in enumerate(idcs):
        mol = mols[idx]
        mol_key = _get_atoms_per_type_str(mol)
        # for now the molecule is considered to be new
        stats[idx, idx_known] = 0
        if np.sum(mol.numbers != 1) > max_heavy_atoms:
            continue  # cannot be in dataset
        if mol_key in train_fps:
            for fp_train in train_fps[mol_key]:
                # compare fingerprints
                fingerprint, smiles, symbols = get_fingerprint(mol, use_bits=use_bits)
                if tanimoto_similarity(fingerprint, fp_train[0], use_bits=use_bits) >= 1:
                    # compare canonical smiles representation
                    if (
                        symbols == fp_train[1]
                        or get_mirror_can(mol) == fp_train[1]
                    ):
                        # store index of match
                        j = fp_train[-1]
                        stats[idx, idx_equals] = train_idx[j]
                        if j >= len(train_idx) - np.sum(thresh):
                            if j > len(train_idx) - n_test_mols:
                                stats[idx, idx_known] = 3  # equals test data
                            else:
                                stats[idx, idx_known] = 2  # equals validation data
                        else:
                            stats[idx, idx_known] = 1  # equals training data
                        break
        if not print_file:
            print("\033[K", end="\r", flush=True)
            print(f"{100 * (i + 1) / len(idcs):.2f}%", end="\r", flush=True)
    if not print_file:
        print("\033[K", end="", flush=True)
    return {"stats": stats}


def get_bond_stats(mol):
        """
        Retrieve the bond and ring count of the molecule. The bond count is
        calculated for every pair of types (e.g. C1N are all single bonds between
        carbon and nitrogen atoms in the molecule, C2N are all double bonds between
        such atoms etc.). The ring count is provided for rings from size 3 to 8 (R3,
        R4, ..., R8) and for rings greater than size eight (R>8).

        Args:
            mol (ase.Atoms): molecule

        Returns:
            dict (str->int): bond and ring counts
        """
        # 1st analyze bonds
        bond_stats = {}
        obmol = analysis.construct_obmol(mol)
        for bond_idx in range(obmol.NumBonds()):
            bond = obmol.GetBond(bond_idx)
            atom1 = bond.GetBeginAtom().GetAtomicNum()
            atom2 = bond.GetEndAtom().GetAtomicNum()
            type1 = analysis.NUMBER_TO_SYMBOL[min(atom1, atom2)]
            type2 = analysis.NUMBER_TO_SYMBOL[max(atom1, atom2)]
            id = f'{type1}{bond.GetBondOrder()}{type2}'
            bond_stats[id] = bond_stats.get(id, 0) + 1
        # remove twice counted bonds
        for bond_type in bond_stats.keys():
            if bond_type[0] == bond_type[2]:
                bond_stats[id] = int(bond_stats[id] / 2)

        # 2nd analyze rings
        rings = obmol.GetSSSR()
        if len(rings) > 0:
            for ring in rings:
                ring_size = ring.Size()
                if ring_size < 9:
                    bond_stats[f"R{ring_size}"] = bond_stats.get(f"R{ring_size}", 0) + 1
                else:
                    bond_stats["R>8"] = bond_stats.get("R>8", 0) + 1

        return bond_stats


def collect_bond_and_ring_stats(mols, stats, stat_heads):
    """
    Compute the bond and ring counts of a list of molecules and write them to the
    provided array of statistics if it contains the corresponding fields (e.g. 'R3'
    for rings of size 3 or 'C1N' for single bonded carbon-nitrogen pairs). Note that
    only statistics of molecules marked as 'valid' in the stats array are computed and
    that only those statistics will be stored, which already have columns in the stats
    array named accordingly in stat_heads (e.g. if 'R5' for rings of size 5 is not
    included in stat_heads, the number of rings of size 5 will not be stored in the
    stats array for the provided molecules).

    Args:
        mols (list of utiltiy_classes.Molecule): list of molecules for which bond and
            ring statistics are computed
        stats (numpy.ndarray): statistics of all molecules where columns
            correspond to molecules and rows correspond to available statistics
            (n_statistics x n_molecules)
        stat_heads (list of str): the names of the statistics stored in each row in
            stats (e.g. 'F' for the number of fluorine atoms or 'R5' for the number of
            rings of size 5)

    Returns:
        dict (str->numpy.ndarray): dictionary containing the updated statistics array
            under 'stats'
    """
    idx_val = stat_heads.index("valid_mol")
    for i, mol in enumerate(mols):
        if stats[idx_val, i] != 1:
            continue
        bond_stats = get_bond_stats(mol)
        for key, value in bond_stats.items():
            if key not in stat_heads:
                continue
            idx = stat_heads.index(key)
            stats[idx, i] = value
    return {"stats": stats}


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()
    print_file = args.print_file

    molecules = []

    mol_path = args.mol_path
    if os.path.isdir(args.mol_path):
        mol_path = os.path.join(args.mol_path, f'generated_molecules_init={args.init}.db')
    if not os.path.isfile(mol_path):
        print(
            f"\n\nThe specified data path ({mol_path}) is neither a file "
            f"nor a directory! Please specify a different data path."
        )
        raise FileNotFoundError
    else:
        with connect(mol_path) as conn:
            for row in conn.select():
                molecules.append(row.toatoms())

    # compute array with valence of provided atom types
    max_type = max(args.valence[::2])
    valence = np.zeros(max_type + 1, dtype=int)
    valence[args.valence[::2]] = args.valence[1::2]

    # print the chosen settings
    valence_str = ""
    for i in range(max_type + 1):
        if valence[i] > 0:
            valence_str += f"type {i}: {valence[i]}, "

    print(f"\nTarget valence:\n{valence_str[:-2]}\n")

    # initial setup of array for statistics and some counters
    n_generated = len(molecules)
    stat_heads = [
        "n_atoms",
        "valid_mol",
        "valid_atoms",
        "duplicating",
        "n_duplicates",
        "known",
        "equals",
        "C",
        "N",
        "O",
        "F",
        "H",
        "H1C",
        "H1N",
        "H1O",
        "C1C",
        "C2C",
        "C3C",
        "C1N",
        "C2N",
        "C3N",
        "C1O",
        "C2O",
        "C1F",
        "N1N",
        "N2N",
        "N1O",
        "N2O",
        "N1F",
        "O1O",
        "O1F",
        "R3",
        "R4",
        "R5",
        "R6",
        "R7",
        "R8",
        "R>8",
    ]
    stats = np.empty((len(stat_heads), 0))
    valid = []  # True if molecule is valid w.r.t valence, False otherwise
    formulas = []

    start_time = time.time()
    for mol in tqdm.tqdm(molecules):
        n_atoms = len(mol.positions)

        # check valency
        if args.threads <= 0:
            valid_mol, valid_atoms = check_valence(
                mol,
                valence,
            )
        else:
            results = {'valid_mol': [], 'valid_atoms': []}
            results = run_threaded(
                check_valence,
                {"mol": mol},
                {"valence": valence},
                results,
                n_threads=args.threads,
            )
            valid_mol = results['valid_mol']
            valid_atoms = results['valid_atoms']

        # collect statistics of generated data
        n_of_types = [np.sum(mol.numbers == i) for i in [6, 7, 8, 9, 1]]
        formulas.append(str(mol.symbols))
        stats_new = np.stack(
            (
                n_atoms,  # n_atoms
                valid_mol,  # valid molecules
                valid_atoms,  # valid atoms (atoms with correct valence)
                0,  # duplicating
                0,  # n_duplicates
                0,  # known
                0,  # equals
                *n_of_types,  # n_atoms per type
                *np.zeros((19, )),  # n_bonds per type pairs
                *np.zeros((7, )),  # ring counts for 3-8 & >8
            ),
            axis=0,
        )
        stats_new = stats_new.reshape(stats_new.shape[0], 1)
        stats = np.hstack((stats, stats_new))
        valid.append(valid_mol)

    if args.threads <= 0:
            still_valid, duplicating, duplicate_count = filter_unique(
                molecules, valid=valid, use_bits=False
            )
    else:
        still_valid, duplicating, duplicate_count = filter_unique_threaded(
            molecules,
            valid,
            n_threads=args.threads,
            n_mols_per_thread=5,
        )

    stats[stat_heads.index("duplicating")] = np.array(duplicating)
    stats[stat_heads.index("n_duplicates")] = np.array(duplicate_count)

    if not print_file:
        print("\033[K", end="\r", flush=True)
    end_time = time.time() - start_time
    m, s = divmod(end_time, 60)
    h, m = divmod(m, 60)
    h, m, s = int(h), int(m), int(s)
    print(f"Needed {h:d}h{m:02d}m{s:02d}s.")

    if args.threads <= 0:
        results = collect_bond_and_ring_stats(molecules, stats, stat_heads)
    else:
        results = {"stats": stats.T}
        run_threaded(
            collect_bond_and_ring_stats,
            {"mols": molecules, "stats": stats},
            {"stat_heads": stat_heads},
            results=results,
            n_threads=args.threads,
        )
    stats = results["stats"]

    print(
        f"Number of generated molecules: {n_generated}\n"
        f"Number of duplicate molecules: {sum(duplicate_count)}"
    )

    n_valid_mol = 0
    for i in range(n_generated):
        if stats[2, i] == 1 and duplicating[i] == -1:
            n_valid_mol += 1

    print(f"Number of unique and valid molecules: {n_valid_mol}")

    # filter molecules which were seen during training
    if args.model_path is not None:
        stats = filter_new(
            molecules,
            stats,
            stat_heads,
            args.model_path,
            args.data_path,
            print_file=print_file,
            n_threads=args.threads,
        )

    # store gathered statistics in metrics dataframe
    stats_df = pd.DataFrame(
        stats.T, columns=np.array(stat_heads)
    )
    stats_df.insert(0, "formula", formulas)
    metric_df_dict = analysis.get_results_as_dataframe(
        [""],
        ["total_loss", "atom_type_loss", "position_loss"],
        args.model_path,
    )
    cum_stats = {
        "valid_mol": stats_df["valid_mol"].sum() / len(stats_df),
        "valid_atoms": stats_df["valid_atoms"].sum() / stats_df["n_atoms"].sum(),
        "n_duplicates": stats_df["duplicating"].apply(lambda x: x != -1).sum(),
        "known": stats_df["known"].apply(lambda x: x > 0).sum(),
        "known_train": stats_df["known"].apply(lambda x: x == 1).sum(),
        "known_val": stats_df["known"].apply(lambda x: x == 2).sum(),
        "known_test": stats_df["known"].apply(lambda x: x == 3).sum(),
    }
    ring_bond_cols = [
        "C",
        "N",
        "O",
        "F",
        "H",
        "H1C",
        "H1N",
        "H1O",
        "C1C",
        "C2C",
        "C3C",
        "C1N",
        "C2N",
        "C3N",
        "C1O",
        "C2O",
        "C1F",
        "N1N",
        "N2N",
        "N1O",
        "N2O",
        "N1F",
        "O1O",
        "O1F",
        "R3",
        "R4",
        "R5",
        "R6",
        "R7",
        "R8",
        "R>8",
    ]
    for col_name in ring_bond_cols:
        cum_stats[col_name] = stats_df[col_name].sum()

    cum_stats_df = pd.DataFrame(
        cum_stats, columns=list(cum_stats.keys()), index=[0]
    )

    metric_df_dict["generated_stats_overall"] = cum_stats_df
    metric_df_dict["generated_stats"] = stats_df

    # store results in pickle file
    stats_path = os.path.join(args.mol_path, f"generated_molecules_init={args.init}_statistics.pkl")
    if os.path.isfile(stats_path):
        file_name, _ = os.path.splitext(stats_path)
        expand = 0
        while True:
            expand += 1
            new_file_name = file_name + "_" + str(expand)
            if os.path.isfile(new_file_name + ".pkl"):
                continue
            else:
                stats_path = new_file_name + ".pkl"
                break
    with open(stats_path, "wb") as f:
        pickle.dump(metric_df_dict, f)
