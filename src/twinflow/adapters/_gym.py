"""Optional gymnasium adapter — kept in its own module so importing `twinflow.adapters`
never requires gymnasium. `TwinEnv.to_gymnasium()` calls in here; if gymnasium is not
installed, the import raises a clear, actionable error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from twinflow.adapters.rl import TwinEnv


def build_gym_env(env: TwinEnv) -> Any:
    """Wrap a `TwinEnv` as a real `gymnasium.Env`. Raises `ImportError` with an
    install hint if gymnasium is absent."""
    try:
        import gymnasium as gym
        from gymnasium import spaces
    except ImportError as exc:  # pragma: no cover - exercised only without gymnasium
        raise ImportError(
            "to_gymnasium() needs gymnasium; install it with `pip install gymnasium`"
        ) from exc

    import numpy as np

    class _TwinGymEnv(gym.Env):  # type: ignore[misc]
        metadata: dict[str, Any] = {"render_modes": []}

        def __init__(self) -> None:
            super().__init__()
            self._env = env
            self.action_space = spaces.MultiDiscrete(list(env.action_space.nvec))
            self.observation_space = spaces.Box(
                low=np.array(env.observation_space.low, dtype=np.float64),
                high=np.array(env.observation_space.high, dtype=np.float64),
                dtype=np.float64,
            )

        def reset(
            self, *, seed: int | None = None, options: dict[str, Any] | None = None
        ) -> tuple[Any, dict[str, Any]]:
            super().reset(seed=seed)
            return self._env.reset(seed=seed or 0)

        def step(self, action: Any) -> tuple[Any, float, bool, bool, dict[str, Any]]:
            return self._env.step(tuple(int(a) for a in action))

    return _TwinGymEnv()
