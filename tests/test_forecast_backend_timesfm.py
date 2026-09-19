"""Contract tests for the ``timesfm`` backend, driven by fake ``torch``/``timesfm`` modules.

The real extra (``torch`` + ``timesfm``, ~1 GB of wheels plus the weights) is not
installed in this repository and no test may download a checkpoint, so the backend
is exercised against minimal stub modules injected into :data:`sys.modules`.  That
pins everything this package owns -- the batching, the input-list copy that the
library's padding trap requires, the quantile-channel mapping, the device rules
and the lazy-import error surface -- without torch, without a network and without
a GPU.

The stubs deliberately reproduce the two measured traps of TimesFM 2.5:

* ``forecast()`` **appends padding rows to the caller's list**;
* the head returns ``(batch, horizon, 10)`` where channel 0 is the **mean** and
  channel 5 does not necessarily agree with the point forecast.

The real library is never imported: every line that could only run with the extra
installed is reached through the stub instead.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
import sys
import types
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.errors import ForecastError
from trading_platform.forecast.backends.timesfm import (
    BACKEND_NAME,
    DEFAULT_MODEL_ID,
    LICENSE_APACHE,
    LICENSE_NON_COMMERCIAL,
    NON_COMMERCIAL_MODEL_IDS,
    TIMESFM_EXTRA_HINT,
    TIMESFM_XREG_EXTRA_HINT,
    TimesFMBackend,
    _calendar_phase,
    _chunks,
    _CovariateProvider,
    _decile_rows,
    _is_timesfm3,
    build_backend,
    license_note,
)
from trading_platform.forecast.types import DEFAULT_QUANTILE_LEVELS, ForecastRequest

ORIGIN = pd.Timestamp("2024-01-01T00:00:00+00:00")
HORIZON = 4

#: ``global_batch_size`` of the 2.5 checkpoint: the size the library pads a batch to.
PADDING_ROWS = 32

#: The absurd value the stub puts in the *mean* channel of the head.
MEAN_CHANNEL_VALUE = 999.0

#: The absurd value the stub puts in the padding rows of a padded batch.
PADDING_VALUE = 1234.0

XREG_MODES = ("xreg + timesfm", "timesfm + xreg")


# ---------------------------------------------------------------------------
# stubs of the optional extra
# ---------------------------------------------------------------------------


class FakeForecastConfig:
    """Stand-in for ``timesfm.ForecastConfig`` recording its keyword options."""

    def __init__(self, **options: Any) -> None:
        self.options: dict[str, Any] = dict(options)
        for name, value in options.items():
            setattr(self, name, value)


@dataclass
class FakeForecastCall:
    """One recorded ``forecast`` invocation."""

    horizon: int
    lengths: list[int]
    dtypes: list[str]
    first_values: list[float]
    array_ids: list[int]
    list_id: int


@dataclass
class FakeCovariateCall:
    """One recorded ``forecast_with_covariates`` invocation."""

    input_lengths: list[int]
    covariates: dict[str, list[list[int]]]
    xreg_mode: str
    normalize: bool
    list_id: int


class FakeLoadedModel:
    """What ``from_pretrained`` returns: records ``compile`` and every call."""

    def __init__(self, module: FakeTimesFMModule, *, model_id: str, torch_compile: bool) -> None:
        self.module = module
        self.model_id = model_id
        self.torch_compile = torch_compile
        self.config: FakeForecastConfig | None = None
        self.compile_kwargs: dict[str, Any] = {}
        self.forecast_calls: list[FakeForecastCall] = []
        self.covariate_calls: list[FakeCovariateCall] = []
        self.model = types.SimpleNamespace(device="cpu")

    def compile(self, config: FakeForecastConfig, **kwargs: Any) -> None:
        """Record the ``ForecastConfig`` the backend compiled the model with."""
        self.config = config
        self.compile_kwargs = dict(kwargs)

    def _head(
        self, inputs: Sequence[np.ndarray], batch: int, horizon: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """Build a deterministic ``(point, quantiles)`` pair for ``batch`` series.

        The point forecast holds the last value of each series plus a fixed drift,
        so a series can be identified by its first value: that is what makes the
        request-order assertions meaningful.
        """
        steps = np.arange(1, horizon + 1, dtype="float64")
        point = np.empty((batch, horizon), dtype="float64")
        for index, series in enumerate(inputs[:batch]):
            point[index] = float(series[-1]) + 0.5 * steps + 0.01 * float(series[0])
        point32 = point.astype("float32")

        quantiles = np.empty((batch, horizon, 10), dtype="float32")
        quantiles[:, :, 0] = MEAN_CHANNEL_VALUE  # the mean channel is not a quantile
        for channel in range(1, 10):
            # Channel 5 is the median of the head, but deliberately *not* the point
            # forecast, so the tests can prove which of the two the backend uses.
            quantiles[:, :, channel] = point32 + (channel - 5.5) * 0.2

        if self.module.corrupt is not None:
            return self.module.corrupt(point32, quantiles)
        return point32, quantiles

    def _pad(self, inputs: list[np.ndarray], batch: int) -> None:
        """Reproduce the library's in-place padding of the caller's list."""
        if self.module.mutate_inputs:
            inputs.extend(np.zeros(3, dtype="float64") for _ in range(PADDING_ROWS - batch))

    def forecast(self, *, horizon: int, inputs: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
        """Record the call, mutate ``inputs`` like the real library, return the head."""
        batch = len(inputs)
        self.forecast_calls.append(
            FakeForecastCall(
                horizon=horizon,
                lengths=[len(series) for series in inputs],
                dtypes=[str(series.dtype) for series in inputs],
                first_values=[float(series[0]) for series in inputs],
                array_ids=[id(series) for series in inputs],
                list_id=id(inputs),
            )
        )
        self._pad(inputs, batch)
        return self._head(inputs, batch, horizon)

    def forecast_with_covariates(
        self,
        *,
        inputs: list[np.ndarray],
        dynamic_categorical_covariates: dict[str, Sequence[Sequence[int]]] | None = None,
        xreg_mode: str = "xreg + timesfm",
        normalize_xreg_target_per_input: bool = True,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Record the covariate call; the horizon is implied by the covariate length."""
        if self.module.xreg_error is not None:
            raise self.module.xreg_error
        batch = len(inputs)
        covariates = {
            name: [list(series) for series in values]
            for name, values in (dynamic_categorical_covariates or {}).items()
        }
        self.covariate_calls.append(
            FakeCovariateCall(
                input_lengths=[len(series) for series in inputs],
                covariates=covariates,
                xreg_mode=xreg_mode,
                normalize=normalize_xreg_target_per_input,
                list_id=id(inputs),
            )
        )
        horizon = self.module.xreg_horizon(covariates, inputs)
        self._pad(inputs, batch)
        return self._head(inputs, batch, horizon)


