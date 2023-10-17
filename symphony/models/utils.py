"""Helpers for the generative models."""

from typing import Callable, Optional, Tuple, Union

import chex
import e3nn_jax as e3nn
import haiku as hk
import jax
import jax.numpy as jnp
import jraph
import ml_collections

from symphony import datatypes
from symphony.models.predictor import Predictor
from symphony.models.embedders.global_embedder import GlobalEmbedder
from symphony.models.focus_predictor import FocusAndTargetSpeciesPredictor
from symphony.models.position_predictor import (
    TargetPositionPredictor,
    FactorizedTargetPositionPredictor,
)
from symphony.models.position_updater import PositionUpdater
from symphony.models.embedders import nequip, marionette, e3schnet, mace, allegro

ATOMIC_NUMBERS = [1, 6, 7, 8, 9]


def get_atomic_numbers(species: jnp.ndarray) -> jnp.ndarray:
    """Returns the atomic numbers for the species."""
    return jnp.asarray(ATOMIC_NUMBERS)[species]


def get_first_node_indices(graphs: jraph.GraphsTuple) -> jnp.ndarray:
    """Returns the indices of the focus nodes in each graph."""
    return jnp.concatenate((jnp.asarray([0]), jnp.cumsum(graphs.n_node)[:-1]))


def segment_softmax_2D_with_stop(
    focus_and_target_species_logits: jnp.ndarray,
    stop_logits: jnp.ndarray,
    segment_ids: jnp.ndarray,
    num_segments: int,
) -> Tuple[jnp.ndarray, jnp.ndarray]:
    """Returns the focus, target and stop probabilities with segment softmax over 2D arrays of species logits."""
    # Subtract the max to avoid numerical issues.
    logits_max = jraph.segment_max(
        focus_and_target_species_logits, segment_ids, num_segments=num_segments
    ).max(axis=-1)
    logits_max = jnp.maximum(logits_max, stop_logits)
    logits_max = jax.lax.stop_gradient(logits_max)
    focus_and_target_species_logits -= logits_max[segment_ids, None]
    stop_logits -= logits_max

    # Normalize exp() by all nodes, all atom types, and the stop for each graph.
    exp_focus_and_target_species_logits = jnp.exp(focus_and_target_species_logits)
    exp_focus_and_target_species_logits_summed = jnp.sum(
        exp_focus_and_target_species_logits, axis=-1
    )
    normalizing_factors = jraph.segment_sum(
        exp_focus_and_target_species_logits_summed,
        segment_ids,
        num_segments=num_segments,
    )
    exp_stop_logits = jnp.exp(stop_logits)

    normalizing_factors += exp_stop_logits
    species_probs = (
        exp_focus_and_target_species_logits / normalizing_factors[segment_ids, None]
    )
    stop_probs = exp_stop_logits / normalizing_factors

    return species_probs, stop_probs


def get_segment_ids(
    n_node: jnp.ndarray,
    num_nodes: int,
) -> jnp.ndarray:
    """Returns the segment ids for each node in the graphs."""
    num_graphs = n_node.shape[0]

    return jnp.repeat(
        jnp.arange(num_graphs), n_node, axis=0, total_repeat_length=num_nodes
    )


def sample_from_angular_distribution_with_radial_field(
    angular_probs: e3nn.SphericalSignal,
    radial_field: e3nn.SphericalSignal,
    rng: chex.PRNGKey,
):
    """Sample a unit vector from an angular distribution."""
    beta_index, alpha_index = angular_probs.sample(rng)
    return (
        radial_field.grid_values[beta_index, alpha_index]
        * angular_probs.grid_vectors[beta_index, alpha_index]
    )


def sample_from_angular_distribution(
    angular_probs: e3nn.SphericalSignal, rng: chex.PRNGKey
):
    """Sample a unit vector from an angular distribution."""
    beta_index, alpha_index = angular_probs.sample(rng)
    return angular_probs.grid_vectors[beta_index, alpha_index]


