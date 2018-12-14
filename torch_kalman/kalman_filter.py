from typing import Union, Iterable, Optional, TypeVar

from tqdm import tqdm

import torch
from numpy import ndarray
from torch import Tensor
from torch.nn import ParameterList

from torch_kalman.design import Design
from torch_kalman.process import Process
from torch_kalman.state_belief import StateBelief, Gaussian
from torch_kalman.state_belief.over_time import StateBeliefOverTime
from torch_kalman.utils import identity


class KalmanFilter(torch.nn.Module):
    def __init__(self,
                 processes: Iterable[Process],
                 measures: Iterable[str],
                 device: Optional[torch.device] = None):
        super().__init__()
        self.design = Design(processes=processes, measures=measures, device=device)

        # parameters from design:
        self.design_parameters = ParameterList()
        for param in self.design.parameters():
            self.design_parameters.append(param)

        # the distributional family, implemented by property (default gaussian)
        self._family = None

        self.to(device=self.design.device)

    @property
    def family(self) -> TypeVar('Gaussian'):
        if self._family is None:
            self._family = Gaussian
        return self._family

    @property
    def state_size(self) -> int:
        return self.design.state_size

    @property
    def measure_size(self) -> int:
        return self.design.measure_size

    # noinspection PyShadowingBuiltins
    def forward(self,
                input: Tensor,
                initial_state: Optional[StateBelief] = None,
                **kwargs) -> StateBeliefOverTime:
        """
        :param input: The multivariate time-series to be fit by the kalman-filter. A Tensor where the first dimension
        represents the groups, the second dimension represents the time-points, and the third dimension represents the
        measures.
        :param initial_state: If a StateBelief, this is used as the prediction for time=0; if None then each process
        generates initial values.
        :param kwargs: Other kwargs that will be passed to the `design_for_batch` method.
        :return: A StateBeliefOverTime consisting of one-step-ahead predictions.
        """

        kwargs = kwargs.copy()
        kwargs['input'] = input

        num_groups, num_timesteps, num_measures = input.shape
        if num_measures != self.measure_size:
            raise ValueError(f"This KalmanFilter has {self.measure_size} measurement-dimensions; but the input shape is "
                             f"{(num_groups, num_timesteps, num_measures)} (last dim should == measure-size).")

        design_for_batch = self.design.for_batch(num_groups=num_groups, num_timesteps=num_timesteps, **kwargs)

        # initial state of the system:
        if initial_state is None:
            state_prediction = self.family(*design_for_batch.get_block_diag_initial_state(num_groups=num_groups))
        else:
            state_prediction = initial_state

        prog = kwargs.get('progress', identity) or identity
        if prog is True:
            prog = tqdm
        iterator = prog(range(num_timesteps))

        # generate one-step-ahead predictions:
        state_predictions = []
        for t in iterator:
            if t > 0:
                # take state-prediction of previous t (now t-1), correct it according to what was actually measured at at t-1
                state_belief = state_prediction.update(obs=input[:, t - 1, :])

                # predict the state for t, from information from t-1
                # F at t-1 is transition *from* t-1 *to* t
                state_prediction = state_belief.predict(F=design_for_batch.F[t - 1], Q=design_for_batch.Q[t - 1])

            # compute how state-prediction at t translates into measurement-prediction at t
            state_prediction.compute_measurement(H=design_for_batch.H[t], R=design_for_batch.R[t])

            # append to output:
            state_predictions.append(state_prediction)

        return self.family.concatenate_over_time(state_beliefs=state_predictions,
                                                 design=self.design,
                                                 start_datetimes=kwargs.get('start_datetimes', None))

    def smooth(self, states: StateBeliefOverTime):
        raise NotImplementedError

    def simulate(self,
                 states: StateBeliefOverTime,
                 horizon: int,
                 from_datetimes: Optional[ndarray] = None,
                 **kwargs) -> Tensor:

        assert horizon > 0

        kwargs = kwargs.copy()

        # forecast-from time:
        if from_datetimes is None:
            initial_state = states.last_prediction
        else:
            initial_state = states.slice_by_dt(datetimes=from_datetimes)
            kwargs['start_datetimes'] = from_datetimes

        design_for_batch = self.design.for_batch(num_groups=initial_state.batch_size, num_timesteps=horizon, **kwargs)

        return initial_state.simulate(design_for_batch=design_for_batch, **kwargs)

    def forecast(self,
                 states: StateBeliefOverTime,
                 horizon: int,
                 forecast_from_datetimes: Optional[ndarray] = None,
                 **kwargs) -> StateBeliefOverTime:

        assert horizon > 0

        kwargs = kwargs.copy()

        # forecast-from time:
        if forecast_from_datetimes is None:
            state_prediction = states.last_prediction
        else:
            state_prediction = states.slice_by_dt(datetimes=forecast_from_datetimes)
            kwargs['start_datetimes'] = forecast_from_datetimes

        design_for_batch = self.design.for_batch(num_groups=state_prediction.batch_size, num_timesteps=horizon, **kwargs)

        prog = kwargs.get('progress', identity) or identity
        if prog is True:
            prog = tqdm
        iterator = prog(range(horizon))

        forecasts = []
        for t in iterator:
            if t > 0:
                # predict the state for t, from information from t-1
                # F at t-1 is transition *from* t-1 *to* t
                state_prediction = state_prediction.predict(F=design_for_batch.F[t - 1], Q=design_for_batch.Q[t - 1])

            # compute how state-prediction at t translates into measurement-prediction at t
            state_prediction.compute_measurement(H=design_for_batch.H[t], R=design_for_batch.R[t])

            # append to output:
            forecasts.append(state_prediction)

        return self.family.concatenate_over_time(state_beliefs=forecasts,
                                                 design=self.design,
                                                 start_datetimes=forecast_from_datetimes)