class FakeTimesFMModule(types.ModuleType):
    """Minimal stand-in for the ``timesfm`` package the backend imports lazily."""

    def __init__(
        self,
        *,
        mutate_inputs: bool = True,
        corrupt: Callable[[np.ndarray, np.ndarray], tuple[Any, Any]] | None = None,
        xreg_error: BaseException | None = None,
    ) -> None:
        super().__init__("timesfm")
        self.__spec__ = importlib.machinery.ModuleSpec("timesfm", loader=None)
        self.mutate_inputs = mutate_inputs
        self.corrupt = corrupt
        self.xreg_error = xreg_error
        self.models: list[FakeLoadedModel] = []
        self.ForecastConfig = FakeForecastConfig

        module = self

        class TimesFM_2p5_200M_torch:  # noqa: N801 - mirrors the library's class name
            """Stand-in for the 2.5 PyTorch wrapper class."""

            @classmethod
            def from_pretrained(
                cls, model_id: str, *, torch_compile: bool = False
            ) -> FakeLoadedModel:
                model = FakeLoadedModel(module, model_id=model_id, torch_compile=torch_compile)
                module.models.append(model)
                return model

        self.TimesFM_2p5_200M_torch = TimesFM_2p5_200M_torch

    @staticmethod
    def xreg_horizon(covariates: dict[str, list[list[int]]], inputs: Sequence[np.ndarray]) -> int:
        """Return the horizon the covariate lists imply (context plus horizon)."""
        series = next(iter(covariates.values()), None)
        if not series:
            return HORIZON
        return len(series[0]) - len(inputs[0])


class FakeCuda:
    """Stand-in for ``torch.cuda``."""

    def __init__(self, available: bool) -> None:
        self._available = available

    def is_available(self) -> bool:
        """Return the configured availability."""
        return self._available


class FakeTorch(types.ModuleType):
    """Minimal stand-in for the pieces of ``torch`` this backend touches."""

    def __init__(self, *, cuda_available: bool = False) -> None:
        super().__init__("torch")
        self.__spec__ = importlib.machinery.ModuleSpec("torch", loader=None)
        self.cuda = FakeCuda(cuda_available)
        self.matmul_precision: str | None = None

    def set_float32_matmul_precision(self, precision: str) -> None:
        """Record the matmul precision the backend asked for."""
        self.matmul_precision = precision


class MissingModuleBlocker:
    """Meta-path finder that makes the optional extras unimportable."""

    def __init__(self, names: Sequence[str]) -> None:
        self.names = tuple(names)

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        """Raise :class:`ModuleNotFoundError` for a blocked (sub)module."""
        if fullname in self.names or fullname.split(".")[0] in self.names:
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return


