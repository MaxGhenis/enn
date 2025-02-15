# python3
# pylint: disable=g-bad-file-header
# Copyright 2021 DeepMind Technologies Limited. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or  implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================

"""Prototype for linear hypermodel in JAX."""

from typing import Callable, Optional, Sequence, Type

import chex
from enn import base
from enn import utils
from enn.networks import priors
import haiku as hk
import jax
import jax.numpy as jnp
import numpy as np


# TODO(author2): Current implementation will produce a hypermodel that can
# *only* work for a single index at a time. However, note that jax.vmap means
# this can easily be converted into a form that works with batched index.


class MLPHypermodel(base.EpistemicNetwork):
  """MLP hypermodel for transformed_base as EpistemicNetwork."""

  def __init__(self,
               transformed_base: hk.Transformed,
               dummy_input: base.Array,
               indexer: base.EpistemicIndexer,
               hidden_sizes: Optional[Sequence[int]] = None,
               return_generated_params: bool = False,
               scale: bool = True,
               w_init: Optional[hk.initializers.Initializer] = None,
               b_init: Optional[hk.initializers.Initializer] = None,
               ):
    """MLP hypermodel for transformed_base as EpistemicNetwork."""

    if hidden_sizes is None:
      hyper_torso = lambda x: x
    else:
      def hyper_torso(index):
        return hk.nets.MLP(hidden_sizes, w_init=w_init, b_init=b_init)(index)

    enn = utils.epistemic_network_from_module(
        enn_ctor=hypermodel_module(
            transformed_base,
            dummy_input,
            hyper_torso,
            return_generated_params=return_generated_params,
            scale=scale),
        indexer=indexer,
    )
    super().__init__(enn.apply, enn.init, enn.indexer)


# pytype: disable=bad-return-type
def hypermodel_module(
    transformed_base: hk.Transformed,
    dummy_input: base.Array,
    hyper_torso: Callable[[base.Index], base.Array] = lambda x: x,
    diagonal_linear_hyper: bool = False,
    return_generated_params: bool = False,
    scale: bool = True,
) -> Type[base.EpistemicModule]:
  """Generates an haiku module for a hypermodel of a transformed base network.

  A hypermodel uses the index z to predict parameters for the base model defined
  by transformed_base. See paper https://arxiv.org/abs/2006.07464.

  For each layer of the base model, we would like the variance of inputs and
  outputs to be equal (this can make SGD work better). If we were initializing
  the weights of the base model, we could achieve this by "scaling" the weights
  of each layer by 1/sqrt(n_i) where n_i is the fan in to the i-th layer of the
  base model. Now, since the weights of the base model are generated by the
  hypermodel's output, we can manually scale the generated weights. Note that
  this scaling is needed only for the weight parameters and not for bias
  parameters. Function `scale_fn` appropriately scales the weights generated by
  the hypermodel.

  Args:
    transformed_base: hk.transformed_without_rng of base model y = f_theta(x).
    dummy_input: example input x, needed to determine weight shapes.
    hyper_torso: transformation of the index before the final layer. Defaults
      to identity and a resultant linear hypermodel.
    diagonal_linear_hyper: a boolean specifying whether the final layer to apply
      to the transformed index is diagonal linear or linear.
    return_generated_params: returns generated params in addition to output.
    scale: a boolean specifying whether to scale the params or not.

  Returns:
    Hypermodel of the "base model" as ctor for EpistemicModule. Should be used
    with only *one* epistemic index at a time (can vmap for batch).
  """
  base_params = transformed_base.init(jax.random.PRNGKey(0), dummy_input)
  base_params_flat = jax.tree_map(jnp.ravel, base_params)
  base_shapes = jax.tree_map(lambda x: jnp.array(jnp.shape(x)), base_params)
  base_shapes_flat = jax.tree_map(len, base_params_flat)

  def scale_fn(module_name, name, value):
    """Scales weight by 1/sqrt(fan_in) and leaves biases unchanged.

    Args:
      module_name: (typically) layer name. Not used but is needed for hk.map.
      name: parameter name.
      value: value of the parameters.

    Returns:
      scaled parameters suitable for use in the apply function of base network.
    """
    del module_name
    # The parameter name can be either 'w' (if the parameter is a weight)
    # or 'b' (if the parameter is a bias)
    return value / jnp.sqrt(value.shape[0]) if name == 'w' else value

  def hyper_fn(inputs: base.Array, index: base.Index) -> base.Array:

    if diagonal_linear_hyper:
      # index must be the same size as the total number of base params.
      chex.assert_shape(index, (np.sum(jax.tree_leaves(base_shapes_flat)),))

      hyper_index = DiagonalLinear()(index)
      flat_output = jnp.split(
          hyper_index, np.cumsum(jax.tree_leaves(base_shapes_flat))[:-1])
      flat_output = jax.tree_unflatten(jax.tree_structure(base_shapes),
                                       flat_output)
    else:
      # Apply the hyper_torso to the epistemic index
      hyper_index = hyper_torso(index)

      # Generate a linear layer of size "base_shapes_flat"
      final_layers = jax.tree_map(hk.Linear, base_shapes_flat)

      # Apply this linear output to the output of the hyper_torso
      flat_output = jax.tree_map(lambda layer: layer(hyper_index), final_layers)

    # Reshape this flattened output to the original base shapes (unflatten)
    generated_params = jax.tree_multimap(jnp.reshape, flat_output, base_shapes)

    if scale:
      # Scale the generated params such that expected variance of the raw
      # generated params is O(1) for both bias and weight parameters.
      generated_params_scaled = hk.data_structures.map(scale_fn,
                                                       generated_params)
    else:
      generated_params_scaled = generated_params

    # Output the original base function(inputs) with these generated params
    out = transformed_base.apply(generated_params_scaled, inputs)
    if return_generated_params:
      out = base.OutputWithPrior(
          train=transformed_base.apply(generated_params_scaled, inputs),
          extra={
              'hyper_net_out': generated_params,
              'base_net_params': generated_params_scaled
          })
    return out

  enn_module = hk.to_module(hyper_fn)
  return enn_module
