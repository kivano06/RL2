from torch.nn.functional import relu
from collections import namedtuple
from collections import deque
import numpy as np
import gym
import copy
import cv2
import random
import torch
import torch.nn as nn
import math
from itertools import count
from tqdm import tqdm

import pickle


cv2.ocl.setUseOpenCL(False)
torch.manual_seed(42)

Transition = namedtuple("Transion", ("state", "action", "next_state", "reward"))


class DQNbn(nn.Module):
    def __init__(self, in_channels=4, n_actions=4):
        """
        Initialize Deep Q Network

        Args:
            in_channels (int): number of input channels
            n_actions (int): number of outputs
        """
        super(DQNbn, self).__init__()
        self.conv1 = nn.Conv2d(in_channels, 32, kernel_size=8, stride=4)
        self.bn1 = nn.BatchNorm2d(32)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=4, stride=2)
        self.bn2 = nn.BatchNorm2d(64)
        self.conv3 = nn.Conv2d(64, 64, kernel_size=3, stride=1)
        self.bn3 = nn.BatchNorm2d(64)
        self.fc4 = nn.Linear(7 * 7 * 64, 512)
        self.head = nn.Linear(512, n_actions)

    def forward(self, x):
        x = x.float() / 255
        x = relu(self.bn1(self.conv1(x)))
        x = relu(self.bn2(self.conv2(x)))
        x = relu(self.bn3(self.conv3(x)))
        x = relu(self.fc4(x.view(x.size(0), -1)))
        return self.head(x)


class DQN(nn.Module):
    def __init__(self, in_channels, n_actions):
        super().__init__()
        self.conv1 = nn.Conv2d(in_channels, 32, kernel_size=8, stride=4)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=4, stride=2)
        self.conv3 = nn.Conv2d(64, 64, kernel_size=3, stride=1)
        self.fc1 = nn.Linear(64 * 7 * 7, 512)
        self.fc2 = nn.Linear(512, n_actions)

    def forward(self, x):
        """
        Input shape: (bs,c,h,w) #(bs,4,84,84)
        Output shape: (bs,n_actions)
        """
        x = x.float() / 255
        x = relu(self.conv1(x))
        x = relu(self.conv2(x))
        x = relu(self.conv3(x))
        x = torch.flatten(x, 1)
        x = relu(self.fc1(x))
        return self.fc2(x)


class ReplayMemory(object):
    def __init__(self, capacity):
        self.capacity = capacity
        self.memory = []

    def push(self, *args):
        if len(self.memory) >= self.capacity:
            self.memory = self.memory[1:]
        self.memory.append(Transition(*args))

    def sample(self, batch_size):
        return random.sample(self.memory, batch_size)

    def __len__(self):
        return len(self.memory)


def make_env(
    env, stack_frames=True, episodic_life=True, clip_rewards=False, scale=False
):
    if episodic_life:
        env = EpisodicLifeEnv(env)

    env = NoopResetEnv(env, noop_max=30)
    env = MaxAndSkipEnv(env, skip=4)
    if "FIRE" in env.unwrapped.get_action_meanings():
        env = FireResetEnv(env)

    env = WarpFrame(env)
    if stack_frames:
        env = FrameStack(env, 4)
    if clip_rewards:
        env = ClipRewardEnv(env)
    return env


class RewardScaler(gym.RewardWrapper):

    def reward(self, reward):
        return reward * 0.1


class ClipRewardEnv(gym.RewardWrapper):
    def __init__(self, env):
        gym.RewardWrapper.__init__(self, env)

    def reward(self, reward):
        """Bin reward to {+1, 0, -1} by its sign."""
        return np.sign(reward)


class LazyFrames(object):
    def __init__(self, frames):
        """This object ensures that common frames between the observations are only stored once.
        It exists purely to optimize memory usage which can be huge for DQN's 1M frames replay
        buffers.
        This object should only be converted to numpy array before being passed to the model.
        You'd not believe how complex the previous solution was."""
        self._frames = frames
        self._out = None

    def _force(self):
        if self._out is None:
            self._out = np.concatenate(self._frames, axis=2)
            self._frames = None
        return self._out

    def __array__(self, dtype=None):
        out = self._force()
        if dtype is not None:
            out = out.astype(dtype)
        return out

    def __len__(self):
        return len(self._force())

    def __getitem__(self, i):
        return self._force()[i]


class FrameStack(gym.Wrapper):
    def __init__(self, env, k):
        """Stack k last frames.
        Returns lazy array, which is much more memory efficient.
        See Also
        --------
        baselines.common.atari_wrappers.LazyFrames
        """
        gym.Wrapper.__init__(self, env)
        self.k = k
        self.frames = deque([], maxlen=k)
        shp = env.observation_space.shape
        self.observation_space = gym.spaces.Box(
            low=0,
            high=255,
            shape=(shp[0], shp[1], shp[2] * k),
            dtype=env.observation_space.dtype,
        )

    def reset(self):
        ob = self.env.reset()
        for _ in range(self.k):
            self.frames.append(ob)
        return self._get_ob()

    def step(self, action):
        ob, reward, done, info = self.env.step(action)
        self.frames.append(ob)
        return self._get_ob(), reward, done, info

    def _get_ob(self):
        assert len(self.frames) == self.k
        return LazyFrames(list(self.frames))