def install_fakes(
    monkeypatch: pytest.MonkeyPatch, *, cuda: bool = False, **timesfm_options: Any
) -> tuple[FakeTorch, FakeTimesFMModule]:
    """Inject fake ``torch`` and ``timesfm`` modules and return them."""
    torch_module = FakeTorch(cuda_available=cuda)
    timesfm_module = FakeTimesFMModule(**timesfm_options)
    monkeypatch.setitem(sys.modules, "torch", torch_module)
    monkeypatch.setitem(sys.modules, "timesfm", timesfm_module)
    return torch_module, timesfm_module


@pytest.fixture()
def without_extras(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make ``torch`` and ``timesfm`` unimportable for the duration of a test."""
    for name in ("torch", "timesfm"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(
        sys, "meta_path", [MissingModuleBlocker(("torch", "timesfm")), *sys.meta_path]
    )
    yield


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_request(values: Sequence[Any], origin: pd.Timestamp = ORIGIN) -> ForecastRequest:
    """Build a request whose context is a tuple, as the contract expects."""
    return ForecastRequest(origin=origin, context=tuple(values))


def make_requests(count: int, *, length: int = 8) -> list[ForecastRequest]:
    """Return ``count`` requests labelled by their own first context value (1, 2, ...)."""
    return [
        make_request([float(index + 1) + float(offset) for offset in range(length)])
        for index in range(count)
    ]


def expected_median(first_value: float, horizon: int = HORIZON) -> list[float]:
    """Median path the stub head implies, normalised to the origin close."""
    return [0.5 * (step + 1) + 0.01 * first_value for step in range(horizon)]


def expected_channel(first_value: float, channel: int, horizon: int = HORIZON) -> list[float]:
    """Value of quantile-head ``channel`` (1..9) after normalisation."""
    return [value + (channel - 5.5) * 0.2 for value in expected_median(first_value, horizon)]


# ---------------------------------------------------------------------------
# identity, factory and licences
# ---------------------------------------------------------------------------


def test_backend_identity_and_factory() -> None:
    backend = TimesFMBackend()
    assert backend.name == BACKEND_NAME == "timesfm"
    built = build_backend(max_horizon=64, timeframe="4h")
    assert isinstance(built, TimesFMBackend)
    assert built.max_horizon == 64
    assert built.timeframe == "4h"


def test_default_checkpoint_is_the_apache_one() -> None:
    backend = TimesFMBackend()
    assert DEFAULT_MODEL_ID == "google/timesfm-2.5-200m-pytorch"
    assert DEFAULT_MODEL_ID not in NON_COMMERCIAL_MODEL_IDS
    assert backend.model_id == DEFAULT_MODEL_ID
    assert backend.license_note() == LICENSE_APACHE == license_note(DEFAULT_MODEL_ID)


def test_non_commercial_checkpoint_is_opt_in_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    model_id = "google/timesfm-3.0-pytorch"
    assert model_id in NON_COMMERCIAL_MODEL_IDS
    with caplog.at_level(logging.WARNING):
        backend = TimesFMBackend(model_id=model_id)
    assert backend.model_id == model_id
    assert backend.license_note() == LICENSE_NON_COMMERCIAL
    assert license_note(model_id) == LICENSE_NON_COMMERCIAL
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert model_id in warnings[0].getMessage()
    assert "non-commercial" in warnings[0].getMessage().lower()


def test_apache_checkpoint_warns_about_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        TimesFMBackend()
    assert caplog.records == []


def test_unknown_checkpoint_defaults_to_the_apache_note() -> None:
    assert license_note("local/fine-tuned-timesfm") == LICENSE_APACHE


# ---------------------------------------------------------------------------
# availability and the lazy import
# ---------------------------------------------------------------------------


def test_is_available_is_false_without_the_extra(without_extras: None) -> None:
    assert TimesFMBackend().is_available() is False


def test_is_available_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_find_spec(name: str, package: str | None = None) -> Any:
        raise RuntimeError("broken environment")

    monkeypatch.setattr(importlib.util, "find_spec", broken_find_spec)
    assert TimesFMBackend().is_available() is False


def test_is_available_is_true_with_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch)
    assert TimesFMBackend().is_available() is True


def test_predict_without_the_extra_names_the_install_command(without_extras: None) -> None:
    with pytest.raises(ForecastError) as excinfo:
        TimesFMBackend().predict(make_requests(1), horizon=HORIZON)
    assert "pip install 'trading-platform[timesfm]'" in str(excinfo.value)
    assert TIMESFM_EXTRA_HINT in str(excinfo.value)


def test_empty_requests_never_import_anything(without_extras: None) -> None:
    assert TimesFMBackend().predict([], horizon=HORIZON) == []


def test_invalid_arguments_are_rejected_before_any_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _torch_module, timesfm_module = install_fakes(monkeypatch)
    backend = TimesFMBackend(max_horizon=8)
    with pytest.raises(ForecastError, match="max_horizon"):
        backend.predict(make_requests(1), horizon=9)
    with pytest.raises(ForecastError, match="quantile"):
        TimesFMBackend(quantile_levels=(0.15, 0.5)).predict(make_requests(1), horizon=2)
    with pytest.raises(ForecastError, match="finite"):
        backend.predict([make_request([1.0, float("nan")])], horizon=2)
    assert timesfm_module.models == []  # nothing was loaded, let alone compiled


# ---------------------------------------------------------------------------
# batching, ordering and the input-list mutation trap
# ---------------------------------------------------------------------------


def test_one_forecast_call_per_chunk_of_per_core_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _torch_module, timesfm_module = install_fakes(monkeypatch)
    backend = TimesFMBackend(per_core_batch_size=2)
    trajectories = backend.predict(make_requests(5, length=6), horizon=3)
    calls = timesfm_module.models[0].forecast_calls
    assert [call.lengths for call in calls] == [[6, 6], [6, 6], [6]]
    assert [call.horizon for call in calls] == [3, 3, 3]
    assert len(trajectories) == 5


def test_requests_are_grouped_by_context_length_and_reassembled_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _torch_module, timesfm_module = install_fakes(monkeypatch)
    requests = [
        make_request([1.0, 2.0, 3.0]),
        make_request([2.0, 3.0, 4.0, 5.0]),
        make_request([3.0, 4.0, 5.0]),
        make_request([4.0, 5.0, 6.0]),
    ]
    backend = TimesFMBackend(per_core_batch_size=8)
    trajectories = backend.predict(requests, horizon=2)

    assert [call.lengths for call in timesfm_module.models[0].forecast_calls] == [
        [3, 3, 3],
        [4],
    ]
    for request, trajectory in zip(requests, trajectories, strict=True):
        first_value = request.context[0]
        assert trajectory.median == pytest.approx(expected_median(first_value, 2), abs=1e-5)
        assert trajectory.origin == request.origin


def test_input_list_mutation_trap_is_neutralised(monkeypatch: pytest.MonkeyPatch) -> None:
    _torch_module, timesfm_module = install_fakes(monkeypatch)  # mutates the list it gets
    backend = TimesFMBackend(per_core_batch_size=4)
    requests = make_requests(3, length=5)

    first = backend.predict(requests, horizon=2)
    second = backend.predict(requests, horizon=2)

    calls = timesfm_module.models[0].forecast_calls
    assert len(calls) == 2
    assert calls[0].list_id != calls[1].list_id  # a fresh list per call, never reused
    assert calls[0].lengths == [5, 5, 5]
    assert calls[1].lengths == [5, 5, 5]  # the padding never came back
    assert calls[0].dtypes == ["float32"] * 3
    assert calls[0].first_values == [1.0, 2.0, 3.0]
    for trajectory in (*first, *second):
        assert trajectory.quantiles.shape == (len(DEFAULT_QUANTILE_LEVELS), 2)
    assert len(requests) == 3


def test_the_model_never_receives_a_caller_owned_array(monkeypatch: pytest.MonkeyPatch) -> None:
    _torch_module, timesfm_module = install_fakes(monkeypatch)
    caller_array = np.array([1.0, 2.0, 3.0, 4.0])
    request = ForecastRequest(origin=ORIGIN, context=caller_array)  # type: ignore[arg-type]
    TimesFMBackend().predict([request], horizon=2)
    call = timesfm_module.models[0].forecast_calls[0]
    assert id(caller_array) not in call.array_ids
    assert call.dtypes == ["float32"]
    assert call.first_values == [1.0]


# ---------------------------------------------------------------------------
# quantile channel mapping
# ---------------------------------------------------------------------------


def test_mean_channel_is_dropped_and_the_median_comes_from_the_point_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _torch_module, _timesfm_module = install_fakes(monkeypatch)
    backend = TimesFMBackend(quantile_levels=(0.1, 0.5, 0.9))
    trajectory = backend.predict([make_request([1.0, 2.0, 3.0, 4.0])], horizon=2)[0]

    assert trajectory.quantile_levels == (0.1, 0.5, 0.9)
    assert trajectory.quantiles.dtype == np.float32
    assert trajectory.quantiles.shape == (3, 2)
    # level 0.5 is the point forecast, *not* channel 5 of the head
    assert trajectory.median == pytest.approx(expected_median(1.0, 2), abs=1e-5)
    assert not np.allclose(trajectory.median, expected_channel(1.0, 5, 2), atol=1e-5)
    # level 0.1 is channel 1, level 0.9 is channel 9
    assert trajectory.quantile(0.1) == pytest.approx(expected_channel(1.0, 1, 2), abs=1e-5)
    assert trajectory.quantile(0.9) == pytest.approx(expected_channel(1.0, 9, 2), abs=1e-5)
    # the absurd mean channel never leaks into any row
    assert float(np.max(np.abs(trajectory.quantiles))) < 100.0


def test_default_levels_are_the_nine_deciles(monkeypatch: pytest.MonkeyPatch) -> None:
    _torch_module, _timesfm_module = install_fakes(monkeypatch)
    trajectory = TimesFMBackend().predict(make_requests(1), horizon=3)[0]
    assert trajectory.quantile_levels == DEFAULT_QUANTILE_LEVELS
    assert trajectory.quantiles.shape == (9, 3)
    for index, level in enumerate(DEFAULT_QUANTILE_LEVELS):
        expected = expected_median(1.0, 3) if level == 0.5 else expected_channel(1.0, index + 1, 3)
        assert trajectory.quantile(level) == pytest.approx(expected, abs=1e-5)


def test_padded_output_rows_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    def pad(point: np.ndarray, quantiles: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Mimic a library that returns the padded global batch size."""
        extra = PADDING_ROWS - point.shape[0]
        padding = np.full((extra, point.shape[1]), PADDING_VALUE, dtype="float32")
        padding_quantiles = np.full(
            (extra, quantiles.shape[1], quantiles.shape[2]), PADDING_VALUE, dtype="float32"
        )
        return (
            np.concatenate([point, padding], axis=0),
            np.concatenate([quantiles, padding_quantiles], axis=0),
        )

    _torch_module, _timesfm_module = install_fakes(monkeypatch, corrupt=pad)
    trajectories = TimesFMBackend().predict(make_requests(2), horizon=2)
    assert len(trajectories) == 2
    for index, trajectory in enumerate(trajectories):
        assert trajectory.median == pytest.approx(expected_median(index + 1, 2), abs=1e-5)


@pytest.mark.parametrize(
    ("corrupt", "fragment"),
    [
        (lambda point, quantiles: (point[0], quantiles), "dimension"),
        (lambda point, quantiles: (point, quantiles[:, :, :, None]), "dimension"),
        (lambda point, quantiles: (point[:, :-1], quantiles), "step"),
        (lambda point, quantiles: (point, quantiles[:, :, :8]), "shape"),
        (lambda point, quantiles: (point[:1], quantiles[:1]), "request"),
        (lambda point, quantiles: (np.array(["nope"]), quantiles), "non-numeric"),
    ],
)
def test_malformed_model_output_is_reported_as_a_forecast_error(
    monkeypatch: pytest.MonkeyPatch,
    corrupt: Callable[[np.ndarray, np.ndarray], tuple[Any, Any]],
    fragment: str,
) -> None:
    install_fakes(monkeypatch, corrupt=corrupt)
    with pytest.raises(ForecastError, match=fragment):
        TimesFMBackend().predict(make_requests(2), horizon=2)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("horizon", [0, -3, 2.5])
def test_invalid_horizon_is_rejected(monkeypatch: pytest.MonkeyPatch, horizon: Any) -> None:
    install_fakes(monkeypatch)
    with pytest.raises(ForecastError, match="horizon"):
        TimesFMBackend().predict(make_requests(1), horizon=horizon)


@pytest.mark.parametrize(
    "context",
    [(), (1.0,), (1.0, 2.0, float("nan")), (1.0, float("inf")), ("a", "b")],
)
def test_invalid_context_is_rejected(
    monkeypatch: pytest.MonkeyPatch, context: Sequence[Any]
) -> None:
    install_fakes(monkeypatch)
    with pytest.raises(ForecastError, match="context"):
        TimesFMBackend().predict([make_request(context)], horizon=2)


@pytest.mark.parametrize(
    "levels",
    [
        (),
        (0.05, 0.5),
        (0.5, 0.1),
        (0.5, 0.5),
        (0.1, 0.4, 0.9),
        (0.1, 0.9),
        (float("nan"), 0.5),
        ("a", 0.5),
    ],
)
def test_invalid_quantile_levels_are_rejected(
    monkeypatch: pytest.MonkeyPatch, levels: Sequence[Any]
) -> None:
    install_fakes(monkeypatch)
    with pytest.raises(ForecastError, match="quantile"):
        TimesFMBackend(quantile_levels=levels).predict(make_requests(1), horizon=2)


def test_quantile_levels_must_be_a_sequence() -> None:
    with pytest.raises(ForecastError, match="quantile_levels"):
        TimesFMBackend(quantile_levels=0.5)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "options",
    [
        {"max_horizon": 0},
        {"max_context": -1},
        {"max_context": 1.5},
        {"max_context": "abc"},
        {"per_core_batch_size": 0},
        {"timeframe": "1y"},
        {"timeframe": ""},
        {"timeframe": 60},
        {"model_id": ""},
        {"device": 3},
    ],
)
def test_invalid_constructor_options_are_rejected(options: dict[str, Any]) -> None:
    with pytest.raises(ForecastError):
        TimesFMBackend(**options)