def sample_from_position_distribution(
    position_probs: e3nn.SphericalSignal, radii: jnp.ndarray, rng: chex.PRNGKey
) -> jnp.ndarray:
    """Samples a position vector from a distribution over all positions."""
    num_radii = radii.shape[0]
    assert radii.shape == (num_radii,)
    assert position_probs.shape == (
        num_radii,
        position_probs.res_beta,
        position_probs.res_alpha,
    )

    # Sample a radius.
    radial_probs = position_distribution_to_radial_distribution(position_probs)
    rng, radius_rng = jax.random.split(rng)
    radius_index = jax.random.choice(radius_rng, num_radii, p=radial_probs)

    # Get the angular probabilities.
    angular_probs = (
        position_probs[radius_index] / position_probs[radius_index].integrate()
    )

    # Sample angles.
    rng, angular_rng = jax.random.split(rng)
    unit_vector = sample_from_angular_distribution(angular_probs, angular_rng)

    # Combine the radius and angles to get the position vectors.
    position_vector = radii[radius_index] * unit_vector
    return position_vector


def segment_sample_2D(
    species_probabilities: jnp.ndarray,
    segment_ids: jnp.ndarray,
    num_segments: int,
    rng: chex.PRNGKey,
) -> Tuple[jnp.ndarray, jnp.ndarray]:
    """Sample indices from a categorical distribution across each segment.
    Args:
        species_probabilities: A 2D array of probabilities.
        segment_ids: A 1D array of segment ids.
        num_segments: The number of segments.
        rng: A PRNG key.
    Returns:
        A 1D array of sampled indices, one for each segment.
    """
    num_nodes, num_species = species_probabilities.shape

    # Normalize the probabilities to sum up for 1 over all nodes in each graph.
    species_probabilities_summed = jraph.segment_sum(
        species_probabilities.sum(axis=-1), segment_ids, num_segments
    )
    species_probabilities = (
        species_probabilities / species_probabilities_summed[segment_ids, None]
    )

    def sample_for_segment(rng: chex.PRNGKey, segment_id: int) -> Tuple[float, float]:
        """Samples a node and species index for a single segment."""
        node_rng, logit_rng, rng = jax.random.split(rng, num=3)
        node_index = jax.random.choice(
            node_rng,
            jnp.arange(num_nodes),
            p=jnp.where(
                segment_id == segment_ids, species_probabilities.sum(axis=-1), 0.0
            ),
        )
        normalized_probs_for_index = species_probabilities[node_index] / jnp.sum(
            species_probabilities[node_index]
        )
        species_index = jax.random.choice(
            logit_rng, jnp.arange(num_species), p=normalized_probs_for_index
        )
        return node_index, species_index

    rngs = jax.random.split(rng, num_segments)
    node_indices, species_indices = jax.vmap(sample_for_segment)(
        rngs, jnp.arange(num_segments)
    )
    assert node_indices.shape == (num_segments,)
    assert species_indices.shape == (num_segments,)
    return node_indices, species_indices


def log_coeffs_to_logits(
    log_coeffs: e3nn.IrrepsArray, res_beta: int, res_alpha: int
) -> e3nn.SphericalSignal:
    """Converts coefficients of the logits to a SphericalSignal representing the logits."""
    num_channels = log_coeffs.shape[0]
    num_radii = log_coeffs.shape[1]
    assert log_coeffs.shape == (
        num_channels,
        num_radii,
        log_coeffs.irreps.dim,
    ), f"{log_coeffs.shape}"

    log_dist = e3nn.to_s2grid(
        log_coeffs, res_beta, res_alpha, quadrature="gausslegendre", p_val=1, p_arg=-1
    )
    assert log_dist.shape == (num_channels, num_radii, res_beta, res_alpha)

    # Combine over all channels.
    log_dist.grid_values = jax.scipy.special.logsumexp(log_dist.grid_values, axis=0)
    assert log_dist.shape == (num_radii, res_beta, res_alpha)

    # Subtract the max to avoid numerical issues.
    max_logit = jnp.max(log_dist.grid_values)
    max_logit = jax.lax.stop_gradient(max_logit)
    log_dist.grid_values -= max_logit

    return log_dist