class WarpFrame(gym.ObservationWrapper):
    def __init__(self, env):
        """Warp frames to 84x84 as done in the Nature paper and later work."""
        gym.ObservationWrapper.__init__(self, env)
        self.width = 84
        self.height = 84
        self.observation_space = gym.spaces.Box(
            low=0, high=255, shape=(self.height, self.width, 1), dtype=np.uint8
        )

    def observation(self, frame):
        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        frame = cv2.resize(
            frame, (self.width, self.height), interpolation=cv2.INTER_AREA
        )
        return frame[:, :, None]


class FireResetEnv(gym.Wrapper):

    def __init__(self, env=None):
        """For environments where the user need to press FIRE for the game to start."""
        super(FireResetEnv, self).__init__(env)
        assert env.unwrapped.get_action_meanings()[1] == "FIRE"
        assert len(env.unwrapped.get_action_meanings()) >= 3

    def step(self, action):
        return self.env.step(action)

    def reset(self, seed=42):
        self.env.reset()
        obs, _, done, _ = self.env.step(1)
        if done:
            self.env.reset()
        obs, _, done, _ = self.env.step(2)
        if done:
            self.env.reset()
        return obs


class EpisodicLifeEnv(gym.Wrapper):
    def __init__(self, env=None):
        """Make end-of-life == end-of-episode, but only reset on true game over.
        Done by DeepMind for the DQN and co. since it helps value estimation.
        """
        super(EpisodicLifeEnv, self).__init__(env)
        self.lives = 0
        self.was_real_done = True
        self.was_real_reset = False

    def step(self, action):
        obs, reward, terminated, truncated, info = self.env.step(action)
        done = terminated or truncated
        self.was_real_done = done
        # check current lives, make loss of life terminal,
        # then update lives to handle bonus lives
        lives = self.env.unwrapped.ale.lives()
        if lives < self.lives and lives > 0:
            # for Qbert somtimes we stay in lives == 0 condtion for a few frames
            # so its important to keep lives > 0, so that we only reset once
            # the environment advertises done.
            done = True
        self.lives = lives
        return obs, reward, done, info

    def reset(self):
        """Reset only when lives are exhausted.
        This way all states are still reachable even though lives are episodic,
        and the learner need not know about any of this behind-the-scenes.
        """
        if self.was_real_done:
            obs = self.env.reset()
            self.was_real_reset = True
        else:
            # no-op step to advance from terminal/lost life state
            obs, _, _, _, _ = self.env.step(0)
            self.was_real_reset = False
        self.lives = self.env.unwrapped.ale.lives()
        return obs


class MaxAndSkipEnv(gym.Wrapper):
    def __init__(self, env=None, skip=4):
        """Return only every `skip`-th frame"""
        super(MaxAndSkipEnv, self).__init__(env)
        # most recent raw observations (for max pooling across time steps)
        self._obs_buffer = deque(maxlen=2)
        self._skip = skip

    def step(self, action):
        total_reward = 0.0
        done = None
        for _ in range(self._skip):
            obs, reward, done, info = self.env.step(action)
            self._obs_buffer.append(obs)
            total_reward += reward
            if done:
                break

        max_frame = np.max(np.stack(self._obs_buffer), axis=0)

        return max_frame, total_reward, done, info

    def reset(self, seed=42):
        """Clear past frame buffer and init. to first obs. from inner env."""
        self._obs_buffer.clear()
        obs = self.env.reset()
        self._obs_buffer.append(obs)
        return obs


class NoopResetEnv(gym.Wrapper):
    def __init__(self, env=None, noop_max=30):
        """Sample initial states by taking random number of no-ops on reset.
        No-op is assumed to be action 0.
        """
        super(NoopResetEnv, self).__init__(env)
        self.noop_max = noop_max
        self.override_num_noops = None
        assert env.unwrapped.get_action_meanings()[0] == "NOOP"

    def step(self, action):
        return self.env.step(action)

    def reset(self):
        """Do no-op action for a number of steps in [1, noop_max]."""
        self.env.reset()
        if self.override_num_noops is not None:
            noops = self.override_num_noops
        else:
            noops = np.random.randint(1, self.noop_max + 1)
        assert noops > 0
        obs = None
        for _ in range(noops):
            obs, _, done, _ = self.env.step(0)
            if done:
                obs = self.env.reset()
        return obs


def get_state(obs):
    state = np.array(obs)
    state = state.transpose((2, 0, 1))
    state = torch.from_numpy(state)
    return state.unsqueeze(0)