def test_introspection_properties() -> None:
    backend = TimesFMBackend(timeframe="4h", max_context=256, max_horizon=32, per_core_batch_size=4)
    assert backend.timeframe == "4h"
    assert backend.max_context == 256
    assert backend.max_horizon == 32
    assert backend.per_core_batch_size == 4
    assert backend.use_xreg is False
    assert backend.device is None
    assert backend.resolved_device is None
    assert backend.quantile_levels == DEFAULT_QUANTILE_LEVELS


# ---------------------------------------------------------------------------
# device policy
# ---------------------------------------------------------------------------


def test_device_none_resolves_to_cpu_on_a_cpu_only_host(monkeypatch: pytest.MonkeyPatch) -> None:
    torch_module, _timesfm_module = install_fakes(monkeypatch, cuda=False)
    backend = TimesFMBackend()
    assert backend._resolve_device() == "cpu"
    assert backend.resolved_device is None
    assert len(backend.predict(make_requests(1), horizon=2)) == 1
    assert backend.resolved_device == "cpu"
    assert torch_module.matmul_precision == "high"


def test_device_none_resolves_to_cuda_when_it_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch, cuda=True)
    assert TimesFMBackend()._resolve_device() == "cuda"
    assert TimesFMBackend(device="cuda")._resolve_device() == "cuda"