# pytype: enable=bad-return-type


class MLPHypermodelWithHypermodelPrior(base.EpistemicNetwork):
  """MLP hypermodel with hypermodel prior as EpistemicNetwork."""

  def __init__(self,
               base_output_sizes: Sequence[int],
               prior_scale: float,
               dummy_input: base.Array,
               indexer: base.EpistemicIndexer,
               prior_base_output_sizes: Sequence[int],
               hyper_hidden_sizes: Optional[Sequence[int]] = None,
               prior_hyper_hidden_sizes: Optional[Sequence[int]] = None,
               w_init: Optional[hk.initializers.Initializer] = None,
               b_init: Optional[hk.initializers.Initializer] = None,
               return_generated_params: bool = False,
               seed: int = 0,
               scale: bool = True,
               ):
    """MLP hypermodel with hypermodel prior as EpistemicNetwork."""

    # Making the base model for the ENN without any prior function
    def base_net(x):
      return hk.nets.MLP(base_output_sizes, w_init=w_init, b_init=b_init)(x)
    transformed_base = hk.without_apply_rng(hk.transform(base_net))

    # Making the base model for the ENN of prior function
    def prior_net(x):
      return hk.nets.MLP(
          prior_base_output_sizes, w_init=w_init, b_init=b_init)(x)
    transformed_prior_base = hk.without_apply_rng(hk.transform(prior_net))

    # Defining an ENN for the prior function
    prior_enn = MLPHypermodel(
        transformed_base=transformed_prior_base,
        dummy_input=dummy_input,
        indexer=indexer,
        hidden_sizes=prior_hyper_hidden_sizes,
        return_generated_params=return_generated_params,
        w_init=w_init,
        b_init=b_init,
        scale=scale,
    )
    prior_fn = priors.convert_enn_to_prior_fn(
        prior_enn, dummy_input, jax.random.PRNGKey(seed))

    # Defining an ENN without any prior function
    enn_wo_prior = MLPHypermodel(
        transformed_base=transformed_base,
        dummy_input=dummy_input,
        indexer=indexer,
        hidden_sizes=hyper_hidden_sizes,
        return_generated_params=return_generated_params,
        w_init=w_init,
        b_init=b_init,
        scale=scale)

    # Defining the ENN with the prior `prior_fn`
    enn = priors.EnnWithAdditivePrior(
        enn_wo_prior, prior_fn, prior_scale=prior_scale)

    super().__init__(enn.apply, enn.init, enn.indexer)


