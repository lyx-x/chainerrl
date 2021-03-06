"""An example of training PPO against OpenAI Gym Envs.

This script is an example of training a PPO agent against OpenAI Gym envs.
Both discrete and continuous action spaces are supported.

To solve CartPole-v0, run:
    python train_ppo_gym.py --env CartPole-v0
"""
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import absolute_import
from builtins import *  # NOQA
from future import standard_library
standard_library.install_aliases()
import argparse

import chainer
from chainer import functions as F
import gym
import gym.wrappers
import numpy as np

from chainerrl.agents import a3c
from chainerrl.agents import PPO
from chainerrl import experiments
from chainerrl import links
from chainerrl import misc
from chainerrl.optimizers.nonbias_weight_decay import NonbiasWeightDecay
from chainerrl import policies


def phi(obs):
    return obs.astype(np.float32)


class A3CFFSoftmax(chainer.ChainList, a3c.A3CModel):
    """An example of A3C feedforward softmax policy."""

    def __init__(self, ndim_obs, n_actions, hidden_sizes=(200, 200)):
        self.pi = policies.SoftmaxPolicy(
            model=links.MLP(ndim_obs, n_actions, hidden_sizes))
        self.v = links.MLP(ndim_obs, 1, hidden_sizes=hidden_sizes)
        super().__init__(self.pi, self.v)

    def pi_and_v(self, state):
        return self.pi(state), self.v(state)


class A3CFFMellowmax(chainer.ChainList, a3c.A3CModel):
    """An example of A3C feedforward mellowmax policy."""

    def __init__(self, ndim_obs, n_actions, hidden_sizes=(200, 200)):
        self.pi = policies.MellowmaxPolicy(
            model=links.MLP(ndim_obs, n_actions, hidden_sizes))
        self.v = links.MLP(ndim_obs, 1, hidden_sizes=hidden_sizes)
        super().__init__(self.pi, self.v)

    def pi_and_v(self, state):
        return self.pi(state), self.v(state)


class A3CFFGaussian(chainer.Chain, a3c.A3CModel):
    """An example of A3C feedforward Gaussian policy."""

    def __init__(self, obs_size, action_space,
                 n_hidden_layers=2, n_hidden_channels=64,
                 bound_mean=None, normalize_obs=None):
        assert bound_mean in [False, True]
        assert normalize_obs in [False, True]
        super().__init__()
        hidden_sizes = (n_hidden_channels,) * n_hidden_layers
        self.normalize_obs = normalize_obs
        with self.init_scope():
            self.pi = policies.FCGaussianPolicyWithStateIndependentCovariance(
                obs_size, action_space.low.size,
                n_hidden_layers, n_hidden_channels,
                var_type='diagonal', nonlinearity=F.tanh,
                bound_mean=bound_mean,
                min_action=action_space.low, max_action=action_space.high,
                mean_wscale=1e-2)
            self.v = links.MLP(obs_size, 1, hidden_sizes=hidden_sizes)
            if self.normalize_obs:
                self.obs_filter = links.EmpiricalNormalization(
                    shape=obs_size
                )

    def pi_and_v(self, state):
        if self.normalize_obs:
            state = F.clip(self.obs_filter(state, update=False),
                           -5.0, 5.0)

        return self.pi(state), self.v(state)