def test_explicit_cpu_device_is_accepted_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch, cuda=True)
    backend = TimesFMBackend(device="cpu")
    assert backend._resolve_device() == "cpu"
    assert backend.predict(make_requests(1), horizon=2)[0].origin == ORIGIN
    assert backend.resolved_device == "cpu"


def test_requested_cuda_falls_back_to_cpu_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install_fakes(monkeypatch, cuda=False)
    backend = TimesFMBackend(device="cuda")
    with caplog.at_level(logging.WARNING):
        backend.predict(make_requests(1), horizon=2)
    assert backend.resolved_device == "cpu"
    assert any("cuda" in record.getMessage().lower() for record in caplog.records)


def test_mps_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch, cuda=False)
    with pytest.raises(ForecastError, match="mps"):
        TimesFMBackend(device="mps").predict(make_requests(1), horizon=2)


def test_unknown_device_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch)
    with pytest.raises(ForecastError, match="unsupported device"):
        TimesFMBackend(device="tpu").predict(make_requests(1), horizon=2)


def test_resolve_device_without_the_extra(without_extras: None) -> None:
    with pytest.raises(ForecastError) as excinfo:
        TimesFMBackend()._resolve_device()
    assert TIMESFM_EXTRA_HINT in str(excinfo.value)


