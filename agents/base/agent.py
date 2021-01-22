from abc import ABC, abstractmethod
import typing as T
import torch
import os
import json
import datetime
import time
import numpy as np

from environments import Environment
from ..explorers import AnyExplorer, RandomExplorer, NoisyExplorer
from ..replay_buffers import AnyReplayBuffer, ReplayBufferEntry, NStepsPrioritizedReplayBuffer, \
    PrioritizedReplayBuffer, NStepsRandomReplayBuffer
from .models import HyperParams, TrainingProgress, TrainingParams, LearningStep, TrainingStep
from ..explorers.noisy_explorer import NoisyLinear
from logger import get_logger
from plotter import TensorBoard


class Agent(ABC):
    def __init__(self,
                 action_space: int,
                 explorer: T.Union[AnyExplorer, None],
                 replay_buffer: AnyReplayBuffer,
                 tp: TrainingParams,
                 hp: HyperParams,
                 use_gpu: bool = True,
                 save_progress: bool = True,
                 tensor_board_log: bool = True):

        self.log = get_logger(self.get_self_class_name())
        self.action_space = action_space
        self.hp: HyperParams = hp
        self.tp: TrainingParams = tp
        self.gamma: float = hp.gamma
        self.explorer: T.Union[AnyExplorer, None] = explorer
        self.replay_buffer: AnyReplayBuffer = replay_buffer
        self.device: str = "cpu"
        self.use_gpu: bool = use_gpu
        if use_gpu:
            if not torch.cuda.is_available():
                self.log.warning("cuda is not available, CPU will be used")
                self.use_gpu = False
            else:
                self.device = "cuda"

        self.save_progress: bool = save_progress
        self.tensor_board_log: bool = tensor_board_log

        self.module_names: T.List[str] = []
        self.modules: T.Dict[torch.nn.Module, T.Dict] = {}
        self.step_callbacks: T.List[T.Callable[[TrainingStep], None]] = []
        self.progress_callbacks: T.List[T.Callable[[TrainingProgress], bool]] = []
        self.learning_callbacks: T.List[T.Callable[[LearningStep], None]] = []
        self.model_wrappers: T.List[T.Callable[[torch.nn.Module], torch.nn.Module]] = []
        self.save_path: T.Union[None, str] = None
        self.summary_writer: T.Union[TensorBoard, None] = None
        self.reward_record: T.List[float] = []
        self.loss_record: T.List[float] = []
        self.sample_inputs: T.Union[None, T.List[torch.Tensor]] = None

        self.link_replay_buffer()
        self.link_explorer()
        self.link_saver()
        self.link_tensorboard()

    def health_check(self, env: Environment):
        self.log.info("checking the model is healthy...")
        s = env.reset()
        self.log.debug(f"state for testing health is:\n{s}")
        self.log.info("testing preprocessing...")
        try:
            self.preprocess(s)
        except Exception as e:
            self.log.error("error while testing preprocessing")
            raise e
        self.log.info("preprocessing is correct")
        self.log.info("testing inference...")
        try:
            self.infer(s)
        except Exception as e:
            self.log.error("error while testing inference")
            raise e
        self.log.info("inference is correct")
        self.log.info("testing learning...")
        while len(self.replay_buffer) < 2:
            a = 0
            s_, r, final = env.step(a)
            self.replay_buffer.add(ReplayBufferEntry(s, s_, a, r, final))
            s = s_
            if final:
                s = env.reset()

        batch = self.replay_buffer.sample(2)
        try:
            self.learn(batch)
        except Exception as e:
            self.log.error("error while testing learning")
            raise e

        self.log.info("learning is correct")
        self.log.info("model is healthy!")
        self.replay_buffer.clear()
        if self.tensor_board_log or self.save_progress:
            self.create_save_folder(env)

        if self.tensor_board_log:
            self.summary_writer = TensorBoard(self.save_path)
            self.tensorboard_log_model_graph()

        if self.save_progress:
            self.save_agent_info()

    def tensorboard_log_training_progress(self, training_progress: TrainingProgress):
        if self.summary_writer:
            self.summary_writer.add_scalar("episode reward", training_progress.total_reward, training_progress.episode)

        return False

    def tensorboard_log_model_graph(self):
        models: T.Dict[str, torch.nn.Module] = {}
        for attr, value in self.__dict__.items():
            if isinstance(value, torch.nn.Module) and not attr.startswith("loss") and self.sample_inputs is not None:
                models[attr] = value

        class AllModels(torch.nn.Module):
            def __init__(self):
                super(AllModels, self).__init__()
                for name, model in models.items():
                    self.__setattr__(name, model)

            def forward(self, x):
                result_unfolded = []
                for result in [self.__getattr__(name)(x) for name in models]:
                    if isinstance(result, tuple):
                        for folded_result in result:
                            result_unfolded.append(folded_result)
                    else:
                        result_unfolded.append(result)
                return tuple(result_unfolded)

        self.summary_writer.add_graph(AllModels(), self.sample_inputs)

    def tensorboard_log_random_explorer_add_epsilon(self, training_progress: TrainingProgress) -> bool:
        if self.summary_writer:
            self.summary_writer.add_scalar("random explorer Epsilon", self.explorer.epsilon, training_progress.episode)
        return False
    
    def tensorboard_log_prioritized_replay_buffer_add_beta(self, training_progress: TrainingProgress):
        if self.summary_writer:
            self.summary_writer.add_scalar("prioritized replay buffer Beta",
                                           self.replay_buffer.beta,
                                           training_progress.episode)
        return False

    def forward_hook(self, module: torch.nn.Module, x: T.Tuple[torch.Tensor], y: torch.Tensor):
        if self.sample_inputs is None:
            self.sample_inputs = x
        if self.summary_writer:
            if len(self.module_names) == 0:
                for attr, value in self.__dict__.items():
                    if isinstance(value, torch.nn.Module) and not attr.startswith("loss"):
                        self.module_names.append(attr)
            if module not in self.modules:
                self.modules[module] = {"name": self.module_names[len(self.modules)], "times": 0, "renders": 0}

            if self.modules[module]["times"] % 1000 == 0:
                self.summary_writer.add_embedding(y,
                                                  tag=self.modules[module]["name"],
                                                  global_step=self.modules[module]["renders"])
                self.modules[module]["renders"] += 1
            self.modules[module]["times"] += 1

    def link_tensorboard(self):
        if self.tensor_board_log:
            self.log.info("linking tensorboard callbacks...")
            self.add_progress_callback(self.tensorboard_log_training_progress)

            def model_wrapper(model: torch.nn.Module):
                model.register_forward_hook(self.forward_hook)
                return model

            self.model_wrappers.append(model_wrapper)
            
    def create_save_folder(self, env: Environment):
        base = os.environ.get("SAVE_DIR", "data")
        if base.endswith("/"):
            base = base[:-1]

        agent = self.get_self_class_name()
        today = str(datetime.datetime.now().date())
        now = str(datetime.datetime.now().time().strftime("%H:%M:%S"))
        folder = ""
        for sub_folder in [base, agent, type(env).__name__, today, now]:
            folder = os.path.join(folder, sub_folder)
            if not os.path.isdir(folder):
                self.log.info(f"folder {folder} does not exists, creating it...")
                os.mkdir(folder)
        self.save_path = folder
        self.log.info(f"all save folders created: {folder}")

    def save_agent_info(self):
        self.log.info("saving agent info...")
        agent_info_path = os.path.join(self.save_path, "agent.json")
        json.dump(self.get_info(), open(agent_info_path, "w"), indent=4)
        self.log.info("agent.json created correctly")

    def save_training_progress_callback(self, training_progress: TrainingProgress) -> bool:
        self.log.debug("save callback triggered")
        if self.save_path is None:
            self.log.info("progress saving aborted, agent cannot be considered healthy yet")
            return False
        folder_checkpoints = os.path.join(self.save_path, "checkpoints")
        if not os.path.isdir(folder_checkpoints):
            os.mkdir(folder_checkpoints)

        folder_checkpoints_checkpoint = os.path.join(folder_checkpoints, str(time.time()) + ".json")
        json.dump(training_progress.__dict__, open(folder_checkpoints_checkpoint, "w"))
        self.log.debug("checkpoint saved correctly")
        return False

    def link_saver(self):
        if self.save_progress:
            self.log.info("linking saving callbacks...")
            self.add_progress_callback(self.save_training_progress_callback)
            self.log.info("progress callbacks linked correctly")
        else:
            self.log.info("progress is not going to be saved")

    def prioritized_replay_buffer_update_priorities(self, learning_step: LearningStep):
        self.log.debug(f"update priorities for {type(self.replay_buffer).__name__} triggered")
        self.replay_buffer.update_priorities(
            [e.index for e in learning_step.batch],
            [abs(x - y) + 1e-7 for x, y in zip(learning_step.x, learning_step.y)]
        )

    def prioritized_replay_buffer_increase_beta(self, _: TrainingStep):
        self.log.debug(f"increase beta for {type(self.replay_buffer).__name__} triggered")
        self.replay_buffer.increase_beta()

    def link_replay_buffer(self):
        if isinstance(self.replay_buffer, (PrioritizedReplayBuffer, NStepsPrioritizedReplayBuffer)):
            self.log.info(f"linking {type(self.replay_buffer).__name__} priority...")

            self.add_learn_callback(self.prioritized_replay_buffer_update_priorities)
            self.add_step_callback(self.prioritized_replay_buffer_increase_beta)

            if self.tensor_board_log:
                self.add_progress_callback(self.tensorboard_log_prioritized_replay_buffer_add_beta)

            self.log.info(f"{type(self.replay_buffer).__name__} priority linked correctly")

        if isinstance(self.replay_buffer, (NStepsPrioritizedReplayBuffer, NStepsRandomReplayBuffer)):
            self.log.info(f"linking {type(self.replay_buffer).__name__} n_steps...")
            self.replay_buffer.set_gamma(self.hp.gamma)
            new_gamma = self.hp.gamma ** self.replay_buffer.rp.n_step
            self.log.info(f"refactoring gamma for {type(self.replay_buffer).__name__}: {self.gamma} -> {new_gamma}")
            self.gamma = new_gamma
            self.log.info(f"{type(self.replay_buffer).__name__} n_steps linked correctly")

    def random_explorer_decay_epsilon(self, _: TrainingStep):
        self.log.debug(f"decay epsilon for {type(self.explorer).__name__} triggered")
        self.explorer.decay()

    def noisy_explorer_reset_noise(self, training_step: TrainingStep):
        self.log.debug(f"reset noise for {type(self.explorer).__name__} triggered")
        if training_step.step % self.explorer.ep.reset_noise_every == 0:
            for attr, value in self.__dict__.items():
                if isinstance(value, torch.nn.Module):
                    for i, layer in enumerate(value.modules()):
                        if isinstance(layer, NoisyLinear):
                            self.log.debug(f"layer {i} for attribute {attr} is noisy, noise reset")
                            layer.reset_noise()

    def link_explorer(self):
        if isinstance(self.explorer, RandomExplorer):
            self.log.info(f"linking {type(self.explorer).__name__}...")
            self.add_step_callback(self.random_explorer_decay_epsilon)
            if self.tensor_board_log:
                self.add_progress_callback(self.tensorboard_log_random_explorer_add_epsilon)
            self.log.info(f"{type(self.explorer).__name__} linked correctly")

        elif isinstance(self.explorer, NoisyExplorer):
            self.log.info(f"linking {type(self.explorer).__name__}...")

            def noisy_linear_model_factory(model: torch.nn.Module) -> torch.nn.Module:
                self.log.info(f"wrapping model with noisy layers triggered")
                return self.explorer.wrap_model(model)

            self.model_wrappers.append(noisy_linear_model_factory)
            self.add_step_callback(self.noisy_explorer_reset_noise)

        else:
            self.log.info(f"{type(self.explorer).__name__} explorer does not need linking")

    def add_step_callback(self, cbk: T.Callable[[TrainingStep], None]):
        self.step_callbacks.append(cbk)
        self.log.info(f"added new step callback, there are {len(self.step_callbacks)} step callbacks")

    def add_progress_callback(self, cbk: T.Callable[[TrainingProgress], bool]):
        self.progress_callbacks.append(cbk)
        self.log.info(f"added new progress callback, there are {len(self.progress_callbacks)} progress callbacks")

    def add_learn_callback(self, cbk: T.Callable[[LearningStep], None]):
        self.learning_callbacks.append(cbk)
        self.log.info(f"added new learn callback, there are {len(self.learning_callbacks)} learn callbacks")

    def call_step_callbacks(self, training_step: TrainingStep):
        self.log.debug(f"new training step: {training_step.__dict__}")
        self.log.debug("calling step callbacks...")
        for i, cbk in enumerate(self.step_callbacks):
            self.log.debug(f"calling step callback {i}, {cbk}")
            cbk(training_step)
        self.log.debug("all step callbacks called")

    def call_progress_callbacks(self, training_progress: TrainingProgress) -> bool:
        self.log.info(f"new training progress: {training_progress.__dict__}")
        self.log.debug("calling progress callbacks...")
        must_exit = False
        for i, cbk in enumerate(self.progress_callbacks):
            self.log.debug(f"calling progress callback {i}, {cbk}")
            may_exit = cbk(training_progress)
            if may_exit:
                must_exit = True
                self.log.warning(f"progress callback {i} said that training should end")
        self.log.debug("all progress callbacks called")
        return must_exit

    def call_learn_callbacks(self, learning_step: LearningStep):
        self.log.debug(f"new learning step: {learning_step.__dict__}"[:45]+"...")
        self.log.debug("calling learning callbacks...")
        for i, cbk in enumerate(self.learning_callbacks):
            self.log.debug(f"calling learning callback {i}, {cbk}")
            cbk(learning_step)
        self.log.debug("all learning callbacks called")

    def build_model(self) -> torch.nn.Module:
        self.log.info("building model from model factory...")
        model = self.model_factory()
        self.log.info("model built correctly")
        for i, wrapper in enumerate(self.model_wrappers):
            self.log.info(f"wrapping model with wrapper {i}, {wrapper}")
            model = wrapper(model)
            self.log.info("model wrapped correctly")
        return model

    def last_layer_factory(self, in_features: int, out_features: int):
        if isinstance(self.explorer, NoisyExplorer):
            return NoisyLinear(in_features, out_features, self.explorer.ep.std_init)
        else:
            return torch.nn.Linear(in_features, out_features)

    def get_self_class_name(self):
        return self.__class__.__bases__[0].__name__

    def get_info(self) -> dict:
        info = {
            "class": self.get_self_class_name(),
            "hyper parameters": {
                "class": type(self.hp).__name__,
                "attributes": self.hp.__dict__
            },
            "training parameters": {
                "class": type(self.tp).__name__,
                "attributes": self.tp.__dict__
            },
            "replay buffer": {
                "class": type(self.replay_buffer).__name__,
                "attributes": {
                    "class": type(self.replay_buffer.rp).__name__,
                    "attributes": self.replay_buffer.rp.__dict__
                }
            },
            "explorer": {
                "class": type(self.explorer).__name__,
                "attributes": {
                    "class": type(self.explorer.ep).__name__,
                    "attributes": self.explorer.ep.__dict__
                }
            }
        }
        return info

    @abstractmethod
    def model_factory(self) -> torch.nn.Module:
        raise NotImplementedError()

    @abstractmethod
    def preprocess(self, x: np.ndarray) -> torch.Tensor:
        raise NotImplementedError()

    @abstractmethod
    def postprocess(self, t: torch.Tensor) -> np.ndarray:
        raise NotImplementedError()

    @abstractmethod
    def infer(self, *args) -> T.Any:
        raise NotImplementedError()

    @abstractmethod
    def learn(self, batch: T.List[ReplayBufferEntry]) -> None:
        raise NotImplementedError()

    @abstractmethod
    def train(self, env: Environment) -> None:
        raise NotImplementedError()