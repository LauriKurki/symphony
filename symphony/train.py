"""Library file for executing the training and evaluation of generative models."""

import functools
import os
import pickle
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple, Union

import chex
import flax
import jax
import jax.numpy as jnp
import jraph
import ml_collections
import optax
import yaml
from absl import logging
import matplotlib.pyplot as plt

from clu import (
    checkpoint,
    metric_writers,
    metrics,
    parameter_overview,
    periodic_actions,
)
from flax.training import train_state

from symphony import datatypes, models, loss
from symphony.data import input_pipeline


@flax.struct.dataclass
class Metrics(metrics.Collection):
    total_loss: metrics.Average.from_output("total_loss")
    focus_and_atom_type_loss: metrics.Average.from_output("focus_and_atom_type_loss")
    position_loss: metrics.Average.from_output("position_loss")
    denoising_loss: metrics.Average.from_output("denoising_loss")


def add_prefix_to_keys(result: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    """Adds a prefix to the keys of a dict, returning a new dict."""
    return {f"{prefix}/{key}": val for key, val in result.items()}


def create_optimizer(config: ml_collections.ConfigDict) -> optax.GradientTransformation:
    """Create an optimizer as specified by the config."""
    # If a learning rate schedule is specified, use it.
    if config.get("learning_rate_schedule") is not None:
        if config.learning_rate_schedule == "constant":
            learning_rate_or_schedule = optax.constant_schedule(config.learning_rate)
        elif config.learning_rate_schedule == "sgdr":
            num_cycles = (
                1
                + config.num_train_steps
                // config.learning_rate_schedule_kwargs.decay_steps
            )
            learning_rate_or_schedule = optax.sgdr_schedule(
                cosine_kwargs=(
                    config.learning_rate_schedule_kwargs for _ in range(num_cycles)
                )
            )
    else:
        learning_rate_or_schedule = config.learning_rate

    if config.optimizer == "adam":
        return optax.adam(learning_rate=learning_rate_or_schedule)

    if config.optimizer == "sgd":
        return optax.sgd(
            learning_rate=learning_rate_or_schedule, momentum=config.momentum
        )
    raise ValueError(f"Unsupported optimizer: {config.optimizer}.")


@jax.profiler.annotate_function
def get_predictions(
    state: train_state.TrainState,
    graphs: datatypes.Fragments,
    rng: Optional[chex.Array],
) -> datatypes.Predictions:
    """Get predictions from the network for input graphs."""
    return state.apply_fn(state.params, rng, graphs)


@functools.partial(jax.jit, static_argnames=["loss_kwargs", "add_noise_to_positions"])
def train_step(
    state: train_state.TrainState,
    graphs: datatypes.Fragments,
    loss_kwargs: Dict[str, Union[float, int]],
    rng: chex.PRNGKey,
    add_noise_to_positions: bool,
    noise_std: float,
) -> Tuple[train_state.TrainState, metrics.Collection]:
    """Performs one update step over the current batch of graphs."""

    def loss_fn(params: optax.Params, graphs: datatypes.Fragments) -> float:
        curr_state = state.replace(params=params)
        preds = get_predictions(curr_state, graphs, rng=None)
        total_loss, (
            focus_and_atom_type_loss,
            position_loss,
            denoising_loss,
        ) = loss.generation_loss(
            preds=preds, graphs=graphs, position_noise=position_noise, **loss_kwargs
        )
        mask = jraph.get_graph_padding_mask(graphs)
        mean_loss = jnp.sum(jnp.where(mask, total_loss, 0.0)) / jnp.sum(mask)
        return mean_loss, (
            total_loss,
            focus_and_atom_type_loss,
            position_loss,
            denoising_loss,
            mask,
        )

    # Add noise to positions, if required.
    if add_noise_to_positions:
        noise_rng, rng = jax.random.split(rng)
        position_noise = (
            jax.random.normal(noise_rng, graphs.nodes.positions.shape) * noise_std
        )
    else:
        position_noise = jnp.zeros_like(graphs.nodes.positions)

    noisy_positions = graphs.nodes.positions + position_noise
    graphs = graphs._replace(nodes=graphs.nodes._replace(positions=noisy_positions))

    # Compute gradients.
    grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
    (
        _,
        (total_loss, focus_and_atom_type_loss, position_loss, denoising_loss, mask),
    ), grads = grad_fn(state.params, graphs)
    state = state.apply_gradients(grads=grads)

    # Log norms of gradients.
    # grad_norms = sum(jax.tree_leaves(jax.tree_map(jnp.linalg.norm, grads)))
    # jax.debug.print("grad_norms={grad_norms}", grad_norms=grad_norms)

    batch_metrics = Metrics.single_from_model_output(
        total_loss=total_loss,
        focus_and_atom_type_loss=focus_and_atom_type_loss,
        position_loss=position_loss,
        denoising_loss=denoising_loss,
        mask=mask,
    )
    return state, batch_metrics


@functools.partial(jax.jit, static_argnames=["loss_kwargs"])
def evaluate_step(
    eval_state: train_state.TrainState,
    graphs: datatypes.Fragments,
    rng: chex.PRNGKey,
    loss_kwargs: Dict[str, Union[float, int]],
) -> metrics.Collection:
    """Computes metrics over a set of graphs."""
    # Compute predictions and resulting loss.
    preds = get_predictions(eval_state, graphs, rng)
    total_loss, (
        focus_and_atom_type_loss,
        position_loss,
        denoising_loss,
    ) = loss.generation_loss(
        preds=preds, graphs=graphs, position_noise=None, **loss_kwargs
    )

    # Consider only valid graphs.
    mask = jraph.get_graph_padding_mask(graphs)
    return Metrics.single_from_model_output(
        total_loss=total_loss,
        focus_and_atom_type_loss=focus_and_atom_type_loss,
        position_loss=position_loss,
        denoising_loss=denoising_loss,
        mask=mask,
    )


def evaluate_model(
    eval_state: train_state.TrainState,
    datasets: Iterator[datatypes.Fragments],
    splits: Iterable[str],
    rng: chex.PRNGKey,
    loss_kwargs: Dict[str, Union[float, int]],
    mask_atom_types_in_fragments: bool,
) -> Dict[str, metrics.Collection]:
    """Evaluates the model on metrics over the specified splits."""

    # Loop over each split independently.
    eval_metrics = {}
    for split in splits:
        split_metrics = None

        # Loop over graphs.
        for graphs in datasets[split].as_numpy_iterator():
            graphs = datatypes.Fragments.from_graphstuple(graphs)

            if mask_atom_types_in_fragments:
                graphs = mask_atom_types(graphs)

            # Compute metrics for this batch.
            step_rng, rng = jax.random.split(rng)
            batch_metrics = evaluate_step(eval_state, graphs, step_rng, loss_kwargs)

            # Update metrics.
            if split_metrics is None:
                split_metrics = batch_metrics
            else:
                split_metrics = split_metrics.merge(batch_metrics)

        eval_metrics[split] = split_metrics

    return eval_metrics


@jax.jit
def mask_atom_types(graphs: datatypes.Fragments) -> datatypes.Fragments:
    """Mask atom types in graphs."""

    def aggregate_sum(arr: jnp.ndarray) -> jnp.ndarray:
        """Aggregates the sum of all elements upto the last in arr into the first element."""
        # Set the first element of arr as the sum of all elements upto the last element.
        # Keep the last element as is.
        # Set all of the other elements to 0.
        return jnp.concatenate(
            [arr[:-1].sum(axis=0, keepdims=True), jnp.zeros_like(arr[:-1]), arr[-1:]],
            axis=0,
        )

    focus_and_target_species_probs = graphs.nodes.focus_and_target_species_probs
    focus_and_target_species_probs = jax.vmap(aggregate_sum)(
        focus_and_target_species_probs
    )
    graphs = graphs._replace(
        nodes=graphs.nodes._replace(
            species=jnp.zeros_like(graphs.nodes.species),
            focus_and_target_species_probs=focus_and_target_species_probs,
        ),
        globals=graphs.globals._replace(
            target_species=jnp.zeros_like(graphs.globals.target_species)
        ),
    )
    return graphs


def train_and_evaluate(
    config: ml_collections.FrozenConfigDict, workdir: str
) -> train_state.TrainState:
    """Execute model training and evaluation loop.

    Args:
      config: Hyperparameter configuration for training and evaluation.
      workdir: Directory where the TensorBoard summaries are written to.

    Returns:
      The train state (which includes the `.params`).
    """
    # We only support single-host training.
    assert jax.process_count() == 1

    # Helper for evaluation.
    def evaluate_model_helper(
        eval_state: train_state.TrainState,
        step: int,
        rng: chex.PRNGKey,
        is_final_eval: bool,
    ) -> Dict[str, metrics.Collection]:
        # Final eval splits are usually different.
        if is_final_eval:
            splits = ["train_eval_final", "val_eval_final", "test_eval_final"]
        else:
            splits = ["train_eval", "val_eval", "test_eval"]

        # Evaluate the model.
        with report_progress.timed("eval"):
            eval_metrics = evaluate_model(
                eval_state,
                datasets,
                splits,
                rng,
                config.loss_kwargs,
                config.mask_atom_types,
            )

        # Compute and write metrics.
        for split in splits:
            eval_metrics[split] = eval_metrics[split].compute()
            writer.write_scalars(step, add_prefix_to_keys(eval_metrics[split], split))
        writer.flush()

        return eval_metrics

    # Create writer for logs.
    writer = metric_writers.create_default_writer(workdir)
    writer.write_hparams(config.to_dict())

    # Get datasets, organized by split.
    logging.info("Obtaining datasets.")
    rng = jax.random.PRNGKey(config.rng_seed)
    rng, dataset_rng = jax.random.split(rng)
    datasets = input_pipeline.get_datasets(dataset_rng, config)
    # datasets = input_pipeline_tf.get_datasets(dataset_rng, config)

    # Create and initialize the network.
    logging.info("Initializing network.")
    train_iter = datasets["train"].as_numpy_iterator()
    init_graphs = next(train_iter)
    net = models.create_model(config, run_in_evaluation_mode=False)

    rng, init_rng = jax.random.split(rng)
    params = jax.jit(net.init)(init_rng, init_graphs)
    parameter_overview.log_parameter_overview(params)

    # Create the optimizer.
    tx = create_optimizer(config)

    # Create the training state.
    state = train_state.TrainState.create(
        apply_fn=jax.jit(net.apply), params=params, tx=tx
    )

    # Create a corresponding evaluation state.
    eval_net = models.create_model(config, run_in_evaluation_mode=False)
    eval_state = state.replace(apply_fn=jax.jit(eval_net.apply))

    # Set up checkpointing of the model.
    # We will record the best model seen during training.
    checkpoint_dir = os.path.join(workdir, "checkpoints")
    ckpt = checkpoint.Checkpoint(checkpoint_dir, max_to_keep=5)
    restored = ckpt.restore_or_initialize(
        {
            "state": state,
            "best_state": state,
            "step_for_best_state": 1.0,
            "metrics_for_best_state": None,
        }
    )
    state = restored["state"]
    best_state = restored["best_state"]
    step_for_best_state = restored["step_for_best_state"]
    metrics_for_best_state = restored["metrics_for_best_state"]
    if metrics_for_best_state is None:
        min_val_loss = float("inf")
    else:
        min_val_loss = metrics_for_best_state["val_eval"]["total_loss"]
    initial_step = int(state.step) + 1

    # Save the config for reproducibility.
    config_path = os.path.join(workdir, "config.yml")
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    # Hooks called periodically during training.
    report_progress = periodic_actions.ReportProgress(
        num_train_steps=config.num_train_steps, writer=writer
    )
    profile = periodic_actions.Profile(
        logdir=workdir,
        every_secs=10800,
    )
    hooks = [report_progress, profile]

    # Begin training loop.
    logging.info("Starting training.")
    train_metrics = None
    # all_grad_norms = []
    for step in range(initial_step, config.num_train_steps + 1):
        # Log, if required.
        first_or_last_step = step in [initial_step, config.num_train_steps]
        if step % config.log_every_steps == 0 or first_or_last_step:
            if train_metrics is not None:
                writer.write_scalars(
                    step, add_prefix_to_keys(train_metrics.compute(), "train")
                )
            train_metrics = None

        # Evaluate on validation and test splits, if required.
        if step % config.eval_every_steps == 0 or first_or_last_step:
            eval_state = eval_state.replace(params=state.params)

            # Evaluate on validation and test splits.
            rng, eval_rng = jax.random.split(rng)
            eval_metrics = evaluate_model_helper(
                eval_state,
                step,
                eval_rng,
                is_final_eval=False,
            )

            # Note best state seen so far.
            # Best state is defined as the state with the lowest validation loss.
            if eval_metrics["val_eval"]["total_loss"] < min_val_loss:
                min_val_loss = eval_metrics["val_eval"]["total_loss"]
                metrics_for_best_state = eval_metrics
                best_state = state
                step_for_best_state = step
                logging.info("New best state found at step %d.", step)

            # Save the current state and best state seen so far.
            with open(os.path.join(checkpoint_dir, f"params_{step}.pkl"), "wb") as f:
                pickle.dump(state.params, f)
            with open(os.path.join(checkpoint_dir, "params_best.pkl"), "wb") as f:
                pickle.dump(best_state.params, f)
            ckpt.save(
                {
                    "state": state,
                    "best_state": best_state,
                    "step_for_best_state": step_for_best_state,
                    "metrics_for_best_state": metrics_for_best_state,
                }
            )

        # Get a batch of graphs.
        try:
            graphs = next(train_iter)
            graphs = datatypes.Fragments.from_graphstuple(graphs)

            if config.mask_atom_types:
                graphs = mask_atom_types(graphs)

        except StopIteration:
            logging.info("No more training data. Continuing with final evaluation.")
            break

        # Perform one step of training.
        with jax.profiler.StepTraceAnnotation("train_step", step_num=step):
            step_rng, rng = jax.random.split(rng)
            state, batch_metrics = train_step(
                state,
                graphs,
                loss_kwargs=config.loss_kwargs,
                rng=step_rng,
                add_noise_to_positions=config.add_noise_to_positions,
                noise_std=config.position_noise_std,
            )

            # all_grad_norms.append(grad_norms)
            # focus_and_atom_type_loss = batch_metrics.compute()[
            #     "focus_and_atom_type_loss"
            # ]
            # if grad_norms > 1e3 or jnp.isnan(focus_and_atom_type_loss):
            #     plt.plot(all_grad_norms)
            #     plt.yscale("log")
            #     plt.xlabel("step")
            #     plt.ylabel("grad norm")
            #     plt.title("Gradient norms")
            #     plt.savefig("grad_norms.png")
            #     plt.close()

            #     preds: datatypes.Predictions = get_predictions(state, graphs, rng=None)
            #     _, (focus_and_atom_type_loss, _) = loss.generation_loss(
            #         preds, graphs, **config.loss_kwargs
            #     )
            #     mask = jraph.get_graph_padding_mask(graphs)
            #     focus_and_atom_type_loss = jnp.where(
            #         mask, focus_and_atom_type_loss, 0.0
            #     )
            #     index = jnp.argmax(focus_and_atom_type_loss)

            #     problematic_graph = jraph.unbatch(graphs)[index]
            #     import ase
            #     import ase.io

            #     problematic_graph_ase = ase.Atoms(
            #         numbers=models.get_atomic_numbers(problematic_graph.nodes.species),
            #         positions=problematic_graph.nodes.positions,
            #     )
            #     ase.io.write(f"problematic_graph_{step}.xyz", problematic_graph_ase)

            #     preds: datatypes.Predictions = get_predictions(
            #         state, problematic_graph, rng=None
            #     )

            #     raise ValueError(
            #         "focus_and_atom_type_loss",
            #         focus_and_atom_type_loss,
            #         "positions",
            #         problematic_graph.nodes.positions,
            #         "species",
            #         problematic_graph.nodes.species,
            #         "target_focus_and_target_species_probs",
            #         problematic_graph.nodes.focus_and_target_species_probs,
            #         "embeddings",
            #         preds.nodes.embeddings,
            #         "focus_and_target_species_logits",
            #         preds.nodes.focus_and_target_species_logits,
            #         "focus_and_target_species_probs",
            #         preds.nodes.focus_and_target_species_probs,
            #     )

        # Update metrics.
        if train_metrics is None:
            train_metrics = batch_metrics
        else:
            train_metrics = train_metrics.merge(batch_metrics)

        # Quick indication that training is happening.
        logging.log_first_n(logging.INFO, "Finished training step %d.", 10, step)
        for hook in hooks:
            hook(step)

    # Once training is complete, return the best state and corresponding metrics.
    logging.info(
        "Evaluating best state from step %d at the end of training.",
        step_for_best_state,
    )
    eval_state = eval_state.replace(params=best_state.params)

    # Evaluate on validation and test splits, but at the end of training.
    rng, eval_rng = jax.random.split(rng)
    metrics_for_best_state = evaluate_model_helper(
        eval_state,
        step,
        eval_rng,
        is_final_eval=True,
    )

    # Checkpoint the best state and corresponding metrics seen during training.
    # Save pickled parameters for easy access during evaluation.
    with report_progress.timed("checkpoint"):
        with open(os.path.join(checkpoint_dir, "params_best.pkl"), "wb") as f:
            pickle.dump(best_state.params, f)
        ckpt.save(
            {
                "state": state,
                "best_state": best_state,
                "step_for_best_state": step_for_best_state,
                "metrics_for_best_state": metrics_for_best_state,
            }
        )

    return best_state, metrics_for_best_state