# ---------------------------------------------------------------------------
# compiled configuration and model reuse
# ---------------------------------------------------------------------------


def test_default_forecast_config_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    _torch_module, timesfm_module = install_fakes(monkeypatch)
    TimesFMBackend(max_context=512, max_horizon=64, per_core_batch_size=8).predict(
        make_requests(1), horizon=2
    )
    model = timesfm_module.models[0]
    assert model.model_id == DEFAULT_MODEL_ID
    assert model.torch_compile is False
    config = model.config
    assert config is not None
    assert config.max_context == 512
    assert config.max_horizon == 64
    assert config.per_core_batch_size == 8
    assert config.normalize_inputs is True
    assert config.use_continuous_quantile_head is True
    assert config.fix_quantile_crossing is True
    assert config.infer_is_positive is False
    assert config.force_flip_invariance is False
    assert config.return_backcast is False


def test_explicit_options_are_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    _torch_module, timesfm_module = install_fakes(monkeypatch)
    backend = TimesFMBackend(
        model_id="local/fine-tuned",
        torch_compile=True,
        normalize_inputs=False,
        use_continuous_quantile_head=False,
        fix_quantile_crossing=False,
        infer_is_positive=True,
        force_flip_invariance=True,
        return_backcast=True,
    )
    backend.predict(make_requests(1), horizon=2)
    model = timesfm_module.models[0]
    assert model.model_id == "local/fine-tuned"
    assert model.torch_compile is True
    config = model.config
    assert config is not None
    assert config.normalize_inputs is False
    assert config.use_continuous_quantile_head is False
    assert config.fix_quantile_crossing is False
    assert config.infer_is_positive is True
    assert config.force_flip_invariance is True
    assert config.return_backcast is True


def test_the_model_is_loaded_and_compiled_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _torch_module, timesfm_module = install_fakes(monkeypatch)
    backend = TimesFMBackend()
    backend.predict(make_requests(1), horizon=2)
    backend.predict(make_requests(1), horizon=2)
    assert len(timesfm_module.models) == 1


# ---------------------------------------------------------------------------
# the optional XReg covariate mode
# ---------------------------------------------------------------------------


