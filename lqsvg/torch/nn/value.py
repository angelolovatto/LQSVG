"""Quadratic value functions as NN modules."""
from __future__ import annotations

import torch
from nnrl.nn.critic import QValue, VValue
from torch import IntTensor, Tensor, nn

import lqsvg.torch.named as nt
from lqsvg.envs.lqr import Linear, LinSDynamics, QuadCost, Quadratic
from lqsvg.envs.lqr.solvers import NamedLQGPrediction
from lqsvg.envs.lqr.utils import dims_from_policy, unpack_obs
from lqsvg.np_util import RNG
from lqsvg.torch.random import spd_matrix, unit_vector

__all__ = ["QuadraticMixin", "QuadQValue", "QuadVValue", "ZeroQValue"]


def index_quadratic_parameters(
    quad: nn.Parameter,
    linear: nn.Parameter,
    const: nn.Parameter,
    index: IntTensor,
    max_idx: int,
) -> tuple[Tensor, Tensor, Tensor]:
    # pylint:disable=missing-function-docstring
    quad, linear, const = nt.horizon(
        nt.matrix(quad), nt.vector(linear), nt.scalar(const)
    )

    index = torch.clamp(index, max=max_idx)
    quad, linear, const = map(
        lambda x: nt.index_by(x, dim="H", index=index), (quad, linear, const)
    )
    return quad, linear, const


class QuadraticMixin:
    """Adds update and standard form to a quadratic NN module."""

    quad: nn.Parameter
    linear: nn.Parameter
    const: nn.Parameter

    def standard_form(self) -> Quadratic:
        """Return parameters in standard quadratic form.

        Returns:
            Tuple with matrix, vector, and scalar parameters, including
            their gradients (cloned)
        """
        params = (self.quad, self.linear, self.const)
        refines = (nt.matrix, nt.vector, nt.scalar)
        quadratic = nt.horizon(*(r(p) for r, p in zip(refines, params)))
        for tensor, param in zip(quadratic, params):
            tensor.grad = None if param.grad is None else param.grad.clone()
        return Quadratic(*quadratic)

    def copy_(self, quadratic: Quadratic) -> QuadraticMixin:
        """Update parameters to existing quadratic."""
        params = (self.quad, self.linear, self.const)
        for param, tensor in zip(params, quadratic):
            param.data.copy_(tensor.data)
        return self

    @staticmethod
    def predict_value(
        policy: Linear, dynamics: LinSDynamics, cost: QuadCost
    ) -> tuple[Quadratic, Quadratic]:
        """Compute the true value functions' parameters for a policy.

        Args:
            policy: the affine feedback policy's parameters
            dynamics: linear stochastic dynamics parameters
            cost: quadratic cost function parameters

        Returns:
            A tuple with parameters for the state and action-value functions
            respectively.
        """
        n_state, n_ctrl, horizon = dims_from_policy(policy)
        prediction = NamedLQGPrediction(n_state, n_ctrl, horizon)
        return prediction(policy, dynamics, cost)


class QuadVValue(VValue, QuadraticMixin):
    """Quadratic time-varying state-value function.

    Can clone the tensors from a quadratic and set them as parameters,
    avoiding in-place modification of the original quadratic's tensors.
    """

    n_state: int
    horizon: int

    def __init__(self, n_state: int, horizon: int, rng: RNG = None):
        super().__init__()
        self.n_state = n_state
        self.horizon = horizon

        self.quad = nn.Parameter(Tensor(horizon + 1, n_state, n_state))
        self.linear = nn.Parameter(Tensor(horizon + 1, n_state))
        self.const = nn.Parameter(Tensor(horizon + 1))
        self.reset_parameters(rng)

    def reset_parameters(self, rng: RNG = None):
        """Standard parameter initialization."""
        n_state, horizon = self.n_state, self.horizon
        self.quad.data.copy_(spd_matrix(size=n_state, horizon=horizon + 1, rng=rng))
        self.linear.data.copy_(unit_vector(size=n_state, horizon=horizon + 1, rng=rng))
        nn.init.uniform_(self.const, -1, 1)

    def match_policy_(
        self, policy: Linear, dynamics: LinSDynamics, cost: QuadCost
    ) -> QuadVValue:
        """Copy a policy's true quadratic state-value function.

        Note:
            The module will predict the same values as the true
            value function, however its gradient w.r.t. the policy's parameters
            is zero, unlike the real value function. Gradients of the predicted
            value w.r.t. to state inputs are still defined.
        """
        _, vvalue = self.predict_value(policy, dynamics, cost)
        return self.copy_(vvalue)

    @classmethod
    def from_existing(cls, quadratic: Quadratic) -> QuadVValue:
        """Create a quadratic state-value function from existing parameters."""
        quad, _, _ = quadratic
        # noinspection PyArgumentList
        n_state = quad.size("C")
        # noinspection PyArgumentList
        horizon = quad.size("H") - 1
        return cls(n_state, horizon).copy_(quadratic)

    @classmethod
    def from_policy(
        cls, policy: Linear, dynamics: LinSDynamics, cost: QuadCost
    ) -> QuadVValue:
        """Create a policy's true quadratic state-value function.

        Note:
            The resulting module will predict the same values as the true
            value function, however its gradient w.r.t. the policy's parameters
            is zero, unlike the real value function. Gradients of the predicted
            value w.r.t. to state and action inputs are still defined.
        """
        _, vvalue = cls.predict_value(policy, dynamics, cost)
        return cls.from_existing(vvalue)

    def forward(self, obs: Tensor) -> Tensor:
        state, time = unpack_obs(obs)
        time = nt.vector_to_scalar(time)
        quad, linear, const = index_quadratic_parameters(
            self.quad, self.linear, self.const, time, max_idx=self.horizon
        )
        state = nt.vector_to_matrix(state)
        cost = nt.matrix_to_scalar(
            nt.transpose(state) @ quad @ state / 2
            + nt.transpose(nt.vector_to_matrix(linear)) @ state
            + nt.scalar_to_matrix(const)
        )
        return cost.neg()