def position_logits_to_position_distribution(
    position_logits: e3nn.SphericalSignal,
) -> e3nn.SphericalSignal:
    """Converts logits to a SphericalSignal representing the position distribution."""

    assert len(position_logits.shape) == 3  # [num_radii, res_beta, res_alpha]
    max_logit = jnp.max(position_logits.grid_values)
    max_logit = jax.lax.stop_gradient(max_logit)

    position_probs = position_logits.apply(lambda logit: jnp.exp(logit - max_logit))

    position_probs.grid_values /= position_probs.integrate().array.sum()
    return position_probs


def safe_log(x: jnp.ndarray, eps: float = 1e-9) -> jnp.ndarray:
    """Computes the log of x, replacing 0 with a small value for numerical stability."""
    return jnp.log(jnp.where(x == 0, eps, x))


def position_distribution_to_radial_distribution(
    position_probs: e3nn.SphericalSignal,
) -> jnp.ndarray:
    """Computes the marginal radial distribution from a logits of a distribution over all positions."""
    assert len(position_probs.shape) == 3  # [num_radii, res_beta, res_alpha]
    return position_probs.integrate().array.squeeze(axis=-1)  # [..., num_radii]


def position_distribution_to_angular_distribution(
    position_probs: e3nn.SphericalSignal,
) -> jnp.ndarray:
    """Returns the marginal radial distribution for a logits of a distribution over all positions."""
    assert len(position_probs.shape) == 3  # [num_radii, res_beta, res_alpha]
    position_probs.grid_values = position_probs.grid_values.sum(axis=0)
    return position_probs


def compute_grid_of_joint_distribution(
    radial_weights: jnp.ndarray,
    log_angular_coeffs: e3nn.IrrepsArray,
    res_beta: int,
    res_alpha: int,
    quadrature: str,
) -> e3nn.SphericalSignal:
    """Combines radial weights and angular coefficients to get a distribution on the spheres."""
    # Convert coefficients to a distribution on the sphere.
    log_angular_dist = e3nn.to_s2grid(
        log_angular_coeffs,
        res_beta,
        res_alpha,
        quadrature=quadrature,
        p_val=1,
        p_arg=-1,
    )

    # Subtract the maximum value for numerical stability.
    log_angular_dist_max = jnp.max(
        log_angular_dist.grid_values, axis=(-2, -1), keepdims=True
    )
    log_angular_dist_max = jax.lax.stop_gradient(log_angular_dist_max)
    log_angular_dist = log_angular_dist.apply(lambda x: x - log_angular_dist_max)

    # Convert to a probability distribution, by taking the exponential and normalizing.
    angular_dist = log_angular_dist.apply(jnp.exp)
    angular_dist = angular_dist / angular_dist.integrate()

    # Check that shapes are correct.
    num_radii = radial_weights.shape[0]
    assert angular_dist.shape == (
        res_beta,
        res_alpha,
    )

    # Mix in the radius weights to get a distribution over all spheres.
    dist = radial_weights * angular_dist[None, :, :]
    assert dist.shape == (num_radii, res_beta, res_alpha)
    return dist


def compute_coefficients_of_logits_of_joint_distribution(
    radial_logits: jnp.ndarray,
    log_angular_coeffs: e3nn.IrrepsArray,
) -> e3nn.IrrepsArray:
    """Combines radial weights and angular coefficients to get a distribution on the spheres."""
    radial_logits = e3nn.IrrepsArray("0e", radial_logits[:, None])
    log_dist_coeffs = jax.vmap(
        lambda log_radial_weight: e3nn.concatenate(
            [log_radial_weight, log_angular_coeffs]
        )
    )(radial_logits)
    log_dist_coeffs = e3nn.sum(log_dist_coeffs.regroup(), axis=-1)

    num_radii = radial_logits.shape[0]
    assert log_dist_coeffs.shape == (num_radii, log_dist_coeffs.irreps.dim)

    return log_dist_coeffs


def get_activation(activation: str) -> Callable[[jnp.ndarray], jnp.ndarray]:
    """Get the activation function."""
    if activation == "shifted_softplus":
        return e3schnet.shifted_softplus
    return getattr(jax.nn, activation)


def _irreps_from_lmax(
    lmax: int, num_channels: int, use_pseudoscalars_and_pseudovectors: bool
) -> e3nn.Irreps:
    """Convenience function to create irreps from lmax."""
    irreps = e3nn.s2_irreps(lmax)
    if use_pseudoscalars_and_pseudovectors:
        irreps += e3nn.Irreps("0o + 1e")
    return (num_channels * irreps).regroup()