def main():
    import logging

    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', type=int, default=0)
    parser.add_argument('--env', type=str, default='Hopper-v1')
    parser.add_argument('--arch', type=str, default='FFGaussian',
                        choices=('FFSoftmax', 'FFMellowmax',
                                 'FFGaussian'))
    parser.add_argument('--normalize-obs', action='store_true')
    parser.add_argument('--bound-mean', action='store_true')
    parser.add_argument('--seed', type=int, default=None)
    parser.add_argument('--outdir', type=str, default=None)
    parser.add_argument('--steps', type=int, default=10 ** 6)
    parser.add_argument('--eval-interval', type=int, default=10000)
    parser.add_argument('--eval-n-runs', type=int, default=10)
    parser.add_argument('--reward-scale-factor', type=float, default=1e-2)
    parser.add_argument('--standardize-advantages', action='store_true')
    parser.add_argument('--render', action='store_true', default=False)
    parser.add_argument('--lr', type=float, default=3e-4)
    parser.add_argument('--weight-decay', type=float, default=0.0)
    parser.add_argument('--demo', action='store_true', default=False)
    parser.add_argument('--load', type=str, default='')
    parser.add_argument('--logger-level', type=int, default=logging.DEBUG)
    parser.add_argument('--monitor', action='store_true')

    parser.add_argument('--update-interval', type=int, default=2048)
    parser.add_argument('--batchsize', type=int, default=64)
    parser.add_argument('--epochs', type=int, default=10)
    parser.add_argument('--entropy-coef', type=float, default=0.0)
    args = parser.parse_args()

    logging.getLogger().setLevel(args.logger_level)

    if args.seed is not None:
        misc.set_random_seed(args.seed)

    args.outdir = experiments.prepare_output_dir(args, args.outdir)

    def make_env(test):
        env = gym.make(args.env)
        if args.monitor:
            env = gym.wrappers.Monitor(env, args.outdir)
        # Scale rewards observed by agents
        if args.reward_scale_factor and not test:
            misc.env_modifiers.make_reward_filtered(
                env, lambda x: x * args.reward_scale_factor)
        if args.render:
            misc.env_modifiers.make_rendered(env)
        return env

    sample_env = gym.make(args.env)
    timestep_limit = sample_env.spec.tags.get(
        'wrapper_config.TimeLimit.max_episode_steps')
    obs_space = sample_env.observation_space
    action_space = sample_env.action_space

    # Switch policy types accordingly to action space types
    if args.arch == 'FFSoftmax':
        model = A3CFFSoftmax(obs_space.low.size, action_space.n)
    elif args.arch == 'FFMellowmax':
        model = A3CFFMellowmax(obs_space.low.size, action_space.n)
    elif args.arch == 'FFGaussian':
        model = A3CFFGaussian(obs_space.low.size, action_space,
                              bound_mean=args.bound_mean,
                              normalize_obs=args.normalize_obs)

    opt = chainer.optimizers.Adam(alpha=args.lr, eps=1e-5)
    opt.setup(model)
    if args.weight_decay > 0:
        opt.add_hook(NonbiasWeightDecay(args.weight_decay))
    agent = PPO(model, opt,
                gpu=args.gpu,
                phi=phi,
                update_interval=args.update_interval,
                minibatch_size=args.batchsize, epochs=args.epochs,
                clip_eps_vf=None, entropy_coef=args.entropy_coef,
                standardize_advantages=args.standardize_advantages,
                )

    if args.load:
        agent.load(args.load)

    if args.demo:
        env = make_env(True)
        eval_stats = experiments.eval_performance(
            env=env,
            agent=agent,
            n_runs=args.eval_n_runs,
            max_episode_len=timestep_limit)
        print('n_runs: {} mean: {} median: {} stdev {}'.format(
            args.eval_n_runs, eval_stats['mean'], eval_stats['median'],
            eval_stats['stdev']))
    else:
        # Linearly decay the learning rate to zero
        def lr_setter(env, agent, value):
            agent.optimizer.alpha = value

        lr_decay_hook = experiments.LinearInterpolationHook(
            args.steps, args.lr, 0, lr_setter)

        # Linearly decay the clipping parameter to zero
        def clip_eps_setter(env, agent, value):
            agent.clip_eps = value

        clip_eps_decay_hook = experiments.LinearInterpolationHook(
            args.steps, 0.2, 0, clip_eps_setter)

        experiments.train_agent_with_evaluation(
            agent=agent,
            env=make_env(False),
            eval_env=make_env(True),
            outdir=args.outdir,
            steps=args.steps,
            eval_n_runs=args.eval_n_runs,
            eval_interval=args.eval_interval,
            max_episode_len=timestep_limit,
            step_hooks=[
                lr_decay_hook,
                clip_eps_decay_hook,
                ],
            )


if __name__ == '__main__':
    main()
