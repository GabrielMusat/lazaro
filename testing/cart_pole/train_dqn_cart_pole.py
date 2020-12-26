import numpy as np
import torch

from agents import DqnAgent, DqnHyperParams, TrainingParams
from agents.explorers import RandomExplorer, RandomExplorerParams
from agents.replay_buffers import NStepsPrioritizedReplayBuffer, NStepPrioritizedReplayBufferParams
from environments import CartPole

from testing.helpers import train

EXPLORER_PARAMS = RandomExplorerParams(init_ep=1, final_ep=0.01, decay_ep=1-1e-3)
AGENT_PARAMS = DqnHyperParams(lr=0.01, gamma=0.995, ensure_every=10)
TRAINING_PARAMS = TrainingParams(learn_every=1, batch_size=128, episodes=500)
REPLAY_BUFFER_PARAMS = NStepPrioritizedReplayBufferParams(max_len=5000, gamma=AGENT_PARAMS.gamma, n_step=3, alpha=0.6,
                                                          init_beta=0.4, final_beta=1.0, increase_beta=1+1e-3)


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
    def model_factory(self) -> torch.nn.Module:
        return CustomActionEstimator(env.get_observation_space()[0], len(env.get_action_space()))

    def preprocess(self, x: np.ndarray) -> torch.Tensor:
        return torch.unsqueeze(torch.tensor(x, dtype=torch.float32), 0)


if __name__ == "__main__":
    agent = CustomDqnAgent(
        AGENT_PARAMS,
        TRAINING_PARAMS,
        RandomExplorer(EXPLORER_PARAMS),
        NStepsPrioritizedReplayBuffer(REPLAY_BUFFER_PARAMS),
        use_gpu=True
    )
    train(agent, env)