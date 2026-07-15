"""Super MIMIC — Sistema cerrado de optimización de ganancias.

Módulo central que une simulación histórica (MIMIC), backtest, Monte Carlo,
meta-aprendizaje y despliegue seguro en un solo flujo operable desde el dashboard.
"""

from .core import Experiment, ExperimentRegistry, ArtifactSchema
from .engines import run_mimic_sweep_experiment
from .runner import RoboticExperimentRunner

__all__ = [
    "Experiment",
    "ExperimentRegistry",
    "ArtifactSchema",
    "run_mimic_sweep_experiment",
    "RoboticExperimentRunner",
]