def select_action(state, eps_end, eps_start, eps_decay, policy_net, device, steps_done):
    sample = random.random()
    eps_threshold = eps_end + (eps_start - eps_end) * math.exp(
        -1.0 * steps_done / eps_decay
    )
    steps_done += 1
    if sample > eps_threshold:
        with torch.no_grad():
            return policy_net(state.to(device)).max(1)[1].view(1, 1), steps_done
    else:
        return (
            torch.tensor([[random.randrange(4)]], device=device, dtype=torch.long),
            steps_done,
        )


def optimize_model(
    memory,
    batch_size,
    device,
    policy_net,
    target_net,
    gamma,
    optimizer,
):
    if len(memory) < batch_size:
        return policy_net, optimizer, None
    transitions = memory.sample(batch_size)
    """
    zip(*transitions) unzips the transitions into
    Transition(*) creates new named tuple
    batch.state - tuple of all the states (each state is a tensor)
    batch.next_state - tuple of all the next states (each state is a tensor)
    batch.reward - tuple of all the rewards (each reward is a float)
    batch.action - tuple of all the actions (each action is an int)    
    """
    batch = Transition(*zip(*transitions))

    actions = tuple((map(lambda a: torch.tensor([[a]], device="cuda"), batch.action)))
    rewards = tuple((map(lambda r: torch.tensor([r], device="cuda"), batch.reward)))

    non_final_mask = torch.tensor(
        tuple(map(lambda s: s is not None, batch.next_state)),
        device=device,
        dtype=torch.uint8,
    )

    non_final_next_states = torch.cat(
        [s for s in batch.next_state if s is not None]
    ).to("cuda")

    state_batch = torch.cat(batch.state).to("cuda")
    action_batch = torch.cat(actions)
    reward_batch = torch.cat(rewards)

    state_action_values = policy_net(state_batch).gather(1, action_batch)

    next_state_values = torch.zeros(batch_size, device=device)
    next_state_values[non_final_mask] = (
        target_net(non_final_next_states).max(1)[0].detach()
    )
    expected_state_action_values = (next_state_values * gamma) + reward_batch

    loss = torch.nn.functional.smooth_l1_loss(
        state_action_values, expected_state_action_values.unsqueeze(1)
    )

    optimizer.zero_grad()
    loss.backward()
    for param in policy_net.parameters():
        param.grad.data.clamp_(-1, 1)
    optimizer.step()
    return policy_net, optimizer, loss


def train(
    model_name,
    env,
    n_episodes,
    memory,
    device,
    initial_memory,
    policy_net,
    target_net,
    gamma,
    optimizer,
    batch_size,
    target_update,
    eps_end,
    eps_start,
    eps_decay,
    render=False,
):
    steps_done = 0
    rewards_list = []
    loss_list = []
    steps_list = []
    pbar = tqdm(range(n_episodes), desc="")
    for episode in pbar:
        obs = env.reset()
        state = get_state(obs)
        total_reward = 0.0
        total_loss = 0.0
        for t in count():
            action, steps_done = select_action(
                state, eps_end, eps_start, eps_decay, policy_net, device, steps_done
            )

            if render and episode % 5 == 0:
                env.render()

            obs, reward, done, info = env.step(action)

            total_reward += reward

            if not done:
                next_state = get_state(obs)
            else:
                next_state = None

            reward = torch.tensor([reward], device=device)

            memory.push(state, action.to(device), next_state, reward.to(device))
            state = next_state

            if steps_done > initial_memory:
                policy_net, optimizer, loss = optimize_model(
                    memory,
                    batch_size,
                    device,
                    policy_net,
                    target_net,
                    gamma,
                    optimizer,
                )
                if loss is not None:
                    total_loss += loss.item()

                if steps_done % target_update == 0:
                    target_net.load_state_dict(policy_net.state_dict())

            if done:
                if steps_done > initial_memory:
                    steps_list.append(t)
                    rewards_list.append(total_reward)
                    loss_list.append(total_loss)
                break
        if episode % 10 == 0 and steps_done > initial_memory:
            eps_threshold = eps_end + (eps_start - eps_end) * math.exp(
                -1.0 * steps_done / eps_decay
            )
            pbar.set_description(
                f"steps: {steps_done} avg_10 steps {np.mean(steps_list[-10:]):.3} reward: {total_reward} loss: {total_loss:.3} avg_10 loss: {np.mean(loss_list[-10:]):.3} avg_10 reward: {np.mean(rewards_list[-10:]):.3} eps: {eps_threshold:.3}"
            )

        if episode % 100 == 0 and steps_done > initial_memory:
            with open("steps_list.pkl", "wb") as file:
                pickle.dump(steps_list, file)
            with open("loss_list.pkl", "wb") as file:
                pickle.dump(loss_list, file)
            with open("rewards_list.pkl", "wb") as file:
                pickle.dump(rewards_list, file)

            torch.save(policy_net, f"models/{model_name}_{episode}")
    env.close()
    return policy_net, rewards_list, loss_list