################################################################################
# Alternative implementation of MLP hypermodel with MLP prior where layers
# are generated by different set of indices.


class HyperLinear(hk.Module):
  """Linear hypermodel."""

  def __init__(self,
               output_size: int,
               index_dim_per_layer: int,
               weight_scaling: float = 1.,
               bias_scaling: float = 1.,
               fixed_bias_val: float = 0.0,
               name: str = 'hyper_linear'):
    super().__init__(name=name)
    self._output_size = output_size
    self._index_dim_per_layer = index_dim_per_layer
    self._weight_scaling = weight_scaling
    self._bias_scaling = bias_scaling
    self._fixed_bias_val = fixed_bias_val

  def __call__(self, x: base.Array, z: base.Index) -> base.Array:
    unused_x_batch_size, hidden_size = x.shape
    init = hk.initializers.RandomNormal()
    w = hk.get_parameter(
        'w', [self._output_size, hidden_size, self._index_dim_per_layer],
        init=init)
    b = hk.get_parameter(
        'b', [self._output_size, self._index_dim_per_layer], init=init)

    w /= jnp.linalg.norm(w, axis=-1, keepdims=True)
    b /= jnp.linalg.norm(b, axis=-1, keepdims=True)

    w *= jnp.sqrt(self._weight_scaling / hidden_size)
    b = b * jnp.sqrt(self._bias_scaling) + self._fixed_bias_val

    weights = jnp.einsum('ohi,i->oh', w, z)
    bias = jnp.einsum('oi,i->o', b, z)

    return jnp.einsum('oh,bh->bo', weights, x) + bias


class PriorMLPIndependentLayers(hk.Module):
  """Prior MLP with each layer generated by an independent index."""

  def __init__(self,
               output_sizes: Sequence[int],
               index_dim: int,
               weight_scaling: float = 1.,
               bias_scaling: float = 1.,
               fixed_bias_val: float = 0.0,
               name: str = 'prior_independent_layers'):
    super().__init__(name=name)
    self._output_sizes = output_sizes
    self._num_layers = len(self._output_sizes)
    self._index_dim = index_dim
    self._weight_scaling = weight_scaling
    self._bias_scaling = bias_scaling
    self._fixed_bias_val = fixed_bias_val

    if self._index_dim < self._num_layers:
      # Assigning all index dimensions to all layers
      self._layers_indices = [jnp.arange(self._index_dim)] * self._num_layers

    else:
      # Spliting index dimension into num_layers chunks
      self._layers_indices = jnp.array_split(
          jnp.arange(self._index_dim), self._num_layers)

    # Defining layers of the prior MLP and associating each layer with a set of
    # indices
    self._layers = []
    for layer_indices, output_size in zip(self._layers_indices,
                                          self._output_sizes):
      index_dim_per_layer = len(layer_indices)
      layer = HyperLinear(output_size, index_dim_per_layer,
                          self._weight_scaling, self._bias_scaling,
                          self._fixed_bias_val)
      self._layers.append(layer)

  def __call__(self, x: base.Array, z: base.Index) -> base.Array:
    if self._index_dim < self._num_layers:
      # Assigning all index dimensions to all layers
      index_layers = [z] * self._num_layers
    else:
      # Spliting index dimension into num_layers chunks
      index_layers = jnp.array_split(z, self._num_layers)

    out = x
    for i, layer in enumerate(self._layers):
      index_layer = index_layers[i]
      out = layer(out, index_layer)
      if i < self._num_layers - 1:
        out = jax.nn.relu(out)
    return out