def test_xreg_forces_the_backcast_and_sends_calendar_covariates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _torch_module, timesfm_module = install_fakes(monkeypatch)
    backend = TimesFMBackend(use_xreg=True, timeframe="1h", xreg_mode="xreg + timesfm")
    trajectories = backend.predict(make_requests(2, length=4), horizon=3)

    model = timesfm_module.models[0]
    config = model.config
    assert config is not None
    assert config.return_backcast is True
    assert model.forecast_calls == []  # the plain call is never used in XReg mode

    assert len(model.covariate_calls) == 1
    call = model.covariate_calls[0]
    assert call.input_lengths == [4, 4]
    assert call.xreg_mode == "xreg + timesfm"
    assert call.normalize is True
    covariates = call.covariates["calendar_phase"]
    assert [len(series) for series in covariates] == [4 + 3, 4 + 3]
    assert all(0 <= value < 24 for series in covariates for value in series)
    assert len(trajectories) == 2


def test_xreg_covariates_are_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    _torch_module, timesfm_module = install_fakes(monkeypatch)
    backend = TimesFMBackend(use_xreg=True, timeframe="4h")
    requests = make_requests(1, length=5)
    backend.predict(requests, horizon=2)
    backend.predict(requests, horizon=2)
    calls = timesfm_module.models[0].covariate_calls
    assert calls[0].covariates == calls[1].covariates
    assert calls[0].list_id != calls[1].list_id
    assert [len(series) for series in calls[0].covariates["calendar_phase"]] == [5 + 2]


def test_xreg_mode_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    _torch_module, timesfm_module = install_fakes(monkeypatch)
    TimesFMBackend(use_xreg=True, xreg_mode=XREG_MODES[1]).predict(make_requests(1), horizon=2)
    assert timesfm_module.models[0].covariate_calls[0].xreg_mode == XREG_MODES[1]


def test_xreg_without_the_xreg_extra_names_the_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ImportError("Failed to load the XReg module. Did you forget to install timesfm[xreg]?")
    install_fakes(monkeypatch, xreg_error=error)
    with pytest.raises(ForecastError) as excinfo:
        TimesFMBackend(use_xreg=True).predict(make_requests(1), horizon=2)
    assert "pip install 'trading-platform[timesfm-xreg]'" in str(excinfo.value)
    assert TIMESFM_XREG_EXTRA_HINT in str(excinfo.value)


# ---------------------------------------------------------------------------
# the calendar phase used by the covariate
# ---------------------------------------------------------------------------


def test_calendar_phase_of_intraday_candles_is_the_time_of_day() -> None:
    assert _calendar_phase(ORIGIN, "1h", 5) == [20, 21, 22, 23, 0]
    assert _calendar_phase(pd.Timestamp("2024-01-01T00:00:00"), "1h", 2) == [23, 0]
    assert _calendar_phase(ORIGIN, "4h", 3) == [4, 5, 0]
    assert _calendar_phase(ORIGIN, "15m", 3) == [94, 95, 0]


def test_calendar_phase_of_daily_candles_is_the_day_of_the_week() -> None:
    assert _calendar_phase(ORIGIN, "1d", 3) == [5, 6, 0]
    # Every weekly candle starts on the same weekday, so that phase is constant:
    # documented degeneracy of the calendar covariate, not a silent surprise.
    assert _calendar_phase(ORIGIN, "1w", 2) == [0, 0]


# ---------------------------------------------------------------------------
# TimesFM 3.0: checkpoint dispatch, the timesfm3 adapter and the covariates
# ---------------------------------------------------------------------------


class FakeForecastOutput:
    """Minimal stand-in for ``timesfm3.evaluator.ForecastOutput``."""

    def __init__(self, horizon: int, channels: int = 9) -> None:
        # The real 3.0 output is (horizon, channels), i.e. the TRANSPOSE of 2.5.
        self.forecast = np.arange(1, horizon + 1, dtype="float32").reshape(horizon, 1)
        self.quantiles = np.repeat(
            np.arange(1, horizon + 1, dtype="float32").reshape(horizon, 1), channels, axis=1
        )
        self.ts_id = None


class FakeTimesFM3:
    """Fake ``timesfm3`` module exposing the pieces the backend imports."""

    def __init__(self, *, horizon_override: int | None = None) -> None:
        self.override = horizon_override
        self.calls: list[dict[str, Any]] = []

    def ModelConfig(self, **kwargs: Any) -> Any:  # noqa: N802 - mirrors the library name
        return types.SimpleNamespace(**kwargs, median_quantile_index=4)

    def TimesFM3Evaluator(self, config: Any) -> Any:  # noqa: N802 - mirrors the library
        return FakeTimesFM3Evaluator(self, config)


class FakeTimesFM3Evaluator:
    """Fake evaluator: records the call and yields one output per context."""

    def __init__(self, module: FakeTimesFM3, config: Any) -> None:
        self._module = module
        self.config = config

    def predict_batch(self, contexts: list[np.ndarray], horizon: int, **kwargs: Any):
        self._module.calls.append({"contexts": contexts, "horizon": horizon, **kwargs})
        produced = horizon if self._module.override is None else self._module.override
        for _ in contexts:
            yield FakeForecastOutput(produced)