class QuadQValue(QValue, QuadraticMixin):
    """Quadratic time-varying action-value function.

    Clones the tensors from a quadratic and sets them as parameters, avoiding
    in-place modification of the original quadratic's tensors.
    """

    n_tau: int
    horizon: int

    def __init__(self, n_tau: int, horizon: int, rng: RNG = None):
        super().__init__()
        self.n_tau = n_tau
        self.horizon = horizon

        self.quad = nn.Parameter(Tensor(horizon, n_tau, n_tau))
        self.linear = nn.Parameter(Tensor(horizon, n_tau))
        self.const = nn.Parameter(Tensor(horizon))
        self.reset_parameters(rng)

    def reset_parameters(self, rng: RNG = None):
        """Standard parameter initialization."""
        n_tau, horizon = self.n_tau, self.horizon
        self.quad.data.copy_(spd_matrix(size=n_tau, horizon=horizon, rng=rng))
        self.linear.data.copy_(unit_vector(size=n_tau, horizon=horizon, rng=rng))
        nn.init.uniform_(self.const, -1, 1)

    @classmethod
    def from_existing(cls, quadratic: Quadratic):
        """Create a quadratic action-value function from existing parameters."""
        quad, _, _ = quadratic
        # noinspection PyArgumentList
        n_tau = quad.size("C")
        # noinspection PyArgumentList
        horizon = quad.size("H")
        new = cls(n_tau, horizon)
        new.copy_(quadratic)
        return new

    @classmethod
    def from_policy(
        cls, policy: Linear, dynamics: LinSDynamics, cost: QuadCost
    ) -> QuadQValue:
        """Create a policy's true quadratic action-value function.

        Note:
            The resulting module will predict the same values as the true
            value function, however its gradient w.r.t. the policy's parameters
            is zero, unlike the real value function. Gradients of the predicted
            value w.r.t. to state and action inputs are still defined.
        """
        qvalue, _ = cls.predict_value(policy, dynamics, cost)
        return cls.from_existing(qvalue)

    def match_policy_(
        self, policy: Linear, dynamics: LinSDynamics, cost: QuadCost
    ) -> QuadQValue:
        """Copy a policy's true quadratic action-value function.

        Note:
            The module will predict the same values as the true
            value function, however its gradient w.r.t. the policy's parameters
            is zero, unlike the real value function. Gradients of the predicted
            value w.r.t. to state and action inputs are still defined.
        """
        qvalue, _ = self.predict_value(policy, dynamics, cost)
        return self.copy_(qvalue)

    def forward(self, obs: Tensor, action: Tensor) -> Tensor:
        state, time = unpack_obs(obs)
        time = nt.vector_to_scalar(time)
        # noinspection PyTypeChecker
        quad, linear, const = index_quadratic_parameters(
            self.quad, self.linear, self.const, time, max_idx=self.horizon - 1
        )
        vec = nt.vector_to_matrix(torch.cat([state, action], dim="R"))
        cost = nt.matrix_to_scalar(
            nt.transpose(vec) @ quad @ vec / 2
            + nt.transpose(nt.vector_to_matrix(linear)) @ vec
            + nt.scalar_to_matrix(const)
        )
        val = cost.neg()
        return nt.where(time.eq(self.horizon), torch.zeros_like(val), val)


class ZeroQValue(QValue):
    """Predicts action-value as zero for all inputs.

    Useful for factoring out the value function in bootstrapped estimators.
    """

    def forward(self, obs: Tensor, action: Tensor) -> Tensor:
        # noinspection PyTypeChecker
        scalar_batch, _ = torch.broadcast_tensors(
            obs.select("R", 0), action.select("R", 0)
        )
        return torch.zeros_like(scalar_batch)
