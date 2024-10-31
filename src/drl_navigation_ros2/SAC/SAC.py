import numpy as np
import torch
import torch.nn.functional as F
import SAC.SAC_utils as utils
from SAC.SAC_critic import DoubleQCritic as critic_model
from SAC.SAC_actor import DiagGaussianActor as actor_model
from torch.utils.tensorboard import SummaryWriter


class SAC(object):
    """SAC algorithm."""

    def __init__(
        self,
        obs_dim,
        action_dim,
        action_range,
        device,
        discount,
        init_temperature,
        alpha_lr,
        alpha_betas,
        actor_lr,
        actor_betas,
        actor_update_frequency,
        critic_lr,
        critic_betas,
        critic_tau,
        critic_target_update_frequency,
        batch_size,
        learnable_temperature,
    ):
        super().__init__()

        self.state_dim = obs_dim
        self.action_dim = action_dim
        self.action_range = action_range
        self.device = torch.device(device)
        self.discount = discount
        self.critic_tau = critic_tau
        self.actor_update_frequency = actor_update_frequency
        self.critic_target_update_frequency = critic_target_update_frequency
        self.batch_size = batch_size
        self.learnable_temperature = learnable_temperature

        self.critic = critic_model(obs_dim=obs_dim, action_dim=action_dim, hidden_dim=1024, hidden_depth=2).to(
            self.device
        )
        self.critic_target = critic_model(obs_dim=obs_dim, action_dim=action_dim, hidden_dim=1024, hidden_depth=2).to(
            self.device
        )
        self.critic_target.load_state_dict(self.critic.state_dict())

        self.actor = actor_model(
            obs_dim=obs_dim,
            action_dim=action_dim,
            hidden_dim=1024,
            hidden_depth=2,
            log_std_bounds=[-5, 2],
        ).to(self.device)

        self.log_alpha = torch.tensor(np.log(init_temperature)).to(self.device)
        self.log_alpha.requires_grad = True
        # set target entropy to -|A|
        self.target_entropy = -action_dim

        # optimizers
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=actor_lr, betas=actor_betas)

        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=critic_lr, betas=critic_betas)

        self.log_alpha_optimizer = torch.optim.Adam([self.log_alpha], lr=alpha_lr, betas=alpha_betas)

        self.critic_target.train()

        self.actor.train(True)
        self.critic.train(True)
        self.step = 0
        self.writer = SummaryWriter()

    def train(self, replay_buffer, iterations, batch_size):
        for _ in range(iterations):
            self.update(replay_buffer=replay_buffer, step=self.step, batch_size=batch_size)
        self.step += 1

    @property
    def alpha(self):
        return self.log_alpha.exp()

    def get_action(self, obs, add_noise):
        if add_noise:
            return (self.act(obs) + np.random.normal(0, 0.2, size=self.action_dim)).clip(
                self.action_range[0], self.action_range[1]
            )
        else:
            return self.act(obs)

    def act(self, obs, sample=False):
        obs = torch.FloatTensor(obs).to(self.device)
        obs = obs.unsqueeze(0)
        dist = self.actor(obs)
        action = dist.sample() if sample else dist.mean
        action = action.clamp(*self.action_range)
        assert action.ndim == 2 and action.shape[0] == 1
        return utils.to_np(action[0])

    def update_critic(self, obs, action, reward, next_obs, done, step):
        dist = self.actor(next_obs)
        next_action = dist.rsample()
        log_prob = dist.log_prob(next_action).sum(-1, keepdim=True)
        target_Q1, target_Q2 = self.critic_target(next_obs, next_action)
        target_V = torch.min(target_Q1, target_Q2) - self.alpha.detach() * log_prob
        target_Q = reward + ((1 - done) * self.discount * target_V)
        target_Q = target_Q.detach()

        # get current Q estimates
        current_Q1, current_Q2 = self.critic(obs, action)
        critic_loss = F.mse_loss(current_Q1, target_Q) + F.mse_loss(current_Q2, target_Q)
        self.writer.add_scalar("train_critic/loss", critic_loss, step)

        # Optimize the critic
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()

        self.critic.log(self.writer, step)

    def update_actor_and_alpha(self, obs, step):
        dist = self.actor(obs)
        action = dist.rsample()
        log_prob = dist.log_prob(action).sum(-1, keepdim=True)
        actor_Q1, actor_Q2 = self.critic(obs, action)

        actor_Q = torch.min(actor_Q1, actor_Q2)
        actor_loss = (self.alpha.detach() * log_prob - actor_Q).mean()

        self.writer.add_scalar("train_actor/loss", actor_loss, step)
        self.writer.add_scalar("train_actor/target_entropy", self.target_entropy, step)
        self.writer.add_scalar("train_actor/entropy", -log_prob.mean(), step)

        # optimize the actor
        self.actor_optimizer.zero_grad()
        actor_loss.backward()
        self.actor_optimizer.step()

        self.actor.log(self.writer, step)

        if self.learnable_temperature:
            self.log_alpha_optimizer.zero_grad()
            alpha_loss = (self.alpha * (-log_prob - self.target_entropy).detach()).mean()
            self.writer.add_scalar("train_alpha/loss", alpha_loss, step)
            self.writer.add_scalar("train_alpha/value", self.alpha, step)
            alpha_loss.backward()
            self.log_alpha_optimizer.step()

    def update(self, replay_buffer, step, batch_size):
        (
            batch_states,
            batch_actions,
            batch_rewards,
            batch_dones,
            batch_next_states,
        ) = replay_buffer.sample_batch(batch_size)

        state = torch.Tensor(batch_states).to(self.device)
        next_state = torch.Tensor(batch_next_states).to(self.device)
        action = torch.Tensor(batch_actions).to(self.device)
        reward = torch.Tensor(batch_rewards).to(self.device)
        done = torch.Tensor(batch_dones).to(self.device)

        self.writer.add_scalar("train/batch_reward", batch_rewards.mean(), step)

        self.update_critic(state, action, reward, next_state, done, step)

        if step % self.actor_update_frequency == 0:
            self.update_actor_and_alpha(state, step)

        if step % self.critic_target_update_frequency == 0:
            utils.soft_update_params(self.critic, self.critic_target, self.critic_tau)

    def prepare_state(self, latest_scan, distance, cos, sin, collision, goal, action):
        # update the returned data from ROS into a form used for learning in the current model
        latest_scan = np.array(latest_scan)

        inf_mask = np.isinf(latest_scan)
        latest_scan[inf_mask] = 7.0

        max_bins = self.state_dim - 5
        bin_size = int(np.ceil(len(latest_scan) / max_bins))

        # Initialize the list to store the minimum values of each bin
        min_values = []

        # Loop through the data and create bins
        for i in range(0, len(latest_scan), bin_size):
            # Get the current bin
            bin = latest_scan[i : i + min(bin_size, len(latest_scan) - i)]
            # Find the minimum value in the current bin and append it to the min_values list
            min_values.append(min(bin))
        state = min_values + [distance, cos, sin] + [action[0], action[1]]

        assert len(state) == self.state_dim
        terminal = 1 if collision or goal else 0

        return state, terminal