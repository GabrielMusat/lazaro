import numpy as np
import torch

from agents.dqn_agent import DqnAgent, HyperParams
from explorers import RandomExplorer, RandomExplorerParams
from trainers import Trainer, TrainingParams
from environments import CartPole

from testing.helpers import train

EXPLORER_PARAMS = RandomExplorerParams(init_ep=1, final_ep=0.05, decay_ep=1-1e-3)
AGENT_PARAMS = HyperParams(lr=0.01, gamma=0.995, memory_len=5000)
TRAINING_PARAMS = TrainingParams(learn_every=1, ensure_every=10, batch_size=128)


env = CartPole()


class CustomActionEstimator(torch.nn.Module):
    def __init__(self, in_size: int, out_size: int):
        super(CustomActionEstimator, self).__init__()
        self.linear1 = torch.nn.Linear(in_size, in_size*10)
        self.relu1 = torch.nn.ReLU()

        self.linear2 = torch.nn.Linear(in_size*10, out_size*10)
        self.relu2 = torch.nn.ReLU()

        self.linear3 = torch.nn.Linear(out_size*10, out_size)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.relu1(self.linear1(x))
        x = self.relu2(self.linear2(x))
        return self.linear3(x)


class CustomDqnAgent(DqnAgent):
    @staticmethod
    def action_estimator_factory() -> torch.nn.Module:
        return CustomActionEstimator(env.get_observation_space()[0], len(env.get_action_space()))

    def preprocess(self, x: np.ndarray) -> torch.Tensor:
        return torch.unsqueeze(torch.tensor(x, dtype=torch.float32), 0)

    def postprocess(self, t: torch.Tensor) -> np.ndarray:
        return np.array(t.squeeze(0))


if __name__ == "__main__":
    trainer = Trainer(env, CustomDqnAgent(AGENT_PARAMS, use_gpu=True), RandomExplorer(EXPLORER_PARAMS), TRAINING_PARAMS)
    train(trainer)