def install_timesfm3(monkeypatch: pytest.MonkeyPatch, **options: Any) -> FakeTimesFM3:
    """Inject a fake ``timesfm3`` module (and a torch stub) and return it."""
    module = FakeTimesFM3(**options)
    monkeypatch.setitem(sys.modules, "timesfm3", module)
    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    return module


def test_timesfm3_checkpoints_are_dispatched_to_the_3_0_path() -> None:
    assert _is_timesfm3("google/timesfm-3.0-pytorch") is True
    assert _is_timesfm3("google/timesfm-2.5-200m-pytorch") is False
    assert _is_timesfm3("anything/else") is False


def test_3_0_path_returns_origin_relative_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    module = install_timesfm3(monkeypatch)
    backend = TimesFMBackend(model_id="google/timesfm-3.0-pytorch")
    trajectories = backend.predict(make_requests(1, length=32), horizon=4)

    # The fake head returns 1..horizon as the LEVEL; the backend must subtract the
    # last context value, exactly like the 2.5 path does.
    context_last = 1.0 + 31.0
    assert trajectories[0].quantiles.shape == (9, 4)
    assert np.allclose(trajectories[0].median, np.arange(1, 5) - context_last)
    assert module.calls[0]["univariate"] is False
    assert module.calls[0]["make_positive"] is False


def test_3_0_path_rejects_a_truncated_horizon(monkeypatch: pytest.MonkeyPatch) -> None:
    # A past-future window shorter than padded_context + horizon makes the library
    # silently decode fewer steps; the backend must refuse rather than store a
    # short path.
    install_timesfm3(monkeypatch, horizon_override=3)
    backend = TimesFMBackend(model_id="google/timesfm-3.0-pytorch")
    with pytest.raises(ForecastError) as excinfo:
        backend.predict(make_requests(1, length=32), horizon=4)
    assert "past-future covariate window" in str(excinfo.value)


def test_3_0_path_without_the_timesfm3_module_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "timesfm3", raising=False)
    monkeypatch.setattr(sys, "meta_path", [MissingModuleBlocker(("timesfm3",)), *sys.meta_path])
    backend = TimesFMBackend(model_id="google/timesfm-3.0-pytorch")
    with pytest.raises(ForecastError) as excinfo:
        backend.predict(make_requests(1, length=32), horizon=2)
    assert "timesfm3" in str(excinfo.value)


def test_3_0_covariates_are_passed_with_the_right_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = install_timesfm3(monkeypatch)
    backend = TimesFMBackend(model_id="google/timesfm-3.0-pytorch", use_covariates=True)
    backend.predict(make_requests(2, length=64), horizon=4)

    call = module.calls[0]
    past_only = call["past_only_covariates"]
    past_future = call["past_future_covariates"]
    # Channel-major and spanning the whole context, which is what 3.0 expects.
    assert len(past_only) == 2
    assert past_only[0].shape == (3, 64)
    # The window must cover context + horizon, or the library infers a shorter one.
    assert len(past_future) == 2
    assert past_future[0].shape == (2, 64 + 4)


def test_covariate_provider_never_looks_past_the_context() -> None:
    provider = _CovariateProvider("1h")
    context = np.arange(50, dtype="float64")
    channels = provider.past_only([context])[0]
    assert channels is not None
    # Appending future values must not change any channel of the same prefix.
    extended = provider.past_only([np.arange(70, dtype="float64")])[0]
    assert np.allclose(channels, extended[:, :50])


def test_covariate_provider_needs_at_least_three_points() -> None:
    provider = _CovariateProvider("1h")
    assert provider.past_only([np.asarray([1.0, 2.0])]) == [None]


def test_covariate_provider_rsi_of_a_flat_context_is_neutral() -> None:
    provider = _CovariateProvider("1h")
    channels = provider.past_only([np.full(40, 5.0)])[0]
    assert channels is not None
    assert np.allclose(channels[0], 0.5)


def test_decile_rows_selects_the_requested_levels() -> None:
    block = np.arange(9 * 4, dtype="float32").reshape(9, 4)
    rows = _decile_rows(block, (0.5, 0.9))
    assert np.array_equal(rows, block[[4, 8], :])


def test_decile_rows_rejects_a_level_the_head_does_not_provide() -> None:
    block = np.zeros((9, 4), dtype="float32")
    with pytest.raises(ForecastError) as excinfo:
        _decile_rows(block, (0.25,))
    assert "nine deciles" in str(excinfo.value)


def test_chunks_splits_a_range_into_consecutive_slices() -> None:
    assert [list(part) for part in _chunks(range(5), 2)] == [[0, 1], [2, 3], [4]]
    assert _chunks(range(0), 3) == []
