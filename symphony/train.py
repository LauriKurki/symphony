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
from symphony.data import input_pipeline_tf


@flax.struct.dataclass
class Metrics(metrics.Collection):
    total_loss: metrics.Average.from_output("total_loss")
    focus_and_atom_type_loss: metrics.Average.from_output("focus_and_atom_type_loss")
    position_loss: metrics.Average.from_output("position_loss")


def add_prefix_to_keys(result: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    """Adds a prefix to the keys of a dict, returning a new dict."""
    return {f"{prefix}/{key}": val for key, val in result.items()}


def device_batch(
    graph_iterator: Iterator[datatypes.Fragments],
) -> Iterator[datatypes.Fragments]:
    """Batches a set of graphs to the size of the number of devices."""
    num_devices = jax.local_device_count()
    batch = []
    for idx, graph in enumerate(graph_iterator):
        if idx % num_devices == num_devices - 1:
            batch.append(graph)
            batch = jax.tree_map(lambda *x: jnp.stack(x, axis=0), *batch)
            batch = datatypes.Fragments.from_graphstuple(batch)
            yield batch

            batch = []
        else:
            batch.append(graph)


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
        tx = optax.adam(learning_rate=learning_rate_or_schedule)

    if config.optimizer == "sgd":
        tx = optax.sgd(
            learning_rate=learning_rate_or_schedule, momentum=config.momentum
        )

    if not config.get("freeze_node_embedders"):
        return tx

    # Freeze parameters of the node embedders, if required.
    def flattened_traversal(fn):
        """Returns function that is called with `(path, param)` instead of pytree."""

        def mask(tree):
            flat = flax.traverse_util.flatten_dict(tree)
            return flax.traverse_util.unflatten_dict(
                {k: fn(k, v) for k, v in flat.items()}
            )

        return mask

    # Freezes the node embedders.
    def label_fn(path, param):
        del param
        if path[0].startswith("node_embedder"):
            return "no"
        return "yes"

    return optax.multi_transform(
        {"yes": tx, "no": optax.set_to_zero()}, flattened_traversal(label_fn)
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


@functools.partial(jax.pmap, axis_name="device", static_broadcasted_argnums=[2, 4, 5])
def train_step(
    graphs: datatypes.Fragments,
    state: train_state.TrainState,
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
        ) = loss.generation_loss(preds=preds, graphs=graphs, **loss_kwargs)
        mask = jraph.get_graph_padding_mask(graphs)
        mean_loss = jnp.sum(jnp.where(mask, total_loss, 0.0)) / jnp.sum(mask)
        return mean_loss, (
            total_loss,
            focus_and_atom_type_loss,
            position_loss,
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
        (total_loss, focus_and_atom_type_loss, position_loss, mask),
    ), grads = grad_fn(state.params, graphs)

    # Average gradients across devices.
    grads = jax.lax.pmean(grads, axis_name="device")
    state = state.apply_gradients(grads=grads)

    batch_metrics = Metrics.gather_from_model_output(
        axis_name="device",
        total_loss=total_loss,
        focus_and_atom_type_loss=focus_and_atom_type_loss,
        position_loss=position_loss,
        mask=mask,
    )
    return state, batch_metrics


@functools.partial(jax.pmap, axis_name="device", static_broadcasted_argnums=[3])
def evaluate_step(
    graphs: datatypes.Fragments,
    eval_state: train_state.TrainState,
    rng: chex.PRNGKey,
    loss_kwargs: Dict[str, Union[float, int]],
) -> metrics.Collection:
    """Computes metrics over a set of graphs."""
    # Compute predictions and resulting loss.
    preds = get_predictions(eval_state, graphs, rng)
    total_loss, (
        focus_and_atom_type_loss,
        position_loss,
    ) = loss.generation_loss(preds=preds, graphs=graphs, **loss_kwargs)

    # Consider only valid graphs.
    mask = jraph.get_graph_padding_mask(graphs)
    return Metrics.gather_from_model_output(
        axis_name="device",
        total_loss=total_loss,
        focus_and_atom_type_loss=focus_and_atom_type_loss,
        position_loss=position_loss,
        mask=mask,
    )


def evaluate_model(
    eval_state: train_state.TrainState,
    datasets: Iterator[datatypes.Fragments],
    splits: Iterable[str],
    rng: chex.PRNGKey,
    loss_kwargs: Dict[str, Union[float, int]],
) -> Dict[str, metrics.Collection]:
    """Evaluates the model on metrics over the specified splits."""

    # Loop over each split independently.
    eval_metrics = {}
    for split in splits:
        split_metrics = flax.jax_utils.replicate(Metrics.empty())

        # Loop over graphs.
        for graphs in device_batch(datasets[split].as_numpy_iterator()):
            # Compute metrics for this batch.
            step_rng, rng = jax.random.split(rng)
            step_rngs = jax.random.split(step_rng, jax.local_device_count())
            batch_metrics = evaluate_step(graphs, eval_state, step_rngs, loss_kwargs)
            split_metrics = split_metrics.merge(batch_metrics)

        eval_metrics[split] = flax.jax_utils.unreplicate(split_metrics)

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

    # Save the config for reproducibility.
    config_path = os.path.join(workdir, "config.yml")
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    # Get datasets, organized by split.
    logging.info("Obtaining datasets.")
    rng = jax.random.PRNGKey(config.rng_seed)
    rng, dataset_rng = jax.random.split(rng)
    # datasets = input_pipeline.get_datasets(dataset_rng, config)
    datasets = input_pipeline_tf.get_datasets(dataset_rng, config)

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

    # Replicate the training and evaluation state across devices.
    state = flax.jax_utils.replicate(state)
    best_state = flax.jax_utils.replicate(best_state)
    eval_state = flax.jax_utils.replicate(eval_state)

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
    train_metrics = flax.jax_utils.replicate(Metrics.empty())
    train_metrics_empty = True
    all_grad_norms = []
    all_param_norms = []
    all_params = []
    all_focus_and_atom_type_losses = []
    all_num_nodes = []
    all_num_edges = []

    for step in range(initial_step, config.num_train_steps + 1):
        # Log, if required.
        first_or_last_step = step in [initial_step, config.num_train_steps]
        if step % config.log_every_steps == 0 or first_or_last_step:
            if not train_metrics_empty:
                writer.write_scalars(
                    step,
                    add_prefix_to_keys(
                        flax.jax_utils.unreplicate(train_metrics).compute(), "train"
                    ),
                )
            train_metrics = flax.jax_utils.replicate(Metrics.empty())
            train_metrics_empty = True

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
                pickle.dump(flax.jax_utils.unreplicate(state.params), f)
            with open(os.path.join(checkpoint_dir, "params_best.pkl"), "wb") as f:
                pickle.dump(flax.jax_utils.unreplicate(best_state.params), f)
            ckpt.save(
                {
                    "state": flax.jax_utils.unreplicate(state),
                    "best_state": flax.jax_utils.unreplicate(best_state),
                    "step_for_best_state": step_for_best_state,
                    "metrics_for_best_state": metrics_for_best_state,
                }
            )

        # Get a batch of graphs.
        try:
            graphs = next(device_batch(train_iter))

        except StopIteration:
            logging.info("No more training data. Continuing with final evaluation.")
            break

        # Perform one step of training.
        with jax.profiler.StepTraceAnnotation("train_step", step_num=step):
            step_rng, rng = jax.random.split(rng)
            step_rngs = jax.random.split(step_rng, jax.local_device_count())
            state, batch_metrics = train_step(
                graphs,
                state,
                config.loss_kwargs,
                step_rngs,
                config.add_noise_to_positions,
                config.position_noise_std,
            )

            # Update metrics.
            train_metrics = train_metrics.merge(batch_metrics)
            train_metrics_empty = False

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
    final_metrics_for_best_state = evaluate_model_helper(
        eval_state,
        step,
        eval_rng,
        is_final_eval=True,
    )

    # Checkpoint the best state and corresponding metrics seen during training.
    # Save pickled parameters for easy access during evaluation.
    with report_progress.timed("checkpoint"):
        with open(os.path.join(checkpoint_dir, "params_best.pkl"), "wb") as f:
            pickle.dump(flax.jax_utils.unreplicate(best_state.params), f)
        ckpt.save(
            {
                "state": flax.jax_utils.unreplicate(state),
                "best_state": flax.jax_utils.unreplicate(best_state),
                "step_for_best_state": step_for_best_state,
                "metrics_for_best_state": metrics_for_best_state,
            }
        )

    return best_state, final_metrics_for_best_state