class MLPHypermodelPriorIndependentLayers(base.EpistemicNetwork):
  """MLP hypermodel with hypermodel prior as EpistemicNetwork."""

  def __init__(self,
               base_output_sizes: Sequence[int],
               prior_scale: float,
               dummy_input: base.Array,
               indexer: base.EpistemicIndexer,
               prior_base_output_sizes: Sequence[int],
               hyper_hidden_sizes: Optional[Sequence[int]] = None,
               w_init: Optional[hk.initializers.Initializer] = None,
               b_init: Optional[hk.initializers.Initializer] = None,
               return_generated_params: bool = False,
               prior_weight_scaling: float = 1.,
               prior_bias_scaling: float = 1.,
               prior_fixed_bias_val: float = 0.0,
               seed: int = 0,
               scale: bool = True,
               problem_temperature: Optional[float] = None
               ):
    """MLP hypermodel with hypermodel prior as EpistemicNetwork."""

    # Making the base model for the ENN without any prior function
    def base_net(x):
      net_out = hk.nets.MLP(base_output_sizes, w_init=w_init, b_init=b_init)(x)
      if problem_temperature:
        net_out /= problem_temperature
      return net_out

    transformed_base = hk.without_apply_rng(hk.transform(base_net))

    # Defining an ENN for the prior function based on an MLP with independent
    # layers which divides index dimension among the MLP layers. To this end, we
    # need to find the index dimension.
    rng = hk.PRNGSequence(seed)
    index = indexer(next(rng))
    index_dim, = index.shape
    def prior_net(x, z):
      net_out = PriorMLPIndependentLayers(
          output_sizes=prior_base_output_sizes,
          index_dim=index_dim,
          weight_scaling=prior_weight_scaling,
          bias_scaling=prior_bias_scaling,
          fixed_bias_val=prior_fixed_bias_val)(x, z)
      if problem_temperature:
        net_out /= problem_temperature
      return net_out

    prior_enn = hk.without_apply_rng(hk.transform(prior_net))

    # Initializing prior ENN to get `prior_fn(x, z)` which forwards prior ENN
    rng = hk.PRNGSequence(seed)
    prior_params = prior_enn.init(next(rng), dummy_input, index)
    def prior_fn(x, z):
      return prior_enn.apply(prior_params, x, z)

    # Defining an ENN without any prior function
    enn_wo_prior = MLPHypermodel(
        transformed_base=transformed_base,
        dummy_input=dummy_input,
        indexer=indexer,
        hidden_sizes=hyper_hidden_sizes,
        return_generated_params=return_generated_params,
        w_init=w_init,
        b_init=b_init,
        scale=scale)

    # Defining the ENN with the prior `prior_fn`
    enn = priors.EnnWithAdditivePrior(
        enn_wo_prior, prior_fn, prior_scale=prior_scale)

    super().__init__(enn.apply, enn.init, enn.indexer)


class DiagonalLinear(hk.Module):
  """Diagonal Linear module."""

  def __init__(
      self,
      with_bias: bool = True,
      w_init: Optional[hk.initializers.Initializer] = None,
      b_init: Optional[hk.initializers.Initializer] = None,
      name: Optional[str] = None,
  ):
    """Constructs the diagonal linear module.

    Args:
      with_bias: Whether to add a bias to the output.
      w_init: Optional initializer for weights. By default, uses random values
        from truncated normal, with stddev ``1 / sqrt(fan_in)``.
      b_init: Optional initializer for bias. By default, zero.
      name: Name of the module.
    """
    super().__init__(name=name)
    self.input_size = None
    self.with_bias = with_bias
    self.w_init = w_init
    self.b_init = b_init or jnp.zeros

  def __call__(self, inputs: jnp.ndarray) -> jnp.ndarray:
    """Computes a linear transform of the input."""
    if not inputs.shape:
      raise ValueError('Input must not be scalar.')

    self.input_size = inputs.shape[-1]
    dtype = inputs.dtype

    w_init = self.w_init
    if w_init is None:
      stddev = 1. / np.sqrt(self.input_size)
      w_init = hk.initializers.TruncatedNormal(stddev=stddev)
    w = hk.get_parameter('w', [self.input_size], dtype, init=w_init)

    out = inputs * jnp.log(1 + jnp.exp(w))

    if self.with_bias:
      b = hk.get_parameter('b', [self.input_size], dtype, init=self.b_init)
      out = out + b

    return out