def get_num_species_for_dataset(dataset: str) -> int:
    """Returns the number of species for a given dataset."""
    if dataset == "qm9":
        return len(ATOMIC_NUMBERS)
    if dataset in ["tetris", "platonic_solids"]:
        return 1
    raise ValueError(f"Unsupported dataset: {dataset}.")


def create_node_embedder(
    config: ml_collections.ConfigDict,
    num_species: int,
) -> hk.Module:
    if config.model == "MACE":
        output_irreps = _irreps_from_lmax(
            config.max_ell,
            config.num_channels,
            config.use_pseudoscalars_and_pseudovectors,
        )
        return mace.MACE(
            output_irreps=output_irreps,
            hidden_irreps=output_irreps,
            readout_mlp_irreps=output_irreps,
            r_max=config.r_max,
            num_interactions=config.num_interactions,
            avg_num_neighbors=config.avg_num_neighbors,
            num_species=num_species,
            max_ell=config.max_ell,
            num_basis_fns=config.num_basis_fns,
            soft_normalization=config.get("soft_normalization"),
        )

    if config.model == "NequIP":
        output_irreps = _irreps_from_lmax(
            config.max_ell,
            config.num_channels,
            config.use_pseudoscalars_and_pseudovectors,
        )
        return nequip.NequIP(
            num_species=num_species,
            r_max=config.r_max,
            avg_num_neighbors=config.avg_num_neighbors,
            max_ell=config.max_ell,
            init_embedding_dims=config.num_channels,
            output_irreps=output_irreps,
            num_interactions=config.num_interactions,
            even_activation=get_activation(config.even_activation),
            odd_activation=get_activation(config.odd_activation),
            mlp_activation=get_activation(config.mlp_activation),
            mlp_n_hidden=config.num_channels,
            mlp_n_layers=config.mlp_n_layers,
            n_radial_basis=config.num_basis_fns,
            skip_connection=config.skip_connection,
        )

    if config.model == "MarioNette":
        output_irreps = _irreps_from_lmax(
            config.max_ell,
            config.num_channels,
            config.use_pseudoscalars_and_pseudovectors,
        )
        return marionette.MarioNette(
            num_species=num_species,
            r_max=config.r_max,
            avg_num_neighbors=config.avg_num_neighbors,
            init_embedding_dims=config.num_channels,
            output_irreps=output_irreps,
            soft_normalization=config.soft_normalization,
            num_interactions=config.num_interactions,
            even_activation=get_activation(config.even_activation),
            odd_activation=get_activation(config.odd_activation),
            mlp_activation=get_activation(config.activation),
            mlp_n_hidden=config.num_channels,
            mlp_n_layers=config.mlp_n_layers,
            n_radial_basis=config.num_basis_fns,
            use_bessel=config.use_bessel,
            alpha=config.alpha,
            alphal=config.alphal,
        )

    if config.model == "E3SchNet":
        return e3schnet.E3SchNet(
            init_embedding_dim=config.num_channels,
            num_interactions=config.num_interactions,
            num_filters=config.num_filters,
            num_radial_basis_functions=config.num_radial_basis_functions,
            activation=get_activation(config.activation),
            cutoff=config.cutoff,
            max_ell=config.max_ell,
            num_species=num_species,
        )

    if config.model == "Allegro":
        output_irreps = _irreps_from_lmax(
            config.max_ell,
            config.num_channels,
            config.use_pseudoscalars_and_pseudovectors,
        )
        return allegro.Allegro(
            num_species=num_species,
            r_max=config.r_max,
            avg_num_neighbors=config.avg_num_neighbors,
            max_ell=config.max_ell,
            output_irreps=output_irreps,
            num_interactions=config.num_interactions,
            mlp_activation=get_activation(config.mlp_activation),
            mlp_n_hidden=config.num_channels,
            mlp_n_layers=config.mlp_n_layers,
            n_radial_basis=config.num_basis_fns,
        )

    raise ValueError(f"Unsupported model: {config.model}.")


def create_position_updater(
    config: ml_collections.ConfigDict,
) -> hk.Transformed:
    """Create a position updater as specified by the config."""
    dataset = config.get("dataset", "qm9")
    num_species = get_num_species_for_dataset(dataset)

    def model_fn(graphs: datatypes.Fragments):
        return PositionUpdater(
            node_embedder_fn=lambda: create_node_embedder(
                config.position_updater.embedder_config,
                num_species,
            )
        )(graphs)

    return hk.transform(model_fn)


def create_model(
    config: ml_collections.ConfigDict, run_in_evaluation_mode: bool
) -> hk.Transformed:
    """Create a model as specified by the config."""

    if config.get("position_updater"):
        return create_position_updater(config)

    def model_fn(
        graphs: datatypes.Fragments,
        focus_and_atom_type_inverse_temperature: float = 1.0,
        position_inverse_temperature: float = 1.0,
    ) -> datatypes.Predictions:
        """Defines the entire network."""

        dataset = config.get("dataset", "qm9")
        num_species = get_num_species_for_dataset(dataset)

        if config.focus_and_target_species_predictor.compute_global_embedding:
            global_embedder_fn = lambda: GlobalEmbedder(
                num_channels=config.focus_and_target_species_predictor.global_embedder.num_channels,
                pooling=config.focus_and_target_species_predictor.global_embedder.pooling,
                num_attention_heads=config.focus_and_target_species_predictor.global_embedder.num_attention_heads,
            )
        else:
            global_embedder_fn = lambda: None

        focus_and_target_species_predictor = FocusAndTargetSpeciesPredictor(
            node_embedder_fn=lambda: create_node_embedder(
                config.focus_and_target_species_predictor.embedder_config,
                num_species,
            ),
            global_embedder_fn=global_embedder_fn,
            latent_size=config.focus_and_target_species_predictor.latent_size,
            num_layers=config.focus_and_target_species_predictor.num_layers,
            activation=get_activation(
                config.focus_and_target_species_predictor.activation
            ),
            num_species=num_species,
        )
        if config.target_position_predictor.get("factorized"):
            target_position_predictor = FactorizedTargetPositionPredictor(
                node_embedder_fn=lambda: create_node_embedder(
                    config.target_position_predictor.embedder_config,
                    num_species,
                ),
                position_coeffs_lmax=config.target_position_predictor.embedder_config.max_ell,
                res_beta=config.target_position_predictor.res_beta,
                res_alpha=config.target_position_predictor.res_alpha,
                num_channels=config.target_position_predictor.num_channels,
                num_species=num_species,
                min_radius=config.target_position_predictor.min_radius,
                max_radius=config.target_position_predictor.max_radius,
                num_radii=config.target_position_predictor.num_radii,
                radial_mlp_latent_size=config.target_position_predictor.radial_mlp_latent_size,
                radial_mlp_num_layers=config.target_position_predictor.radial_mlp_num_layers,
                radial_mlp_activation=get_activation(
                    config.target_position_predictor.radial_mlp_activation
                ),
                apply_gate=config.target_position_predictor.get("apply_gate"),
            )
        else:
            target_position_predictor = TargetPositionPredictor(
                node_embedder_fn=lambda: create_node_embedder(
                    config.target_position_predictor.embedder_config,
                    num_species,
                ),
                position_coeffs_lmax=config.target_position_predictor.embedder_config.max_ell,
                res_beta=config.target_position_predictor.res_beta,
                res_alpha=config.target_position_predictor.res_alpha,
                num_channels=config.target_position_predictor.num_channels,
                num_species=num_species,
                min_radius=config.target_position_predictor.min_radius,
                max_radius=config.target_position_predictor.max_radius,
                num_radii=config.target_position_predictor.num_radii,
                apply_gate=config.target_position_predictor.get("apply_gate"),
            )

        predictor = Predictor(
            focus_and_target_species_predictor=focus_and_target_species_predictor,
            target_position_predictor=target_position_predictor,
        )

        if run_in_evaluation_mode:
            return predictor.get_evaluation_predictions(
                graphs,
                focus_and_atom_type_inverse_temperature,
                position_inverse_temperature,
            )
        else:
            return predictor.get_training_predictions(graphs)

    return hk.transform(model_fn)
